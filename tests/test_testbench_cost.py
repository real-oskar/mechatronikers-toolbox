"""Cost, model calls and tokens per test bench case (#58), with a scripted fake model."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from mechatronikers.agent.budget import BudgetGuard
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.llm import ModelClient, text_block
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.__main__ import main
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.merge import merge_runs
from mechatronikers.testbench.pipelines import MODEL_EVENTS, PipelineOutcome, model_usage
from mechatronikers.testbench.run import run


def _answer(tokens: tuple[int, int], reported: float | None = None) -> AIMessage:
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)}
    meta = {} if reported is None else {"token_usage": {"cost": reported}}
    return AIMessage(content="ok", usage_metadata=usage, response_metadata=meta)  # type: ignore[arg-type]


# Default fallback prices 3 / 15 credits per million tokens: 0.006 + 0.0123 (reported) + 0.
ANSWERS = [_answer((1000, 200)), _answer((5000, 300), reported=0.0123), _answer((0, 0))]


def _spend(out_dir: Path, ledger: Path, answers: list[AIMessage]) -> BudgetGuard:
    """What the agent pipeline does per case: every call through ModelClient and the budget
    guard, model events into the case folder."""
    config = AgentConfig(model="fake", ledger=ledger)
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    with (out_dir / MODEL_EVENTS).open("w") as events:
        emitter = EventEmitter("agent", session_id="ses-t", run_id="run-t", stream=events)
        emitter.emit("run.started", {"drawing": "x.pdf"})  # other events are not spend
        client = ModelClient(GenericFakeChatModel(messages=iter(answers)), config, guard, emitter)

        async def calls() -> None:
            for _ in answers:
                await client.ask("step", "system", [text_block("drawing")])

        asyncio.run(calls())
    return guard


GUARDS: list[BudgetGuard] = []


def spending_pipeline(
    case: TestCase, *, model: str, variant: str, out_dir: Path
) -> PipelineOutcome:
    GUARDS.append(_spend(out_dir, out_dir / "spend.json", ANSWERS))
    return PipelineOutcome(step_path=None)  # no geometry: cost does not depend on it


def silent_pipeline(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
    return PipelineOutcome(step_path=None)  # no model calls, no events (like the self-check)


def crashing_pipeline(
    case: TestCase, *, model: str, variant: str, out_dir: Path
) -> PipelineOutcome:
    GUARDS.append(_spend(out_dir, out_dir / "spend.json", ANSWERS[:1]))
    raise RuntimeError("sandbox died")


def _case(root: Path, name: str = "Plate") -> TestCase:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / f"{name}_dwg.pdf").write_bytes(b"%PDF-1.4")
    (folder / f"{name}.stp").write_bytes(b"ISO-10303-21;")  # never compared: no STEP produced
    return TestCase(
        case_id=name, drawing=folder / f"{name}_dwg.pdf", reference_step=folder / f"{name}.stp"
    )


def _run(cases: list[TestCase], pipeline: object, out: Path) -> None:
    run(
        cases,
        pipeline,  # type: ignore[arg-type]
        pipeline_name="fake",
        test_set="unit",
        models=["fake"],
        variants=["vector"],
        out_dir=out,
    )


def test_case_cost_comes_from_the_recorded_spend(tmp_path: Path) -> None:
    GUARDS.clear()
    out = tmp_path / "run"
    _run([_case(tmp_path / "data")], spending_pipeline, out)
    (guard,) = GUARDS
    row = json.loads((out / "results.jsonl").read_text())
    ledger = json.loads((guard.ledger).read_text())
    assert row["cost"] == pytest.approx(guard.run_spent) == pytest.approx(ledger["total"])
    assert row["cost"] == pytest.approx(0.006 + 0.0123)
    assert row["model_calls"] == guard.calls == ledger["calls"] == 3
    assert (row["input_tokens"], row["output_tokens"]) == (6000, 500)
    summary = (out / "summary.md").read_text()
    assert "- **Cost: 0.0183 credits · 3 model calls · 6,000 input / 500 output tokens**" in summary
    assert "| Plate | fake | vector | 1 | 3 | 6,000 | 500 | 0.0183 |" in summary
    assert "| **Total** | | | | **3** | **6,000** | **500** | **0.0183** |" in summary


def test_a_crashed_case_still_reports_what_it_spent(tmp_path: Path) -> None:
    GUARDS.clear()
    out = tmp_path / "run"
    _run([_case(tmp_path / "data")], crashing_pipeline, out)
    row = json.loads((out / "results.jsonl").read_text())
    assert row["status"] == "pipeline_error" and "sandbox died" in row["error"]
    assert row["cost"] == pytest.approx(GUARDS[0].run_spent) == pytest.approx(0.006)
    assert row["model_calls"] == 1


def test_no_model_events_means_no_recorded_cost(tmp_path: Path) -> None:
    out = tmp_path / "run"
    _run([_case(tmp_path / "data")], silent_pipeline, out)
    row = json.loads((out / "results.jsonl").read_text())
    assert row["cost"] is None and row["model_calls"] is None
    assert "no recorded spend for 1 of 1 rows" in (out / "summary.md").read_text()
    (out / "cut.ndjson").write_text('{"type": "model.response", "payload": {"cost": 0.5}}\n{"ty')
    (out / MODEL_EVENTS).write_text((out / "cut.ndjson").read_text())
    usage = model_usage(out)  # a line cut off by a crash is skipped
    assert usage is not None and usage.cost == 0.5 and usage.model_calls == 1


def test_merged_summary_sums_cost_and_fills_in_older_runs(tmp_path: Path) -> None:
    GUARDS.clear()
    cases = [_case(tmp_path / "data", "Plate"), _case(tmp_path / "data", "Bolt")]
    _run(cases, spending_pipeline, tmp_path / "base")
    _run(cases[1:], crashing_pipeline, tmp_path / "up")  # Bolt rerun: replaces its cost
    results = tmp_path / "base" / "results.jsonl"  # as written before #58: no cost fields
    old = [json.loads(line) for line in results.read_text().splitlines()]
    for row in old:
        for key in ("cost", "model_calls", "input_tokens", "output_tokens"):
            del row[key]
    results.write_text("".join(json.dumps(row) + "\n" for row in old))

    _, rows = merge_runs(tmp_path / "base", [tmp_path / "up"], tmp_path / "merged")
    assert [(r.case_id, r.model_calls) for r in rows] == [("Plate", 3), ("Bolt", 1)]
    summary = (tmp_path / "merged" / "summary.md").read_text()
    assert "| **Total** | | | | **4** | **7,000** | **700** | **0.0243** |" in summary


def test_cli_run_prints_the_total_cost(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    GUARDS.clear()
    _case(tmp_path / "data")
    argv = ["--data-dir", str(tmp_path / "data"), "run", "--models", "fake"]
    argv += ["--pipeline", "tests.test_testbench_cost:spending_pipeline"]
    argv += ["--out", str(tmp_path / "run"), "--no-review-package"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "Plate" in out and "(0.0183 credits)" in out
    assert "cost: 0.0183 credits · 3 model calls · 6,000 input / 500 output tokens" in out


def test_the_agent_pipeline_writes_its_events_where_the_bench_reads_the_spend() -> None:
    import inspect

    from mechatronikers.agent import pipeline

    assert f'out_dir / "{MODEL_EVENTS}"' in inspect.getsource(pipeline._run)
