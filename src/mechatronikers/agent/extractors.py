"""Model-based extraction backends for the extraction benchmark (T12 candidates E3 and E4).

Both find text with drawing-mcp's offline detector and let the configured vision model read
crops; E4 asks the model only where Tesseract is unsure. They implement the bench's `Extractor`
protocol (testbench/extraction.py) and are built by `load_extractor` with a shared, budget-guarded
`CropReader` from `bench_reader`: one credit cap for the whole bench run (`--max-credits`).
"""

import os
from pathlib import Path
from typing import Any

from mechatronikers.agent.budget import BudgetExceededError, BudgetGuard
from mechatronikers.agent.config import AgentConfig, load_dotenv
from mechatronikers.agent.crop_reader import Crop, CropReader, ReadOutcome
from mechatronikers.drawing_mcp.raster import (
    TextCandidate,
    crop_image,
    load_grey,
    paper_level,
    png_bytes,
    text_candidates,
)
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.extraction import (
    BackendUnavailableError,
    ExtractedLabel,
    ExtractionInput,
    ExtractionResult,
    InputKind,
    granularity,
)

CROP_GLYPH_PX = 32.0  # glyph height in the crops the model sees (smaller crops, fewer tokens)
DEFAULT_THRESHOLD = 0.8  # E4: Tesseract readings below this confidence go to the model


def _sheet(inp: ExtractionInput) -> tuple[Any, float]:
    if inp.kind != "raster":
        raise ValueError("this backend reads raster images only")
    grey, header_dpi = load_grey(inp.path)
    dpi = inp.dpi or header_dpi
    if not dpi:
        raise ValueError(f"{inp.path}: resolution unknown (no dpi given and none in the file)")
    return grey, dpi


def _crops(grey: Any, candidates: list[TextCandidate], glyph_px: float) -> list[Crop]:
    paper = paper_level(grey)
    crops = []
    for c in candidates:
        image = crop_image(grey, c, glyph_target_px=glyph_px, paper=paper)
        crops.append(Crop(id=c.id, png=png_bytes(image), size=image.size))
    return crops


def _read(reader: CropReader, crops: list[Crop], domain: str) -> ReadOutcome:
    try:
        return reader.run(reader.read(crops, domain))
    except BudgetExceededError as exc:
        # Not a failure of the backend: the sheet was not read within the cap (n/a, unscored).
        raise BackendUnavailableError(f"budget reached: {exc}") from exc


def _box(c: TextCandidate) -> tuple[float, float, float, float]:
    return (float(c.bbox[0]), float(c.bbox[1]), float(c.bbox[2]), float(c.bbox[3]))


def _meta(reader: CropReader, outcome: ReadOutcome, **extra: Any) -> dict[str, Any]:
    estimate = outcome.estimate
    return {
        "model": reader.config.model,
        "calls": outcome.calls,
        "dry_run": outcome.dry_run,
        "estimate": estimate.model_dump(),
        **extra,
    }


class VisionCropExtractor:
    """E3: the detector's crops, each read by the vision model. Crops the model calls
    non-lettering are dropped; unreadable ones stay as located, unread labels."""

    inputs: frozenset[InputKind] = frozenset({"raster"})

    def __init__(self, reader: CropReader, *, glyph_px: float = CROP_GLYPH_PX) -> None:
        self.reader = reader
        self.glyph_px = glyph_px
        self.name = "e3 (dry run)" if reader.dry_run else "e3"

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        grey, dpi = _sheet(inp)
        candidates = text_candidates(grey, dpi, granularity(inp.domain))
        outcome = _read(self.reader, _crops(grey, candidates, self.glyph_px), inp.domain)
        labels = []
        for c in candidates:
            reading = outcome.readings[c.id]
            if not reading.lettering:
                continue
            labels.append(
                ExtractedLabel(
                    text=reading.text, bbox=_box(c), confidence=1.0 if reading.text else 0.0
                )
            )
        return ExtractionResult(
            labels=labels,
            credits=outcome.credits,
            meta=_meta(self.reader, outcome, candidates=len(candidates), crops=len(candidates)),
        )


class HybridExtractor:
    """E4: Tesseract reads every crop; the vision model re-reads only the crops Tesseract read
    with a confidence below `threshold` (or not at all)."""

    inputs: frozenset[InputKind] = frozenset({"raster"})

    def __init__(
        self,
        reader: CropReader,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        lang: str = "eng",
        glyph_px: float = CROP_GLYPH_PX,
    ) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        self.reader = reader
        self.threshold = threshold
        self.lang = lang
        self.glyph_px = glyph_px
        self.name = "e4 (dry run)" if reader.dry_run else "e4"

    def extract(self, inp: ExtractionInput) -> ExtractionResult:
        from mechatronikers.drawing_mcp.ocr import (
            TesseractUnavailableError,
            read_candidates,
            tesseract_version,
        )

        grey, dpi = _sheet(inp)
        candidates = text_candidates(grey, dpi, granularity(inp.domain))
        try:
            engine = tesseract_version()
            reads = read_candidates(grey, candidates, lang=self.lang)
        except TesseractUnavailableError as exc:
            raise BackendUnavailableError(str(exc)) from exc
        pairs = list(zip(candidates, reads, strict=True))
        unsure = [c for c, r in pairs if not r.text or r.conf < self.threshold]
        outcome = _read(self.reader, _crops(grey, unsure, self.glyph_px), inp.domain)
        labels = []
        for c, r in pairs:
            reading = outcome.readings.get(c.id)
            if reading is None:  # Tesseract was sure
                labels.append(ExtractedLabel(text=r.text, bbox=_box(c), confidence=r.conf))
            elif reading.lettering:
                text = reading.text if reading.answered else (r.text or None)
                labels.append(
                    ExtractedLabel(
                        text=text,
                        bbox=_box(c),
                        confidence=1.0 if reading.text else (r.conf if text else 0.0),
                    )
                )
        return ExtractionResult(
            labels=labels,
            credits=outcome.credits,
            meta=_meta(
                self.reader,
                outcome,
                engine=engine,
                threshold=self.threshold,
                candidates=len(candidates),
                crops=len(unsure),
            ),
        )


def bench_reader(*, max_credits: float | None, events: Path, dry_run: bool = False) -> CropReader:
    """The model reader shared by all model backends of one bench run. A real run needs
    MT_MODEL / OPENROUTER_API_KEY and spends at most `max_credits` (and never more than the
    ledger's total budget allows). A dry run needs neither: it only estimates."""
    if dry_run:
        load_dotenv()
        config = (
            AgentConfig.from_env()
            if "MT_MODEL" in os.environ
            else AgentConfig(model="(MT_MODEL not set)")
        )
        return CropReader(None, config, dry_run=True)
    if max_credits is None or max_credits <= 0:
        raise ValueError("model backends need --max-credits > 0 (or --dry-run)")
    from mechatronikers.agent.llm import ModelClient, create_model

    config = AgentConfig.from_env()
    guard = BudgetGuard(max_credits, config.budget_total, config.ledger)
    events.parent.mkdir(parents=True, exist_ok=True)
    stream = events.open("a")
    emitter = EventEmitter("extract-bench", run_id=events.parent.name, stream=stream)
    reader = CropReader(ModelClient(create_model(config), config, guard, emitter), config)
    reader.on_close(stream.close)
    return reader
