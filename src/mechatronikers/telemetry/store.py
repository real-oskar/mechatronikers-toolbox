"""SQLite event store (docs/08). Ingest order is the global order; (emitter_id, seq) is unique.

The mode 1 `EventRepository` (`storage.repositories`); the hub implements it with Postgres.
"""

import sqlite3
import threading
from pathlib import Path

from mechatronikers.telemetry.events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    emitter_id TEXT,
    seq INTEGER NOT NULL,
    component TEXT NOT NULL,
    type TEXT NOT NULL,
    level TEXT NOT NULL,
    ts TEXT NOT NULL,
    recv_ts TEXT,
    body TEXT NOT NULL,
    UNIQUE (emitter_id, seq)
);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, id);
CREATE TABLE IF NOT EXISTS log_cursors (
    source TEXT PRIMARY KEY,
    last_ts TEXT NOT NULL
);
"""


class EventStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def add(self, event: Event) -> int | None:
        """Store an event; returns its ingest id, or None if it was already stored."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO events (session_id, emitter_id, seq, component, type, level,"
                " ts, recv_ts, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.session_id,
                    event.emitter_id,
                    event.seq,
                    event.component,
                    event.type,
                    event.level,
                    event.ts,
                    event.recv_ts,
                    event.to_json(),
                ),
            )
            return cur.lastrowid if cur.rowcount else None

    def last_seq(self, emitter_id: str) -> int | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(seq) FROM events WHERE emitter_id = ?", (emitter_id,)
            ).fetchone()
        return None if row[0] is None else int(row[0])

    def events(self, session_id: str, after: int = 0, limit: int = 1000) -> list[tuple[int, Event]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, body FROM events WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, after, limit),
            ).fetchall()
        return [(int(i), Event.model_validate_json(body)) for i, body in rows]

    def count(self, session_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row[0])

    def last_activity(self, session_id: str, ignore_types: tuple[str, ...] = ()) -> str | None:
        """Timestamp of the newest event, ignoring e.g. reminders themselves."""
        marks = ",".join("?" * len(ignore_types)) or "''"
        with self._lock:
            row = self._db.execute(
                f"SELECT MAX(ts) FROM events WHERE session_id = ? AND type NOT IN ({marks})",  # noqa: S608 - placeholders only
                (session_id, *ignore_types),
            ).fetchone()
        return None if row[0] is None else str(row[0])

    def get_cursor(self, source: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT last_ts FROM log_cursors WHERE source = ?", (source,)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_cursor(self, source: str, last_ts: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO log_cursors (source, last_ts) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_ts = excluded.last_ts",
                (source, last_ts),
            )

    def connection(self) -> sqlite3.Connection:
        return self._db

    def close(self) -> None:
        with self._lock:
            self._db.close()
