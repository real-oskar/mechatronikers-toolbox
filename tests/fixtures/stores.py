"""Implementations under the storage contract tests (`tests/test_contract_*.py`).

Each entry is a factory: given a fresh temporary folder, a context manager that yields an empty
store and cleans it up afterwards. The Postgres implementations (#73) run only when `MT_PG_DSN`
is set (the CI `postgres` job); each test then gets a fresh, migrated schema that is dropped
afterwards, so the tests never touch other data in that database.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mechatronikers.pgstore import (
    DEFAULT_ORG,
    DSN_ENV,
    PgEventRepository,
    PgReviewRepository,
    PgRunRepository,
    Pool,
    connect,
    dsn_from_env,
    migrate,
)
from mechatronikers.review_app.store import JsonReviewRepository
from mechatronikers.session.store import SessionStore
from mechatronikers.storage import (
    BlobStore,
    EventRepository,
    LocalBlobStore,
    ReviewRepository,
    RunRepository,
)
from mechatronikers.telemetry.store import EventStore

Factory = Callable[[Path], AbstractContextManager[Any]]
NEEDS_PG = pytest.mark.skipif(
    dsn_from_env() is None, reason=f"set {DSN_ENV} to run the Postgres store tests"
)
REVIEW_RUN = "run-1"  # the run of the review contract tests' gates


@contextmanager
def sqlite_events(tmp: Path) -> Iterator[EventRepository]:
    store = EventStore(tmp / "state.db")
    try:
        yield store
    finally:
        store.close()


@contextmanager
def sqlite_runs(tmp: Path) -> Iterator[RunRepository]:
    # as in `session watch`: the session table lives in the event store's database
    events = EventStore(tmp / "state.db")
    try:
        yield SessionStore(events.connection())
    finally:
        events.close()


@contextmanager
def json_reviews(tmp: Path) -> Iterator[ReviewRepository]:
    yield JsonReviewRepository(tmp / "review")


@contextmanager
def local_blobs(tmp: Path) -> Iterator[BlobStore]:
    yield LocalBlobStore(tmp / "storage")


@contextmanager
def pg_schema() -> Iterator[str]:
    """DSN of a fresh, migrated schema in the `MT_PG_DSN` database, dropped afterwards."""
    dsn = dsn_from_env()
    if dsn is None:
        pytest.skip(f"set {DSN_ENV} to run the Postgres store tests")
    schema = f"mt_test_{uuid.uuid4().hex[:12]}"
    with connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        with connect(scoped) as conn:
            migrate(conn)
        yield scoped
    finally:
        with connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@contextmanager
def pg_pool() -> Iterator[Pool]:
    with pg_schema() as dsn:
        pool = Pool(dsn)
        try:
            yield pool
        finally:
            pool.close()


def add_run(pool: Pool, run_id: str, org_id: str = DEFAULT_ORG, **cols: Any) -> str:
    """A `runs` row (the hub creates these; review rows and artifacts reference them)."""
    values = {"id": run_id, "org_id": org_id, "state": "running"} | cols
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("INSERT INTO runs ({}) VALUES ({})").format(
                sql.SQL(", ").join(map(sql.Identifier, values)),
                sql.SQL(", ").join(sql.Placeholder() * len(values)),
            ),
            list(values.values()),
        )
    return run_id


@contextmanager
def pg_events(tmp: Path) -> Iterator[EventRepository]:
    with pg_pool() as pool:
        yield PgEventRepository(pool)


@contextmanager
def pg_runs(tmp: Path) -> Iterator[RunRepository]:
    with pg_pool() as pool:
        yield PgRunRepository(pool)


@contextmanager
def pg_reviews(tmp: Path) -> Iterator[ReviewRepository]:
    with pg_pool() as pool:
        yield PgReviewRepository(pool, add_run(pool, REVIEW_RUN))


EVENT_REPOSITORIES = [
    pytest.param(sqlite_events, id="sqlite"),
    pytest.param(pg_events, id="postgres", marks=NEEDS_PG),
]
RUN_REPOSITORIES = [
    pytest.param(sqlite_runs, id="sqlite"),
    pytest.param(pg_runs, id="postgres", marks=NEEDS_PG),
]
REVIEW_REPOSITORIES = [
    pytest.param(json_reviews, id="json"),
    pytest.param(pg_reviews, id="postgres", marks=NEEDS_PG),
]
BLOB_STORES = [pytest.param(local_blobs, id="local")]
# Mode 1 records carry the session's gateway token (the session manager needs it); hub records
# never do, because the token never leaves the worker host (docs/13 §5).
TOKENLESS_RUN_REPOSITORIES = {pg_runs}
