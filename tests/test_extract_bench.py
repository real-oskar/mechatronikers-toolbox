import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mechatronikers.testbench.__main__ import main
from mechatronikers.testbench.extract_bench import (
    BenchCase,
    SheetTruth,
    TruthLabel,
    bench_cases,
    derive_truth,
    edit_distance,
    match_labels,
    normalize_text,
    numeric_value,
    polygon_box_iou,
    rates,
    run_bench,
    score_sheet,
    si_value,
    to_sheet,
)
from mechatronikers.testbench.extraction import (
    ExtractedLabel,
    ExtractionInput,
    ExtractionResult,
    StubExtractor,
    VectorPathExtractor,
    load_extractor,
)
from mechatronikers.testbench.variants import (
    PRESETS,
    VariantManifest,
    VariantSpec,
    library_versions,
    manifest_file,
    prepare_variant,
)
from tests.fixtures.drawings import make_drawing
from tests.fixtures.schematics import make_schematic

# Small and fast: every raster test renders an A3/A4 sheet at a few dozen dpi.
SCAN = VariantSpec(
    name="scan-test", kind="raster", dpi=50, max_rotation_deg=2.0, max_shear=0.01,
    contrast=0.75, blur_px=0.5, noise=4.0, jpeg_quality=70,
)  # fmt: skip
VECTOR = PRESETS["vector"]

REFERENCE = {
    "components": [
        {"ref": "R1", "value": "10k"},
        {"ref": "R2", "value": "4k7"},
        {"ref": "R3", "value": "1k"},
        {"ref": "V1", "value": "5"},
    ],
    "nets": [],
}


def _part(tmp_path: Path) -> BenchCase:
    return BenchCase(case_id="Plate", domain="part", pdf=make_drawing(tmp_path / "plate.pdf"))


def _circuit(tmp_path: Path) -> BenchCase:
    netlist = tmp_path / "divider.reference.json"
    netlist.write_text(json.dumps(REFERENCE))
    pdf = make_schematic(tmp_path / "divider_sch.pdf")
    return BenchCase(case_id="divider", domain="circuit", pdf=pdf, netlist=netlist, split="dev")


def _to_px(
    manifest: VariantManifest, box: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    a, b, c, d, e, f = manifest.sheet_to_image
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    return [(a * x + b * y + c, d * x + e * y + f) for x, y in corners]


# --------------------------------------------------------------------------- variants


def test_raster_variants_are_deterministic_cached_and_mapped(tmp_path: Path) -> None:
    pdf = make_drawing(tmp_path / "plate.pdf")
    first, manifest = prepare_variant("Plate", pdf, SCAN, tmp_path / "a", seed=7)
    second, again = prepare_variant("Plate", pdf, SCAN, tmp_path / "b", seed=7)
    assert first.read_bytes() == second.read_bytes() and manifest == again
    other, changed = prepare_variant("Plate", pdf, SCAN, tmp_path / "c", seed=8)
    assert (
        other.read_bytes() != first.read_bytes() and changed.rotation_deg != manifest.rotation_deg
    )
    assert 0 < abs(manifest.rotation_deg) <= 2 and abs(manifest.shear) <= 0.01

    stamp = first.stat().st_mtime_ns
    cached, _ = prepare_variant("Plate", pdf, SCAN, tmp_path / "a", seed=7)
    assert cached.stat().st_mtime_ns == stamp  # reused, not rendered again

    # The manifest (transform, source path) is kept apart from the pixels extractors get, and
    # records the library versions the pixels depend on; other versions regenerate.
    stored = manifest_file(tmp_path / "a", "Plate", SCAN.name)
    assert first.parent != stored.parent and not list(first.parent.glob("*.json"))
    assert manifest.libraries == library_versions() and set(manifest.libraries) == {
        "pypdfium2", "pdfium", "pillow", "numpy",
    }  # fmt: skip
    old = manifest.model_copy(update={"libraries": {**manifest.libraries, "numpy": "0.0"}})
    stored.write_text(old.model_dump_json())
    regenerated, fresh = prepare_variant("Plate", pdf, SCAN, tmp_path / "a", seed=7)
    assert fresh.libraries == library_versions() and regenerated.read_bytes() == first.read_bytes()
    assert VariantManifest.model_validate_json(stored.read_text()) == fresh

    image = np.asarray(Image.open(first))
    assert image.ndim == 2 and manifest.image_size_px == (image.shape[1], image.shape[0])
    # The stored transform puts a filled glyph (centre (61, 110) mm) on ink, and blank paper
    # (200, 200 mm) on paper, despite rotation, shear and noise.
    (gx, gy), (px, py) = (_to_px(manifest, (x, y, x, y))[0] for x, y in ((61, 110), (200, 200)))
    assert image[round(gy), round(gx)] < 120 < image[round(py), round(px)]


def test_vector_variant_is_the_source_pdf(tmp_path: Path) -> None:
    pdf = make_drawing(tmp_path / "plate.pdf")
    path, manifest = prepare_variant("Plate", pdf, VECTOR, tmp_path / "v")
    assert path == pdf and manifest.image is None and manifest.sheet_to_image[0] == 1.0
    assert manifest.sheet_size_mm == pytest.approx((420, 297), abs=0.5)
    presets = {"vector", "raster-300", "raster-150", "raster-100", "scan-sim", "low-quality"}
    assert presets <= set(PRESETS)


# --------------------------------------------------------------------------- scoring pieces


def test_text_value_and_geometry_helpers() -> None:
    assert normalize_text("⌀ 12,5") == normalize_text("Ø12.5") == "ø12.5"
    assert normalize_text("3× M10") == normalize_text("3x m10")
    assert edit_distance("kitten", "sitting") == 3
    assert si_value("100n") == pytest.approx(1e-7)
    assert si_value("2.2K") == si_value("2k2") == pytest.approx(2200)
    assert si_value("10uF") == si_value("10µF") == pytest.approx(1e-5)
    assert si_value("1Meg") == si_value("1M") == pytest.approx(1e6)
    assert si_value("1m") == pytest.approx(1e-3) and si_value("100mH") == pytest.approx(0.1)
    assert si_value("1N4148") is None and si_value("VSIN") is None and si_value("~") is None
    assert numeric_value("3x Ø12", "part") == ("diameter", 12.0, 3)
    assert numeric_value("A-A", "part") is None
    assert numeric_value("4k7", "circuit") == ("si", 4700.0, 1)

    box = (0.0, 0.0, 2.0, 1.0)
    assert polygon_box_iou([(0, 0), (2, 0), (2, 1), (0, 1)], box) == pytest.approx(1.0)
    assert polygon_box_iou([(1, 0), (3, 0), (3, 1), (1, 1)], box) == pytest.approx(1 / 3)
    assert polygon_box_iou([(5, 5), (6, 5), (6, 6), (5, 6)], box) == 0.0
    diamond = [(1.0, -0.5), (1.5, 0.5), (1.0, 1.5), (0.5, 0.5)]  # area 1, 0.75 inside
    assert polygon_box_iou(diamond, box) == pytest.approx(0.75 / 2.25)

    truth = [TruthLabel(id="a", bbox=(0, 0, 2, 1)), TruthLabel(id="b", bbox=(2.2, 0, 4, 1))]
    # Both boxes clear the threshold only on "a": one to one, the closer box wins it.
    polys = [[(0, 0), (4, 0), (4, 1), (0, 1)], [(0.1, 0), (2, 0), (2, 1), (0.1, 1)]]
    assert match_labels(truth, polys) == [(1, 0, pytest.approx(0.95))]

    # A chain of overlapping labels: shifting every match by one gives 9 matches at IoU 0.6,
    # the perfect pairs only 8 at IoU 1. More matches win over better IoUs.
    chain = [TruthLabel(id=f"c{i}", bbox=(0.5 * i, 0, 0.5 * i + 2, 1)) for i in range(9)]
    shifted = [
        [(0.5 * i, 0), (0.5 * i + 2, 0), (0.5 * i + 2, 1), (0.5 * i, 1)] for i in range(1, 10)
    ]
    assert len(match_labels(chain, shifted)) == 9


def test_score_sheet_counts_detection_text_values_and_designators() -> None:
    truth = SheetTruth(
        case_id="t", domain="circuit", source="x.pdf", source_sha256="0", sheet_size=(100, 100),
        derived_from="test",
        labels=[
            TruthLabel(id="r", bbox=(10, 10, 20, 15), text="R1", role="designator"),
            TruthLabel(id="v", bbox=(10, 20, 20, 25), text="4k7", role="value",
                       value=("si", 4700.0, 1)),
            TruthLabel(id="n", bbox=(10, 30, 30, 35), text="OUT"),
            TruthLabel(id="u", bbox=(50, 50, 60, 55), text=None),  # located, text unknown
        ],
    )  # fmt: skip
    labels = [
        ExtractedLabel(text="R1", bbox=(10, 10, 20, 15)),  # right
        ExtractedLabel(text="4.7k", bbox=(10.2, 20, 20, 25.2)),  # same value, other spelling
        ExtractedLabel(text="0UT", bbox=(10, 30, 30, 35)),  # misread
        ExtractedLabel(text="??", bbox=(50, 50, 60, 55)),  # on the unknown-text label
        ExtractedLabel(text="noise", bbox=(80, 80, 90, 85)),  # nothing there
    ]
    polys = [
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)] for x0, y0, x1, y1 in (b.bbox for b in labels)
    ]
    s = score_sheet(truth, labels, polys)
    assert (s.truth, s.predicted, s.matched, s.text_known) == (4, 5, 4, 3)
    assert (s.text_correct, s.text_judged, s.values_truth, s.values_correct) == (1, 4, 1, 1)
    assert (s.key_truth, s.key_correct) == (2, 1)
    r = rates(s)
    assert r.det_precision == pytest.approx(0.8) and r.det_recall == 1.0
    assert r.text_recall == pytest.approx(1 / 3) and r.text_precision == pytest.approx(0.25)
    assert r.cer == pytest.approx((0 + 2 / 3 + 1 / 3) / 3) and r.value_accuracy == 1.0
    assert rates(s + s).det_precision == r.det_precision


def test_reads_are_not_judged_on_sheets_without_text_truth() -> None:
    """Part sheets: the vector path knows where labels are, not what they say. A reading backend
    with a false positive must show no text precision there, not 0 %."""
    truth = SheetTruth(
        case_id="p", domain="part", source="p.pdf", source_sha256="0", sheet_size=(420, 297),
        derived_from="test",
        labels=[TruthLabel(id="a", bbox=(10, 10, 20, 15)),
                TruthLabel(id="b", bbox=(10, 20, 20, 25))],
    )  # fmt: skip
    labels = [
        ExtractedLabel(text="60", bbox=(10, 10, 20, 15)),
        ExtractedLabel(text="Ø20", bbox=(10, 20, 20, 25)),
        ExtractedLabel(text="x", bbox=(100, 100, 110, 105)),  # false positive
    ]
    polys = [
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)] for x0, y0, x1, y1 in (b.bbox for b in labels)
    ]
    s = score_sheet(truth, labels, polys)
    r = rates(s)
    assert (s.matched, s.text_judged, s.text_correct) == (2, 0, 0)
    assert r.text_precision is None and r.text_recall is None and r.cer is None
    assert r.det_precision == pytest.approx(2 / 3) and r.det_recall == 1.0


# --------------------------------------------------------------------------- truth and runs


def test_ground_truth_from_the_vector_path(tmp_path: Path) -> None:
    part = derive_truth(_part(tmp_path))
    assert part.domain == "part" and len(part.labels) >= 4 and part.text_known == 0
    assert {label.region for label in part.labels} >= {"drawing"}

    circuit = derive_truth(_circuit(tmp_path))
    texts = {label.text: label for label in circuit.labels}
    designators = {t for t, label in texts.items() if label.role == "designator"}
    assert designators == {"R1", "R2", "R3", "V1"}
    assert texts["4k7"].role == "value" and texts["4k7"].value == ("si", 4700.0, 1)
    assert circuit.unlocated_designators == [] and circuit.text_known == len(circuit.labels)


def test_run_bench_scores_e0_skips_raster_and_maps_a_raster_stub(tmp_path: Path) -> None:
    cases = [_part(tmp_path), _circuit(tmp_path)]
    truths = {c.case_id: derive_truth(c) for c in cases}

    def oracle(inp: ExtractionInput) -> list[ExtractedLabel]:
        """Reads the truth (allowed only in this test): every label in image px as an oriented
        box, except the first one; the last one is misread."""
        if inp.kind != "raster":
            return []
        assert not inp.path.with_suffix(".json").exists()  # nothing beside the image
        stored = manifest_file(tmp_path / "variants", inp.case_id, inp.variant)
        manifest = VariantManifest.model_validate_json(stored.read_text())
        out = []
        for label in truths[inp.case_id].labels[1:]:
            quad = _to_px(manifest, label.bbox)
            out.append(ExtractedLabel(text=label.text, bbox=(0, 0, 1, 1), quad=tuple(quad)))
        if out and out[-1].text is not None:
            out[-1].text += "?"
        return out

    class Broken:
        name = "broken"
        inputs = frozenset({"vector"})

        def extract(self, inp: ExtractionInput) -> ExtractionResult:
            raise RuntimeError("engine crashed")

    out = tmp_path / "run"
    extractors = [VectorPathExtractor(), StubExtractor(oracle, credits=0.5), Broken()]
    info, rows = run_bench(
        cases, [VECTOR, SCAN], extractors, out_dir=out, variants_dir=tmp_path / "variants", seed=3
    )
    assert len(rows) == 2 * 2 * 3 and info.extractors == ["e0", "stub", "broken"]
    by = {(r.case_id, r.variant, r.extractor): r for r in rows}

    for case_id in ("Plate", "divider"):
        e0 = by[case_id, "vector", "e0"]
        assert e0.status == "ok" and e0.rates is not None
        assert e0.rates.det_precision == e0.rates.det_recall == 1.0
        assert by[case_id, "scan-test", "e0"].status == "skipped"
        broken = by[case_id, "vector", "broken"]
        assert broken.status == "error" and "engine crashed" in (broken.note or "")
        # a crash counts as a sheet with every label missed, not as "not applicable"
        assert broken.score is not None and broken.rates is not None
        assert broken.score.truth == len(truths[case_id].labels) and broken.score.matched == 0
        assert broken.rates.det_recall == 0.0 and broken.rates.det_precision is None
        stub = by[case_id, "scan-test", "stub"]
        n = len(truths[case_id].labels)
        assert stub.score is not None and stub.score.matched == n - 1 and stub.credits == 0.5
    circuit = by["divider", "vector", "e0"].rates
    assert circuit is not None and circuit.text_recall == 1.0 and circuit.key_recall == 1.0
    stub_c = by["divider", "scan-test", "stub"].score
    assert stub_c is not None and stub_c.text_correct == len(truths["divider"].labels) - 2

    assert len((out / "results.jsonl").read_text().splitlines()) == len(rows)
    assert json.loads((out / "run.json").read_text())["seed"] == 3
    assert SheetTruth.model_validate_json((out / "truth" / "divider.json").read_text()).labels
    assert (out / "predictions" / "stub" / "scan-test" / "Plate.json").is_file()
    summary = (out / "summary.md").read_text()
    assert "n/a (skipped: reads vector input only)" in summary and "| divider |" in summary
    assert "| broken | vector | part | 1 / 1 · **1 failed** | – | 0.0 % |" in summary
    assert "**failed**: RuntimeError: engine crashed" in summary
    with pytest.raises(ValueError, match="unique"):
        run_bench(cases, [VECTOR], [StubExtractor(), StubExtractor()],
                  out_dir=out, variants_dir=tmp_path / "variants")  # fmt: skip


def test_to_sheet_is_identity_for_vector_input(tmp_path: Path) -> None:
    _, manifest = prepare_variant("Plate", make_drawing(tmp_path / "p.pdf"), VECTOR, tmp_path)
    label = ExtractedLabel(bbox=(1, 2, 3, 4))
    assert to_sheet([label], manifest) == [[(1, 2), (3, 2), (3, 4), (1, 4)]]


def test_cli_and_case_discovery(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    (data / "Plate").mkdir(parents=True)
    make_drawing(data / "Plate" / "Plate_dwg.pdf")
    (data / "circuits" / "divider").mkdir(parents=True)
    make_schematic(data / "circuits" / "divider" / "divider_sch.pdf")
    (data / "circuits" / "divider" / "divider.reference.json").write_text(json.dumps(REFERENCE))
    manifest = {"cases": {"divider": {"split": "dev"}}}
    (data / "circuits" / "cases.json").write_text(json.dumps(manifest))

    cases = bench_cases(data)
    assert [(c.case_id, c.domain) for c in cases] == [("Plate", "part"), ("divider", "circuit")]
    assert [c.case_id for c in bench_cases(data, "circuits")] == ["divider"]

    out = tmp_path / "out"
    argv = ["--data-dir", str(data), "extract-bench", "--variants", "vector,raster-100"]
    argv += ["--extractors", "e0", "--out", str(out), "--variants-dir", str(tmp_path / "v")]
    assert main(argv) == 0
    assert "| e0 | raster-100 | part | 0 / 1 | n/a" in (out / "summary.md").read_text()
    assert (tmp_path / "v" / "images" / "Plate" / "raster-100.png").is_file()
    base = ["--data-dir", str(data), "extract-bench", "--variants", "vector"]
    for wrong in (
        ["--data-dir", str(data), "extract-bench", "--variants", "fax"],
        ["--data-dir", str(data), "extract-bench", "--variants", "vector,vector"],
        [*base, "--extractors", "e0,e0"],
        [*base, "--iou", "0"],
        [*base, "--iou", "1.5"],
        [*base, "--cases", "Plate,Nope"],
    ):
        with pytest.raises(SystemExit):
            main(wrong)
    with pytest.raises(ValueError, match="unknown extractor"):
        load_extractor("tesseract")
    stub = load_extractor("mechatronikers.testbench.extraction:StubExtractor")
    assert isinstance(stub, StubExtractor)
