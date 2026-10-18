"""The hub's stores (docs/13 "Central store"): events with their source, which worker mirrors
which run and session, and the uploaded artifacts.

Two backends behind the same two protocols:

| Protocol    | SQLite (`--data-dir`, tests)         | Postgres (`MT_PG_DSN`, `hub.pg`)        |
|-------------|--------------------------------------|-----------------------------------------|
| `HubEvents` | `RepositoryEvents` over `EventStore` | `PgStore.events` (`add_many`, by run)   |
| `HubStore`  | `SqliteHubStore` + `LocalBlobStore`  | `runs`/`workers` + `PgStore.artifacts`  |

Both read and write events through the `EventRepository` of #72; the hub adds the line's source
(docs/13 §1) and reads by run.
"""

import hashlib
import re
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from pydantic import BaseModel

from mechatronikers.storage.blobs import BlobStore
from mechatronikers.storage.repositories import EventRepository
from mechatronikers.telemetry.events import Event, Source

# The store's id charset (`mt_id` in pgstore/migrations/0001_initial.sql).
ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Roles a worker may upload below `artifacts/<session_id>/` (docs/13): inputs, snapshots, logs and
# test cases are written by other paths.
ArtifactRole = Literal["sheet", "spec", "export", "netlist", "report"]


class RunOwnershipError(PermissionError):
    """A line or upload for a run (or session) that belongs to another worker or run."""


class HubRunRecord(BaseModel):
    """A run the hub knows. In v0 every run mirrors one mode 1 session (docs/13 step 5, D99): the
    worker whose forwarder pushed its first line owns it; with the queue (#77) the claim does."""

    run_id: str
    session_id: str
    worker_id: str
    created_at: str  # hub clock


class ArtifactRecord(BaseModel):
    """One uploaded file: the blob key is derived by the hub, never chosen by a worker."""

    id: str
    run_id: str
    session_id: str
    build_id: str
    role: ArtifactRole
    name: str
    blob_key: str
    content_type: str
    sha256: str
    bytes: int
    created_at: str  # hub clock


class HubEvents(Protocol):
    """Event storage of the hub: the docs/08 events with the source the forwarder named."""

    def add_many(self, batch: Sequence[tuple[Event, Source]]) -> list[int | None]:
        """Store in order; per event its ingest id, or None for a duplicate `(emitter_id, seq)`.
        Raises ValueError (storing nothing) for an event the store cannot hold."""
        ...

    def run_events(
        self, run: HubRunRecord, after: int = 0, limit: int = 1000
    ) -> list[tuple[int, Event]]:
        """The run's events with an ingest id above `after`, oldest first."""
        ...


class HubStore(Protocol):
    def bind_run(
        self, run_id: str, session_id: str, worker_id: str, created_at: str
    ) -> HubRunRecord:
        """The run's record, created on first sight for this worker and session. Raises
        `RunOwnershipError` if the run belongs to another worker or session, or the session to
        another run."""
        ...

    def get_run(self, run_id: str) -> HubRunRecord | None: ...

    def runs(self) -> list[HubRunRecord]:
        """All runs, oldest first."""
        ...

    def put_artifact(
        self,
        data: BinaryIO,
        *,
        run: HubRunRecord,
        key: str,
        role: ArtifactRole,
        build_id: str,
        name: str,
        sha256: str,
        content_type: str,
    ) -> tuple[ArtifactRecord, bool]:
        """Store the bytes (hash-checked) and their index row; returns the record and whether it
        is new. The same content again returns the stored record (a retried upload); other
        content under a stored key raises `BlobConflictError`."""
        ...

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        """A run's artifacts in upload order."""
        ...

    def open_artifact(self, record: ArtifactRecord) -> BinaryIO:
        """A streaming, hash-checked read."""
        ...

    def close(self) -> None: ...


class RepositoryEvents:
    """`HubEvents` over any `EventRepository` (SQLite in a single hub process): the source is
    kept in the stored event (`Event.source`); a run's events are its session's (v0, D99)."""

    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def add_many(self, batch: Sequence[tuple[Event, Source]]) -> list[int | None]:
        ids = []
        for event, source in batch:
            event.source = source
            ids.append(self.events.add(event))
        return ids

    def run_events(
        self, run: HubRunRecord, after: int = 0, limit: int = 1000
    ) -> list[tuple[int, Event]]:
        return self.events.events(run.session_id, after, limit)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES hub_runs (run_id),
    blob_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hub_artifacts_run ON hub_artifacts (run_id, created_at);
"""


class SqliteHubStore:
    """`HubStore` in one SQLite file plus a blob store (a single hub process; tests)."""

    def __init__(self, path: Path, blobs: BlobStore) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.blobs = blobs
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _row(self, sql: str, *args: str) -> HubRunRecord | None:
        row = self._db.execute(sql, args).fetchone()
        if row is None:
            return None
        return HubRunRecord(run_id=row[0], session_id=row[1], worker_id=row[2], created_at=row[3])

    def bind_run(
        self, run_id: str, session_id: str, worker_id: str, created_at: str
    ) -> HubRunRecord:
        select = "SELECT run_id, session_id, worker_id, created_at FROM hub_runs WHERE "
        with self._lock:
            run = self._row(select + "run_id = ?", run_id)
            if run is None:
                if self._row(select + "session_id = ?", session_id) is not None:
                    raise RunOwnershipError("the session belongs to another run")
                self._db.execute(
                    "INSERT INTO hub_runs (run_id, session_id, worker_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, session_id, worker_id, created_at),
                )
                return HubRunRecord(
                    run_id=run_id, session_id=session_id, worker_id=worker_id, created_at=created_at
                )
        if run.worker_id != worker_id:
            raise RunOwnershipError("the run belongs to another worker")
        if run.session_id != session_id:
            raise RunOwnershipError("the run belongs to another session")
        return run

    def get_run(self, run_id: str) -> HubRunRecord | None:
        with self._lock:
            return self._row(
                "SELECT run_id, session_id, worker_id, created_at FROM hub_runs WHERE run_id = ?",
                run_id,
            )

    def runs(self) -> list[HubRunRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, session_id, worker_id, created_at FROM hub_runs "
                "ORDER BY created_at, run_id"
            ).fetchall()
        return [
            HubRunRecord(run_id=r[0], session_id=r[1], worker_id=r[2], created_at=r[3])
            for r in rows
        ]

    def put_artifact(
        self,
        data: BinaryIO,
        *,
        run: HubRunRecord,
        key: str,
        role: ArtifactRole,
        build_id: str,
        name: str,
        sha256: str,
        content_type: str,
    ) -> tuple[ArtifactRecord, bool]:
        info = self.blobs.put(
            key, data, sha256=sha256, content_type=content_type, retention_class="standard"
        )
        record = ArtifactRecord(
            id="art-" + hashlib.sha256(key.encode()).hexdigest()[:20],
            run_id=run.run_id,
            session_id=run.session_id,
            build_id=build_id,
            role=role,
            name=name,
            blob_key=key,
            content_type=info.content_type,
            sha256=info.sha256,
            bytes=info.bytes,
            created_at=info.created_at,
        )
        with self._lock:
            row = self._db.execute(
                "SELECT record FROM hub_artifacts WHERE blob_key = ?", (key,)
            ).fetchone()
            if row is not None:  # the blob store refused other content under this key already
                return ArtifactRecord.model_validate_json(row[0]), False
            self._db.execute(
                "INSERT INTO hub_artifacts (id, run_id, blob_key, sha256, created_at, record) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    run.run_id,
                    key,
                    info.sha256,
                    record.created_at,
                    record.model_dump_json(),
                ),
            )
        return record, True

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT record FROM hub_artifacts WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [ArtifactRecord.model_validate_json(r[0]) for r in rows]

    def open_artifact(self, record: ArtifactRecord) -> BinaryIO:
        return self.blobs.open(record.blob_key)

    def close(self) -> None:
        with self._lock:
            self._db.close()
