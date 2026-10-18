"""The hub on the central Postgres store (#73, `pgstore.PgStore`): `HubEvents` and `HubStore`.

Events go through `PgEventRepository` (`add_many` with the line's source, `run_events`), artifacts
through `PgArtifactStore` (blob store + `artifacts` rows). Writes may use a store of their own (a
second, small pool), so polling readers (SSE, overview) never starve ingest of connections: the
pool does not hand out connections first come, first served (#122). The hub's own records are
`runs` rows (a mirrored run has no plan, D99) and a `workers` row per worker token. Needs the
`hub` extra.
"""

import threading
from collections.abc import Sequence
from typing import Any, BinaryIO

from mechatronikers.hub.records import ArtifactRecord, ArtifactRole, HubRunRecord, RunOwnershipError
from mechatronikers.pgstore.artifacts import ArtifactRecord as PgArtifactRecord
from mechatronikers.pgstore.repositories import INGEST_LOCK
from mechatronikers.pgstore.store import PgStore
from mechatronikers.pgstore.values import format_ts, lock_key, parse_ts
from mechatronikers.telemetry.events import Event, Source

_RUN_COLUMNS = "id, session_id, worker_id, created_at"


def _run(row: dict[str, Any]) -> HubRunRecord:
    return HubRunRecord(
        run_id=row["id"],
        session_id=row["session_id"],
        worker_id=row["worker_id"],
        created_at=format_ts(row["created_at"]),
    )


def _artifact(record: PgArtifactRecord) -> ArtifactRecord:
    return ArtifactRecord(
        id=record.id,
        run_id=record.run_id or "",
        session_id=record.session_id or "",
        build_id=record.build_id or "",
        role=record.role,  # the hub only writes its worker roles
        name=record.name,
        blob_key=record.key,
        content_type=record.content_type,
        sha256=record.sha256,
        bytes=record.bytes,
        created_at=record.created_at,
    )


class PgHubEvents:
    """`HubEvents` over `PgStore.events`: the source goes into `events.source` and the body."""

    def __init__(self, store: PgStore, writer: PgStore | None = None) -> None:
        self.events = store.events  # reads (run views, SSE, overview)
        self.writes = (writer or store).events  # ingest, on its own pool if given

    def add_many(self, batch: Sequence[tuple[Event, Source]]) -> list[int | None]:
        for event, source in batch:
            event.source = source
        return self.writes.add_many(batch)

    def run_events(
        self, run: HubRunRecord, after: int = 0, limit: int = 1000
    ) -> list[tuple[int, Event]]:
        return self.events.run_events(run.run_id, after, limit)


class PgHubStore:
    """`HubStore` over `runs`, `workers` and `PgStore.artifacts` of one organisation."""

    def __init__(self, store: PgStore, writer: PgStore | None = None) -> None:
        self.store = store  # reads
        self.writer = writer or store  # run binding and uploads, on their own pool if given
        self.org_id = store.org_id
        self._artifact_lock = threading.Lock()

    def bind_run(
        self, run_id: str, session_id: str, worker_id: str, created_at: str
    ) -> HubRunRecord:
        with self.writer.pool.connection() as conn, conn.transaction():
            # one binder per session and per run at a time, across hub processes (sorted: no
            # two binders wait on each other in a cycle)
            keys = {lock_key(self.org_id, "bind-session", session_id)}
            keys.add(lock_key(self.org_id, "bind-run", run_id))
            for key in sorted(keys):
                conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (INGEST_LOCK, key))
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE org_id = %s AND id = %s",  # noqa: S608
                (self.org_id, run_id),
            ).fetchone()
            if row is None:
                other = conn.execute(
                    "SELECT 1 FROM runs WHERE org_id = %s AND session_id = %s",
                    (self.org_id, session_id),
                ).fetchone()
                if other is not None:
                    raise RunOwnershipError("the session belongs to another run")
                conn.execute(
                    "INSERT INTO workers (id, org_id, name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (worker_id, self.org_id, worker_id),
                )
                started = parse_ts(created_at)
                row = conn.execute(
                    "INSERT INTO runs (id, org_id, session_id, worker_id, state, started_at,"
                    " created_at) VALUES (%s, %s, %s, %s, 'running', %s, COALESCE(%s, now()))"
                    f" RETURNING {_RUN_COLUMNS}",
                    (run_id, self.org_id, session_id, worker_id, started, started),
                ).fetchone()
                assert row is not None
                return _run(row)
        run = _run(row)
        if run.worker_id != worker_id:
            raise RunOwnershipError("the run belongs to another worker")
        if run.session_id != session_id:
            raise RunOwnershipError("the run belongs to another session")
        return run

    def get_run(self, run_id: str) -> HubRunRecord | None:
        with self.store.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE org_id = %s AND id = %s"  # noqa: S608
                " AND session_id IS NOT NULL AND worker_id IS NOT NULL",
                (self.org_id, run_id),
            ).fetchone()
        return None if row is None else _run(row)

    def runs(self) -> list[HubRunRecord]:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE org_id = %s"  # noqa: S608
                " AND session_id IS NOT NULL AND worker_id IS NOT NULL ORDER BY created_at, id",
                (self.org_id,),
            ).fetchall()
        return [_run(r) for r in rows]

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
        with self._artifact_lock:  # `new` is exact within this hub process
            return self._put(data, run, key, role, build_id, name, sha256, content_type)

    def _put(
        self,
        data: BinaryIO,
        run: HubRunRecord,
        key: str,
        role: ArtifactRole,
        build_id: str,
        name: str,
        sha256: str,
        content_type: str,
    ) -> tuple[ArtifactRecord, bool]:
        with self.writer.pool.connection() as conn:
            known = conn.execute(
                "SELECT 1 FROM artifacts WHERE org_id = %s AND blob_key = %s", (self.org_id, key)
            ).fetchone()
        record = self.writer.artifacts.put(
            data,
            key=key,
            role=role,
            name=name,
            sha256=sha256,
            content_type=content_type,
            run_id=run.run_id,
            session_id=run.session_id,
            build_id=build_id,
        )
        return _artifact(record), known is None

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return [_artifact(r) for r in self.store.artifacts.list(run_id=run_id)]

    def open_artifact(self, record: ArtifactRecord) -> BinaryIO:
        return self.store.artifacts.open(record.id)

    def close(self) -> None:
        self.store.close()
        if self.writer is not self.store:
            self.writer.close()
