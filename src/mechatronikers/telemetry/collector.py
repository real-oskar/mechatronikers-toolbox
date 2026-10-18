"""Collector: turns container stdout lines into stored events (docs/08).

- Lines that are valid events are stored as they are (plus `recv_ts`).
- Any other line becomes a `log.line` event, so nothing is lost.
- Provenance (docs/08): only the host agent's lines (`source="agent"`) are trusted as they are. A
  line read from a sandbox belongs to the session being read, never has a host-only type and never
  claims a host emitter this collector has seen; a line that claims otherwise is stored as
  `log.line` next to a `log.provenance_mismatch` warning.
- No line can stop collection: an event that cannot be stored and read back (bad timestamp, seq
  out of range, unserialisable payload) becomes `log.line` next to a `log.invalid_event` warning.
- Sequence gaps and clock skew per emitter are reported as warning events.
- Every stored event is appended to a per-session NDJSON archive, compressed with zstd later.
"""

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard
from pydantic import ValidationError

from mechatronikers.storage.repositories import EventRepository
from mechatronikers.telemetry.events import Event, Level, utc_now

CLOCK_SKEW_WARN_S = 0.5
# Lines posted by the host agent (`POST /ingest` or its local fallback). Every other source is
# output read from a sandbox container.
TRUSTED_SOURCE = "agent"
# Types only host processes emit: the agent and the circuit driver (`agent.*`, `model.*`, which
# carry run status and cost), the session manager, the jobs adapter (`job.*`, #119) and the
# collector itself. A sandbox line is never stored as one of them. Other types stay valid from the
# sandbox for its own session in mode 1, e.g. the review app's `review.*` decisions (docs/08,
# Provenance).
HOST_ONLY_TYPES = ("agent.", "model.", "session.", "collector.", "log.", "job.")
MAX_LINE_CHARS = 4000  # `log.line` payloads are cut to this length
MAX_EVENT_CHARS = 256 * 1024  # longer sandbox lines are stored as (cut) log lines, not events
_INT64 = 2**63
_CLAIM_CHARS = 200


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _text(value: str) -> str:
    """Drop what cannot be stored as UTF-8 (lone surrogates from JSON escapes)."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _clip(value: str | None) -> str | None:
    """Claimed values are sandbox output: keep the warning small."""
    return None if value is None else _text(value[:_CLAIM_CHARS])


def _check_storable(event: Event) -> None:
    """Raise unless the event can be ordered, stored and read back (#78 review)."""
    if _parse_ts(event.ts).tzinfo is None:
        raise ValueError("ts has no time zone")
    if not 0 <= event.seq < _INT64:
        raise ValueError("seq out of range")
    Event.model_validate_json(event.to_json())


class Collector:
    def __init__(self, store: EventRepository, archive_dir: Path) -> None:
        self.store = store
        self.archive_dir = archive_dir
        self.emitter_id = f"collector-{uuid.uuid4().hex[:8]}"
        self._seq = 0
        self._skew_reported: set[str] = set()
        self._host_emitters: set[str] = {self.emitter_id}
        self._lock = threading.Lock()

    def _own_event(
        self, type: str, payload: dict[str, Any], session_id: str | None, level: Level = "info"
    ) -> Event:
        with self._lock:
            self._seq += 1
            seq = self._seq
        now = utc_now()
        return Event(
            ts=now,
            recv_ts=now,
            mono_ns=0,
            seq=seq,
            emitter_id=self.emitter_id,
            session_id=session_id,
            component="collector",
            type=type,
            level=level,
            payload=payload,
        )

    def ingest_line(
        self,
        line: str,
        *,
        source: str,
        session_id: str | None = None,
        observed_ns: int | None = None,
    ) -> int | None:
        """Store one output line. Returns the ingest id, or None for duplicates/blank lines.

        `source` is where the line was read: `"agent"` for the host agent, anything else (the
        container role) for sandbox output. `session_id` is the session the line belongs to: it
        fills a missing session id and, for sandbox output, is the only session the line can be
        filed under. `observed_ns` is when the runtime recorded the line (podman log timestamp).
        Clock skew is measured against it; without it, against the receive time (live only).
        """
        line = line.rstrip("\n")
        if not line.strip():
            return None
        try:
            return self._ingest_line(line, source, session_id, observed_ns)
        except Exception as exc:  # noqa: BLE001 - a poison line must never stop collection
            self._store(
                self._own_event(
                    "log.invalid_event",
                    {"source": source, "reason": _clip(f"{type(exc).__name__}: {exc}")},
                    session_id,
                    "warning",
                )
            )
            return self._log_line(line, source, session_id)

    def _ingest_line(
        self, line: str, source: str, session_id: str | None, observed_ns: int | None
    ) -> int | None:
        if source != TRUSTED_SOURCE and len(line) > MAX_EVENT_CHARS:
            return self._log_line(line, source, session_id)
        try:
            event = Event.model_validate(json.loads(line))
        except (ValueError, ValidationError, RecursionError):
            return self._log_line(line, source, session_id)
        if source == TRUSTED_SOURCE:
            if event.session_id is None:
                event.session_id = session_id
            _check_storable(event)
            return self.ingest(event, observed_ns=observed_ns)
        reason = self._provenance_problem(event, session_id)
        if reason is not None:
            self._store(
                self._own_event(
                    "log.provenance_mismatch",
                    {
                        "source": source,
                        "reason": reason,
                        "claimed_session_id": _clip(event.session_id),
                        "claimed_type": _clip(event.type),
                        "claimed_emitter_id": _clip(event.emitter_id),
                    },
                    session_id,
                    "warning",
                )
            )
            return self._log_line(line, source, session_id)
        if session_id is not None:
            event.session_id = session_id
        event.recv_ts = None  # set by the collector only
        event.source = None  # set by the hub only, from the forwarder's channel (docs/13 §1)
        _check_storable(event)
        return self._store(event, observed_ns=observed_ns)

    def _provenance_problem(self, event: Event, session_id: str | None) -> str | None:
        """Why a sandbox line must not be stored as the event it claims to be, if it must not."""
        if session_id is not None and event.session_id not in (None, session_id):
            return "session_mismatch"
        if event.type.startswith(HOST_ONLY_TYPES):
            return "host_only_type"
        if event.emitter_id in self._host_emitters:
            return "host_emitter"
        return None

    def _log_line(self, line: str, source: str, session_id: str | None) -> int | None:
        level: Level = "warning" if "Traceback" in line or "Error" in line else "info"
        payload: dict[str, Any] = {"source": source, "line": _text(line[:MAX_LINE_CHARS])}
        if len(line) > MAX_LINE_CHARS:
            payload["truncated"] = True
        return self._store(self._own_event("log.line", payload, session_id, level))

    def ingest(self, event: Event, *, observed_ns: int | None = None) -> int | None:
        """Store an event from a trusted host component (session manager, agent)."""
        if event.emitter_id:
            # A sandbox line claiming this emitter could take its sequence numbers (#78).
            self._host_emitters.add(event.emitter_id)
        return self._store(event, observed_ns=observed_ns)

    def _store(self, event: Event, *, observed_ns: int | None = None) -> int | None:
        if event.recv_ts is None:
            event.recv_ts = utc_now()
        if event.emitter_id and event.emitter_id != self.emitter_id:
            self._check_order(event)
            self._check_skew(event, observed_ns)
        ingest_id = self.store.add(event)
        if ingest_id is not None:
            self._archive(event)
        return ingest_id

    def _check_order(self, event: Event) -> None:
        assert event.emitter_id is not None
        last = self.store.last_seq(event.emitter_id)
        expected = 1 if last is None else last + 1
        if event.seq > expected:
            self._store(
                self._own_event(
                    "collector.seq_gap",
                    {
                        "emitter_id": event.emitter_id,
                        "component": event.component,
                        "expected": expected,
                        "got": event.seq,
                    },
                    event.session_id,
                    "warning",
                )
            )

    def _check_skew(self, event: Event, observed_ns: int | None) -> None:
        assert event.emitter_id is not None and event.recv_ts is not None
        if event.emitter_id in self._skew_reported:
            return
        if observed_ns is not None:
            observed = datetime.fromtimestamp(observed_ns / 1e9, UTC)
        else:
            observed = _parse_ts(event.recv_ts)
        skew = (observed - _parse_ts(event.ts)).total_seconds()
        if abs(skew) > CLOCK_SKEW_WARN_S:
            self._skew_reported.add(event.emitter_id)
            self._store(
                self._own_event(
                    "collector.clock_skew",
                    {"emitter_id": event.emitter_id, "component": event.component, "skew_s": skew},
                    event.session_id,
                    "warning",
                )
            )

    def _archive_path(self, event: Event) -> Path:
        day = (event.recv_ts or event.ts)[:10]
        return self.archive_dir / "logs" / (event.session_id or "_unscoped") / day / "events.ndjson"

    def _archive(self, event: Event) -> None:
        path = self._archive_path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a") as f:
            f.write(event.to_json() + "\n")

    def compress_archives(self, session_id: str) -> list[Path]:
        """zstd-compress the session's NDJSON archives (e.g. when the session is shelved)."""
        compressed = []
        with self._lock:
            for path in sorted((self.archive_dir / "logs" / session_id).glob("*/*.ndjson")):
                target = path.with_suffix(".ndjson.zst")
                part = 1
                while target.exists():  # a day may be compressed more than once
                    part += 1
                    target = path.with_name(f"events.{part}.ndjson.zst")
                target.write_bytes(zstandard.ZstdCompressor(level=10).compress(path.read_bytes()))
                path.unlink()
                compressed.append(target)
        return compressed
