"""Extractor interface for the extraction benchmark, the E0 baseline and a stub (docs/09).

An extractor turns one drawing input (vector PDF or raster image) into labels: text, position and
confidence. Coordinates are in the frame of the input: sheet millimetres from the top-left corner
for vector input, pixels of the given image for raster input. The bench maps raster coordinates
back to the sheet with the variant's stored transform. Extractors get only the input file; the
transform and the ground truth stay with the scorer (manifests are written apart from the
images). Backends must not look for them.

Backends are written where they belong (local OCR inside drawing-mcp, model-based readers in the
agent layer, D50) and plug into the bench through a small adapter with this protocol. Built-in
names may carry options (`e4@threshold=0.6`); other backends load as `module:attribute` (a
factory) like test bench pipelines.

Built in (T12 candidates, D85): E0 vector path; E1 Tesseract on the whole sheet; E1 on the
crops of the raster text detector (`e1-crops`); the detector alone (`detect`, located but not
read); E3 a vision model reading the detector's crops; E4 Tesseract on the crops with the vision
model re-reading the crops Tesseract is unsure about. E3 and E4 call a model and live in the
agent layer (`agent/extractors.py`).
"""

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from mechatronikers.spec.drawing import BBox, Vec2

if TYPE_CHECKING:
    from mechatronikers.agent.crop_reader import CropReader
    from mechatronikers.drawing_mcp.raster import Granularity

InputKind = Literal["vector", "raster"]
Domain = Literal["part", "circuit"]


class ExtractionInput(BaseModel):
    case_id: str
    variant: str
    kind: InputKind
    domain: Domain  # mechanical drawing or schematic, as the caller knows from the flow
    path: Path  # PDF (vector) or PNG (raster)
    dpi: float | None = None  # nominal resolution of a raster input (also in the PNG header)


class ExtractedLabel(BaseModel):
    text: str | None = None  # None: located, not read
    bbox: BBox  # input frame: sheet mm (vector) or image px (raster)
    quad: tuple[Vec2, Vec2, Vec2, Vec2] | None = None  # oriented box, same frame; wins over bbox
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExtractionResult(BaseModel):
    labels: list[ExtractedLabel]
    credits: float = Field(default=0.0, ge=0)  # model cost for this sheet; 0 for local backends
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. engine version, model id


class BackendUnavailableError(RuntimeError):
    """The backend cannot run on this machine (e.g. the Tesseract binary is missing). The bench
    reports such pairs as unavailable, not as errors."""


class Extractor(Protocol):
    name: str
    inputs: frozenset[InputKind]  # input kinds the backend can read; others are skipped

    def extract(self, inp: ExtractionInput) -> ExtractionResult: ...


class VectorPathExtractor:
    """E0: the existing vector path, baseline and upper bound on clean vector input.

    Mechanical drawings: drawing-mcp's glyph-outline clusters. NX writes text as outlines without
    a text layer, so the labels are located exactly but not read (text None). Schematics: the PDF
    text layer, merged into runs as circuit-mcp does. Raster input is out of reach for this path.
    """

    name = "e0"
    inputs: frozenset[InputKind] = frozenset({"vector"})

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        if inp.kind != "vector":
            raise ValueError("E0 reads vector PDFs only")
        if inp.domain == "circuit":
            from mechatronikers.circuit_mcp.analysis import merge_words
            from mechatronikers.circuit_mcp.extract import extract_texts

            runs = merge_words(extract_texts(inp.path))
            labels = [ExtractedLabel(text=run.text, bbox=run.bbox) for run in runs]
        else:
            from mechatronikers.drawing_mcp.analysis import analyse

            labels = [
                ExtractedLabel(text=label.text, bbox=label.bbox)
                for label in analyse(inp.path).summary.labels
            ]
        return ExtractionResult(labels=labels, meta={"path": "vector"})


class StubExtractor:
    """Returns scripted labels and never looks at the drawing: for tests and harness checks.
    Without a script it finds nothing (every truth label becomes a miss)."""

    name = "stub"
    inputs: frozenset[InputKind] = frozenset({"vector", "raster"})

    def __init__(
        self,
        script: Callable[[ExtractionInput], list[ExtractedLabel]] | None = None,
        *,
        credits: float = 0.0,
    ) -> None:
        self.script = script
        self.credits = credits

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        labels = self.script(inp) if self.script is not None else []
        return ExtractionResult(labels=labels, credits=self.credits)


def granularity(domain: Domain) -> "Granularity":
    """Label granularity of the vector path in each domain: glyph clusters on drawings, text runs
    (lines) on schematics."""
    return "line" if domain == "circuit" else "cluster"


def _raster(inp: ExtractionInput) -> tuple[Any, float]:
    from mechatronikers.drawing_mcp.raster import load_grey

    if inp.kind != "raster":
        raise ValueError("this backend reads raster images only")
    grey, header_dpi = load_grey(inp.path)
    dpi = inp.dpi or header_dpi
    if not dpi:
        raise ValueError(f"{inp.path}: resolution unknown (no dpi given and none in the file)")
    return grey, dpi


def _px_box(box: tuple[int, int, int, int]) -> BBox:
    return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))


def _tesseract_meta() -> dict[str, Any]:
    from mechatronikers.drawing_mcp.ocr import TesseractUnavailableError, tesseract_version

    try:
        return {"engine": tesseract_version()}
    except TesseractUnavailableError as exc:
        raise BackendUnavailableError(str(exc)) from exc


class CandidateExtractor:
    """The raster text detector alone (drawing-mcp `raster.text_candidates`): boxes, no text.
    Shows the detection ceiling of the crop-based backends at no cost."""

    name = "detect"
    inputs: frozenset[InputKind] = frozenset({"raster"})

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        from mechatronikers.drawing_mcp.raster import text_candidates

        grey, dpi = _raster(inp)
        candidates = text_candidates(grey, dpi, granularity(inp.domain))
        labels = [ExtractedLabel(bbox=_px_box(c.bbox), confidence=0.0) for c in candidates]
        return ExtractionResult(labels=labels, meta={"candidates": len(candidates)})


class TesseractSheetExtractor:
    """E1: Tesseract on the whole sheet (sparse text), upright and turned by 90° for text running
    bottom to top. Finds and reads in one go; slanted labels are out of its reach."""

    name = "e1"
    inputs: frozenset[InputKind] = frozenset({"raster"})

    def __init__(self, *, min_conf: float = 0.3, lang: str = "eng") -> None:
        self.min_conf = min_conf
        self.lang = lang

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        from mechatronikers.drawing_mcp.ocr import TesseractUnavailableError, read_sheet

        meta = _tesseract_meta()
        grey, dpi = _raster(inp)
        try:
            lines = read_sheet(grey, dpi, lang=self.lang, min_conf=self.min_conf)
        except TesseractUnavailableError as exc:
            raise BackendUnavailableError(str(exc)) from exc
        labels = [
            ExtractedLabel(text=line.text, bbox=_px_box(line.bbox), confidence=line.conf)
            for line in lines
        ]
        return ExtractionResult(labels=labels, meta={**meta, "lang": self.lang})


class TesseractCropExtractor:
    """E1 on crops: the raster text detector finds the labels, Tesseract reads each crop (turned
    upright from the detected baseline, both reading directions tried)."""

    name = "e1-crops"
    inputs: frozenset[InputKind] = frozenset({"raster"})

    def __init__(self, *, lang: str = "eng") -> None:
        self.lang = lang

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        from mechatronikers.drawing_mcp.ocr import TesseractUnavailableError, read_candidates
        from mechatronikers.drawing_mcp.raster import text_candidates

        meta = _tesseract_meta()
        grey, dpi = _raster(inp)
        candidates = text_candidates(grey, dpi, granularity(inp.domain))
        try:
            reads = read_candidates(grey, candidates, lang=self.lang)
        except TesseractUnavailableError as exc:
            raise BackendUnavailableError(str(exc)) from exc
        labels = [
            ExtractedLabel(text=r.text or None, bbox=_px_box(c.bbox), confidence=r.conf)
            for c, r in zip(candidates, reads, strict=True)
        ]
        return ExtractionResult(
            labels=labels, meta={**meta, "lang": self.lang, "candidates": len(candidates)}
        )


def _e3(*, reader: "CropReader | None" = None, **options: Any) -> Extractor:
    from mechatronikers.agent.extractors import VisionCropExtractor

    if reader is None:
        raise ValueError("e3 calls a model: give --max-credits (or --dry-run)")
    return VisionCropExtractor(reader, **options)


def _e4(*, reader: "CropReader | None" = None, **options: Any) -> Extractor:
    from mechatronikers.agent.extractors import HybridExtractor

    if reader is None:
        raise ValueError("e4 calls a model: give --max-credits (or --dry-run)")
    return HybridExtractor(reader, **options)


BUILTIN: dict[str, Callable[..., Extractor]] = {
    "e0": VectorPathExtractor,
    "stub": StubExtractor,
    "detect": CandidateExtractor,
    "e1": TesseractSheetExtractor,
    "e1-crops": TesseractCropExtractor,
    "e3": _e3,
    "e4": _e4,
}
MODEL_BACKENDS = frozenset({"e3", "e4"})  # need a model reader (budget-guarded)


def _option(value: str) -> Any:
    try:
        return float(value)
    except ValueError:
        return value


def parse_spec(spec: str) -> tuple[str, dict[str, Any]]:
    """'e4@threshold=0.6@lang=deu' -> ('e4', {'threshold': 0.6, 'lang': 'deu'})."""
    name, *pairs = spec.split("@")
    options: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"bad option {pair!r} in {spec!r}; use name@key=value")
        options[key] = _option(value)
    return name, options


def needs_model(spec: str) -> bool:
    return parse_spec(spec)[0] in MODEL_BACKENDS


def load_extractor(spec: str, *, reader: "CropReader | None" = None) -> Extractor:
    """A built-in name with options ('e1', 'e4@threshold=0.6') or 'package.module:factory'.
    Model backends get the budget-guarded `reader`."""
    name, options = parse_spec(spec)
    if name in BUILTIN:
        kwargs = {**options, "reader": reader} if name in MODEL_BACKENDS else options
        try:
            extractor = BUILTIN[name](**kwargs)
        except TypeError as exc:
            raise ValueError(f"bad options for {name!r}: {exc}") from exc
        if options:
            extractor.name = extractor.name.replace(name, spec, 1)  # one row per configuration
        return extractor
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"unknown extractor {spec!r}; use {sorted(BUILTIN)} or 'module:factory'")
    try:
        factory: Callable[[], Extractor] = getattr(importlib.import_module(module_name), attr)
    except (ModuleNotFoundError, AttributeError) as exc:
        raise ValueError(f"cannot load extractor {spec!r}: {exc}") from exc
    return factory()
