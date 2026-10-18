"""Extraction benchmark: drawing extraction paths scored on input variants (docs/09, D76).

Ground truth comes from the vector path on the clean PDF, without hand labels:

- Mechanical drawings: drawing-mcp's glyph-outline clusters give every label's exact box. NX
  writes text as outlines without a text layer, so the vector path does not know what a label
  says; text and value metrics for these sheets stay empty until label texts exist (D-2).
- Schematics: the PDF text layer gives box and exact text of every text run. Designators and
  values are marked where circuit-mcp's reading agrees with the case's reference netlist (ground
  truth for evaluation only, read by the test bench alone).

Every extractor runs on every case × variant it can read; outputs are mapped back to sheet mm,
matched to the ground truth by IoU and scored. Results: run.json, results.jsonl, summary.md,
truth/<case>.json and the raw predictions per extractor and variant.
"""

import json
import math
import platform
import re
import subprocess
import time
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, Field

from mechatronikers.spec.drawing import BBox, Vec2
from mechatronikers.testbench.cases import ensure_materialized
from mechatronikers.testbench.extraction import (
    BackendUnavailableError,
    Domain,
    ExtractedLabel,
    ExtractionInput,
    Extractor,
)
from mechatronikers.testbench.variants import (
    VariantManifest,
    VariantSpec,
    image_to_sheet,
    prepare_variant,
)

Polygon = list[Vec2]
Role = Literal["designator", "value", "other"]
DEFAULT_IOU = 0.5
VALUE_REL_TOL = 1e-3  # numeric match of a read value; absorbs formatting, not misreadings

# --------------------------------------------------------------------------- cases


class BenchCase(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest class

    case_id: str
    domain: Domain
    pdf: Path
    netlist: Path | None = None  # schematics: reference netlist (ground truth, bench only)
    split: str | None = None  # schematics: dev / holdout


def bench_cases(
    data_dir: str | Path = "data", which: Literal["all", "parts", "circuits"] = "all"
) -> list[BenchCase]:
    """data/<Part>/<Part>_dwg.pdf (no reference model needed) and the cases in
    data/circuits/cases.json (colour PDF + reference netlist)."""
    root = Path(data_dir)
    cases: list[BenchCase] = []
    if which in ("all", "parts"):
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            pdf = folder / f"{folder.name}_dwg.pdf"
            if pdf.is_file():
                cases.append(BenchCase(case_id=folder.name, domain="part", pdf=pdf))
    if which in ("all", "circuits") and (root / "circuits" / "cases.json").is_file():
        from mechatronikers.testbench.circuits import circuit_cases

        for c in circuit_cases(root):
            cases.append(
                BenchCase(
                    case_id=c.case_id, domain="circuit", pdf=c.pdf, netlist=c.reference,
                    split=c.split,
                )
            )  # fmt: skip
    ids = [c.case_id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"case ids are not unique: {ids}")
    return cases


# --------------------------------------------------------------------------- text and values


_FOLD = str.maketrans(
    {"⌀": "Ø", "∅": "Ø", "ø": "Ø", "×": "x", "−": "-", "–": "-", "μ": "u", ",": "."}
)


def normalize_text(text: str) -> str:
    """Comparison form: Unicode-compatible, one diameter sign, decimal point, no whitespace,
    case-insensitive. '⌀ 12,5' and 'Ø12.5' compare equal."""
    folded = unicodedata.normalize("NFKC", text).translate(_FOLD)
    return "".join(folded.split()).casefold()


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


_SI_PREFIX = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "g": 1e9,
    "t": 1e12,
}
_SI_UNITS = {"", "f", "h", "v", "a", "w", "hz", "ohm", "ω"}


def si_value(text: str) -> float | None:
    """Component value with SPICE-style prefix: '100n', '2.2K', '10uF', '100mH', '1Meg', '4k7'.
    'M' is mega, 'm' milli; other prefixes ignore case. Model names ('1N4148') give None."""
    t = unicodedata.normalize("NFKC", text).strip().replace(",", ".").replace("μ", "u")
    if m := re.fullmatch(r"(\d+)([pnumkKMR])(\d+)", t):  # 4k7, 2R2
        prefix = m.group(2)
        scale = 1.0 if prefix == "R" else 1e6 if prefix == "M" else _SI_PREFIX[prefix.lower()]
        return float(f"{m.group(1)}.{m.group(3)}") * scale
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-zΩ]*)", t)
    if not m:
        return None
    number, suffix = float(m.group(1)), m.group(2)
    scale = 1.0
    if suffix.lower().startswith("meg"):
        scale, suffix = 1e6, suffix[3:]
    elif suffix.startswith("M"):
        scale, suffix = 1e6, suffix[1:]
    elif suffix[:1].lower() in _SI_PREFIX:
        scale, suffix = _SI_PREFIX[suffix[:1].lower()], suffix[1:]
    return number * scale if suffix.lower() in _SI_UNITS else None


ValueKey = tuple[str, float, int]  # kind, value, count


def numeric_value(text: str, domain: Domain) -> ValueKey | None:
    """What a label states as a number: a dimension on drawings, a component value on
    schematics."""
    if domain == "part":
        from mechatronikers.drawing_mcp.dimensions import parse_dimension_text

        parsed = parse_dimension_text(text)
        if parsed.kind == "other" or parsed.value is None:
            return None
        return parsed.kind, parsed.value, parsed.count
    value = si_value(text)
    return None if value is None else ("si", value, 1)


def _same_value(a: ValueKey, b: ValueKey, rel_tol: float) -> bool:
    return (
        a[0] == b[0] and a[2] == b[2] and math.isclose(a[1], b[1], rel_tol=rel_tol, abs_tol=1e-12)
    )


# --------------------------------------------------------------------------- ground truth


class TruthLabel(BaseModel):
    id: str
    bbox: BBox  # sheet mm
    text: str | None = None  # None: located by the vector path, text unknown
    region: str | None = None  # drawing / title_block / border
    role: Role = "other"  # designator / value: confirmed by the reference netlist
    value: ValueKey | None = None  # stated number (dimensions in the drawing area, values)


class SheetTruth(BaseModel):
    case_id: str
    domain: Domain
    source: str
    source_sha256: str
    sheet_size: Vec2
    derived_from: str
    labels: list[TruthLabel]
    netlist_designators: list[str] = Field(default_factory=list)
    unlocated_designators: list[str] = Field(default_factory=list)  # netlist refs not on the sheet

    @property
    def text_known(self) -> int:
        return sum(label.text is not None for label in self.labels)


def part_truth(case: BenchCase) -> SheetTruth:
    from mechatronikers.drawing_mcp.analysis import analyse

    summary = analyse(ensure_materialized(case.pdf)).summary
    labels = []
    for label in summary.labels:
        value = None
        if label.text is not None and label.region == "drawing":
            value = numeric_value(label.text, "part")
        labels.append(
            TruthLabel(
                id=label.id, bbox=label.bbox, text=label.text, region=label.region, value=value
            )
        )
    return SheetTruth(
        case_id=case.case_id,
        domain="part",
        source=str(case.pdf),
        source_sha256=summary.source_sha256,
        sheet_size=summary.sheet_size,
        derived_from="drawing-mcp vector path: glyph-outline clusters (text not in the PDF)",
        labels=labels,
    )


def _bbox_key(text: str, box: BBox) -> tuple[str, tuple[float, ...]]:
    return text, tuple(round(v, 3) for v in box)


def circuit_truth(case: BenchCase, reference: dict[str, Any] | None = None) -> SheetTruth:
    """All text runs on the sheet. Designators: texts in the drawing area equal to a reference of
    the netlist. Values: texts circuit-mcp assigned to a component whose netlist value they
    equal (a bare value like '5' cannot be told from a pin or zone number by text alone)."""
    from mechatronikers.circuit_mcp.analysis import analyse_schematic, base_ref, merge_words
    from mechatronikers.circuit_mcp.extract import extract_texts

    pdf = ensure_materialized(case.pdf)
    if reference is None:
        if case.netlist is None:
            raise ValueError(f"{case.case_id}: schematic case without a reference netlist")
        reference = json.loads(case.netlist.read_text())
    components = {str(c["ref"]): c for c in reference["components"]}
    spec = analyse_schematic(pdf)
    items = {_bbox_key(t.text, t.bbox): t for t in spec.texts}
    labels: list[TruthLabel] = []
    for i, run in enumerate(merge_words(extract_texts(pdf))):
        item = items.get(_bbox_key(run.text, run.bbox))
        role: Role = "other"
        region = "border"
        if item is not None:
            region = "title_block" if item.role == "title_block" else "drawing"
            owner = components.get(base_ref(item.owner)) if item.owner else None
            if region == "drawing" and base_ref(item.text) in components:
                role = "designator"  # printed reference designator, straight from the netlist
            elif (
                item.role in ("value", "param")
                and owner is not None
                and normalize_text(str(owner["value"])) == normalize_text(item.text)
            ):
                role = "value"
        labels.append(
            TruthLabel(
                id=f"t{i}",
                bbox=run.bbox,
                text=run.text,
                region=region,
                role=role,
                value=numeric_value(run.text, "circuit") if role == "value" else None,
            )
        )
    located = {base_ref(label.text or "") for label in labels if label.role == "designator"}
    return SheetTruth(
        case_id=case.case_id,
        domain="circuit",
        source=str(case.pdf),
        source_sha256=spec.source_sha256,
        sheet_size=spec.sheet_size,
        derived_from="PDF text layer (merged runs); roles confirmed by the reference netlist",
        labels=labels,
        netlist_designators=sorted(components),
        unlocated_designators=sorted(set(components) - located),
    )


def derive_truth(case: BenchCase) -> SheetTruth:
    return circuit_truth(case) if case.domain == "circuit" else part_truth(case)


# --------------------------------------------------------------------------- geometry


def _corners(box: BBox) -> Polygon:
    x0, x1 = sorted((box[0], box[2]))
    y0, y1 = sorted((box[1], box[3]))
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _area(poly: Polygon) -> float:
    if len(poly) < 3:
        return 0.0
    twice = sum(
        x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1], strict=True)
    )
    return abs(twice) / 2


def _clip_half(poly: Polygon, axis: int, bound: float, keep_above: bool) -> Polygon:
    def inside(p: Vec2) -> bool:
        return p[axis] >= bound if keep_above else p[axis] <= bound

    def cross(p: Vec2, q: Vec2) -> Vec2:
        t = (bound - p[axis]) / (q[axis] - p[axis])
        return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))

    out: Polygon = []
    for i, current in enumerate(poly):
        previous = poly[i - 1]
        if inside(current):
            if not inside(previous):
                out.append(cross(previous, current))
            out.append(current)
        elif inside(previous):
            out.append(cross(previous, current))
    return out


def polygon_box_iou(poly: Polygon, box: BBox) -> float:
    """IoU of a convex polygon (a predicted box mapped to the sheet) with an axis-aligned box
    (Sutherland–Hodgman clipping)."""
    x0, x1 = sorted((box[0], box[2]))
    y0, y1 = sorted((box[1], box[3]))
    clipped = poly
    for axis, bound, above in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        clipped = _clip_half(clipped, axis, bound, above)
        if not clipped:
            return 0.0
    inter = _area(clipped)
    union = _area(poly) + (x1 - x0) * (y1 - y0) - inter
    return inter / union if union > 0 else 0.0


def to_sheet(labels: list[ExtractedLabel], manifest: VariantManifest) -> list[Polygon]:
    """Predicted boxes (input frame) as polygons in sheet mm."""
    matrix = np.eye(3) if manifest.spec.kind == "vector" else image_to_sheet(manifest)
    polygons = []
    for label in labels:
        points = list(label.quad) if label.quad is not None else _corners(label.bbox)
        polygons.append(
            [
                (
                    float(matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]),
                    float(matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]),
                )
                for x, y in points
            ]
        )
    return polygons


def match_labels(
    truth: list[TruthLabel], polygons: list[Polygon], iou_threshold: float = DEFAULT_IOU
) -> list[tuple[int, int, float]]:
    """One-to-one matches (prediction index, truth index, IoU) with IoU ≥ threshold, maximising
    the number of matches first and the summed IoU second."""
    if not truth or not polygons:
        return []
    from scipy.optimize import linear_sum_assignment

    tb = np.array([_corners(t.bbox)[0] + _corners(t.bbox)[2] for t in truth])
    pb = np.array(
        [[min(x for x, _ in p), min(y for _, y in p), max(x for x, _ in p), max(y for _, y in p)]
         for p in polygons]
    )  # fmt: skip
    overlap = (
        (pb[:, None, 0] < tb[None, :, 2])
        & (pb[:, None, 2] > tb[None, :, 0])
        & (pb[:, None, 1] < tb[None, :, 3])
        & (pb[:, None, 3] > tb[None, :, 1])
    )
    weight = np.zeros(overlap.shape)
    ious = np.zeros(overlap.shape)
    # The summed IoU term stays below 1, so one more match always outweighs better IoUs.
    tie_break = 1.0 / (min(len(truth), len(polygons)) + 1)
    for i, j in zip(*np.nonzero(overlap), strict=True):
        iou = polygon_box_iou(polygons[i], truth[j].bbox)
        ious[i, j] = iou
        if iou >= iou_threshold:
            weight[i, j] = 1.0 + iou * tie_break
    rows, cols = linear_sum_assignment(-weight)
    return [
        (int(i), int(j), float(ious[i, j]))
        for i, j in zip(rows, cols, strict=True)
        if weight[i, j] > 0
    ]


# --------------------------------------------------------------------------- scoring


class SheetScore(BaseModel):
    truth: int = 0
    predicted: int = 0
    matched: int = 0  # IoU ≥ threshold, one to one
    text_known: int = 0  # truth labels whose text the ground truth knows
    text_judged: int = 0  # reads that can be judged (sheet has text truth; label known or none)
    text_correct: int = 0  # matched and the normalised text is equal
    cer_sum: float = 0.0  # character error rate summed over *matched* known-text labels
    cer_count: int = 0
    values_truth: int = 0  # truth labels stating a number
    values_correct: int = 0  # matched and the read number agrees (kind, count, value)
    key_truth: int = 0  # designators and values (schematics)
    key_correct: int = 0

    def __add__(self, other: "SheetScore") -> "SheetScore":
        a, b = self.model_dump(), other.model_dump()
        return SheetScore(**{k: a[k] + b[k] for k in a})


class Rates(BaseModel):
    """Shares in 0..1; None where the denominator is zero (not measurable)."""

    det_precision: float | None
    det_recall: float | None
    text_precision: float | None
    text_recall: float | None
    cer: float | None  # over matched labels only (misses are in the recalls); lower is better
    value_accuracy: float | None
    key_recall: float | None


def _share(num: float, den: float) -> float | None:
    return num / den if den else None


def rates(s: SheetScore) -> Rates:
    return Rates(
        det_precision=_share(s.matched, s.predicted),
        det_recall=_share(s.matched, s.truth),
        text_precision=_share(s.text_correct, s.text_judged),
        text_recall=_share(s.text_correct, s.text_known),
        cer=_share(s.cer_sum, s.cer_count),
        value_accuracy=_share(s.values_correct, s.values_truth),
        key_recall=_share(s.key_correct, s.key_truth),
    )


def score_sheet(
    truth: SheetTruth,
    labels: list[ExtractedLabel],
    polygons: list[Polygon],
    *,
    iou_threshold: float = DEFAULT_IOU,
    value_rel_tol: float = VALUE_REL_TOL,
) -> SheetScore:
    matches = match_labels(truth.labels, polygons, iou_threshold)
    truth_of = {i: j for i, j, _ in matches}
    s = SheetScore(
        truth=len(truth.labels),
        predicted=len(labels),
        matched=len(matches),
        text_known=truth.text_known,
        values_truth=sum(t.value is not None for t in truth.labels),
        key_truth=sum(t.role != "other" for t in truth.labels),
    )
    for i, label in enumerate(labels):
        j = truth_of.get(i)
        target = truth.labels[j] if j is not None else None
        # A read is judged where the truth knows texts: on a known-text label, or on no label
        # of a sheet with text truth (a false positive). Sheets without text truth judge nothing.
        judgeable = target.text is not None if target is not None else truth.text_known > 0
        if label.text is not None and judgeable:
            s.text_judged += 1
        if target is None or target.text is None:
            continue
        expected = normalize_text(target.text)
        read = normalize_text(label.text) if label.text is not None else ""
        s.cer_sum += edit_distance(read, expected) / max(1, len(expected))
        s.cer_count += 1
        correct = label.text is not None and read == expected
        s.text_correct += correct
        s.key_correct += correct and target.role != "other"
        if target.value is not None and label.text is not None:
            got = numeric_value(label.text, truth.domain)
            s.values_correct += got is not None and _same_value(got, target.value, value_rel_tol)
    return s


# --------------------------------------------------------------------------- runs


class BenchRow(BaseModel):
    case_id: str
    domain: Domain
    split: str | None = None
    variant: str
    extractor: str
    # error rows are scored as sheets with no output; unavailable rows (backend not installed)
    # are n/a like skipped ones
    status: Literal["ok", "skipped", "unavailable", "error"]
    note: str | None = None
    runtime_s: float | None = None
    credits: float | None = None
    score: SheetScore | None = None
    rates: Rates | None = None


class BenchInfo(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None = None
    cases: list[str]
    variants: list[VariantSpec]
    extractors: list[str]
    seed: int
    iou_threshold: float
    value_rel_tol: float
    git_commit: str | None
    python: str
    host: str


def _git_commit() -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return proc.stdout.strip() or None


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _row(
    case: BenchCase,
    truth: SheetTruth,
    inp: ExtractionInput,
    manifest: VariantManifest,
    extractor: Extractor,
    out_dir: Path,
    iou_threshold: float,
) -> BenchRow:
    base: dict[str, Any] = {
        "case_id": case.case_id,
        "domain": case.domain,
        "split": case.split,
        "variant": inp.variant,
        "extractor": extractor.name,
    }
    if inp.kind not in extractor.inputs:
        reads = ", ".join(sorted(extractor.inputs))
        return BenchRow(**base, status="skipped", note=f"reads {reads} input only")
    start = time.perf_counter()
    try:
        result = extractor.extract(inp)
    except BackendUnavailableError as exc:  # not installed here: n/a, like a skipped pair
        return BenchRow(**base, status="unavailable", note=str(exc))
    except Exception as exc:  # a failing backend is a result, not a crashed run
        # Scored as a sheet with no output (every truth label missed), so a backend cannot look
        # better by crashing on hard inputs.
        missed = score_sheet(truth, [], [], iou_threshold=iou_threshold)
        return BenchRow(
            **base,
            status="error",
            note=f"{type(exc).__name__}: {exc}",
            runtime_s=time.perf_counter() - start,
            credits=0.0,
            score=missed,
            rates=rates(missed),
        )
    runtime = time.perf_counter() - start
    target = out_dir / "predictions" / _safe(extractor.name) / _safe(inp.variant)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{_safe(case.case_id)}.json").write_text(result.model_dump_json(indent=1))
    score = score_sheet(
        truth, result.labels, to_sheet(result.labels, manifest), iou_threshold=iou_threshold
    )
    return BenchRow(
        **base,
        status="ok",
        runtime_s=runtime,
        credits=result.credits,
        score=score,
        rates=rates(score),
    )


def run_bench(
    cases: list[BenchCase],
    variants: list[VariantSpec],
    extractors: list[Extractor],
    *,
    out_dir: Path,
    variants_dir: Path,
    seed: int = 0,
    iou_threshold: float = DEFAULT_IOU,
    progress: bool = False,
) -> tuple[BenchInfo, list[BenchRow]]:
    """Every case × variant × extractor; writes run.json, results.jsonl, summary.md, truth/ and
    predictions/ into out_dir. Variants are cached in variants_dir."""
    names = [e.name for e in extractors]
    if len(names) != len(set(names)):
        raise ValueError(f"extractor names must be unique: {names}")
    variant_names = [v.name for v in variants]
    if len(variant_names) != len(set(variant_names)):
        raise ValueError(f"variant names must be unique: {variant_names}")
    if not 0 < iou_threshold <= 1:
        raise ValueError(f"IoU threshold must be in (0, 1], got {iou_threshold}")
    started = datetime.now(UTC)
    info = BenchInfo(
        run_id=f"extract-{started:%Y%m%d-%H%M%S}",
        started_at=started.isoformat(),
        cases=[c.case_id for c in cases],
        variants=variants,
        extractors=names,
        seed=seed,
        iou_threshold=iou_threshold,
        value_rel_tol=VALUE_REL_TOL,
        git_commit=_git_commit(),
        python=platform.python_version(),
        host=platform.platform(),
    )
    (out_dir / "truth").mkdir(parents=True, exist_ok=True)
    rows: list[BenchRow] = []
    truths: dict[str, SheetTruth] = {}
    with (out_dir / "results.jsonl").open("w") as results:
        for case in cases:
            truth = truths[case.case_id] = derive_truth(case)
            truth_path = out_dir / "truth" / f"{_safe(case.case_id)}.json"
            truth_path.write_text(truth.model_dump_json(indent=1))
            for spec in variants:
                path, manifest = prepare_variant(case.case_id, case.pdf, spec, variants_dir, seed)
                inp = ExtractionInput(
                    case_id=case.case_id,
                    variant=spec.name,
                    kind=spec.kind,
                    domain=case.domain,
                    path=path,
                    dpi=spec.dpi,
                )
                for extractor in extractors:
                    row = _row(case, truth, inp, manifest, extractor, out_dir, iou_threshold)
                    rows.append(row)
                    results.write(row.model_dump_json() + "\n")
                    results.flush()
                    if progress:
                        print(
                            f"{case.case_id:<18} {spec.name:<12} {extractor.name:<8} "
                            f"{_verdict(row)}"
                        )
    info.finished_at = datetime.now(UTC).isoformat()
    (out_dir / "run.json").write_text(info.model_dump_json(indent=2))
    (out_dir / "summary.md").write_text(summary_markdown(info, rows, truths.values()))
    return info, rows


def _verdict(row: BenchRow) -> str:
    if row.status == "error":
        return f"FAILED (scored as all missed): {row.note}"
    if row.status != "ok" or row.rates is None:
        return f"{row.status}: {row.note}"
    r = row.rates
    return f"det P {_pct(r.det_precision)} R {_pct(r.det_recall)} · text R {_pct(r.text_recall)}"


def _cell(note: str | None) -> str:
    return (note or "")[:60].replace("|", "/").replace("\n", " ")


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{100 * value:.1f} %"


def summary_markdown(
    info: BenchInfo, rows: list[BenchRow], truths: Iterable[SheetTruth] = ()
) -> str:
    truths = list(truths)
    domains = {t.case_id: t.domain for t in truths}
    parts = sum(d == "part" for d in domains.values())
    lines = [
        f"# Extraction benchmark {info.run_id}",
        "",
        f"- Cases: {len(info.cases)} ({parts} drawings, {len(domains) - parts} schematics)"
        f" · Variants: {', '.join(v.name for v in info.variants)}"
        f" · Extractors: {', '.join(info.extractors)}",
        f"- Seed {info.seed} · match at IoU ≥ {info.iou_threshold:g} · value tolerance "
        f"{info.value_rel_tol:g} rel · commit `{info.git_commit}` · started {info.started_at}",
        "- Ground truth: the vector path on the clean PDF (docs/09). Drawing labels are located "
        "but not read by it (NX glyph outlines), so text and value metrics cover schematics only.",
        "- Sheets: scored / all; a failed sheet (backend error) counts as all labels missed. "
        "n/a: the extractor cannot read this input kind, or cannot run here (unavailable: not "
        "installed, model budget reached). CER covers matched labels only.",
        "",
        "## By extractor and variant",
        "",
        "| Extractor | Variant | Domain | Sheets | Det. P | Det. R | Text P | Text R "
        "| CER (matched) | Value acc. | Desig./values R | s/sheet | Credits/sheet |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str, str], list[BenchRow]] = {}
    for row in rows:
        groups.setdefault((row.extractor, row.variant, row.domain), []).append(row)
    for (extractor, variant, domain), members in groups.items():
        scored = [x for x in members if x.status in ("ok", "error") and x.score is not None]
        if not scored:
            notes = sorted({f"{x.status}: {x.note}" for x in members})
            lines.append(
                f"| {extractor} | {variant} | {domain} | 0 / {len(members)} "
                f"| n/a ({'; '.join(notes)}) |  |  |  |  |  |  |  |  |"
            )
            continue
        failed = sum(x.status == "error" for x in scored)
        total = rates(sum((x.score for x in scored if x.score is not None), SheetScore()))
        runtime = sum(x.runtime_s or 0.0 for x in scored) / len(scored)
        credits = sum(x.credits or 0.0 for x in scored) / len(scored)
        sheets = f"{len(scored)} / {len(members)}" + (f" · **{failed} failed**" if failed else "")
        lines.append(
            f"| {extractor} | {variant} | {domain} | {sheets} "
            f"| {_pct(total.det_precision)} | {_pct(total.det_recall)} "
            f"| {_pct(total.text_precision)} | {_pct(total.text_recall)} | {_pct(total.cer)} "
            f"| {_pct(total.value_accuracy)} | {_pct(total.key_recall)} | {runtime:.2f} "
            f"| {credits:.3f} |"
        )
    lines += [
        "",
        "## Per case",
        "",
        "| Case | Domain | Variant | Extractor | Status | Truth | Pred. | Matched | Det. P "
        "| Det. R | Text R | Value acc. | Time (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        s, r = row.score, row.rates
        if s is None or r is None:
            lines.append(
                f"| {row.case_id} | {row.domain} | {row.variant} | {row.extractor} "
                f"| {row.status}: {_cell(row.note)} "
                "|  |  |  |  |  |  |  |  |"
            )
            continue
        status = "ok" if row.status == "ok" else f"**failed**: {_cell(row.note)}"
        lines.append(
            f"| {row.case_id} | {row.domain} | {row.variant} | {row.extractor} | {status} "
            f"| {s.truth} | {s.predicted} | {s.matched} | {_pct(r.det_precision)} "
            f"| {_pct(r.det_recall)} | {_pct(r.text_recall)} | {_pct(r.value_accuracy)} "
            f"| {row.runtime_s or 0.0:.2f} |"
        )
    circuits = [t for t in truths if t.domain == "circuit"]
    if circuits:
        lines += [
            "",
            "## Ground truth notes (schematics)",
            "",
            "| Case | Text runs | Designators on sheet / netlist | Values | Not on sheet |",
            "|---|---|---|---|---|",
        ]
        for t in circuits:
            located = len(t.netlist_designators) - len(t.unlocated_designators)
            values = sum(label.role == "value" for label in t.labels)
            lines.append(
                f"| {t.case_id} | {len(t.labels)} | {located} / {len(t.netlist_designators)} "
                f"| {values} | {', '.join(t.unlocated_designators) or '–'} |"
            )
    lines.append("")
    return "\n".join(lines)
