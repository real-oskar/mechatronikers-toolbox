"""Session records in the shared state database (docs/07).

`SessionStore` is the mode 1 `RunRepository` (`storage.repositories`): one plan per sandbox, so a
run is its session's record.
"""

import sqlite3
import threading
from typing import Literal

from pydantic import BaseModel

SessionState = Literal[
    "created",
    "running",
    "awaiting_review",
    "snapshotting",
    "shelved",
    "failed",
    "expired",
    "deleted",
]
RetentionClass = Literal["ephemeral", "standard", "audit"]
# How long a snapshot is kept after shelving (D21, D81).
RETENTION_DAYS: dict[RetentionClass, int] = {"ephemeral": 1, "standard": 30, "audit": 365}
Consent = Literal["training_allowed", "eval_only", "not_allowed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    port INTEGER,
    image TEXT NOT NULL,
    retention_class TEXT NOT NULL,
    consent TEXT NOT NULL,
    last_reminder_at TEXT,
    snapshot_id TEXT,
    expires_at TEXT
);
"""


class SessionRecord(BaseModel):
    id: str
    state: SessionState
    # gateway token: set by the session manager (mode 1, the column stays NOT NULL); hub records
    # carry none, because the token never leaves the worker host (docs/13)
    token: str | None = None
    created_at: str
    updated_at: str
    port: int | None = None
    image: str
    retention_class: RetentionClass = "standard"
    consent: Consent = "not_allowed"
    last_reminder_at: str | None = None
    snapshot_id: str | None = None
    expires_at: str | None = None


class SessionStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def save(self, record: SessionRecord) -> None:
        fields = list(SessionRecord.model_fields)
        values = [getattr(record, f) for f in fields]
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO sessions ({', '.join(fields)}) "  # noqa: S608 - fixed columns
                f"VALUES ({', '.join('?' * len(fields))})",
                values,
            )

    def get(self, session_id: str) -> SessionRecord:
        with self._lock:
            cur = self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            names = [d[0] for d in cur.description]
        if row is None:
            raise KeyError(f"unknown session: {session_id}")
        return SessionRecord(**dict(zip(names, row, strict=True)))

    def all(self, states: tuple[SessionState, ...] | None = None) -> list[SessionRecord]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM sessions ORDER BY created_at")
            rows = cur.fetchall()
            names = [d[0] for d in cur.description]
        records = [SessionRecord(**dict(zip(names, row, strict=True))) for row in rows]
        return [r for r in records if states is None or r.state in states]
