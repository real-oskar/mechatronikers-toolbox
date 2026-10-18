"""Repository interfaces over the stores (D72, docs/13 "Central store", migration step 4).

Callers depend on these protocols, not on a database. Mode 1 implements them with today's stores;
the hub implements them with Postgres (#73). The contract tests (`tests/test_contract_*.py`) run
against every implementation, so both behave alike.

| Interface          | Mode 1 implementation                   | Hub (#73, `pgstore`)   |
|--------------------|-----------------------------------------|------------------------|
| `EventRepository`  | `telemetry.store.EventStore` (SQLite)   | `PgEventRepository`    |
| `RunRepository`    | `session.store.SessionStore` (SQLite)   | `PgRunRepository`      |
| `ReviewRepository` | `review_app.store.JsonReviewRepository` | `PgReviewRepository`   |

Repositories only store records. Rules stay with their callers, so both modes share them: the
review rules in `review_app.store.ReviewStore`, the phase and summary rules in `session.runs`, the
ingest rules in `telemetry.collector.Collector`.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from mechatronikers.session.store import SessionRecord, SessionState
from mechatronikers.spec.review import GateState, StoredItem
from mechatronikers.telemetry.events import Event


class EventRepository(Protocol):
    """Events in ingest order (docs/08), and the read positions of host-side log sources."""

    def add(self, event: Event) -> int | None:
        """Store an event. Returns its ingest id (increasing: the global order), or None if an
        event with the same `(emitter_id, seq)` is stored already. Events without an emitter id
        are never de-duplicated. Ids become visible in increasing order, so a reader paging
        with `events(after=…)` never skips an event stored later. A bare Postgres sequence does
        not ensure this (its values commit out of order); one ingest writer at a time per session
        and run does (`PgEventRepository`)."""
        ...

    def last_seq(self, emitter_id: str) -> int | None:
        """The highest sequence number stored for one emitter."""
        ...

    def events(self, session_id: str, after: int = 0, limit: int = 1000) -> list[tuple[int, Event]]:
        """`(ingest id, event)` of one session with an ingest id above `after`, oldest first."""
        ...

    def count(self, session_id: str) -> int: ...

    def last_activity(self, session_id: str, ignore_types: tuple[str, ...] = ()) -> str | None:
        """The newest `ts` of a session's events (as sent), ignoring the given types."""
        ...

    def get_cursor(self, source: str) -> str | None:
        """Where reading a source stopped (container logs; later the forwarder per session)."""
        ...

    def set_cursor(self, source: str, last_ts: str) -> None: ...

    def close(self) -> None: ...


class RunRepository(Protocol):
    """Run records for the run table and the session lifecycle (docs/07).

    In mode 1 one sandbox runs one plan, so a run is its session's record. The table rows
    (`RunSummary`) are derived from these records and the events by `session.runs`. Records of
    the session manager carry the gateway token; hub records carry none (`token=None`).
    """

    def save(self, record: SessionRecord) -> None:
        """Insert or replace the record with this id."""
        ...

    def get(self, session_id: str) -> SessionRecord:
        """Raises KeyError for an unknown id."""
        ...

    def all(self, states: tuple[SessionState, ...] | None = None) -> list[SessionRecord]:
        """All records (or those in one of `states`), oldest first."""
        ...


class ReviewRepository(Protocol):
    """Gates and points of interest of one run (D62): records only, no rules.

    Mode 1 keeps them as JSON files in the session volume; the hub keeps them per run (D80).
    """

    def transaction(self) -> AbstractContextManager[None]:
        """One read-modify-write at a time per run: the review rules check and write inside it.
        Every other method may be called inside; transactions are not nested."""
        ...

    def gates(self) -> list[GateState]:
        """All gates, ordered by `request.created_at`."""
        ...

    def get_gate(self, gate_id: str) -> GateState | None: ...

    def save_gate(self, state: GateState) -> None:
        """Insert or replace the gate with id `state.request.id`."""
        ...

    def items(self, revision: int | None = None) -> list[StoredItem]:
        """All POIs (or those of one revision), ordered by revision, `created_at` and id."""
        ...

    def get_item(self, item_id: str) -> StoredItem | None: ...

    def save_item(self, item: StoredItem) -> None:
        """Insert or replace the POI with id `item.item.id`."""
        ...
