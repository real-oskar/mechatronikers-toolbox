"""The hub's overview rows (`GET /api/runs`, docs/13): one `HubRun` per run, from trusted events.

The phase, summary and stop rules are mode 1's (`session.runs`), applied to the run's **host**
events only (docs/13 §1): container lines of state-driving types (`session.*`, `run.*`, `agent.*`,
`review.*`, `model.*`) are kept for the timeline and the audit trail, but cannot move a phase,
add cost or change the revision here; only the reviewer's POI decisions count (below). Checks
(`validation.completed`) are not state-driving and come from every source of the run's own
session, as does the D95 warning about builds without runner isolation (`unisolated_builds`),
which can only add a warning.

v0 specifics (docs/13 step 5, before gates and the queue):

- `state` is the session state of the last trusted `session.*` event.
- `cost` is the worker's settled spend (`run.settled`, #77) once there is one; until then the sum
  of the host's `model.response` estimates, which the agent's budget guard emits on the host.
- `open_pois` also counts the reviewer's POI decisions (`review.approved`, `.rejected`,
  `.edited`, and circuit-mcp's `.resolved`) from the sandbox: mirrored mode 1 runs are reviewed
  there (D86, D99).
  They can only close the session's own POIs.
- `reviewer` stays empty: gate decisions on mirrored mode 1 runs are made in the sandbox; it
  comes from `gates.decided_by` with #76. `plan` and `batch` come with the queue.

Rows are cached per run and recomputed after new events (one hub process).
"""

import base64
import binascii
import json
import math
import threading
from typing import Any, get_args

from mechatronikers.hub.records import HubEvents, HubRunRecord, HubStore
from mechatronikers.session import runs as rules
from mechatronikers.session.store import SessionRecord, SessionState
from mechatronikers.spec.review import HubRun, Phase, RunKind, RunPage
from mechatronikers.telemetry.events import Event

PHASES: tuple[str, ...] = get_args(Phase)
KINDS: tuple[str, ...] = get_args(RunKind)
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
EVENTS_PAGE = 1000

# Reviewer decisions on POIs that a mirrored mode 1 run makes in its sandbox (D99).
# `resolved`: circuit-mcp, when a gate 1 correction fixed what an item named (#129, #152).
SANDBOX_DECISIONS = ("review.approved", "review.rejected", "review.edited", "review.resolved")
_SESSION_STATES: dict[str, SessionState] = {
    "session.created": "created",
    "session.started": "running",
    "session.awaiting_review": "awaiting_review",
    "session.resumed": "running",
    "session.restored": "running",
    "session.snapshot_started": "snapshotting",
    "session.shelved": "shelved",
    "session.stopped": "shelved",
    "session.expired": "expired",
    "session.deleted": "deleted",
    "session.failed": "failed",
}


def trusted(events: list[Event]) -> list[Event]:
    """Events of host processes only (docs/13 §1)."""
    return [e for e in events if e.source == "host"]


def session_state(events: list[Event]) -> SessionState:
    state: SessionState = "created"
    for event in events:
        state = _SESSION_STATES.get(event.type, state)
    return state


def _settled(events: list[Event]) -> float | None:
    for event in reversed(events):
        if event.type == "run.settled":
            try:
                cost = float(event.payload["cost_eur"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            return cost if math.isfinite(cost) and cost >= 0 else None
    return None


def _with_decisions(events: list[Event]) -> list[Event]:
    """Host events plus the reviewer's POI decisions from the sandbox's review app (D99): in
    mode 1 review happens in the sandbox, and these events are legitimate for their own session
    (D86). They can only close the session's own POIs, never add one, cost or a phase."""
    return [e for e in events if e.source == "host" or e.type in SANDBOX_DECISIONS]


def summarize_run(record: HubRunRecord, events: list[Event]) -> HubRun:
    host = trusted(events)
    created = next((e for e in host if e.type == "session.created"), None)
    session = SessionRecord(
        id=record.session_id,
        state=session_state(host),
        created_at=created.ts if created else record.created_at,
        updated_at=record.created_at,
        image="",
    )
    summary = rules.summarize(session, host, None)
    # checks are not state-driving: the run's own session reports them (cad-mcp in the sandbox)
    with_checks = rules.summarize(session, events, None)
    cost = _settled(host)
    row: dict[str, Any] = summary.model_dump(exclude={"link", "session_id"})
    row.update(
        checks_passed=with_checks.checks_passed,
        checks_total=with_checks.checks_total,
        # a warning only ever added by the sandbox about itself (D95): from every source
        unisolated_builds=with_checks.unisolated_builds,
        open_pois=rules.open_pois(_with_decisions(events)),
        cost=summary.cost if cost is None else round(cost, 6),
        last_activity=with_checks.last_activity,
    )
    return HubRun(run_id=record.run_id, session_id=record.session_id, **row)


def run_events(events: HubEvents, run: HubRunRecord) -> list[Event]:
    """All events of a run, oldest first."""
    found: list[Event] = []
    after = 0
    while True:
        rows = events.run_events(run, after, EVENTS_PAGE)
        found += [e for _, e in rows]
        if len(rows) < EVENTS_PAGE:
            return found
        after = rows[-1][0]


def encode_cursor(row: HubRun) -> str:
    raw = json.dumps([row.started_at, row.run_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        started_at, run_id = json.loads(raw)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("invalid cursor") from None
    if not isinstance(started_at, str) or not isinstance(run_id, str):
        raise ValueError("invalid cursor")
    return started_at, run_id


class Overview:
    """Cached rows per run; `mark` after new events, and the next read recomputes that run."""

    def __init__(self, runs: HubStore, events: HubEvents) -> None:
        self.runs = runs
        self.events = events
        self._rows: dict[str, HubRun] = {}
        self._dirty: set[str] = set()
        self._lock = threading.Lock()

    def mark(self, run_id: str) -> None:
        with self._lock:
            self._dirty.add(run_id)

    def _fresh(self, record: HubRunRecord) -> HubRun:
        with self._lock:
            row = self._rows.get(record.run_id)
            if row is not None and record.run_id not in self._dirty:
                return row
            self._dirty.discard(record.run_id)
        row = summarize_run(record, run_events(self.events, record))
        with self._lock:
            self._rows[record.run_id] = row
        return row

    def row(self, run_id: str) -> HubRun | None:
        record = self.runs.get_run(run_id)
        return None if record is None else self._fresh(record)

    def page(
        self,
        *,
        kind: str | None = None,
        phase: str | None = None,
        batch: str | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = PAGE_SIZE,
    ) -> RunPage:
        """Newest first by (started_at, run_id); the cursor is that key of the last row, so a
        run that starts between two requests neither repeats a row nor drops one."""
        rows = [self._fresh(record) for record in self.runs.runs()]
        needle = (q or "").strip().lower()
        rows = [
            r
            for r in rows
            if (kind is None or r.run_kind == kind)
            and (phase is None or r.phase == phase)
            and (batch is None or r.batch == batch)
            and (
                not needle
                or any(
                    needle in (value or "").lower()
                    for value in (r.run_id, r.session_id, r.drawing, r.part_name, r.plan)
                )
            )
        ]
        rows.sort(key=lambda r: (r.started_at, r.run_id), reverse=True)
        if cursor is not None:
            after = decode_cursor(cursor)
            rows = [r for r in rows if (r.started_at, r.run_id) < after]
        page = rows[:limit]
        more = len(rows) > limit
        return RunPage(runs=page, next_cursor=encode_cursor(page[-1]) if more and page else None)
