"""Central Postgres store of the hosted hub (docs/13 "Central store", D72).

psycopg 3 with numbered plain-SQL migrations (`migrations/NNNN_name.sql`), and the Postgres
implementations of the `storage` repositories plus the artifact index (`PgStore` bundles them).
Needs the `hub` extra (`uv sync --extra hub`); the sandbox image does not include it. Mode 1 keeps
SQLite and `var/storage/`.
"""

from mechatronikers.pgstore.artifacts import (
    RETENTION_DAYS,
    ArtifactRecord,
    ArtifactRole,
    PgArtifactStore,
    check_artifact_key,
)
from mechatronikers.pgstore.db import (
    DEFAULT_ORG,
    DSN_ENV,
    Connection,
    Pool,
    PoolTimeoutError,
    StoreConfigError,
    connect,
    dsn_from_env,
    scrub,
)
from mechatronikers.pgstore.migrate import (
    Migration,
    MigrationError,
    MigrationStatus,
    load,
    migrate,
    status,
)
from mechatronikers.pgstore.repositories import (
    EventSource,
    PgEventRepository,
    PgReviewRepository,
    PgRunRepository,
)
from mechatronikers.pgstore.store import PgStore, events_id_cache

__all__ = [
    "DEFAULT_ORG",
    "DSN_ENV",
    "RETENTION_DAYS",
    "ArtifactRecord",
    "ArtifactRole",
    "Connection",
    "EventSource",
    "Migration",
    "MigrationError",
    "MigrationStatus",
    "PgArtifactStore",
    "PgEventRepository",
    "PgReviewRepository",
    "PgRunRepository",
    "PgStore",
    "Pool",
    "PoolTimeoutError",
    "StoreConfigError",
    "check_artifact_key",
    "connect",
    "dsn_from_env",
    "events_id_cache",
    "load",
    "migrate",
    "scrub",
    "status",
]
