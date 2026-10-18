"""Test bench pipeline (#19): one test case through a fresh sandbox and the agent.

Starting a test bench run is the user's OK for its sandboxes: each is shelved (snapshot, then
stopped) after its case unless `MT_KEEP_SANDBOXES=1`. Review gate 1 is approved automatically and
recorded as automatic (docs/09).
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from mechatronikers.agent.budget import BudgetGuard
from mechatronikers.agent.config import AgentConfig, run_language
from mechatronikers.agent.graph import (
    AgentState,
    build_graph,
    failed_features,
    failing_checks,
    stop_reason,
)
from mechatronikers.agent.llm import ModelClient, create_model
from mechatronikers.agent.tools import SandboxTools
from mechatronikers.agent.unstated import assumable, undimensioned_items
from mechatronikers.session.__main__ import build_manager
from mechatronikers.session.manager import configured_image
from mechatronikers.spec import ValidationReport
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.pipelines import CaseTimeoutError, MissingDimension, PipelineOutcome


def build_id_of(artifacts: list[dict[str, Any]]) -> str | None:
    """Build id from artifact references (artifact://<session>/<build>/<file>)."""
    for ref in artifacts:
        uri = str(ref.get("uri", ""))
        parts = uri.removeprefix("artifact://").split("/")
        if len(parts) >= 3 and parts[1].startswith("build-"):
            return parts[1]
    return None


def missing_dimensions(state: dict[str, Any]) -> list[MissingDimension]:
    """Unprinted values: measured gap dimensions, the plan's assumptions (assumed radii, chamfers
    and symmetries included) and the features seen without dimensions (metric M3, D93)."""
    out = [
        MissingDimension(id=d["id"], reason=d.get("reason"))
        for d in state.get("consistency", {}).get("dimensions", [])
        if d.get("stated") is None
    ]
    spec = state.get("spec", {})
    for i, a in enumerate(spec.get("assumptions", []), 1):
        if isinstance(a, dict):
            out.append(MissingDimension(id=str(a.get("id", f"a-{i}")), reason=a.get("reason")))
        elif isinstance(a, str):  # older runs stored plain sentences
            out.append(MissingDimension(id=f"a-{i}", reason=a))
    features = assumable(spec.get("features", []))
    out += [
        MissingDimension(id=item.id, reason=item.reason)
        for item in undimensioned_items(spec.get("undimensioned", []), [], features)
    ]
    return out


def blocking_review_items(state: dict[str, Any]) -> list[str]:
    """Ids of review items still open with severity "blocking" when the run ended: from the
    drawing check, the coverage check and unresolved build failures (D91: solved needs none)."""
    items = [
        *state.get("consistency", {}).get("review_items", []),
        *state.get("coverage", {}).get("review_items", []),
    ]
    ids = [
        str(i.get("id"))
        for i in items
        if isinstance(i, dict)
        and i.get("severity") == "blocking"
        and i.get("status", "open") == "open"
    ]
    return ids + [f"fail-{n}" for n, _ in enumerate(state.get("failures", []), 1)]


async def _run(case: TestCase, model: str, out_dir: Path, var_dir: Path) -> PipelineOutcome:
    config = AgentConfig.from_env().model_copy(update={"lang": run_language()})
    if model and model != "default":
        config = config.model_copy(update={"model": model})
    manager = build_manager(var_dir, configured_image())
    info = manager.start(case.drawing)
    session_id = info.session_id
    events = (out_dir / "agent-events.ndjson").open("w")
    emitter = EventEmitter(
        "agent", session_id=session_id, run_id=f"run-{uuid.uuid4().hex[:8]}", stream=events
    )
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    state: dict[str, Any] = {}
    try:
        tools = SandboxTools(info.mcp, timeout_s=config.tool_timeout_s)
        await tools.start()
        llm = ModelClient(create_model(config), config, guard, emitter)
        graph = build_graph(
            tools, llm, emitter, max_fix_iterations=config.max_fix_iterations, auto_approve=True
        )
        thread: RunnableConfig = {"configurable": {"thread_id": emitter.run_id}}
        start = AgentState(drawing=case.drawing.name, lang=config.lang)
        state = dict(await graph.ainvoke(start, thread))
        build_id = build_id_of(state.get("artifacts", []))
        if build_id:
            manager.copy_artifacts(session_id, build_id, out_dir / "artifacts")
    finally:
        events.close()
        (out_dir / "agent_state.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False, default=str)
        )
        manager.collect(session_id)
        with (out_dir / "session-events.ndjson").open("w") as f:
            for _, event in manager.collector.store.events(session_id, limit=100_000):
                f.write(event.to_json() + "\n")
        if os.environ.get("MT_KEEP_SANDBOXES") != "1":
            manager.shelve(session_id, confirmed=True, reason="testbench_run")
    steps = (
        sorted((out_dir / "artifacts").glob("*.ap242.step"))
        if (out_dir / "artifacts").is_dir()
        else []
    )
    report = state.get("validation", {}).get("report")
    return PipelineOutcome(
        step_path=steps[0] if steps and state.get("status") == "done" else None,
        report=None if report is None else ValidationReport.model_validate(report),
        missing_dimensions=missing_dimensions(state) if state else None,
        session_id=session_id,
        blocking_review_items=blocking_review_items(state) if state else None,
        meta={
            "model": config.model,
            "model_provider": config.model_provider,
            "lang": config.lang,  # MT_LANG: the language of the planning prompts' reasons
            "status": state.get("status"),
            "error": state.get("error"),
            "cost": round(guard.run_spent, 6),
            "model_calls": guard.calls,
            "fix_iterations": state.get("iteration", 0),
            # why the case ended without a model, for the summary (#92)
            "stop_reason": stop_reason(state),
            # Only for a give-up: after an error or the budget, the stored validation and
            # failures can be from an earlier round than the last build.
            **(
                {"failing_checks": failing_checks(state), "failed_features": failed_features(state)}
                if stop_reason(state) in ("cap", "plateau")
                else {"failing_checks": [], "failed_features": []}
            ),
        },
    )


_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    """One event loop for all cases of a run: model clients cache async HTTP connections bound to
    the loop they were created in, so a fresh asyncio.run() per case fails with 'Event loop is
    closed' from the second case on."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def run_case(
    case: TestCase, *, model: str, variant: str, out_dir: Path, timeout_s: float | None = None
) -> PipelineOutcome:
    """Pipeline entry point for `python -m mechatronikers.testbench run` (default pipeline).

    `timeout_s` (`--case-timeout`, #145): the case is cancelled when it runs longer; `_run`
    still writes its files and shelves the session on the way out."""
    if variant != "vector":
        raise ValueError(f"variant {variant!r} not supported yet (raster path: #22)")
    var_dir = Path(os.environ.get("MT_VAR_DIR", "var"))

    async def bounded() -> PipelineOutcome:
        limit = asyncio.timeout(timeout_s)
        try:
            async with limit:
                return await _run(case, model, out_dir, var_dir)
        except TimeoutError as exc:
            if not limit.expired():  # e.g. the sandbox not healthy in time: not the case limit
                raise
            raise CaseTimeoutError(f"case ran longer than {timeout_s:g} s") from exc

    return _loop().run_until_complete(bounded())
