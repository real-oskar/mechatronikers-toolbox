"""Model-based extraction backends (E3, E4) with a scripted fake model: no network, no credits."""

import asyncio
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from mechatronikers.agent import prompts
from mechatronikers.agent.budget import BudgetExceededError, BudgetGuard
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.crop_reader import (
    RESERVE_MARGIN,
    CallEstimate,
    Crop,
    CropReader,
    CropReading,
    ReadOutcome,
    estimate_call,
    image_tokens,
    tile_tokens,
)
from mechatronikers.agent.extractors import HybridExtractor, VisionCropExtractor, bench_reader
from mechatronikers.agent.llm import ModelClient
from mechatronikers.drawing_mcp import ocr
from mechatronikers.drawing_mcp.ocr import CropRead
from mechatronikers.drawing_mcp.raster import load_grey, text_candidates
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.__main__ import main
from mechatronikers.testbench.extract_bench import BenchCase, run_bench
from mechatronikers.testbench.extraction import (
    BackendUnavailableError,
    ExtractionInput,
    load_extractor,
)
from mechatronikers.testbench.variants import VariantSpec
from tests.fixtures.drawings import make_drawing
from tests.fixtures.raster import DPI, draw_sheet, save_sheet

TEXTS = [("R12", (100, 100), 0), ("100nF", (400, 100), 0), ("GND", (800, 150), 90)]
PNG = b"\x89PNG fake"


def _answer(payload: dict[str, Any], tokens: tuple[int, int] = (1000, 100)) -> AIMessage:
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)}
    return AIMessage(content="```json\n" + json.dumps(payload) + "\n```", usage_metadata=usage)  # type: ignore[arg-type]


class Recording(GenericFakeChatModel):
    """Scripted answers; keeps the crop ids of every request."""

    asked: list[list[str]] = []

    def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        human = messages[-1].content
        ids = [b["text"].split()[1] for b in human if b.get("text", "").startswith("crop ")]
        self.asked.append(ids)
        return super()._generate(messages, *args, **kwargs)


def _reader(
    tmp_path: Path, answers: list[AIMessage], limit: float = 1.0
) -> tuple[CropReader, Recording, BudgetGuard]:
    config = AgentConfig(model="fake", ledger=tmp_path / "spend.json")
    guard = BudgetGuard(limit, config.budget_total, config.ledger)
    model = Recording(messages=iter(answers), asked=[])
    emitter = EventEmitter("extract-bench", run_id="t", stream=io.StringIO())
    return CropReader(ModelClient(model, config, guard, emitter), config, batch=2), model, guard


def _crops(n: int) -> list[Crop]:
    return [Crop(id=f"c{i}", png=PNG, size=(120, 50)) for i in range(n)]


def test_token_estimates() -> None:
    assert image_tokens(750, 1) == 1 and image_tokens(150, 50) == 10
    assert image_tokens(3136, 1000) == image_tokens(1568, 500)  # scaled down like the provider
    assert tile_tokens(120, 50) == 255 and tile_tokens(1024, 1024) == 85 + 170 * 4
    config = AgentConfig(model="m", price_input=3.0, price_output=15.0)
    one = estimate_call(_crops(1), config, "system")
    two = estimate_call(_crops(2), config, "system")
    assert two.tokens_in - one.tokens_in == image_tokens(120, 50) + 6
    assert two.credits > one.credits > 0 and two.credits_tiles > two.credits
    assert (one + two).tokens_out == one.tokens_out + two.tokens_out


def test_reader_batches_retries_skipped_crops_and_records_spend(tmp_path: Path) -> None:
    reader, model, guard = _reader(
        tmp_path,
        [
            _answer({"c0": "R12", "c1": False}),
            _answer({"c2": None, "c9": "not asked"}),  # c3 skipped
            _answer({"c3": 470}),  # retry for c3 only; a bare number is text too
        ],
    )
    outcome = asyncio.run(reader.read(_crops(4), "circuit"))
    assert model.asked == [["c0", "c1"], ["c2", "c3"], ["c3"]]
    r = outcome.readings
    assert r["c0"] == CropReading(id="c0", text="R12")
    assert not r["c1"].lettering and r["c2"].text is None and r["c2"].lettering
    assert r["c3"].text == "470" and "c9" not in r
    assert outcome.calls == 3 and not outcome.dry_run
    # cost from the token usage at the configured fallback prices, recorded in the ledger
    assert outcome.credits == pytest.approx(3 * (1000 * 3.0 + 100 * 15.0) / 1e6)
    assert guard.total_spent() == pytest.approx(outcome.credits) and guard.calls == 3


def test_crops_never_answered_stay_unread(tmp_path: Path) -> None:
    reader, _, _ = _reader(tmp_path, [_answer({}), _answer({})])
    outcome = asyncio.run(reader.read(_crops(1), "part"))
    assert outcome.readings["c0"] == CropReading(id="c0", answered=False)


def test_reader_stops_before_a_call_would_pass_the_cap(tmp_path: Path) -> None:
    reader, model, guard = _reader(tmp_path, [], limit=1e-6)
    with pytest.raises(BudgetExceededError, match="would pass the run budget"):
        asyncio.run(reader.read(_crops(2), "part"))
    assert model.asked == [] and guard.total_spent() == 0


def test_each_call_reserves_its_cost_before_it_is_made(tmp_path: Path) -> None:
    """First call: reserved at the tile-billed estimate. Later calls: at the highest
    reported/estimated ratio seen so far, with headroom, so the cap is not passed."""
    reader, model, guard = _reader(tmp_path, [_answer({"c0": "A", "c1": "B"})], limit=0.0095)
    config = reader.config
    call = estimate_call(_crops(2), config, prompts.READ_CROPS)
    assert reader.reserve(call) == pytest.approx(max(call.credits, call.credits_tiles))
    with pytest.raises(BudgetExceededError, match="reserving"):
        asyncio.run(reader.read(_crops(4), "part"))
    paid = (1000 * 3.0 + 100 * 15.0) / 1e6  # the fake's token usage at the fallback prices
    assert model.asked == [["c0", "c1"]] and guard.total_spent() == pytest.approx(paid)
    assert reader.reserve(call) == pytest.approx(paid * RESERVE_MARGIN)  # learned from call 1
    assert guard.run_spent + reader.reserve(call) > guard.run_limit


def test_reader_releases_what_it_was_built_with() -> None:
    closed: list[str] = []
    reader = CropReader(None, AgentConfig(model="m"), dry_run=True)
    reader.on_close(lambda: closed.append("events"))
    reader.close()
    reader.close()
    assert closed == ["events"]


def test_dry_run_estimates_without_calls() -> None:
    reader = CropReader(None, AgentConfig(model="m"), batch=2, dry_run=True)
    outcome = asyncio.run(reader.read(_crops(5), "circuit"))
    assert outcome.calls == 0 and outcome.dry_run
    assert outcome.credits == pytest.approx(outcome.estimate.credits)
    assert outcome.credits > 0
    assert reader.estimated.tokens_in == outcome.estimate.tokens_in
    assert all(not r.answered for r in outcome.readings.values())
    with pytest.raises(ValueError, match="dry run"):
        CropReader(None, AgentConfig(model="m"))


def _sheet(tmp_path: Path) -> tuple[ExtractionInput, list[str]]:
    path = save_sheet(draw_sheet(TEXTS), tmp_path / "sheet.png")
    inp = ExtractionInput(case_id="s", variant="r", kind="raster", domain="circuit", path=path)
    grey, _ = load_grey(path)
    return inp, [c.id for c in text_candidates(grey, DPI, "line")]


def test_e3_reads_crops_and_drops_what_is_not_lettering(tmp_path: Path) -> None:
    inp, ids = _sheet(tmp_path)
    assert len(ids) == 3
    reader, model, _ = _reader(tmp_path, [_answer({ids[0]: "R12", ids[1]: False}),
                                          _answer({ids[2]: None})])  # fmt: skip
    result = VisionCropExtractor(reader).extract(inp)
    reader.close()
    reader.close()  # idempotent
    assert [(label.text, label.confidence) for label in result.labels] == [
        ("R12", 1.0),
        (None, 0.0),
    ]
    assert result.credits > 0 and result.meta["calls"] == 2 and result.meta["crops"] == 3
    assert model.asked == [ids[:2], ids[2:]]


def test_e3_reports_a_reached_budget_as_unavailable(tmp_path: Path) -> None:
    inp, _ = _sheet(tmp_path)
    reader, _, _ = _reader(tmp_path, [], limit=1e-6)
    with pytest.raises(BackendUnavailableError, match="budget reached"):
        VisionCropExtractor(reader).extract(inp)
    reader.close()


class ScriptedReader:
    """Stands in for CropReader: records the crops, answers from a script."""

    def __init__(self, answers: dict[str, str | None | bool]) -> None:
        self.answers = answers
        self.dry_run = False
        self.config = AgentConfig(model="fake")
        self.crops: list[str] = []

    def run(self, work: Any) -> Any:
        return asyncio.run(work)

    async def read(self, crops: Sequence[Crop], domain: str) -> ReadOutcome:
        self.crops += [c.id for c in crops]
        readings = {}
        for c in crops:
            value = self.answers.get(c.id, "")
            readings[c.id] = CropReading(
                id=c.id,
                text=value if isinstance(value, str) and value else None,
                lettering=value is not False,
                answered=c.id in self.answers,
            )
        return ReadOutcome(readings, credits=0.01 * len(crops), calls=1, estimate=CallEstimate())


def test_e4_sends_only_unsure_crops_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, ids = _sheet(tmp_path)
    tesseract = [
        CropRead(text="R12", conf=0.95, rotation=0.0),  # sure: kept
        CropRead(text="1OOnF", conf=0.4, rotation=0.0),  # unsure: re-read
        CropRead(text="", conf=0.0, rotation=0.0),  # nothing read: re-read
    ]
    monkeypatch.setattr(ocr, "read_candidates", lambda grey, cands, **kw: tesseract)
    monkeypatch.setattr(ocr, "tesseract_version", lambda binary=None: "tesseract test")
    scripted = ScriptedReader({ids[1]: "100nF", ids[2]: False})
    result = HybridExtractor(scripted, threshold=0.8).extract(inp)  # type: ignore[arg-type]
    assert scripted.crops == ids[1:]
    assert [label.text for label in result.labels] == ["R12", "100nF"]  # third: no lettering
    assert result.meta["crops"] == 2 and result.meta["engine"] == "tesseract test"
    assert result.credits == pytest.approx(0.02)

    # A crop the model never answered keeps Tesseract's reading.
    scripted = ScriptedReader({})
    result = HybridExtractor(scripted, threshold=0.8).extract(inp)  # type: ignore[arg-type]
    assert [label.text for label in result.labels] == ["R12", "1OOnF", None]
    with pytest.raises(ValueError, match="threshold"):
        HybridExtractor(scripted, threshold=1.5)  # type: ignore[arg-type]


def test_e4_without_tesseract_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, _ = _sheet(tmp_path)
    monkeypatch.setenv("MT_TESSERACT", str(tmp_path / "missing"))
    with pytest.raises(BackendUnavailableError, match="not installed"):
        HybridExtractor(ScriptedReader({})).extract(inp)  # type: ignore[arg-type]


def test_model_backends_load_with_a_reader_and_mark_dry_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("MT_MODEL", raising=False)
    reader = bench_reader(max_credits=None, events=tmp_path / "e.ndjson", dry_run=True)
    assert reader.dry_run and reader.config.model == "(MT_MODEL not set)"
    assert load_extractor("e3", reader=reader).name == "e3 (dry run)"
    e4 = load_extractor("e4@threshold=0.5", reader=reader)
    assert e4.name == "e4@threshold=0.5 (dry run)" and isinstance(e4, HybridExtractor)
    assert e4.threshold == 0.5
    with pytest.raises(ValueError, match="max-credits"):
        bench_reader(max_credits=None, events=tmp_path / "e.ndjson")


def test_cli_needs_a_cap_for_model_backends_and_estimates_in_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    (data / "Plate").mkdir(parents=True)
    make_drawing(data / "Plate" / "Plate_dwg.pdf")
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("MT_MODEL", raising=False)
    base = ["--data-dir", str(data), "extract-bench", "--variants", "raster-100"]
    base += ["--variants-dir", str(tmp_path / "v")]
    with pytest.raises(SystemExit):
        main([*base, "--extractors", "e3"])
    assert "--max-credits" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*base, "--extractors", "e4@bad"])  # a bad option is a usage error, no traceback
    assert "bad option" in capsys.readouterr().err

    out = tmp_path / "out"
    assert main([*base, "--extractors", "detect,e3", "--dry-run", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "dry run, no model calls: estimated" in printed
    summary = (out / "summary.md").read_text()
    assert "| e3 (dry run) | raster-100 | part | 1 / 1 |" in summary
    prediction = json.loads((out / "predictions" / "e3_dry_run_" / "raster-100" / "Plate.json")
                            .read_text())  # fmt: skip
    assert prediction["meta"]["dry_run"] and prediction["credits"] > 0


class LoopBoundModel(BaseChatModel):
    """Answers every crop it is shown. Like an HTTP client with a connection pool, it stays bound
    to the event loop of its first call: a call from another loop fails as httpx does."""

    loop: Any = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "loop-bound-fake"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        ids = [
            b["text"].split()[1]
            for b in messages[-1].content
            if isinstance(b, dict) and str(b.get("text", "")).startswith("crop ")
        ]
        usage = {"input_tokens": 500, "output_tokens": 50, "total_tokens": 550}
        answer = AIMessage(content=json.dumps({i: "R47" for i in ids}), usage_metadata=usage)  # type: ignore[arg-type]
        return ChatResult(generations=[ChatGeneration(message=answer)])

    async def _agenerate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> Any:
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
        elif self.loop is not loop:
            raise RuntimeError("Event loop is closed")
        self.calls += 1
        return self._generate(messages, stop)


@pytest.mark.parametrize("specs", [["e3", "e4"], ["e4"]])
def test_model_backends_share_one_event_loop_across_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, specs: list[str]
) -> None:
    """Regression: one asyncio.run per sheet left the model client bound to a closed loop, so
    every sheet after the first (e4 after e3 on the same sheet, or e4 alone) failed."""
    monkeypatch.setattr(
        ocr, "read_candidates", lambda grey, cands, **kw: [CropRead("", 0.0, 0.0)] * len(cands)
    )
    monkeypatch.setattr(ocr, "tesseract_version", lambda binary=None: "tesseract test")
    config = AgentConfig(model="fake", ledger=tmp_path / "spend.json")
    guard = BudgetGuard(1.0, config.budget_total, config.ledger)
    model = LoopBoundModel()
    emitter = EventEmitter("extract-bench", run_id="t", stream=io.StringIO())
    reader = CropReader(ModelClient(model, config, guard, emitter), config)
    case = BenchCase(case_id="Plate", domain="part", pdf=make_drawing(tmp_path / "plate.pdf"))
    variants = [
        VariantSpec(name="raster-a", kind="raster", dpi=120),
        VariantSpec(name="raster-b", kind="raster", dpi=150),
    ]
    try:
        _, rows = run_bench(
            [case],
            variants,
            [load_extractor(s, reader=reader) for s in specs],
            out_dir=tmp_path / "run",
            variants_dir=tmp_path / "v",
        )
    finally:
        reader.close()
    assert [(r.variant, r.extractor, r.status) for r in rows] == [
        (v.name, s, "ok") for v in variants for s in specs
    ], [r.note for r in rows]
    assert all((r.credits or 0) > 0 for r in rows)
    assert model.calls >= len(rows) and guard.calls == model.calls
