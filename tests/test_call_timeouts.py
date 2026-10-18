"""Hung model and tool calls end as errors instead of blocking a run (#145)."""

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

from mechatronikers.agent import pipeline as agent_pipeline
from mechatronikers.agent.budget import BudgetGuard, LedgerIssue
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.graph import AgentState, build_graph
from mechatronikers.agent.llm import (
    TIMEOUT_OUTPUT_TOKENS,
    ModelClient,
    ModelTimeoutError,
    create_model,
    text_block,
)
from mechatronikers.agent.tools import SandboxTools, ToolError
from mechatronikers.session.connections import mcp_connections
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.run import case_dir, run
from tests.test_agent import sandbox  # noqa: F401  (the sandbox app fixture)

FAST = {"model_timeout_s": 0.05, "model_max_retries": 1}


class SilentModel(GenericFakeChatModel):
    """A model whose request never gets an answer (the 55-minute hang of #145)."""

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> BaseMessage:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _client(tmp_path: Path, events: io.StringIO) -> ModelClient:
    config = AgentConfig(model="fake", ledger=tmp_path / "spend.json", **FAST)
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    emitter = EventEmitter("agent", stream=events)
    return ModelClient(SilentModel(messages=iter([])), config, guard, emitter)


def test_a_model_that_never_answers_ends_as_a_model_error(tmp_path: Path) -> None:
    events = io.StringIO()
    llm = _client(tmp_path, events)
    recovered = LedgerIssue("budget.ledger_recovered", "warning", {"ledger": "spend.json"})
    issues = [[], [recovered]]  # the ledger is restored while the timeout is recorded
    llm.budget.take_issues = lambda: issues.pop(0)  # type: ignore[method-assign]
    started = time.monotonic()
    with pytest.raises(
        ModelTimeoutError, match=r"read_labels: no answer from fake \(TimeoutError: no answer after"
    ):
        asyncio.run(llm.ask("read_labels", "sys" * 400, [text_block("x" * 4000)]))
    assert time.monotonic() - started < 5
    request, ledger, error = _events(events)
    assert request["type"] == "model.request" and ledger["type"] == "budget.ledger_recovered"
    assert (error["type"], error["level"]) == ("model.error", "error")
    payload = error["payload"]
    assert payload["timeout"] is True and payload["attempts"] == 2
    # Each attempt may be billed in full: its estimated input plus the output ceiling.
    assert payload["output_tokens"] == TIMEOUT_OUTPUT_TOKENS and payload["input_tokens"] == 1300
    config = llm.config
    per_attempt = 1300 * config.price_input + TIMEOUT_OUTPUT_TOKENS * config.price_output
    assert payload["cost"] == pytest.approx(2 * per_attempt / 1e6, abs=1e-6)
    assert payload["cost_reported"] is False
    assert llm.budget.run_spent == pytest.approx(payload["cost"], abs=1e-6)
    assert json.loads((tmp_path / "spend.json").read_text())["total"] > 0


def test_the_outer_bound_allows_every_attempt_and_its_back_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AgentConfig(model="fake", ledger=tmp_path / "spend.json")
    assert (config.model_timeout_s, config.model_max_retries) == (900.0, 2)
    llm = ModelClient(SilentModel(messages=iter([])), config, None, None)  # type: ignore[arg-type]
    assert llm.deadline_s >= 3 * 900 + 16  # never cuts an attempt the client still allows
    # a configured output cap goes to the model (and is what a timed-out attempt is charged)
    import langchain.chat_models

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda m, **k: calls.append(k))
    create_model(config.model_copy(update={"api_key": "sk-test", "max_output_tokens": 8000}))
    create_model(config.model_copy(update={"api_key": "sk-test"}))
    assert calls[0]["max_tokens"] == 8000 and "max_tokens" not in calls[1]
    assert (calls[1]["timeout"], calls[1]["max_retries"]) == (900.0, 2)


def test_the_run_takes_the_error_path_when_the_model_hangs(
    sandbox: tuple[str, dict[str, str]],  # noqa: F811
    tmp_path: Path,
) -> None:
    url, _ = sandbox
    events = io.StringIO()

    async def scenario() -> dict[str, Any]:
        tools = SandboxTools(mcp_connections(url, "unused"))
        await tools.start()
        graph = build_graph(tools, _client(tmp_path, events), EventEmitter("agent", stream=events))
        thread: Any = {"configurable": {"thread_id": "run-hang"}}
        return dict(await graph.ainvoke(AgentState(drawing="sheet.pdf"), thread))

    state = asyncio.run(asyncio.wait_for(scenario(), 120))
    assert state["status"] == "error" and "no answer from fake" in state["error"]
    types = [e["type"] for e in _events(events)]
    assert "model.error" in types and "agent.step_failed" in types


class SilentTool(BaseTool):
    name: str = "slow"
    description: str = "never answers"

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)


def test_an_mcp_tool_call_that_never_answers_times_out() -> None:
    tools = SandboxTools({}, timeout_s=0.05)
    tools.tools = {"slow": SilentTool()}
    with pytest.raises(ToolError, match=r"slow: no answer in 0.05 s"):
        asyncio.run(tools.json("slow", {}))

    async def never() -> list[BaseTool]:
        await asyncio.sleep(3600)
        return []

    tools.client.get_tools = never  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="did not list their tools"):
        asyncio.run(tools.start())


class FakeManager:
    """The session manager as the agent pipeline uses it, without Podman."""

    def __init__(self) -> None:
        self.shelved: list[str] = []
        self.collector = SimpleNamespace(store=SimpleNamespace(events=lambda *a, **k: []))

    def start(self, drawing: Path) -> Any:
        return SimpleNamespace(session_id=f"ses-{drawing.stem}", mcp={})

    def collect(self, session_id: str) -> int:
        return 0

    def shelve(self, session_id: str, *, confirmed: bool, reason: str) -> None:
        assert confirmed
        self.shelved.append(session_id)


def test_a_case_over_its_limit_is_recorded_its_session_shelved_and_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()

    class FakeTools:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def start(self) -> None: ...

    class FakeGraph:
        async def ainvoke(self, start: AgentState, config: Any) -> dict[str, Any]:
            if start["drawing"] == "hang.pdf":
                await asyncio.sleep(3600)  # e.g. a stalled connection the client never notices
            return {"status": "build_failed", "error": "plateau"}

    monkeypatch.chdir(tmp_path)  # no .env, a fresh ledger
    monkeypatch.setenv("MT_MODEL", "fake")
    monkeypatch.delenv("MT_KEEP_SANDBOXES", raising=False)
    monkeypatch.setattr(agent_pipeline, "build_manager", lambda *a: manager)
    monkeypatch.setattr(agent_pipeline, "SandboxTools", FakeTools)
    monkeypatch.setattr(agent_pipeline, "create_model", lambda config: None)
    monkeypatch.setattr(agent_pipeline, "build_graph", lambda *a, **k: FakeGraph())
    cases = [
        TestCase(case_id=name, drawing=tmp_path / f"{name}.pdf", reference_step=tmp_path / "r")
        for name in ("hang", "next")
    ]
    started = time.monotonic()
    _, (hung, following) = run(
        cases,
        agent_pipeline.run_case,
        pipeline_name="agent",
        test_set="unit",
        models=["default"],
        variants=["vector"],
        out_dir=tmp_path / "run",
        case_timeout_s=0.5,
    )
    assert time.monotonic() - started < 30
    assert hung.status == "pipeline_error" and hung.error == "timeout: case ran longer than 0.5 s"
    assert following.error is None
    assert following.status == "ok" and following.meta["status"] == "build_failed"
    # the hung case's sandbox is not left running, and its files are written
    assert manager.shelved == ["ses-hang", "ses-next"]
    hung_dir = case_dir(tmp_path / "run", "hang", "default", "vector", 1)
    assert (hung_dir / "agent_state.json").read_text() == "{}"


def test_only_the_case_limit_is_recorded_as_a_timeout(tmp_path: Path) -> None:
    def unhealthy(case: TestCase, *, model: str, variant: str, out_dir: Path) -> Any:
        raise TimeoutError  # e.g. a sandbox that did not get healthy: not the case limit

    case = TestCase(case_id="x", drawing=tmp_path / "x.pdf", reference_step=tmp_path / "r")
    _, (row,) = run(
        [case],
        unhealthy,
        pipeline_name="unhealthy",
        test_set="unit",
        models=["m"],
        variants=["vector"],
        out_dir=tmp_path / "run",
        case_timeout_s=60,
    )
    assert row.status == "pipeline_error" and row.error == "TimeoutError: no message"
