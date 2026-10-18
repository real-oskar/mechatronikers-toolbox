"""Drive one agent run through its review gates (#20).

At an `interrupt()` the run opens a gate through its `GateClient` (the sandbox review API in
mode 1, the hub's in mode 2), waits for the reviewer's decision and resumes the graph with it.
With a persistent checkpointer (thread id = run id) a run that was stopped while waiting continues
from the checkpoint (`agent resume`).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt
from pydantic import BaseModel

from mechatronikers.agent.graph import AgentState
from mechatronikers.review_app.client import POLL_S, GateClient, gateway_review_url
from mechatronikers.spec import ReviewItem, ValidationReport
from mechatronikers.spec.review import Assumption, GateDecision, GateRequest
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now


class ReviewClient(Protocol):
    async def decide(self, request: GateRequest) -> GateDecision: ...


class HttpReviewClient(GateClient):
    """A `GateClient` on a session gateway (mode 1): `base_url` is the gateway, not the API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        poll_s: float = POLL_S,
        transport: httpx.AsyncBaseTransport | None = None,
        emitter: EventEmitter | None = None,
    ) -> None:
        super().__init__(
            gateway_review_url(base_url),
            token,
            poll_s=poll_s,
            transport=transport,
            emitter=emitter,
        )


@dataclass
class RunHooks:
    """Session side effects while a gate waits (the session goes to awaiting_review)."""

    awaiting: Callable[[dict[str, Any]], None] = field(default=lambda _: None)
    resumed: Callable[[dict[str, Any]], None] = field(default=lambda _: None)


def gate_request(
    payload: dict[str, Any], interrupt_id: str, run_id: str, max_iterations: int | None
) -> GateRequest:
    validation = payload.get("validation")
    return GateRequest(
        id=f"gate-{interrupt_id[:24]}",
        kind=payload.get("kind", "spec_review"),
        run_id=run_id,
        run_kind="part",
        revision=payload.get("revision", 1),
        created_at=utc_now(),
        part_name=payload.get("part_name"),
        features=payload.get("features", []),
        assumptions=[Assumption.model_validate(a) for a in payload.get("assumptions", [])],
        overall_size_dims=payload.get("overall_size_dims"),
        review_items=[ReviewItem.model_validate(i) for i in payload.get("review_items", [])],
        possibly_missing=[
            ReviewItem.model_validate(i) for i in payload.get("possibly_missing", [])
        ],
        build_id=payload.get("build_id"),
        validation=None if validation is None else ValidationReport.model_validate(validation),
        failures=payload.get("failures", []),
        iteration=payload.get("iteration", 0),
        max_iterations=payload.get("max_iterations", max_iterations),
        plan=payload.get("plan", []),
        dimensions=payload.get("dimensions", []),
        sheet_size=payload.get("sheet_size"),
        drawing=payload.get("drawing"),
    )


class RunOutcome(BaseModel):
    """What `agent.run_finished` reports: enough for the run table and the validation gate."""

    status: str
    part_name: str | None = None
    paused_at: str | None = None  # gate kind while waiting for a reviewer
    gate_id: str | None = None
    revision: int = 1
    build_id: str | None = None  # the validated build, which the exported files come from (#101)
    features_sha256: str | None = None  # its feature-list hash
    iteration: int = 0
    checks_passed: int | None = None
    checks_total: int | None = None
    error: str | None = None


def outcome(state: dict[str, Any], gate: GateRequest | None = None) -> RunOutcome:
    validation = state.get("validation") or {}
    report = validation.get("report") or {}
    checks = report.get("checks")
    return RunOutcome(
        status="awaiting_review" if gate else state.get("status", "running"),
        part_name=(state.get("spec") or {}).get("part_name")
        or (state.get("title") or {}).get("part_name"),
        paused_at=gate.kind if gate else None,
        gate_id=gate.id if gate else None,
        revision=state.get("revision", 1),
        build_id=validation.get("build_id"),
        features_sha256=validation.get("features_sha256"),
        iteration=state.get("iteration", 0),
        checks_passed=None if checks is None else sum(bool(c["passed"]) for c in checks),
        checks_total=None if checks is None else len(checks),
        error=state.get("error"),
    )


async def drive(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    run_id: str,
    emitter: EventEmitter,
    review: ReviewClient | None,
    *,
    start: AgentState | None = None,
    hooks: RunHooks | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Run (or, with `start=None`, resume) a graph until it ends."""
    hooks = hooks or RunHooks()
    config: RunnableConfig = {"configurable": {"thread_id": run_id}}
    state: dict[str, Any]
    if start is not None:
        state = await graph.ainvoke(start, config)
    else:
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise ValueError(f"no checkpoint for run {run_id}")
        if snapshot.interrupts:
            state = {**snapshot.values, "__interrupt__": list(snapshot.interrupts)}
        elif snapshot.next:  # stopped between steps: continue from the last checkpoint
            emitter.emit("agent.resumed", {"from": "checkpoint", "next": list(snapshot.next)})
            state = await graph.ainvoke(None, config)
        else:
            state = dict(snapshot.values)
    while "__interrupt__" in state:
        pending: Interrupt = state["__interrupt__"][0]
        if review is None:
            raise RuntimeError("the run reached a review gate but has no review client")
        request = gate_request(pending.value, pending.id, run_id, max_iterations)
        info = {"gate_id": request.id, "kind": request.kind, "revision": request.revision}
        emitter.emit("agent.interrupt", info)
        emitter.emit("agent.run_finished", outcome(state, request).model_dump(mode="json"))
        hooks.awaiting(info)
        decision = await review.decide(request)
        hooks.resumed({**info, "action": decision.action})
        emitter.emit(
            "agent.resumed", {**info, "action": decision.action, "reviewer": decision.reviewer}
        )
        state = await graph.ainvoke(Command(resume=decision.model_dump(mode="json")), config)
    emitter.emit("agent.run_finished", outcome(state).model_dump(mode="json"))
    return state


# --------------------------------------------------------------------------- run registry


class RunRecord(BaseModel):
    run_id: str
    session_id: str
    drawing: str
    started_at: str


class RunRegistry:
    """Which runs belong to which session (`var/agent-runs.json`), for `agent resume`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> list[RunRecord]:
        if not self.path.is_file():
            return []
        return [RunRecord.model_validate(r) for r in json.loads(self.path.read_text())]

    def add(self, record: RunRecord) -> None:
        records = [*self._load(), record]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([r.model_dump() for r in records], indent=2))

    def latest(self, session_id: str) -> RunRecord | None:
        runs = [r for r in self._load() if r.session_id == session_id]
        return max(runs, key=lambda r: r.started_at) if runs else None
