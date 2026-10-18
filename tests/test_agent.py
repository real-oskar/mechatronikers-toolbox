"""Agent end to end against the real sandbox servers, with a scripted fake model (no credits)."""

import asyncio
import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import Field

from mechatronikers.agent import prompts
from mechatronikers.agent.budget import BudgetExceededError, BudgetGuard
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.graph import (
    AgentState,
    build_graph,
    failed_features,
    failing_checks,
    stop_reason,
)
from mechatronikers.agent.llm import (
    ModelClient,
    create_model,
    extract_json,
    image_block,
    prompt_hash,
    text_block,
)
from mechatronikers.agent.runner import drive
from mechatronikers.agent.tools import SandboxTools
from mechatronikers.drawing_mcp.analysis import analyse
from mechatronikers.sandbox.app import create_app
from mechatronikers.session.connections import mcp_connections
from mechatronikers.spec.review import GateDecision, GateRequest
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now
from tests.fixtures.drawings import make_drawing


@pytest.fixture
def sandbox(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[str, dict[str, str]]]:
    """The sheet of `make_drawing` (options via indirect parametrization) in a running sandbox."""
    (tmp_path / "input").mkdir()
    pdf = make_drawing(tmp_path / "input" / "sheet.pdf", **getattr(request, "param", {}))
    summary = analyse(pdf).summary
    labels = summary.labels

    def at(x: float, y: float) -> str:
        return next(
            lb.id
            for lb in labels
            if lb.bbox[0] <= x <= lb.bbox[2] and lb.bbox[1] <= y <= lb.bbox[3]
        )

    ids = {"linear": at(108, 120.5), "round": at(130, 52.5)}
    others = [lb.id for lb in labels if lb.region == "drawing" and lb.id not in ids.values()]
    ids["view"] = next(v.id for v in summary.views if v.kind == "orthographic")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app("ses-agent", tmp_path / "builds", "subprocess"),
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", {**ids, "others": ",".join(others)}
    server.should_exit = True
    thread.join(timeout=10)


def _answer(payload: Any, tokens: tuple[int, int] = (0, 0)) -> AIMessage:
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)}
    return AIMessage(content="```json\n" + json.dumps(payload) + "\n```", usage_metadata=usage)  # type: ignore[arg-type]


PLATE = {
    "type": "extrude_base", "id": "base", "depth": 10, "direction": "positive",
    "profile": {"segments": [
        {"kind": "line", "start": [-50, -30], "end": [50, -30]},
        {"kind": "line", "start": [50, -30], "end": [50, 30]},
        {"kind": "line", "start": [50, 30], "end": [-50, 30]},
        {"kind": "line", "start": [-50, 30], "end": [-50, -30]},
    ]},
}  # fmt: skip


def _hole(x: float) -> dict[str, Any]:
    return {
        "type": "hole",
        "id": "bore",
        "diameter": 20,
        "placement": {"origin": [x, 0, 10], "normal": [0, 0, -1]},
    }


def _script(ids: dict[str, str], plan_hole_x: float, fix: bool) -> list[AIMessage]:
    answers = [
        _answer({"labels": [
            {"id": ids["linear"], "text": "100", "underlined": False, "readable": True},
            {"id": ids["round"], "text": "Ø20", "underlined": False, "readable": True},
        ] + [{"id": o, "text": "", "readable": False} for o in ids["others"].split(",")]}),
        _answer({"part_name": "Plate", "scale": 1.0}),
        _answer({"part_name": "Plate", "overall_size_dims": [f"d-{ids['linear']}", None, None],
                 "features": [PLATE, _hole(plan_hole_x)], "assumptions": []}),
    ]  # fmt: skip
    if fix:
        answers.append(_answer({"features": [PLATE, _hole(0)], "assumptions": []}))
    return answers


class RecordingModel(GenericFakeChatModel):
    """The scripted fake model, also keeping the system prompt and the content of every call."""

    systems: list[str] = Field(default_factory=list)
    contents: list[Any] = Field(default_factory=list)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> BaseMessage:
        self.systems.append(input[0].content)
        self.contents.append(input[1].content)
        return await super().ainvoke(input, config, **kwargs)


class FakeReview:
    """Decides gates like a reviewer in the review app would."""

    def __init__(self, *actions: str | dict[str, Any]) -> None:
        self.actions = list(actions)
        self.requests: list[GateRequest] = []

    async def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        action = self.actions.pop(0)
        if action == "stop":  # the agent process dies while the gate waits
            raise KeyboardInterrupt
        extra = action if isinstance(action, dict) else {"action": action}
        if extra["action"] == "reject":
            extra = {**extra, "reason": "wrong part frame"}
        return GateDecision(gate_id=request.id, reviewer="test", decided_at=utc_now(), **extra)


async def _run(
    url: str,
    answers: list[AIMessage],
    tmp_path: Path,
    review: FakeReview | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    resume: bool = False,
    lang: str | None = None,
    systems: list[str] | None = None,
    contents: list[Any] | None = None,
    model_type: type[RecordingModel] = RecordingModel,
    **kwargs: Any,
) -> tuple[Any, list[dict[str, Any]], BudgetGuard]:
    config = AgentConfig(model="fake", ledger=tmp_path / "spend.json", **kwargs.pop("config", {}))
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    events = io.StringIO()
    emitter = EventEmitter("agent", session_id="ses-agent", run_id="run-t1", stream=events)
    tools = SandboxTools(mcp_connections(url, "unused"))
    await tools.start()
    model = model_type(messages=iter(answers))
    llm = ModelClient(model, config, guard, emitter)
    graph = build_graph(tools, llm, emitter, checkpointer=checkpointer, **kwargs)
    start = None if resume else AgentState(drawing="sheet.pdf", **({"lang": lang} if lang else {}))
    review = review or FakeReview("approve", "finish")
    try:
        state = await drive(graph, "run-t1", emitter, review, start=start, max_iterations=3)
    finally:
        parsed = [json.loads(line) for line in events.getvalue().splitlines()]
        if systems is not None:
            systems.extend(model.systems)
        if contents is not None:
            contents.extend(model.contents)
    return state, parsed, guard


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def _payloads(events: list[dict[str, Any]], type: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e["type"] == type]


def test_happy_path_builds_and_exports(sandbox: tuple[str, dict[str, str]], tmp_path: Path) -> None:
    url, ids = sandbox
    systems: list[str] = []
    state, events, _ = asyncio.run(
        _run(url, _script(ids, 0, fix=False), tmp_path, auto_approve=True, systems=systems)
    )
    assert state["status"] == "done", state.get("error")
    assert systems[2] == prompts.in_language(prompts.PLAN_SPEC, "en")  # no run language: English
    assert {d["stated_text"] for d in state["consistency"]["dimensions"]} == {"100", "Ø20"}
    assert all(d["status"] == "auto_ok" for d in state["consistency"]["dimensions"])
    formats = {a.get("format") for a in state["artifacts"]}
    assert {"step-ap242", "step-ap214", "stl", "svg-iso"} <= formats
    assert _types(events).count("model.request") == 3 and "review.gate_passed" in _types(events)
    # overall size: x from the printed "100", y and z have no printed overall dimension
    checks = {c["id"]: c for c in state["validation"]["report"]["checks"]}
    assert checks["V2.bbox_x"]["passed"] and "V2.bbox_z" not in checks
    assert [n.split(":")[0] for n in state["build_notes"]] == [
        "Overall size y not checked",
        "Overall size z not checked",
    ]
    assert state["coverage"]["circles_checked"] == 1 and state["coverage"]["review_items"] == []


def test_fix_loop_corrects_a_failed_feature(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    state, events, _ = asyncio.run(
        _run(url, _script(ids, 200, fix=True), tmp_path, auto_approve=True)
    )
    assert state["status"] == "done", state.get("error")
    assert state["iteration"] == 1 and state["failure_history"] == [1, 0]
    assert "model.request" in _types(events) and _types(events).count("agent.step_started") >= 8
    build = [f for f in _payloads(events, "review.item_created") if f["step"] == "build"]
    assert {f["item"]["kind"] for f in build} == {"unsupported", "validation_failure"}  # notes
    failed = [f for f in build if f["item"]["kind"] == "validation_failure"]
    assert failed[0]["item"]["feature_ids"] == ["bore"] and failed[0]["iteration"] == 0


def test_build_gives_up_after_plateau(sandbox: tuple[str, dict[str, str]], tmp_path: Path) -> None:
    url, ids = sandbox
    wrong = [_answer({"features": [PLATE, _hole(300)]}), _answer({"features": [PLATE, _hole(400)]})]
    answers = _script(ids, 200, fix=False) + wrong
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "build_failed"
    assert state["iteration"] == 2  # two rounds without improvement before giving up
    (failed,) = _payloads(events, "agent.build_failed")
    # what the gate card, run table and test bench show (#92)
    assert (failed["reason"], failed["iteration"], failed["max_iterations"]) == ("plateau", 2, 3)
    # the bore never went in, so the checks of the remaining plate pass: the feature is named
    assert failed["checks"] == failing_checks(state) == []
    assert [f["feature_id"] for f in failed["failures"]] == failed_features(state) == ["bore"]
    assert stop_reason(state) == "plateau"


def test_build_gives_up_at_the_rework_limit(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    wrong_axis = [None, None, f"d-{ids['linear']}"]  # the printed 100 is the length in x
    answers = _script(ids, 0, fix=False)
    answers[2] = _answer(
        {"part_name": "Plate", "overall_size_dims": wrong_axis, "features": [PLATE, _hole(0)]}
    )
    answers.append(_answer({"features": [PLATE, _hole(0)], "overall_size_dims": wrong_axis}))
    state, events, _ = asyncio.run(
        _run(url, answers, tmp_path, auto_approve=True, max_fix_iterations=1)
    )
    assert state["status"] == "build_failed" and stop_reason(state) == "cap"
    (failed,) = _payloads(events, "agent.build_failed")
    assert (failed["reason"], failed["iteration"], failed["max_iterations"]) == ("cap", 1, 1)
    assert failed["checks"] == failing_checks(state) == ["V2.bbox_z"]
    assert failed_features(state) == []


def test_stop_reason_and_failing_checks_of_a_state() -> None:
    report = {"checks": [{"id": "V1.solid_count", "passed": True}, {"id": "V2.x", "passed": False}]}
    state: dict[str, Any] = {
        "spec": {"features": [{"id": "base"}, {"id": "bore"}]},
        "failures": [{"feature_id": "bore", "message": "no material removed"}],
        "validation": {"report": report},
    }
    assert failing_checks(state) == ["V2.x"]
    # the last build stopped at the base body: the stored report is from an earlier round
    state["failures"] = [{"feature_id": "base", "message": "zero area"}]
    assert failing_checks(state) == []
    assert stop_reason({"status": "error"}) == "error"
    assert stop_reason({"status": "budget_exceeded"}) == "budget"
    assert stop_reason({"status": "aborted", "stop_reason": "cap"}) == "cap"
    assert stop_reason({"status": "done", "stop_reason": "cap"}) is None
    assert stop_reason({"status": "running"}) is None


def test_review_gate_waits_for_the_reviewer(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    review = FakeReview("approve", "finish")
    state, events, _ = asyncio.run(_run(url, _script(ids, 0, fix=False), tmp_path, review))
    assert state["status"] == "done"
    gate = review.requests[0]
    assert gate.kind == "spec_review" and gate.features == ["extrude_base base", "hole bore"]
    assert gate.run_kind == "part" and gate.circuit is None
    assert gate.overall_size_dims == [f"d-{ids['linear']}", None, None]
    assert gate.possibly_missing == [] and gate.id.startswith("gate-")
    types = _types(events)
    assert types.index("agent.interrupt") < types.index("agent.resumed")
    assert "review.gate_passed" not in types  # recorded by the review app, not the agent
    paused, at_gate_2, finished = _payloads(events, "agent.run_finished")
    assert at_gate_2["paused_at"] == "result_review"
    assert (paused["status"], paused["paused_at"], paused["gate_id"]) == (
        "awaiting_review",
        "spec_review",
        gate.id,
    )
    assert finished["status"] == "done" and finished["build_id"]
    assert finished["checks_passed"] == finished["checks_total"] > 0


def test_rejected_plan_ends_the_run(sandbox: tuple[str, dict[str, str]], tmp_path: Path) -> None:
    url, ids = sandbox
    answers = _script(ids, 0, fix=False)
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, FakeReview("reject")))
    assert state["status"] == "rejected" and "wrong part frame" in state["error"]
    assert "build" not in [p["step"] for p in _payloads(events, "agent.step_started")]
    assert _payloads(events, "agent.run_finished")[-1]["status"] == "rejected"


@pytest.mark.parametrize("lang", [None, "de"])  # None: a state checkpointed before #54
def test_waiting_run_resumes_from_the_sqlite_checkpoint(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path, lang: str | None
) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    url, ids = sandbox
    first, second = FakeReview("stop"), FakeReview("approve", "finish")
    fix = _answer({"features": [PLATE, _hole(0)], "assumptions": []})
    systems: list[str] = []

    async def scenario() -> Any:
        db = str(tmp_path / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            with pytest.raises(KeyboardInterrupt):
                await _run(url, _script(ids, 200, fix=False), tmp_path, first, saver, lang=lang)
        async with AsyncSqliteSaver.from_conn_string(db) as saver:  # a new agent process
            return await _run(url, [fix], tmp_path, second, saver, resume=True, systems=systems)

    state, events, _ = asyncio.run(scenario())
    assert state["status"] == "done", state.get("error")
    assert first.requests[0].id == second.requests[0].id  # same gate after the restart
    # the new process opens the same gate again, but does not start the gate step again
    assert _agent_sequence(events, "agent.interrupt")[:5] == [
        *AT_GATE,
        "agent.resumed approve",
        "agent.step_completed review_gate_1",
        "agent.step_started build",
    ]
    # nothing is read or planned twice; the fix keeps the run language from the checkpoint
    assert [p["step"] for p in _payloads(events, "model.request")] == ["fix_spec"]
    assert systems == [prompts.in_language(prompts.FIX_SPEC, lang or "en")]


def test_budget_stops_before_spending_more(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = [_answer({"labels": []}, tokens=(400_000, 20_000))] + _script(ids, 0, fix=False)[1:]
    state, events, guard = asyncio.run(
        _run(url, answers, tmp_path, auto_approve=True, config={"budget_run": 1.0})
    )
    # first call costs 0.4M*3 + 20k*15 per M = 1.5 credits > budget: no second call happens
    assert state["status"] == "budget_exceeded" and stop_reason(state) == "budget"
    assert _types(events).count("model.request") == 1 and guard.run_spent == pytest.approx(1.5)
    assert json.loads((tmp_path / "spend.json").read_text())["total"] == pytest.approx(1.5)


def test_budget_guard_and_json_extraction(tmp_path: Path) -> None:
    guard = BudgetGuard(1.0, 2.0, tmp_path / "ledger.json")
    guard.record(0.6)
    guard.check()
    guard.record(0.6)
    with pytest.raises(BudgetExceededError, match="run budget"):
        guard.check()
    fresh = BudgetGuard(5.0, 1.0, tmp_path / "ledger.json")  # ledger already at 1.2
    with pytest.raises(BudgetExceededError, match="total budget"):
        fresh.check()
    assert extract_json('Sure! ```json\n{"a": [1]}\n``` done') == {"a": [1]}
    assert extract_json('prefix {"b": 2} suffix') == {"b": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_budget_guard_counts_the_calls_of_every_provider(tmp_path: Path) -> None:
    """Cost reported (OpenRouter), only token counts (other providers) or no usage at all."""
    usage = UsageMetadata(input_tokens=10_000, output_tokens=1_000, total_tokens=11_000)
    answers = [
        AIMessage("a", usage_metadata=usage, response_metadata={"token_usage": {"cost": 0.01}}),
        AIMessage("b", usage_metadata=usage, response_metadata={"usage": {"input_tokens": 10_000}}),
        AIMessage("c" * 4000),  # e.g. a local server that reports nothing
    ]
    config = AgentConfig(
        model="fake", model_provider="anthropic", ledger=tmp_path / "spend.json", budget_run=0.07
    )
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    events = io.StringIO()
    emitter = EventEmitter("agent", session_id="ses-agent", run_id="run-t1", stream=events)
    llm = ModelClient(GenericFakeChatModel(messages=iter(answers)), config, guard, emitter)
    content = [text_block("x" * 397), image_block({"base64": "AAAA"})]

    async def calls() -> None:
        for _ in answers:
            await llm.ask("read_labels", "sys", content)
        with pytest.raises(BudgetExceededError, match="run budget"):
            await llm.ask("read_labels", "sys", content)

    asyncio.run(calls())
    responses = [json.loads(line)["payload"] for line in events.getvalue().splitlines()]
    responses = [r for r in responses if "cost" in r]
    # 10k in at 3 + 1k out at 15 per million tokens; without usage 400 characters of text are
    # 100 tokens plus 1600 per image, 4000 answer characters 1000 tokens
    assert [r["cost"] for r in responses] == [0.01, 0.045, pytest.approx(0.0201)]
    assert [r["cost_reported"] for r in responses] == [True, False, False]
    assert [r["input_tokens"] for r in responses] == [10_000, 10_000, 1700]
    assert guard.calls == 3 and guard.run_spent == pytest.approx(0.0751)


def test_model_provider_reaches_init_chat_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import langchain.chat_models
    from langchain_openai import ChatOpenAI

    from mechatronikers.agent.config import OPENROUTER_URL

    calls: list[dict[str, Any]] = []

    def init_chat_model(model: str, **kwargs: Any) -> Any:
        calls.append({"model": model, **kwargs})
        return None

    def created(**env: str) -> dict[str, Any]:
        for name in (
            "MT_MODEL_PROVIDER",
            "MT_BASE_URL",
            "OPENROUTER_API_KEY",
            "MT_MODEL_TIMEOUT_S",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        create_model(AgentConfig.from_env())
        return calls.pop()

    # Default (OpenRouter's OpenAI-compatible API): the same call as before #59.
    model = create_model(AgentConfig(model="anthropic/claude-x", api_key="sk-test"))
    assert isinstance(model, ChatOpenAI) and model.openai_api_base == OPENROUTER_URL
    assert model.extra_body == {"usage": {"include": True}} and model.temperature == 0.0
    assert (model.request_timeout, model.max_retries) == (900.0, 2)  # a hung call ends (#145)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("MT_MODEL", "some/model")
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", init_chat_model)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        created()
    assert created(OPENROUTER_API_KEY="sk-test") == {
        "model": "some/model",
        "model_provider": "openai",
        "base_url": OPENROUTER_URL,
        "api_key": "sk-test",
        "temperature": 0.0,
        "timeout": 900.0,
        "max_retries": 2,
        "extra_body": {"usage": {"include": True}},
    }
    # Other providers: their own key variable and default endpoint; no OpenRouter key or usage.
    assert created(MT_MODEL_PROVIDER="Anthropic", OPENROUTER_API_KEY="sk-test") == {
        "model": "some/model",
        "model_provider": "anthropic",
        "temperature": 0.0,
        "timeout": 900.0,
        "max_retries": 2,
    }
    ollama = created(
        MT_MODEL_PROVIDER="ollama", MT_BASE_URL="http://127.0.0.1:11434", MT_MODEL_TIMEOUT_S="30"
    )
    assert ollama == {
        "model": "some/model",
        "model_provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "temperature": 0.0,
        "timeout": 30.0,
        "max_retries": 2,
    }
    other = created(MT_BASE_URL="http://127.0.0.1:8000/v1", OPENROUTER_API_KEY="sk-test")
    assert other["model_provider"] == "openai" and "api_key" not in other  # not OpenRouter


def test_missing_label_readings_are_requested_again(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = _script(ids, 0, fix=False)
    unreadable = [{"id": o, "text": "", "readable": False} for o in ids["others"].split(",")]
    first = _answer(
        {"labels": [{"id": ids["linear"], "text": "100", "readable": True}, *unreadable]}
    )
    retry = _answer({"labels": [{"id": ids["round"], "text": "Ø20", "readable": True}]})
    state, events, _ = asyncio.run(
        _run(url, [first, retry, *answers[1:]], tmp_path, auto_approve=True)
    )
    assert state["status"] == "done", state.get("error")
    assert "agent.labels_missing" in _types(events)
    assert {r["text"] for r in state["readings"].values() if r.get("readable")} == {"100", "Ø20"}


def test_assumptions_given_as_sentences_are_normalized() -> None:
    from mechatronikers.agent.graph import normalize_assumptions
    from mechatronikers.agent.pipeline import missing_dimensions

    items = normalize_assumptions(
        ["Tube height read as total height.", {"kind": "radius", "value": 1, "reason": "note"}, 3]
    )
    assert items == [
        {"id": "a-1", "kind": "note", "value": None, "reason": "Tube height read as total height."},
        {"id": "a-2", "kind": "radius", "value": 1, "reason": "note"},
    ]
    old_state = {"spec": {"assumptions": ["plain sentence"]}}
    assert [(m.id, m.reason) for m in missing_dimensions(old_state)] == [("a-1", "plain sentence")]


def test_printed_overall_size_mismatch_goes_to_the_fix_loop(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    linear = f"d-{ids['linear']}"
    answers = _script(ids, 0, fix=False)
    plan = {
        "part_name": "Plate",
        "overall_size_dims": [None, None, linear],  # wrong axis: the 100 is the length in x
        "features": [PLATE, _hole(0)],
        "assumptions": [],
    }
    answers[2] = _answer(plan)
    answers.append(
        _answer({"features": [PLATE, _hole(0)], "overall_size_dims": [linear, None, None]})
    )
    state, _, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert state["iteration"] == 1 and state["failure_history"] == [1, 0]
    assert state["spec"]["overall_size_dims"] == [linear, None, None]


def test_replan_adds_what_the_coverage_check_found(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = _script(ids, 0, fix=False)
    answers[2] = _answer({"part_name": "Plate", "features": [PLATE], "assumptions": []})
    answers.insert(3, _answer({"features": [PLATE, _hole(0)], "assumptions": []}))
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert [f["id"] for f in state["spec"]["features"]] == ["base", "bore"]
    assert state["coverage"]["review_items"] == []
    assert "agent.replanned" in _types(events) and _types(events).count("model.request") == 4
    found = _payloads(events, "review.item_created")  # findings, shown on their step
    unread = [("check", "unreadable")] * len(ids["others"].split(","))  # the model could not read
    assert [(f["step"], f["item"]["kind"]) for f in found] == unread + [
        ("plan", "possibly_missing")
    ] * 2


def test_replan_that_leaves_more_findings_is_discarded(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    shorter = {
        **PLATE,
        "profile": {"segments": [
            {"kind": "line", "start": [-45, -30], "end": [45, -30]},
            {"kind": "line", "start": [45, -30], "end": [45, 30]},
            {"kind": "line", "start": [45, 30], "end": [-45, 30]},
            {"kind": "line", "start": [-45, 30], "end": [-45, -30]},
        ]},
    }  # fmt: skip
    answers = _script(ids, 0, fix=False)
    answers[2] = _answer({"part_name": "Plate", "features": [PLATE], "assumptions": []})
    answers.insert(3, _answer({"features": [shorter], "assumptions": []}))  # 100 now unused too
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert "agent.replan_discarded" in _types(events)
    assert state["spec"]["features"][0]["profile"]["segments"][0]["end"] == [50, -30]
    items = state["coverage"]["review_items"]  # the findings stay visible for the reviewer
    assert [i["kind"] for i in items] == ["possibly_missing", "possibly_missing"]
    assert "Ø20" in items[0]["message"] and items[1]["dimension_ids"] == [f"d-{ids['round']}"]


def test_failed_base_body_goes_to_the_fix_loop(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = _script(ids, 0, fix=True)
    flat = {
        **PLATE,
        "profile": {
            "segments": [  # closed but zero-area: the base cannot build
                {"kind": "line", "start": [0, 0], "end": [100, 0]},
                {"kind": "line", "start": [100, 0], "end": [0, 0]},
            ]
        },
    }
    plan = {
        "part_name": "Plate",
        "overall_size_dims": [f"d-{ids['linear']}", None, None],
        "features": [flat, _hole(0)],
        "assumptions": [],
    }
    answers[2] = _answer(plan)
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert state["iteration"] == 1
    assert "agent.step_failed" not in _types(events)


def test_update_run_from_gate_2_builds_a_new_revision(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    linear = f"d-{ids['linear']}"
    point = {
        "item": {
            "id": "poi-1",
            "kind": "user_note",
            "severity": "warning",
            "message": "plate is 90 long",
            "reason": "marked by reviewer test",
            "dimension_ids": [linear],
        },
        "revision": 1,
        "origin": "reviewer",
        "created_at": utc_now(),
        "created_by": "test",
        "proposed_value": 90,
    }
    update = {"action": "update", "update": {"revision": 1, "items": [point], "note": "shorter"}}
    review = FakeReview("approve", update, "finish")
    shorter = {
        **PLATE,
        "profile": {"segments": [
            {"kind": "line", "start": [-45, -30], "end": [45, -30]},
            {"kind": "line", "start": [45, -30], "end": [45, 30]},
            {"kind": "line", "start": [45, 30], "end": [-45, 30]},
            {"kind": "line", "start": [-45, 30], "end": [-45, -30]},
        ]},
    }  # fmt: skip
    answers = _script(ids, 0, fix=False)
    answers.append(_answer({"features": [shorter, _hole(0)], "assumptions": []}))  # revise
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    assert state["revision"] == 2
    gate_2, second_gate_2 = review.requests[1], review.requests[2]
    assert gate_2.kind == "result_review" and gate_2.build_id and gate_2.sheet_size
    assert {d.id for d in gate_2.dimensions} == {linear, f"d-{ids['round']}"}
    assert [f.id for f in gate_2.plan] == ["base", "bore"]
    assert second_gate_2.revision == 2
    edited = next(d for d in state["consistency"]["dimensions"] if d["id"] == linear)
    assert (edited["value"], edited["method"], edited["stated"]) == (90, "human", 100)
    assert _payloads(events, "spec.dimension_changed")[0]["old_value"] == 100
    assert _payloads(events, "agent.revision_started")[0]["points"] == 1
    assert _payloads(events, "agent.run_finished")[-1]["revision"] == 2


def _cad_events(out: str) -> list[dict[str, Any]]:
    """cad-mcp's and its build runner's events, which the in-process sandbox writes to stdout."""
    events = []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("component") in ("cad-mcp", "build-runner"):
            events.append(event)
    return events


def _assert_one_build(
    cad: list[dict[str, Any]], gate_2: GateRequest, finished: dict[str, Any]
) -> tuple[str, str]:
    """The build gate 2 shows: validated last, the run result's, and the one of every file
    exported for it (#101). Returns the build id and its feature-list hash."""
    build = gate_2.build_id
    assert build is not None and finished["build_id"] == build
    validated = [p for p in _payloads(cad, "validation.completed") if p["build_id"] == build]
    assert validated and validated[-1]["passed"]
    digest = validated[-1]["features_sha256"]
    assert digest and finished["features_sha256"] == digest
    assert gate_2.validation is not None and gate_2.validation.build_id == build
    exported = [p for p in _payloads(cad, "artifact.created") if p["build_id"] == build]
    assert {p["format"] for p in exported} >= {"step-ap242", "step-ap214", "stl", "svg-iso"}
    for ref in exported:
        assert ref["uri"].split("/")[3] == build and ref["features_sha256"] == digest
    return build, digest


def test_one_build_ties_validation_gate_2_run_result_and_exports(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, ids = sandbox
    review = FakeReview("approve", "finish")
    state, events, _ = asyncio.run(_run(url, _script(ids, 0, fix=False), tmp_path, review))
    assert state["status"] == "done", state.get("error")
    cad = _cad_events(capsys.readouterr().out)
    build, _ = _assert_one_build(
        cad, review.requests[1], _payloads(events, "agent.run_finished")[-1]
    )
    assert {p["build_id"] for p in _payloads(cad, "artifact.created")} == {build}
    assert {a["build_id"] for a in state["artifacts"]} == {build}  # what the test bench copies
    assert _payloads(cad, "build.started")[-1]["build_id"] == build  # validate_part: no rebuild


def test_update_run_exports_the_build_its_gate_shows(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D70: each revision's gate 2, run result and exports name the same (new) build."""
    url, ids = sandbox
    update = {"action": "update", "update": {"revision": 1, "items": [], "note": "shorter"}}
    review = FakeReview("approve", update, "finish")
    answers = _script(ids, 0, fix=False)
    answers.append(_answer({"features": [PLATE, _hole(10)], "assumptions": []}))  # revise
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    assert state["revision"] == 2
    cad = _cad_events(capsys.readouterr().out)
    finished = _payloads(events, "agent.run_finished")
    paused = [f for f in finished if f["paused_at"] == "result_review"]
    assert [f["revision"] for f in paused] == [1, 2]
    first_gate_2, second_gate_2 = review.requests[1], review.requests[2]
    first = _assert_one_build(cad, first_gate_2, paused[0])
    second = _assert_one_build(cad, second_gate_2, paused[1])
    assert _assert_one_build(cad, second_gate_2, finished[-1]) == second
    assert first[0] != second[0] and first[1] != second[1]  # moved hole: other geometry
    exported = {p["build_id"] for p in _payloads(cad, "artifact.created")}
    assert exported == {first[0], second[0]}


def _agent_sequence(
    events: list[dict[str, Any]], start: str = "agent.step_started give_up"
) -> list[str]:
    """Agent events from `start` on; step events with their step, decisions with their action."""

    def label(e: dict[str, Any]) -> str:
        details = [str(e["payload"][k]) for k in ("step", "action") if k in e["payload"]]
        return " ".join([e["type"], *details])

    sequence = [label(e) for e in events if e["type"].startswith("agent.")]
    return sequence[sequence.index(start) :]


GIVE_UP = ["agent.step_started give_up", "agent.build_failed"]  # once, before the gate opens
AT_GATE = ["agent.interrupt", "agent.run_finished"]


def _gives_up(ids: dict[str, str]) -> list[AIMessage]:
    return _script(ids, 200, fix=False) + [
        _answer({"features": [PLATE, _hole(300)]}),
        _answer({"features": [PLATE, _hole(400)]}),  # no improvement twice: gives up
    ]


def test_give_up_then_retry_with_a_hint(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = [*_gives_up(ids), _answer({"features": [PLATE, _hole(0)]})]  # after the hint
    retry = {"action": "retry", "hint": "the bore is in the centre of the plate"}
    review = FakeReview("approve", retry, "finish")
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    gave_up = review.requests[1]
    assert gave_up.kind == "gave_up" and gave_up.failures
    assert [p["paused_at"] for p in _payloads(events, "agent.run_finished")][:3] == [
        "spec_review",
        "gave_up",
        "result_review",
    ]
    # The decision continues the waiting give_up step; nothing of it is emitted again (#66).
    assert _agent_sequence(events)[:7] == [
        *GIVE_UP,
        *AT_GATE,
        "agent.resumed retry",
        "agent.step_completed give_up",
        "agent.step_started fix",
    ]
    assert [e["type"] for e in events if e["level"] == "error"] == ["agent.build_failed"]


def test_give_up_then_abort(sandbox: tuple[str, dict[str, str]], tmp_path: Path) -> None:
    url, ids = sandbox
    review = FakeReview("approve", {"action": "abort", "reason": "drawing unclear"})
    state, events, _ = asyncio.run(_run(url, _gives_up(ids), tmp_path, review))
    assert state["status"] == "aborted" and stop_reason(state) == "plateau"
    assert _payloads(events, "agent.run_finished")[-1]["status"] == "aborted"
    assert _agent_sequence(events) == [
        *GIVE_UP,
        *AT_GATE,
        "agent.resumed abort",
        "agent.step_completed give_up",
        "agent.run_finished",
    ]


def test_run_waiting_at_the_gave_up_gate_resumes_after_a_restart(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    url, ids = sandbox
    abort = FakeReview({"action": "abort", "reason": "drawing unclear"})

    async def scenario() -> Any:
        db = str(tmp_path / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            with pytest.raises(KeyboardInterrupt):
                await _run(url, _gives_up(ids), tmp_path, FakeReview("approve", "stop"), saver)
        async with AsyncSqliteSaver.from_conn_string(db) as saver:  # a new agent process
            return await _run(url, [], tmp_path, abort, saver, resume=True)

    state, events, _ = asyncio.run(scenario())
    assert state["status"] == "aborted" and abort.requests[0].kind == "gave_up"
    # the new process opens the same gate again, but does not start or fail the step again
    assert [" ".join([e["type"], e["payload"].get("step", "")]).strip() for e in events] == [
        *AT_GATE,
        "agent.resumed",
        "agent.step_completed give_up",
        "agent.run_finished",
    ]


def test_review_gates_start_once_when_the_run_resumes(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    review = FakeReview("approve", "finish")
    state, events, _ = asyncio.run(_run(url, _script(ids, 0, fix=False), tmp_path, review))
    assert state["status"] == "done", state.get("error")
    # LangGraph reruns a gate node with the decision: its step starts once, when the gate opens
    assert _agent_sequence(events, "agent.step_started review_gate_1")[:6] == [
        "agent.step_started review_gate_1",
        *AT_GATE,
        "agent.resumed approve",
        "agent.step_completed review_gate_1",
        "agent.step_started build",
    ]
    assert _agent_sequence(events, "agent.step_started review_gate_2") == [
        "agent.step_started review_gate_2",
        *AT_GATE,
        "agent.resumed finish",
        "agent.step_completed review_gate_2",
        "agent.run_finished",
    ]


def test_automatic_gates_still_start_their_step(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """Guard: without an interrupt (test bench) each gate step still starts and completes."""
    url, ids = sandbox
    state, events, _ = asyncio.run(
        _run(url, _script(ids, 0, fix=False), tmp_path, auto_approve=True)
    )
    assert state["status"] == "done", state.get("error")
    assert _agent_sequence(events, "agent.step_started review_gate_1")[:3] == [
        "agent.step_started review_gate_1",
        "agent.step_completed review_gate_1",
        "agent.step_started build",
    ]
    assert _agent_sequence(events, "agent.step_started review_gate_2") == [
        "agent.step_started review_gate_2",
        "agent.step_completed review_gate_2",
        "agent.run_finished",
    ]


def test_reviewer_edits_become_human_values() -> None:
    from mechatronikers.agent.graph import apply_edits

    consistency = {
        "dimensions": [
            {"id": "d-1", "value": 100, "stated": 100, "method": "vlm", "status": "auto_ok"},
            {"id": "d-2", "value": 20, "stated": 20, "method": "vlm", "status": "needs_review"},
        ],
        "review_items": [{"id": "r-1", "dimension_ids": ["d-2"]}],
    }
    update = {
        "items": [{"item": {"id": "poi-1", "dimension_ids": ["d-1"]}, "proposed_value": 90}],
        "actions": [{"item_id": "rev1-r-1", "action": {"action": "edited", "new_value": 21}}],
    }
    result, changes = apply_edits(consistency, update, "Anna")
    assert [(c["dimension_id"], c["new_value"]) for c in changes] == [("d-1", 90), ("d-2", 21)]
    assert result["dimensions"][1]["method"] == "human"
    assert "Anna" in result["dimensions"][1]["reason"]
    assert consistency["dimensions"][0]["value"] == 100  # the input is not changed


PLANNING = {
    "plan_spec": prompts.PLAN_SPEC,
    "replan_spec": prompts.REPLAN_SPEC,
    "fix_spec": prompts.FIX_SPEC,
    "revise_spec": prompts.REVISE_SPEC,
}


def test_run_language_asks_for_german_reasons(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """--lang de: plan, replan, fix and revise ask for German reasons; the gates carry them."""
    url, ids = sandbox
    assumed = {"id": "a-1", "kind": "note", "value": None, "reason": "Plattendicke aus der Ansicht"}
    unresolved = {"id": "poi-1", "reason": "Die Zeichnung zeigt an dieser Stelle keine Fase."}
    point = {
        "item": {
            "id": "poi-1",
            "kind": "user_note",
            "message": "chamfer missing",
            "reason": "marked by reviewer test",
        },
        "revision": 1,
        "origin": "reviewer",
        "created_at": utc_now(),
        "created_by": "test",
    }
    update = {"action": "update", "update": {"revision": 1, "items": [point]}}
    review = FakeReview("approve", update, "finish")
    answers = _script(ids, 0, fix=False)
    answers[2] = _answer({"part_name": "Plate", "features": [PLATE], "assumptions": [assumed]})
    answers += [
        _answer({"features": [PLATE, _hole(0)], "assumptions": [assumed]}),  # replan
        _answer(  # revise: moves the bore off the plate, so the fix loop runs too
            {"features": [PLATE, _hole(200)], "assumptions": [assumed], "unresolved": [unresolved]}
        ),
        _answer({"features": [PLATE, _hole(0)], "assumptions": [assumed]}),  # fix
    ]
    systems: list[str] = []
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review, lang="de", systems=systems))
    assert state["status"] == "done", state.get("error")
    requests = _payloads(events, "model.request")
    assert [r["step"] for r in requests] == [
        "read_labels", "read_title", "plan_spec", "replan_spec", "revise_spec", "fix_spec"
    ]  # fmt: skip
    assert len(systems) == len(answers)  # the language costs no extra model call
    for request, system in zip(requests, systems, strict=True):
        assert request["prompt"] == prompt_hash(system)  # the logged hash covers the language
        if request["step"] in PLANNING:
            assert system == prompts.in_language(PLANNING[request["step"]], "de")
            assert 'write every "reason" (assumptions, unresolved points) in German' in system
        else:  # reading transcribes the drawing: no language
            assert system in (prompts.READ_LABELS, prompts.READ_TITLE)
    gate_1, _, revision_2 = review.requests
    assert [a.reason for a in gate_1.assumptions] == [assumed["reason"]]
    assert revision_2.revision == 2 and revision_2.assumptions[0].reason == assumed["reason"]
    still_open = [i for i in revision_2.review_items if i.message == "Not resolved: poi-1"]
    assert [i.reason for i in still_open] == [unresolved["reason"]]


def test_run_language_from_the_cli_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mechatronikers.agent.__main__ import build_parser, load_config, run_started

    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("MT_MODEL", "fake")
    monkeypatch.delenv("MT_LANG", raising=False)
    monkeypatch.delenv("MT_MODEL_PROVIDER", raising=False)
    parser = build_parser()
    run = ["run", "--session", "ses-1", "--drawing", "sheet.pdf"]
    assert load_config(parser.parse_args(run)).lang == "en"
    config = load_config(parser.parse_args([*run, "--lang", "de"]))
    assert run_started(config, "sheet.pdf", False) == {
        "kind": "part",
        "drawing": "sheet.pdf",
        "model": "fake",
        "model_provider": "openai",
        "max_fix_iterations": 3,
        "auto_approve": False,
        "lang": "de",
    }
    monkeypatch.setenv("MT_LANG", " DE ")
    assert load_config(parser.parse_args(run)).lang == "de"
    assert load_config(parser.parse_args([*run, "--lang", "en"])).lang == "en"  # the flag wins
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--drawing", "sheet.pdf", "--lang", "fr"])
    # A bad MT_LANG stops only a new run that would use it.
    monkeypatch.setenv("MT_LANG", "fr")
    with pytest.raises(ValueError, match="unknown run language 'fr'"):
        load_config(parser.parse_args(run))
    assert load_config(parser.parse_args([*run, "--lang", "de"])).lang == "de"
    assert load_config(parser.parse_args(["resume", "--session", "ses-1"])).model == "fake"
    assert AgentConfig.from_env().model == "fake"  # what `spend` reads


# --------------------------------------------------------------------------- unstated (#90, D93)


def _plan(ids: dict[str, str], features: list[dict[str, Any]], **extra: Any) -> AIMessage:
    return _answer(
        {
            "part_name": "Plate",
            "overall_size_dims": [f"d-{ids['linear']}", None, None],
            "features": features,
            "assumptions": [],
            **extra,
        }
    )


def _unstated_found(events: list[dict[str, Any]], kind: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f["step"], f["item"])
        for f in _payloads(events, "review.item_created")
        if f["item"]["kind"] == kind
    ]


@pytest.mark.parametrize("sandbox", [{"rib": True}], indirect=True)
def test_undimensioned_feature_becomes_a_review_item_on_its_view(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """A strip drawn in the view without any dimension is listed, not dropped (class 1)."""
    url, ids = sandbox
    rib = {
        "id": "u-1",
        "view_id": ids["view"],
        "bbox": [63, 86, 157, 93],
        "feature_id": None,
        "reason": "strip across the plate; no width, height or position printed",
    }
    answers = _script(ids, 0, fix=False)
    answers[2] = _plan(ids, [PLATE, _hole(0)], undimensioned=[rib, "something on the back"])
    review = FakeReview("approve", "finish")
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    for gate in review.requests:  # gate 1 and gate 2 show it as a finding on the drawing
        items = [i for i in gate.review_items if i.kind == "undimensioned_feature"]
        located = [(i.id, i.sheet_ref.view_id if i.sheet_ref else None) for i in items]
        assert located == [("undim-1", ids["view"]), ("undim-2", None)]
        assert items[0].sheet_ref and items[0].sheet_ref.bbox == (63, 86, 157, 93)
        assert items[0].reason == rib["reason"] and items[0].message.endswith("not modelled")
    found = _unstated_found(events, "undimensioned_feature")
    assert [(step, item["id"]) for step, item in found] == [
        ("plan", "undim-1"),
        ("plan", "undim-2"),
    ]


def test_symmetric_extrusion_is_an_assumption_at_gate_1(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """The plan extrudes about a mid-plane the drawing does not define (class 2)."""
    url, ids = sandbox
    plate = {**PLATE, "direction": "symmetric"}
    hole = {**_hole(0), "placement": {"origin": [0, 0, 5], "normal": [0, 0, -1]}}
    answers = _script(ids, 0, fix=False)
    answers[2] = _plan(ids, [plate, hole])
    review = FakeReview("reject")
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "rejected"
    (assumption,) = review.requests[0].assumptions
    assert (assumption.kind, assumption.value, assumption.feature_ids) == (
        "symmetry",
        "±5",
        ["base"],
    )
    assert "mid-plane" in assumption.reason
    found = _unstated_found(events, "assumption")
    assert [(step, item["feature_ids"]) for step, item in found] == [("plan", ["base"])]


@pytest.mark.parametrize("sandbox", [{"corner_radius": 2.0}], indirect=True)
def test_unprinted_radius_is_built_as_assumed_and_listed(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """Rounded corners without a printed radius: modelled from the drawing, built with a
    method="assumed" dimension, listed at gate 1 (class 3)."""
    url, ids = sandbox
    corners = {
        "type": "fillet",
        "id": "corners",
        "radius": 2,
        "edges": {"by": "near_points", "points": [[x, y, 5] for x in (-50, 50) for y in (-30, 30)]},
    }
    seen = {
        "id": "u-1",
        "view_id": ids["view"],
        "bbox": [56, 38, 64, 44],  # partly outside the view: clipped
        "feature_id": "corners",
        "reason": "corners drawn rounded, no radius printed; R2 measured on the drawing",
    }
    answers = _script(ids, 0, fix=False)
    answers[2] = _plan(ids, [PLATE, _hole(0), corners], undimensioned=[seen])
    review = FakeReview("approve", "finish")
    state, _, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    gate_1 = review.requests[0]
    (assumption,) = gate_1.assumptions
    assert (assumption.kind, assumption.value) == ("radius", 2)
    assert (assumption.feature_ids, assumption.dimension_ids) == (
        ["corners"],
        ["d-assumed-corners"],
    )
    (item,) = [i for i in gate_1.review_items if i.kind == "undimensioned_feature"]
    assert (
        item.feature_ids == ["corners"] and "built as corners with an assumed size" in item.message
    )
    assert item.sheet_ref and item.sheet_ref.bbox == pytest.approx((58, 40, 64, 44), abs=1e-3)
    # the exported part spec records the radius as assumed, linked to the fillet
    (exported,) = tmp_path.glob("artifacts/*/Plate.features.json")
    part = json.loads(exported.read_text())
    (dim,) = [d for d in part["dimensions"] if d["method"] == "assumed"]
    assert (dim["id"], dim["value"], dim["stated"], dim["status"]) == (
        "d-assumed-corners",
        2,
        None,
        "needs_review",
    )
    fillet = next(f for f in part["features"] if f["id"] == "corners")
    assert fillet["dimension_ids"] == ["d-assumed-corners"]


def _calls(events: list[dict[str, Any]]) -> list[str]:
    return [p["step"] for p in _payloads(events, "model.request")]


def test_null_for_a_field_with_a_default_is_accepted(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """GPT-style answers write null for a value they do not have (#147)."""
    url, ids = sandbox
    answers = _script(ids, 0, fix=False)
    answers[2] = _plan(ids, [PLATE, {**_hole(0), "cs_angle": None, "cb_depth": None}])
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert state["spec"]["features"][1]["cs_angle"] == 90.0  # the spec default
    assert _calls(events) == ["read_labels", "read_title", "plan_spec"]
    assert "agent.answer_repaired" not in _types(events)
    # the replaced null is traced; cb_depth takes null, so it stays
    (normalized,) = _payloads(events, "agent.answer_normalized")
    assert normalized == {
        "step": "plan",
        "call": "plan_spec",
        "count": 1,
        "paths": ["features.1.cs_angle"],
    }


def test_invalid_plan_answer_is_repaired_with_one_more_call(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    answers = _script(ids, 0, fix=False)
    answers.insert(2, _plan(ids, [PLATE, {**_hole(0), "diameter": None}]))  # required: no default
    contents: list[Any] = []
    state, events, _ = asyncio.run(
        _run(url, answers, tmp_path, auto_approve=True, contents=contents)
    )
    assert state["status"] == "done", state.get("error")
    assert _calls(events) == ["read_labels", "read_title", "plan_spec", "plan_spec_repair"]
    (repaired,) = _payloads(events, "agent.answer_repaired")
    assert (repaired["step"], repaired["call"], repaired["errors"]) == ("plan", "plan_spec", 1)
    assert repaired["messages"] == [
        "features.1.hole.diameter: Input should be a valid number (input: null)"
    ]
    # the same task once more (text and sheet image), with the error list at the end
    assert contents[3][:-1] == contents[2]
    assert "features.1.hole.diameter" in contents[3][-1]["text"]
    assert "agent.step_failed" not in _types(events)


def test_unterminated_fix_answer_is_repaired_with_one_more_call(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """Gemini in the fix step: "Unterminated string ..." (#147)."""
    url, ids = sandbox
    answers = _script(ids, 200, fix=True)
    cut = AIMessage(content='{"features": [{"type": "extrude_base", "id": "ba')
    answers.insert(3, cut)
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "done", state.get("error")
    assert state["iteration"] == 1
    assert _calls(events)[-2:] == ["fix_spec", "fix_spec_repair"]
    (repaired,) = _payloads(events, "agent.answer_repaired")
    assert (repaired["step"], repaired["errors"]) == ("fix", 1)
    assert repaired["messages"][0].startswith("Unterminated string")


def test_two_invalid_answers_fail_the_step_as_before(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    invalid = _plan(ids, [PLATE, {**_hole(0), "diameter": None}])  # null in a required field
    answers = [*_script(ids, 0, fix=False)[:2], invalid, invalid]
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, auto_approve=True))
    assert state["status"] == "error" and stop_reason(state) == "error"
    assert state["error"].startswith("plan: 1 validation error") and "diameter" in state["error"]
    (failed,) = _payloads(events, "agent.step_failed")
    assert failed["step"] == "plan"
    assert _calls(events) == ["read_labels", "read_title", "plan_spec", "plan_spec_repair"]
    assert "agent.answer_repaired" not in _types(events)


class HangsAfterReading(RecordingModel):
    """Answers the two read calls, then never answers (a hung call, #145)."""

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> BaseMessage:
        if len(self.systems) >= 2:
            self.systems.append(input[0].content)
            await asyncio.sleep(3600)
        return await super().ainvoke(input, config, **kwargs)


def test_a_model_timeout_is_not_repaired(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, ids = sandbox
    fast = {"model_timeout_s": 0.05, "model_max_retries": 0}
    state, events, _ = asyncio.run(
        _run(
            url,
            _script(ids, 0, fix=False),
            tmp_path,
            auto_approve=True,
            config=fast,
            model_type=HangsAfterReading,
        )
    )
    assert state["status"] == "error" and state["error"].startswith("plan: ")
    assert _calls(events) == ["read_labels", "read_title", "plan_spec"]  # no plan_spec_repair
    assert "model.error" in _types(events) and "agent.answer_repaired" not in _types(events)
    (failed,) = _payloads(events, "agent.step_failed")
    assert failed["step"] == "plan"


def test_unresolved_null_in_an_update_run_means_none(
    sandbox: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    """GPT-style revise answers write "unresolved": null; the update run must not crash."""
    url, ids = sandbox
    update = {"action": "update", "update": {"revision": 1, "note": "check the bore"}}
    review = FakeReview("approve", update, "finish")
    answers = _script(ids, 0, fix=False)
    answers.append(_answer({"features": [PLATE, _hole(0)], "assumptions": [], "unresolved": None}))
    state, events, _ = asyncio.run(_run(url, answers, tmp_path, review))
    assert state["status"] == "done", state.get("error")
    assert state["revision"] == 2 and "agent.step_failed" not in _types(events)
    revise = [p for p in _payloads(events, "review.item_created") if p["step"] == "revise"]
    assert not any(p["item"]["id"].startswith("open-") for p in revise)
