"""Run table rows: one `RunSummary` per session, from the session store and its events (#20).

Everything is derived from events (docs/08 L7), so the table needs no extra state. The same phase
rules are mirrored in the frontend (`frontend/src/lib/status.ts`) for the run view. They read only
the repository interfaces, so the hub's overview (docs/13) can apply them to its own records.
"""

import math
from collections.abc import Callable
from typing import Any

from mechatronikers.session.store import RETENTION_DAYS, SessionRecord
from mechatronikers.spec.review import Phase, RunKind, RunSummary, StopReason
from mechatronikers.storage.repositories import EventRepository, RunRepository
from mechatronikers.telemetry import Event

_ANALYSING = ("analyse", "read", "check", "plan", "replan")
_ENDED: dict[str, Phase] = {
    "done": "valid",  # a part becomes "released" only after "Finish inspection" (below)
    "build_failed": "gave_up",
    "rejected": "failed",
    "aborted": "aborted",  # the reviewer ended the run at a give-up (D70)
    "error": "failed",
    "budget_exceeded": "failed",
}
_PAUSED: dict[str, Phase] = {
    "spec_review": "waiting_gate_1",
    "result_review": "valid",
    "gave_up": "gave_up",
}
# A circuit run's own steps (circuit_mcp/run.py, #110): analyse, ERC, gate 1, netlists, ngspice.
_CIRCUIT_STEPS: dict[str, Phase] = {
    "analyse": "analysing",
    "check": "checking",
    "review_gate_1": "waiting_gate_1",
    "export": "exporting",
    "simulate": "simulating",
}
# a reviewer's decision on one point; `resolved`: a gate 1 correction fixed what it named (#129)
_CLOSED_ITEM = ("review.approved", "review.rejected", "review.resolved")
PAGE = 1000


def all_events(store: EventRepository, session_id: str) -> list[Event]:
    events: list[Event] = []
    after = 0
    while True:
        rows = store.events(session_id, after, PAGE)
        events += [e for _, e in rows]
        if len(rows) < PAGE:
            return events
        after = rows[-1][0]


# Payloads come from the session's own sandbox too (docs/08, Provenance): read every field
# defensively, so one odd value degrades that field of that row, never the whole table (#78).


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _cost(value: Any) -> float:
    try:
        cost = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return cost if math.isfinite(cost) else 0.0


def _last(events: list[Event], type: str) -> tuple[int, Event] | None:
    for i in range(len(events) - 1, -1, -1):
        if events[i].type == type:
            return i, events[i]
    return None


def phase(state: str, events: list[Event], kind: RunKind = "part") -> Phase:
    """The run's phase from its events; circuit runs have their own steps (#110)."""
    if state in ("shelved", "expired", "deleted", "snapshotting"):
        return "shelved"
    if state == "failed":
        return "failed"
    finished, started = _last(events, "agent.run_finished"), _last(events, "agent.step_started")
    if finished and (started is None or finished[0] > started[0]):
        paused, status = (
            _str(finished[1].payload.get("paused_at")),
            _str(finished[1].payload.get("status")),
        )
        if paused in _PAUSED:
            return _PAUSED[paused]
        if status == "done" and kind == "part" and _finished_inspection(events, finished[0]):
            return "released"
        if status in _ENDED:
            return _ENDED[status]
    if started is None:
        return "provisioning"
    step, revision = _str(started[1].payload.get("step")), _revision(events)
    if kind == "circuit":
        return _CIRCUIT_STEPS.get(step or "", "analysing")
    if step in _ANALYSING:
        return "analysing"
    if step == "review_gate_1":
        return "waiting_gate_1"
    if step == "fix":
        return "fixing"
    if step == "revise" or revision > 1:
        return "update_run"
    return "building"


def _finished_inspection(events: list[Event], end: int) -> bool:
    """The reviewer finished the first article inspection (#152): the run driver resumed from
    gate 2 with "finish" before it ended. `agent.resumed` comes from the host (never from a
    container, docs/13 §1) and is the only proof: a run that ended `done` without gate 2, e.g.
    with `--auto-approve`, stays "valid"."""
    for e in reversed(events[:end]):
        if e.type == "agent.resumed" and e.source != "container":
            return e.payload.get("kind") == "result_review" and e.payload.get("action") == "finish"
    return False


def _revision(events: list[Event]) -> int:
    return max([_int(e.payload.get("revision", 1), 1) for e in events] or [1])


_STOPPED: dict[str, StopReason] = {"error": "error", "budget_exceeded": "budget"}


def _ended(events: list[Event]) -> Event | None:
    """The run's last `agent.run_finished`, unless a step started after it."""
    finished, started = _last(events, "agent.run_finished"), _last(events, "agent.step_started")
    if finished and (started is None or finished[0] > started[0]):
        return finished[1]
    return None


def _gave_up(events: list[Event]) -> dict[str, Any] | None:
    """The give_up step's `agent.build_failed` payload, if the run stopped there (#92)."""
    finished = _ended(events)
    if finished is None:
        return None
    payload = finished.payload
    if payload.get("status") not in ("build_failed", "aborted") and (
        payload.get("paused_at") != "gave_up"
    ):
        return None
    failed = _last(events, "agent.build_failed")
    return failed[1].payload if failed else {}


def stop_reason(events: list[Event]) -> StopReason | None:
    """Why the run ended without a model: the fix loop's cap or plateau (from the give_up step),
    a failed step or the budget (#92). Mirrored in the frontend (`lib/status.ts`)."""
    gave_up = _gave_up(events)
    if gave_up is not None:
        reason = gave_up.get("reason")
        return reason if reason == "cap" or reason == "plateau" else None
    finished = _ended(events)
    status = finished.payload.get("status") if finished else None
    return _STOPPED.get(str(status)) if status else None


def failed_features(events: list[Event]) -> list[str]:
    """Features the last build of a give-up could not add (#92)."""
    failures = (_gave_up(events) or {}).get("failures")
    if not isinstance(failures, list):
        return []
    return [
        str(f["feature_id"])
        for f in failures
        if isinstance(f, dict) and f.get("feature_id") not in (None, "*")
    ]


def failing_checks(events: list[Event]) -> list[str]:
    """Failed check ids of a give-up's last build, as the give_up step named them (#92): none if
    that build stopped at the base body. Nothing for other stops or for give-ups recorded before
    #92: the latest stored report may be from an earlier round."""
    checks = (_gave_up(events) or {}).get("checks")
    return [c for c in checks if isinstance(c, str)] if isinstance(checks, list) else []


def open_pois(events: list[Event]) -> int:
    """POIs of the current revision without a final reviewer decision. Failed checks are not
    counted: the fix loop and the validation panel track them. A release does not close them
    (D70): "released · 4 open points" stays honest."""
    revision = _revision(events)
    items: set[str] = set()
    for e in events:
        if e.type == "review.item_created" and _int(e.payload.get("revision", 1), 1) == revision:
            item = _dict(e.payload.get("item"))
            if item.get("kind") != "validation_failure" and item.get("id"):
                items.add(f"{e.payload.get('origin', 'agent')}:{item['id']}")
        elif e.type in _CLOSED_ITEM and isinstance(e.payload.get("item_id"), str):
            item_id = e.payload["item_id"]
            # Agent items are stored as rev<n>-<id> in the review app.
            prefix, dash, agent_id = item_id.partition("-")
            items.discard(f"reviewer:{item_id}")
            items.discard(f"agent:{agent_id if prefix.startswith('rev') and dash else item_id}")
    return len(items)


def run_kind(events: list[Event]) -> RunKind:
    """`kind` of the FIRST `agent.run_started`, like the run view (#80): container output is
    untrusted (docs/13), so a later line cannot switch it. Runs without it are part runs."""
    first = next((e for e in events if e.type == "agent.run_started"), None)
    return "circuit" if first is not None and first.payload.get("kind") == "circuit" else "part"


def unisolated_builds(events: list[Event]) -> int:
    """Builds that ran without runner namespaces (D95): the gate state may be untrusted. The
    sandbox reports them, so an odd `build_id` counts as a build of its own: another warning can
    only raise the count, never clear it (#137 review)."""
    builds: set[str] = set()
    odd = 0
    for e in events:
        if e.type == "runner.isolation_unavailable" and e.level == "warning":
            build_id = e.payload.get("build_id")
            if isinstance(build_id, str):
                builds.add(build_id)
            else:
                odd += 1
    return len(builds) + odd


def summarize(record: SessionRecord, events: list[Event], link: str | None) -> RunSummary:
    try:
        return _summarize(record, events, link)
    except Exception:  # noqa: BLE001 - one session's events must not break the run table
        return _summarize(record, [], link)


def _summarize(record: SessionRecord, events: list[Event], link: str | None) -> RunSummary:
    run_started = _last(events, "agent.run_started")
    finished = _last(events, "agent.run_finished")
    validation = _last(events, "validation.completed")
    input_added = _last(events, "session.input_added")
    started_payload = run_started[1].payload if run_started else {}
    checks = None
    if validation:
        found = _dict(validation[1].payload.get("report")).get("checks")
        checks = [c for c in found if isinstance(c, dict)] if isinstance(found, list) else None
    drawing = _str(started_payload.get("drawing")) or (
        _str(input_added[1].payload.get("file")) if input_added else None
    )
    return RunSummary(
        session_id=record.id,
        run_kind=run_kind(events),
        drawing=drawing,
        part_name=_str(finished[1].payload.get("part_name")) if finished else None,
        state=record.state,
        phase=phase(record.state, events, run_kind(events)),
        checks_passed=None if checks is None else sum(bool(c.get("passed")) for c in checks),
        checks_total=None if checks is None else len(checks),
        open_pois=open_pois(events),
        revision=_revision(events),
        fix_iterations=sum(
            1 for e in events if e.type == "agent.step_started" and e.payload.get("step") == "fix"
        ),
        max_fix_iterations=(
            None
            if started_payload.get("max_fix_iterations") is None
            else _int(started_payload.get("max_fix_iterations"), 0)
        ),
        model=_str(started_payload.get("model")),
        cost=round(
            sum(_cost(e.payload.get("cost")) for e in events if e.type == "model.response"), 6
        ),
        unisolated_builds=unisolated_builds(events),
        started_at=record.created_at,
        # when the host last heard from the session (sandbox clocks can drift, T13)
        last_activity=max((e.recv_ts or e.ts for e in events), default=None),
        link=link if record.state in ("running", "awaiting_review") else None,
        stop_reason=stop_reason(events),
        failing_checks=failing_checks(events),
        failed_features=failed_features(events),
        retention_class=record.retention_class,
        retention_days=RETENTION_DAYS.get(record.retention_class),
        expires_at=record.expires_at,
    )


def run_table(
    runs: RunRepository, events: EventRepository, link: Callable[[str], str | None] | None = None
) -> list[RunSummary]:
    """One row per run, newest first."""
    return [
        summarize(r, all_events(events, r.id), link(r.id) if link else None)
        for r in reversed(runs.all())
    ]
