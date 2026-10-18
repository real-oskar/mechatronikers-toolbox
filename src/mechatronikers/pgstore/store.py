"""The hub's central store in one object: the repositories and the artifacts of one
organisation on one Postgres database and one blob store (D72, docs/13 "Central store")."""

from pathlib import Path
from typing import Any

from psycopg import errors

from mechatronikers.pgstore.artifacts import PgArtifactStore
from mechatronikers.pgstore.db import DEFAULT_ORG, Connection, Pool, StoreConfigError, connect
from mechatronikers.pgstore.migrate import migrate
from mechatronikers.pgstore.repositories import (
    PgEventRepository,
    PgReviewRepository,
    PgRunRepository,
)
from mechatronikers.pgstore.values import check_id, is_id
from mechatronikers.storage.blobs import BlobStore, LocalBlobStore


def events_id_cache(conn: Connection) -> int:
    """The cache size of the `events.id` sequence (must stay 1, migration 0002)."""
    try:
        row = conn.execute(
            "SELECT seqcache FROM pg_sequence "
            "WHERE seqrelid = pg_get_serial_sequence('events', 'id')::regclass"
        ).fetchone()
    except errors.UndefinedTable:
        row = None
    if row is None:
        raise StoreConfigError("the events table is missing: run the migrations")
    return int(row["seqcache"])


class PgStore:
    def __init__(self, pool: Pool, blobs: BlobStore, *, org_id: str = DEFAULT_ORG) -> None:
        self.pool = pool
        self.org_id = check_id(org_id, "org id")
        self.events = PgEventRepository(pool, org_id=org_id)
        self.runs = PgRunRepository(pool, org_id=org_id)
        self.artifacts = PgArtifactStore(pool, blobs, org_id=org_id)

    @classmethod
    def open(
        cls,
        blob_root: Path,
        dsn: str | None = None,
        *,
        org_id: str = DEFAULT_ORG,
        apply_migrations: bool = True,
        **pool_options: Any,
    ) -> "PgStore":
        """The store at `dsn` (default `MT_PG_DSN`) with blobs below `blob_root`; applies
        pending migrations first unless told not to (they are idempotent and locked), and
        refuses a database whose event ids could commit out of order. `pool_options` go to
        `Pool` (max_size, timeout, lock_timeout, statement_timeout)."""
        # migrations on their own connection: no pool timeouts while another hub migrates
        with connect(dsn) as conn:
            if apply_migrations:
                migrate(conn)
            cache = events_id_cache(conn)
        if cache != 1:
            raise StoreConfigError(
                f"events.id has a sequence cache of {cache}; paging by id needs CACHE 1 "
                "(migration 0002)"
            )
        pool = Pool(dsn, **pool_options)
        try:
            return cls(pool, LocalBlobStore(blob_root), org_id=org_id)
        except BaseException:
            pool.close()
            raise

    def has_run(self, run_id: str) -> bool:
        if not is_id(run_id):
            return False
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE org_id = %s AND id = %s", (self.org_id, run_id)
            ).fetchone()
        return row is not None

    def reviews(self, run_id: str) -> PgReviewRepository:
        """The review records of one run of this organisation; KeyError for an unknown run."""
        if not self.has_run(run_id):
            raise KeyError(f"unknown run: {run_id}")
        return PgReviewRepository(self.pool, run_id, org_id=self.org_id)

    def close(self) -> None:
        self.pool.close()
