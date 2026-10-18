"""Numbered plain-SQL migrations of the central store (docs/13 "Central store", D72).

Files `migrations/NNNN_<name>.sql` run in version order (1..n, no gaps). Each file runs in its own
transaction together with its row in `schema_migrations`, so a failing file leaves nothing behind
and the next run starts at that file again; statements that cannot run in a transaction (e.g.
`CREATE INDEX CONCURRENTLY`) are therefore not allowed. Applied files are recorded with their
SHA-256: editing an applied file is an error, not a silent skip (add a new file instead). A
transaction-level advisory lock serialises runners, so two hub replicas starting at once apply
each file exactly once.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# pg_advisory_xact_lock key of the migration runner ("mtpgmigr"); no other code uses it.
LOCK_KEY = 0x6D74_7067_6D69_6772

_BOOTSTRAP = b"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    name: str
    applied_at: datetime | None


def load(root: Traversable | None = None) -> list[Migration]:
    """The migration files of `root` (default: the packaged `migrations/`) in version order."""
    folder = root if root is not None else resources.files("mechatronikers.pgstore") / "migrations"
    found: dict[int, Migration] = {}
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _FILE.match(entry.name)
        if match is None:
            raise MigrationError(f"migration files are named NNNN_name.sql, not {entry.name}")
        version = int(match[1])
        if version in found:
            raise MigrationError(f"two migration files have version {version:04d}")
        found[version] = Migration(version, match[2], entry.read_text(encoding="utf-8"))
    versions = sorted(found)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must run from 1 without gaps, got {versions}")
    return [found[v] for v in versions]


def _applied(conn: psycopg.Connection[Any]) -> dict[int, dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS present")
        row = cur.fetchone()
        if row is None or not row["present"]:
            return {}
        cur.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        return {int(r["version"]): r for r in cur.fetchall()}


def _check(applied: dict[int, dict[str, Any]], migrations: Sequence[Migration]) -> None:
    known = {m.version: m for m in migrations}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise MigrationError(
            f"the database has migrations this code does not know ({unknown}): it is newer"
        )
    changed = [
        f"{v:04d}_{known[v].name}" for v in applied if applied[v]["checksum"] != known[v].checksum
    ]
    if changed:
        raise MigrationError(
            f"applied migrations were edited afterwards: {', '.join(sorted(changed))}; "
            "add a new migration instead"
        )


def migrate(
    conn: psycopg.Connection[Any], migrations: Sequence[Migration] | None = None
) -> list[int]:
    """Apply the pending migrations; returns the versions this call applied ([] if none)."""
    todo = load() if migrations is None else list(migrations)
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("migrate needs a connection that is not inside a transaction")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(_BOOTSTRAP)
        _check(_applied(conn), todo)
    done: list[int] = []
    for migration in todo:
        with conn.transaction():
            # Re-read under the lock: another runner may have applied it meanwhile.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            applied = _applied(conn)
            if migration.version in applied:
                _check(applied, todo)
                continue
            # bytes: the file is trusted SQL from the package, run as one multi-statement query
            conn.execute(migration.sql.encode())
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
        done.append(migration.version)
    return done


def status(
    conn: psycopg.Connection[Any], migrations: Sequence[Migration] | None = None
) -> list[MigrationStatus]:
    """Every known migration with the time it was applied (None = pending)."""
    applied = _applied(conn)
    return [
        MigrationStatus(m.version, m.name, applied.get(m.version, {}).get("applied_at"))
        for m in (load() if migrations is None else migrations)
    ]
