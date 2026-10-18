"""Postgres implementations of the repository interfaces (#73 phase B, docs/13 "Central store").

| Interface (`storage`) | Here                 | Tables                                       |
|-----------------------|----------------------|----------------------------------------------|
| `EventRepository`     | `PgEventRepository`  | `events`, `event_cursors`                    |
| `RunRepository`       | `PgRunRepository`    | `sessions`                                   |
| `ReviewRepository`    | `PgReviewRepository` | `gates`, `gate_corrections`, `findings`,     |
|                       |                      | `finding_actions`                            |

They pass the same contract tests as the mode 1 stores (`tests/test_contract_*.py`). Each one is
scoped to one organisation (D82: the default one until OIDC) and borrows connections from a
shared `Pool`, whose connections run READ COMMITTED: every statement after a lock wait sees what
the previous lock holder committed, which the review rules and de-duplication rely on. Records are
kept whole as JSONB, so they read back equal; the typed columns are copies for filters, the
provenance columns (`events.source`, `emitter_id`, `run_id`) and the hub's own clock. Values
Postgres cannot hold are made storable and logged (`values.clean`: NUL and lone surrogates become
U+FFFD, NaN and Infinity null); values that cannot be stored at all (an id outside the id
charset, `seq` outside bigint) raise ValueError before anything is written, never a database
error.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from psycopg.types.json import Jsonb

from mechatronikers.pgstore.db import DEFAULT_ORG, Connection, Pool
from mechatronikers.pgstore.values import (
    BIGINT,
    check_id,
    clean,
    clean_text,
    is_id,
    lock_key,
    parse_ts,
)
from mechatronikers.session.store import SessionRecord, SessionState
from mechatronikers.spec.review import GateState, StoredItem
from mechatronikers.telemetry.events import Event

log = logging.getLogger(__name__)

EventSource = Literal["host", "container"]
SOURCES: tuple[EventSource, ...] = ("host", "container")
# Namespaces of pg_advisory_xact_lock(int, int); the two-key space never meets the migration
# runner's single bigint key.
INGEST_LOCK = 0x6D74_6576  # "mtev": event ingest, per session, run and emitter
REVIEW_LOCK = 0x6D74_7276  # "mtrv": review writes, per run


def _lock(conn: Connection, namespace: int, keys: Sequence[int]) -> None:
    """Take the locks in ascending order, so two writers never wait on each other in a cycle."""
    for key in sorted(set(keys)):
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (namespace, key))


# -- events -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _EventRow:
    params: dict[str, Any]
    locks: tuple[int, ...]


_INSERT_EVENT = """
INSERT INTO events (org_id, session_id, run_id, emitter_id, seq, source, component, type, level,
                    ts, body)
VALUES (%(org_id)s, %(session_id)s, %(run_id)s, %(emitter_id)s, %(seq)s, %(source)s,
        %(component)s, %(type)s, %(level)s, %(ts)s, %(body)s)
ON CONFLICT (emitter_id, seq) DO NOTHING
RETURNING id
"""


class PgEventRepository:
    """Events and log cursors of one organisation (`EventRepository`).

    **Paging never skips an event.** An identity value is taken at insert time but becomes
    visible at commit, so two concurrent ingest transactions can commit their ids out of order,
    and a reader paging with `after=<id>` (SSE, a run view) would skip the lower id for good.
    Ingest therefore takes transaction-level advisory locks on the session, the run and the
    emitter of every event it stores *before* it inserts: writers of one session (or run) take
    their ids and commit one after the other, so what a reader of that session (or run) sees is
    always a prefix of what it will see. Sessions and runs still ingest in parallel. The emitter
    lock serialises writers of one `(emitter_id, seq)`, so concurrent duplicates never wait on
    each other's unique index entries. Only this class writes `events`, and the id sequence keeps
    CACHE 1 (migration 0002; `PgStore.open` checks it): a cache would hand each backend its own
    block of ids, out of lock order. A reader across all sessions (a global tail) would need one
    lock for all ingest; nothing reads that way.

    **Provenance.** `source` says where a line was read: `host` (a trusted host process) or
    `container` (untrusted, the default). `run_id` and `emitter_id` are columns too, so readers
    apply the docs/13 §1 rules without parsing the body.
    """

    def __init__(self, pool: Pool, *, org_id: str = DEFAULT_ORG) -> None:
        self._pool = pool
        self.org_id = check_id(org_id, "org id")

    # -- ingest ---------------------------------------------------------------------------

    def add(self, event: Event, *, source: EventSource = "container") -> int | None:
        """Store one event; its ingest id, or None for a duplicate `(emitter_id, seq)`."""
        return self.add_many([(event, source)])[0]

    def add_many(self, batch: Sequence[tuple[Event, EventSource]]) -> list[int | None]:
        """Store events in order in one transaction (an ingest request); per event its ingest
        id or None for a duplicate. Raises ValueError, storing nothing, if one of them cannot
        be stored (see `problem`)."""
        rows = [self._row(event, source) for event, source in batch]
        if not rows:
            return []
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, INGEST_LOCK, [key for row in rows for key in row.locks])
            return [self._insert(conn, row) for row in rows]

    @staticmethod
    def problem(event: Event) -> str | None:
        """Why `event` cannot be stored, or None. Ingest can drop such lines one by one."""
        if not BIGINT[0] <= event.seq <= BIGINT[1]:
            return "seq out of range"
        for name in ("session_id", "run_id"):
            value = getattr(event, name)
            if value is not None and not is_id(value):
                return f"invalid {name}"
        if event.emitter_id is not None and not 1 <= len(event.emitter_id) <= 100:
            return "emitter_id must have 1 to 100 characters"
        return None

    def _row(self, event: Event, source: EventSource) -> _EventRow:
        if source not in SOURCES:
            raise ValueError(f"source must be host or container, not {source!r}")
        problem = self.problem(event)
        if problem is not None:
            raise ValueError(
                f"event {str(event.emitter_id)[:100]!r}/{event.seq} cannot be stored: {problem}"
            )
        replaced: list[str] = []
        body = clean(event.model_dump(mode="json", by_alias=True, exclude_none=True), replaced)
        if replaced:
            log.warning(
                "event %r/%s: stored with unstorable values replaced at %s",
                body.get("emitter_id"),
                event.seq,
                ", ".join(replaced[:20]),
            )
        params = {
            "org_id": self.org_id,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "emitter_id": body.get("emitter_id"),
            "seq": event.seq,
            "source": source,
            "component": body["component"],
            "type": body["type"],
            "level": body["level"],
            "ts": body["ts"],
            "body": Jsonb(body),
        }
        locks = [lock_key(self.org_id, "s", event.session_id)] if event.session_id else []
        if event.run_id:
            locks.append(lock_key(self.org_id, "r", event.run_id))
        if params["emitter_id"] is not None:
            locks.append(lock_key("e", params["emitter_id"]))  # unique across orgs, as the index
        return _EventRow(params, tuple(locks))

    def _insert(self, conn: Connection, row: _EventRow) -> int | None:
        found = conn.execute(_INSERT_EVENT, row.params).fetchone()
        return None if found is None else int(found["id"])

    # -- reads ----------------------------------------------------------------------------

    def last_seq(self, emitter_id: str) -> int | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT MAX(seq) AS seq FROM events WHERE org_id = %s AND emitter_id = %s",
                (self.org_id, clean_text(emitter_id)),
            ).fetchone()
        return None if row is None or row["seq"] is None else int(row["seq"])

    def events(self, session_id: str, after: int = 0, limit: int = 1000) -> list[tuple[int, Event]]:
        return self._page("session_id", session_id, after, limit)

    def run_events(self, run_id: str, after: int = 0, limit: int = 1000) -> list[tuple[int, Event]]:
        """`(ingest id, event)` of one run with an ingest id above `after`, oldest first (the
        hub's run view and its SSE stream page by run)."""
        return self._page("run_id", run_id, after, limit)

    def _page(
        self, column: Literal["session_id", "run_id"], value: str, after: int, limit: int
    ) -> list[tuple[int, Event]]:
        if not is_id(value):
            return []
        query = (
            "SELECT id, body FROM events WHERE org_id = %s AND session_id = %s AND id > %s "
            "ORDER BY id LIMIT %s"
            if column == "session_id"
            else "SELECT id, body FROM events WHERE org_id = %s AND run_id = %s AND id > %s "
            "ORDER BY id LIMIT %s"
        )
        with self._pool.connection() as conn:
            rows = conn.execute(query, (self.org_id, value, after, limit)).fetchall()
        return [(int(r["id"]), Event.model_validate(r["body"])) for r in rows]

    def count(self, session_id: str) -> int:
        if not is_id(session_id):
            return 0
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE org_id = %s AND session_id = %s",
                (self.org_id, session_id),
            ).fetchone()
        return 0 if row is None else int(row["n"])

    def last_activity(self, session_id: str, ignore_types: tuple[str, ...] = ()) -> str | None:
        if not is_id(session_id):
            return None
        with self._pool.connection() as conn:
            # "C": compare as bytes, like SQLite, whatever the database's collation
            row = conn.execute(
                'SELECT MAX(ts COLLATE "C") AS ts FROM events '
                "WHERE org_id = %s AND session_id = %s AND NOT (type = ANY(%s::text[]))",
                (self.org_id, session_id, [clean_text(t) for t in ignore_types]),
            ).fetchone()
        return None if row is None or row["ts"] is None else str(row["ts"])

    # -- cursors --------------------------------------------------------------------------

    def get_cursor(self, source: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT last_ts FROM event_cursors WHERE org_id = %s AND source = %s",
                (self.org_id, clean_text(source)),
            ).fetchone()
        return None if row is None else str(row["last_ts"])

    def set_cursor(self, source: str, last_ts: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO event_cursors (org_id, source, last_ts) VALUES (%s, %s, %s) "
                "ON CONFLICT (org_id, source) DO UPDATE SET last_ts = EXCLUDED.last_ts",
                (self.org_id, clean_text(source), clean_text(last_ts)),
            )

    def close(self) -> None:
        """Nothing to do: the pool's owner (`PgStore`) closes the connections."""


# -- runs ---------------------------------------------------------------------------------


_SAVE_SESSION = """
INSERT INTO sessions AS s (id, org_id, run_id, state, snapshot_id, expires_at, created_at,
                           updated_at, record)
VALUES (%(id)s, %(org_id)s, %(run_id)s, %(state)s, %(snapshot_id)s, %(expires_at)s,
        COALESCE(%(created_at)s, now()), COALESCE(%(updated_at)s, now()), %(record)s)
ON CONFLICT (id) DO UPDATE SET
    run_id = COALESCE(EXCLUDED.run_id, s.run_id),
    state = EXCLUDED.state,
    snapshot_id = EXCLUDED.snapshot_id,
    expires_at = EXCLUDED.expires_at,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    record = EXCLUDED.record
WHERE s.org_id = EXCLUDED.org_id
RETURNING id
"""


class PgRunRepository:
    """Run records of one organisation (`RunRepository`): the `sessions` table.

    Hub records carry no gateway token: it never leaves the worker host (docs/13 §5). `save`
    refuses a record with one, and the table's CHECK refuses a `token` key as well.
    """

    def __init__(self, pool: Pool, *, org_id: str = DEFAULT_ORG) -> None:
        self._pool = pool
        self.org_id = check_id(org_id, "org id")

    def save(self, record: SessionRecord, *, run_id: str | None = None) -> None:
        """Insert or replace the record; `run_id` links the session to its hub run (kept when
        a later save leaves it out)."""
        if record.token is not None:
            raise ValueError("hub run records carry no gateway token (docs/13 §5)")
        check_id(record.id, "session id")
        if record.snapshot_id is not None:
            check_id(record.snapshot_id, "snapshot id")
        if run_id is not None:
            check_id(run_id, "run id")
        replaced: list[str] = []
        body = clean(record.model_dump(mode="json", exclude={"token"}), replaced)
        if replaced:
            log.warning("session %s: unstorable values replaced at %s", record.id, replaced)
        with self._pool.connection() as conn:
            saved = conn.execute(
                _SAVE_SESSION,
                {
                    "id": record.id,
                    "org_id": self.org_id,
                    "run_id": run_id,
                    "state": record.state,
                    "snapshot_id": record.snapshot_id,
                    "expires_at": parse_ts(record.expires_at),
                    "created_at": parse_ts(record.created_at),
                    "updated_at": parse_ts(record.updated_at),
                    "record": Jsonb(body),
                },
            ).fetchone()
        if saved is None:
            raise ValueError(f"session {record.id} belongs to another organisation")

    def get(self, session_id: str) -> SessionRecord:
        row = None
        if is_id(session_id):
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT record FROM sessions WHERE org_id = %s AND id = %s",
                    (self.org_id, session_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"unknown session: {session_id}")
        return SessionRecord.model_validate(row["record"])

    def all(self, states: tuple[SessionState, ...] | None = None) -> list[SessionRecord]:
        # oldest first by the record's created_at as sent, compared as bytes (like SQLite)
        query = "SELECT record FROM sessions WHERE org_id = %s"
        params: list[Any] = [self.org_id]
        if states is not None:
            query += " AND state = ANY(%s::text[])"
            params.append(list(states))
        query += ' ORDER BY record->>\'created_at\' COLLATE "C", id COLLATE "C"'
        with self._pool.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [SessionRecord.model_validate(r["record"]) for r in rows]


# -- reviews ------------------------------------------------------------------------------


_SAVE_GATE = """
INSERT INTO gates AS g (run_id, id, org_id, kind, run_kind, revision, request, decision, action,
                        decided_by, decided_at)
VALUES (%(run_id)s, %(id)s, %(org_id)s, %(kind)s, %(run_kind)s, %(revision)s, %(request)s,
        %(decision)s, %(action)s, %(decided_by)s,
        CASE WHEN %(action)s::text IS NULL THEN NULL ELSE now() END)
ON CONFLICT (run_id, id) DO UPDATE SET
    kind = EXCLUDED.kind,
    run_kind = EXCLUDED.run_kind,
    revision = EXCLUDED.revision,
    request = EXCLUDED.request,
    decision = EXCLUDED.decision,
    action = EXCLUDED.action,
    decided_by = EXCLUDED.decided_by,
    decided_at = EXCLUDED.decided_at
WHERE g.org_id = EXCLUDED.org_id
  AND (g.request, g.decision) IS DISTINCT FROM (EXCLUDED.request, EXCLUDED.decision)
"""
_SAVE_CORRECTION = """
INSERT INTO gate_corrections (run_id, gate_id, position, org_id, target, target_id, field,
                              old_value, new_value)
VALUES (%(run_id)s, %(gate_id)s, %(position)s, %(org_id)s, %(target)s, %(target_id)s, %(field)s,
        %(old_value)s, %(new_value)s)
ON CONFLICT (run_id, gate_id, position) DO NOTHING
"""
_SAVE_FINDING = """
INSERT INTO findings AS f (run_id, id, org_id, revision, origin, kind, severity, status, record,
                           created_by)
VALUES (%(run_id)s, %(id)s, %(org_id)s, %(revision)s, %(origin)s, %(kind)s, %(severity)s,
        %(status)s, %(record)s, %(created_by)s)
ON CONFLICT (run_id, id) DO UPDATE SET
    revision = EXCLUDED.revision,
    origin = EXCLUDED.origin,
    kind = EXCLUDED.kind,
    severity = EXCLUDED.severity,
    status = EXCLUDED.status,
    record = EXCLUDED.record,
    created_by = EXCLUDED.created_by
WHERE f.org_id = EXCLUDED.org_id
"""
_SAVE_ACTION = """
INSERT INTO finding_actions (run_id, finding_id, org_id, action, reviewer, old_value, new_value,
                             comment, at)
VALUES (%(run_id)s, %(finding_id)s, %(org_id)s, %(action)s, %(reviewer)s, %(old_value)s,
        %(new_value)s, %(comment)s, COALESCE(%(at)s, now()))
"""


def _scalar(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


class PgReviewRepository:
    """Gates and POIs of one run (`ReviewRepository`, D80): `gates` with the decision's
    corrections as `gate_corrections` rows (D84), `findings` with every history entry appended to
    `finding_actions` (the audit trail). The run's `runs` row must exist (foreign keys); a gate's
    request names the same run.

    `transaction()` takes a transaction-level advisory lock on the run, so read-modify-writes of
    one run run one at a time across threads and hub processes, while other runs go on. Each
    statement after the lock wait sees what the previous holder committed (the pool's READ
    COMMITTED). The open transaction is kept per run and thread in the pool, so every
    `PgReviewRepository` of that run on that thread (e.g. one per request handler) joins it; a
    second `transaction()` of the run on the thread raises RuntimeError (not nested), never
    waits on its own lock. A write outside a transaction takes the same lock for its own
    statements. The pool's `lock_timeout` bounds every wait. The database holds the rules too: a
    gate's request never changes and a decision is final.
    """

    def __init__(self, pool: Pool, run_id: str, *, org_id: str = DEFAULT_ORG) -> None:
        self._pool = pool
        self.run_id = check_id(run_id, "run id")
        self.org_id = check_id(org_id, "org id")
        self._key = ("review", self.org_id, self.run_id)

    # -- transactions ---------------------------------------------------------------------

    def _pinned(self) -> Connection | None:
        return self._pool.pinned(self._key)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._pinned() is not None:
            raise RuntimeError("review transactions are not nested")
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, REVIEW_LOCK, [lock_key(self.org_id, self.run_id)])
            with self._pool.pin(self._key, conn):
                yield

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        pinned = self._pinned()
        if pinned is not None:
            yield pinned
            return
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def _write(self) -> Iterator[Connection]:
        """Statements that commit together, under the run's lock."""
        pinned = self._pinned()
        if pinned is not None:
            with pinned.transaction():  # a savepoint: a failed write leaves the rest usable
                yield pinned
            return
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, REVIEW_LOCK, [lock_key(self.org_id, self.run_id)])
            yield conn

    # -- gates ----------------------------------------------------------------------------

    def gates(self) -> list[GateState]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT request, decision FROM gates WHERE org_id = %s AND run_id = %s",
                (self.org_id, self.run_id),
            ).fetchall()
        states = [GateState.model_validate(r) for r in rows]
        return sorted(states, key=lambda s: (s.request.created_at, s.request.id))

    def get_gate(self, gate_id: str) -> GateState | None:
        check_id(gate_id, "gate id")
        with self._read() as conn:
            row = conn.execute(
                "SELECT request, decision FROM gates WHERE org_id = %s AND run_id = %s AND id = %s",
                (self.org_id, self.run_id, gate_id),
            ).fetchone()
        return None if row is None else GateState.model_validate(row)

    def save_gate(self, state: GateState) -> None:
        check_id(state.request.id, "gate id")
        if state.request.run_id != self.run_id:
            raise ValueError(f"gate {state.request.id} of run {state.request.run_id!r} is not "
                             f"a gate of run {self.run_id}")  # fmt: skip
        replaced: list[str] = []
        request = clean(state.request.model_dump(mode="json"), replaced, "/request")
        decision = (
            None
            if state.decision is None
            else clean(state.decision.model_dump(mode="json"), replaced, "/decision")
        )
        if replaced:
            log.warning("gate %s: unstorable values replaced at %s", state.request.id, replaced)
        with self._write() as conn:
            conn.execute(
                _SAVE_GATE,
                {
                    "run_id": self.run_id,
                    "id": state.request.id,
                    "org_id": self.org_id,
                    "kind": request["kind"],
                    "run_kind": request["run_kind"],
                    "revision": request["revision"],
                    "request": Jsonb(request),
                    "decision": None if decision is None else Jsonb(decision),
                    "action": None if decision is None else decision["action"],
                    "decided_by": None if decision is None else decision["reviewer"],
                },
            )
            for position, correction in enumerate((decision or {}).get("corrections", [])):
                conn.execute(
                    _SAVE_CORRECTION,
                    {
                        "run_id": self.run_id,
                        "gate_id": state.request.id,
                        "position": position,
                        "org_id": self.org_id,
                        "target": correction["target"],
                        "target_id": correction["id"],
                        "field": correction["field"],
                        "old_value": correction.get("old"),
                        "new_value": correction["new"],
                    },
                )

    # -- points of interest ---------------------------------------------------------------

    def items(self, revision: int | None = None) -> list[StoredItem]:
        query = "SELECT record FROM findings WHERE org_id = %s AND run_id = %s"
        params: list[Any] = [self.org_id, self.run_id]
        if revision is not None:
            query += " AND revision = %s"
            params.append(revision)
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        stored = [StoredItem.model_validate(r["record"]) for r in rows]
        return sorted(stored, key=lambda s: (s.revision, s.created_at, s.item.id))

    def get_item(self, item_id: str) -> StoredItem | None:
        check_id(item_id, "item id")
        with self._read() as conn:
            row = conn.execute(
                "SELECT record FROM findings WHERE org_id = %s AND run_id = %s AND id = %s",
                (self.org_id, self.run_id, item_id),
            ).fetchone()
        return None if row is None else StoredItem.model_validate(row["record"])

    def save_item(self, item: StoredItem) -> None:
        item_id = check_id(item.item.id, "item id")
        replaced: list[str] = []
        record = clean(item.model_dump(mode="json"), replaced)
        if replaced:
            log.warning("item %s: unstorable values replaced at %s", item_id, replaced)
        poi = record["item"]
        with self._write() as conn:
            conn.execute(
                _SAVE_FINDING,
                {
                    "run_id": self.run_id,
                    "id": item_id,
                    "org_id": self.org_id,
                    "revision": record["revision"],
                    "origin": record["origin"],
                    "kind": poi["kind"],
                    "severity": poi["severity"],
                    "status": poi["status"],
                    "record": Jsonb(record),
                    "created_by": record["created_by"],
                },
            )
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM finding_actions WHERE run_id = %s AND finding_id = %s",
                (self.run_id, item_id),
            ).fetchone()
            known = 0 if row is None else int(row["n"])
            for action in poi["history"][known:]:  # history only grows (ReviewStore)
                conn.execute(
                    _SAVE_ACTION,
                    {
                        "run_id": self.run_id,
                        "finding_id": item_id,
                        "org_id": self.org_id,
                        "action": action["action"],
                        "reviewer": action["by"],
                        "old_value": _scalar(action.get("old_value")),
                        "new_value": _scalar(action.get("new_value")),
                        "comment": action.get("comment"),
                        "at": parse_ts(action.get("at")),
                    },
                )
