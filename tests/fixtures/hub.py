"""A hub on temporary stores for the hub tests, and the recorded run as mode 1 stores it."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mechatronikers.hub.app import create_hub_app
from mechatronikers.hub.auth import HubAuth
from mechatronikers.hub.records import HubEvents, HubStore, RepositoryEvents, SqliteHubStore
from mechatronikers.session.store import SessionRecord, SessionStore
from mechatronikers.storage import BlobStore, LocalBlobStore
from mechatronikers.telemetry import Event
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.events import Source
from mechatronikers.telemetry.store import EventStore
from tests.fixtures.stores import NEEDS_PG, pg_schema

SHARED = "shared-browser-token-0123456789"
WORKERS = {"worker-a": "worker-a-token-0123456789abcd", "worker-b": "worker-b-token-0123456789abcd"}
ORIGIN = "http://testserver"  # TestClient's base URL
FIXTURE = Path(__file__).parent / "events" / "fuss-run.ndjson"
GATEWAY_TOKEN = "0f" * 24  # what the session manager mints: secrets.token_hex(24)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


BROWSER = bearer(SHARED)
WORKER_A = bearer(WORKERS["worker-a"])
WORKER_B = bearer(WORKERS["worker-b"])
NDJSON = {"Content-Type": "application/x-ndjson"}


BACKENDS = ["sqlite", pytest.param("postgres", marks=NEEDS_PG)]


@dataclass
class Hub:
    client: TestClient
    events: HubEvents
    runs: HubStore
    blobs: BlobStore


@contextmanager
def _stores(tmp: Path, backend: str) -> Iterator[tuple[HubEvents, HubStore, BlobStore]]:
    if backend == "postgres":
        from mechatronikers.hub.pg import PgHubEvents, PgHubStore
        from mechatronikers.pgstore.store import PgStore

        with pg_schema() as dsn:  # a fresh, migrated schema, dropped afterwards
            # as `serve`: writes on a pool of their own
            store = PgStore.open(tmp / "hub" / "blobs", dsn, apply_migrations=False)
            writer = PgStore.open(tmp / "hub" / "blobs", dsn, apply_migrations=False, max_size=2)
            try:
                yield PgHubEvents(store, writer), PgHubStore(store, writer), store.artifacts.blobs
            finally:
                store.close()
                writer.close()
        return
    events = EventStore(tmp / "hub" / "hub.db")
    blobs = LocalBlobStore(tmp / "hub" / "blobs")
    runs = SqliteHubStore(tmp / "hub" / "hub.db", blobs)
    try:
        yield RepositoryEvents(events), runs, blobs
    finally:
        events.close()
        runs.close()


@contextmanager
def hub(tmp: Path, backend: str = "sqlite", **kwargs: Any) -> Iterator[Hub]:
    with _stores(tmp, backend) as (events, runs, blobs):
        app = create_hub_app(
            events=events, runs=runs, auth=HubAuth.create(SHARED, WORKERS), **kwargs
        )
        with TestClient(app) as client:
            yield Hub(client, events, runs, blobs)


def event(
    type: str,
    *,
    emitter: str = "agent-1",
    seq: int = 1,
    session: str = "ses-a1",
    run: str = "run-a1",
    payload: dict[str, Any] | None = None,
    ts: str = "2026-10-17T08:00:00.000000Z",
) -> Event:
    return Event(
        ts=ts,
        recv_ts=ts,
        mono_ns=0,
        seq=seq,
        emitter_id=emitter,
        session_id=session,
        run_id=run,
        component=type.split(".")[0],
        type=type,
        payload=payload or {},
    )


def line(e: Event | dict[str, Any], source: Source = "host") -> str:
    body = e if isinstance(e, dict) else json.loads(e.to_json())
    return json.dumps({"source": source, "event": body})


def ndjson(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def fixture_events() -> list[Event]:
    return [Event.model_validate(json.loads(text)) for text in FIXTURE.read_text().splitlines()]


@dataclass
class Host:
    """A sandbox host after the recorded run (mode 1): the collector's store and the session."""

    store: EventStore
    sessions: SessionStore
    collector: Collector
    record: SessionRecord


@contextmanager
def mode1_host(tmp: Path, state: str = "running") -> Iterator[Host]:
    """The recorded run as `session watch` stores it: the session manager's events as trusted
    host events, the agent's lines through `POST /ingest`, everything else as container output."""
    store = EventStore(tmp / "host" / "state.db")
    sessions = SessionStore(store.connection())
    collector = Collector(store, tmp / "host" / "storage")
    events = fixture_events()
    record = SessionRecord(
        id=events[0].session_id or "",
        state=state,  # type: ignore[arg-type]
        token=GATEWAY_TOKEN,
        created_at=events[0].ts,
        updated_at=events[0].ts,
        port=40000,
        image="localhost/mechatronikers:dev",
    )
    sessions.save(record)
    for e in events:
        if e.component == "session-manager":
            collector.ingest(e)
        elif e.component == "agent":
            collector.ingest_line(e.to_json(), source="agent")
        else:
            collector.ingest_line(e.to_json(), source="session", session_id=record.id)
    try:
        yield Host(store, sessions, collector, record)
    finally:
        store.close()
