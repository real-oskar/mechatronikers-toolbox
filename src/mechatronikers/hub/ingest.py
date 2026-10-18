"""Hub ingest (docs/13 §1, D79, D86): events that a worker's forwarder pushes, one envelope per
NDJSON line: `{"source": "host" | "container", "event": <docs/08 event>}`.

Rules, per line (a rejected line is reported, never stored; the others of the request are):

- The event validates against the docs/08 schema and can be stored and read back; it names its
  emitter (`emitter_id`), session and run. `(emitter_id, seq)` is unique: a line sent twice is a
  duplicate, so a forwarder that resends after a restart stores nothing twice.
- **Own runs only:** the run and session must belong to the calling worker. In v0 (no queue yet)
  a worker's first line for a new run binds the run and its session to that worker; another
  worker, or another session for the same run, is refused. With #77 the claim binds them.
- **Provenance:** the hub stores the source the forwarder names; summaries, phases and the
  overview read state-driving events only from `host` lines (`hub.overview`). A container line may
  not use an emitter id that the hub has seen from a host or in another session (as the mode 1
  collector's guard, D86: ids seen by this process).
- **No secrets:** a line that holds a hub token or looks like it holds a gateway token (48 hex
  characters, a `?t=` link, the gateway cookie, a bearer header, a `"token"` key) is refused, so the
  hub never stores one (docs/13 §5).
"""

import re
import threading
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mechatronikers.hub.records import ID, HubEvents, HubStore, RunOwnershipError
from mechatronikers.telemetry.collector import _check_storable, _parse_ts
from mechatronikers.telemetry.events import EVENT_SCHEMA, Event, Source, utc_now

MAX_LINE_BYTES = 256 * 1024  # as the collector's limit for sandbox lines
MAX_LINES = 5000
MAX_BODY_BYTES = 16 * 1024 * 1024
HUB_EMITTER_PREFIX = "hub-"  # the hub's own events; no forwarded line may claim it
_GUARD_LIMIT = 200_000  # emitter ids the guard remembers before it starts over

_SECRET_PATTERNS = [
    # the session manager's gateway tokens are secrets.token_hex(24)
    (re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{48}(?![0-9A-Fa-f])"), "gateway_token"),
    (re.compile(r"[?&]t=[A-Za-z0-9_-]{16,}"), "token_link"),
    (re.compile(r"mt_token="), "token_cookie"),
    (re.compile(r"bearer\s+[A-Za-z0-9_.~+/=-]{16,}", re.IGNORECASE), "bearer_token"),
    (re.compile(r'"token"\s*:'), "token_field"),
]


class IngestLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Source
    event: dict[str, Any]


class Rejected(BaseModel):
    line: int  # 0-based index in the request body
    reason: str


class IngestResult(BaseModel):
    stored: int = 0
    duplicates: int = 0
    rejected: list[Rejected] = Field(default_factory=list)


def find_secret(text: str, known: tuple[str, ...] = ()) -> str | None:
    """Why `text` must not be stored (a secret's kind), or None."""
    if any(secret and secret in text for secret in known):
        return "hub_token"
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return None


class _RejectedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmitterGuard:
    """Which emitter ids came from a host and which from which session's containers."""

    def __init__(self) -> None:
        self.host: set[str] = set()
        self.container: dict[str, str] = {}

    def problem(self, source: Source, emitter_id: str, session_id: str) -> str | None:
        if source == "host":
            return "emitter_conflict" if emitter_id in self.container else None
        if emitter_id in self.host:
            return "emitter_conflict"
        seen = self.container.get(emitter_id)
        return "emitter_conflict" if seen not in (None, session_id) else None

    def note(self, source: Source, emitter_id: str, session_id: str) -> None:
        if len(self.host) + len(self.container) > _GUARD_LIMIT:
            self.container.clear()  # bounded memory; the guard degrades, ingest goes on
        if source == "host":
            self.host.add(emitter_id)
        else:
            self.container[emitter_id] = session_id


class HubEmitter:
    """The hub's own events (e.g. `artifact.uploaded`): a trusted host source (docs/08)."""

    def __init__(self, events: HubEvents) -> None:
        self.events = events
        self.emitter_id = f"{HUB_EMITTER_PREFIX}{uuid.uuid4().hex[:12]}"
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, session_id: str, run_id: str, type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            now = utc_now()
            event = Event(
                ts=now,
                recv_ts=now,
                mono_ns=0,
                seq=self._seq,
                emitter_id=self.emitter_id,
                session_id=session_id,
                run_id=run_id,
                component="hub",
                type=type,
                payload=payload,
            )
            self.events.add_many([(event, "host")])


class Ingestor:
    """Applies the rules above and stores a request's accepted lines in one batch; one request
    at a time per hub process (the Postgres store also orders writers per session and run)."""

    def __init__(
        self,
        events: HubEvents,
        runs: HubStore,
        *,
        secrets: tuple[str, ...] = (),
        on_stored: Callable[[str], None] = lambda run_id: None,
    ) -> None:
        self.events = events
        self.runs = runs
        self.secrets = secrets
        self.on_stored = on_stored
        self.guard = EmitterGuard()
        self._lock = threading.Lock()

    def ingest(self, worker_id: str, body: bytes) -> IngestResult:
        result = IngestResult()
        lines = [line for line in body.split(b"\n") if line.strip()]
        if len(lines) > MAX_LINES:
            raise ValueError(f"at most {MAX_LINES} lines per request")
        with self._lock:
            accepted: list[tuple[int, Event, Source]] = []
            for index, raw in enumerate(lines):
                try:
                    event, source = self._line(worker_id, raw)
                except _RejectedError as rejected:
                    result.rejected.append(Rejected(line=index, reason=rejected.reason))
                    continue
                accepted.append((index, event, source))
            for (_, event, _), ingest_id in zip(
                accepted, self._store(accepted, result), strict=True
            ):
                if ingest_id is False:
                    continue
                if ingest_id is None:
                    result.duplicates += 1
                else:
                    result.stored += 1
                    self.on_stored(event.run_id or "")
        result.rejected.sort(key=lambda r: r.line)
        return result

    def _store(
        self, accepted: list[tuple[int, Event, Source]], result: IngestResult
    ) -> list[int | None | Literal[False]]:
        """Ingest ids (None: duplicate, False: refused by the store) in one batch; if the store
        refuses the batch, line by line, so one odd line does not cost the others."""
        if not accepted:
            return []
        try:
            return list(self.events.add_many([(e, s) for _, e, s in accepted]))
        except ValueError:
            pass
        ids: list[int | None | Literal[False]] = []
        for index, event, source in accepted:
            try:
                ids.append(self.events.add_many([(event, source)])[0])
            except ValueError:
                result.rejected.append(Rejected(line=index, reason="unstorable"))
                ids.append(False)
        return ids

    def _line(self, worker_id: str, raw: bytes) -> tuple[Event, Source]:
        if len(raw) > MAX_LINE_BYTES:
            raise _RejectedError("too_large")
        try:
            envelope = IngestLine.model_validate_json(raw)
        except ValidationError:
            raise _RejectedError("invalid_envelope") from None
        try:
            event = Event.model_validate(envelope.event)
            _check_storable(event)
            if event.recv_ts is not None:
                _parse_ts(event.recv_ts)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise _RejectedError("invalid_event") from None
        if event.schema_ != EVENT_SCHEMA:
            raise _RejectedError("unknown_schema")
        emitter = event.emitter_id
        if not emitter or len(emitter) > 100 or "\x00" in emitter:
            raise _RejectedError("missing_emitter")
        if emitter.startswith(HUB_EMITTER_PREFIX):
            raise _RejectedError("reserved_emitter")
        session_id, run_id = event.session_id, event.run_id
        if not session_id or not ID.match(session_id) or not run_id or not ID.match(run_id):
            raise _RejectedError("missing_run")
        secret = find_secret(event.to_json(), self.secrets)
        if secret is not None:
            raise _RejectedError(f"secret:{secret}")
        try:
            self.runs.bind_run(run_id, session_id, worker_id, utc_now())
        except RunOwnershipError:
            raise _RejectedError("not_your_run") from None
        problem = self.guard.problem(envelope.source, emitter, session_id)
        if problem is not None:
            raise _RejectedError(problem)
        self.guard.note(envelope.source, emitter, session_id)
        if event.recv_ts is None:
            event.recv_ts = utc_now()
        return event, envelope.source
