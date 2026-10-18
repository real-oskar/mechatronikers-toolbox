"""Forwarder: pushes a sandbox host's events and artifacts to the hub (docs/13 §1, D79).

Runs inside `session watch --hub URL` (opt-in), next to the collector: the hub never connects into
a host, the host dials out. The local event store is the buffer, so a hub outage loses nothing:

- **Cursor per session** (`EventRepository` cursors, key `hub:<url>:<session_id>`): the last
  ingest id the hub has acknowledged. It moves only after the hub stored a batch. A restart
  resends at most the last unacknowledged batch, and the hub drops what it has by
  `(emitter_id, seq)`.
- **Batches** hold at most `BATCH` lines and `MAX_REQUEST_BYTES` (well below the hub's 16 MiB).
  A line over the hub's per-line limit, or one the hub still refuses as too large (413, after the
  batch was halved down to it), is sent as a `log.truncated` placeholder with the same emitter
  and sequence number, so the cursor moves on and nothing is lost silently.
- **One session cannot block the others:** a session whose batch the hub refuses is reported and
  retried next round, the others go on; only an unreachable hub or a refused token stops the
  round. Rounds back off exponentially (up to `MAX_BACKOFF_S`) while the hub is down.
- **One hub run per session** (v0, one plan per sandbox, D99): `mirror_run_id(session_id)`. Every
  line of the session is filed under that session and run; the line's own ids are overwritten,
  never only filled (docs/13 §1).
- **Source per line (inferred, D99):** the collector stores no sandbox line under a host-only
  type (`agent.*`, `model.*`, `session.*`, `collector.*`, `log.*`) or under a host emitter id it
  has seen (D86), so an emitter with a host-only event in the session is taken as a host process
  (session manager, collector, agent or circuit driver); all other emitters are `container`.
- **No secrets leave the host:** a line that contains the session's gateway token or the worker's
  hub token is not sent (docs/13 §5); the host's provider keys (`*_API_KEY`, `*_SECRET`, …) are
  replaced by `[redacted]`. The hub refuses token-like lines as well.
- **Artifacts:** files announced by `artifact.created` (with a `format`) below the session's
  `/session/artifacts/` are copied out of a running sandbox, size-checked in the sandbox before the
  copy, and uploaded with their SHA-256; the hub derives the key. Copies that are not regular files
  are refused. A failed copy or a refused upload (other than a conflict) is retried with back-off,
  up to `ARTIFACT_ATTEMPTS` times, without holding up the events (in memory: a restart forgets).
"""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from mechatronikers.hub.ingest import MAX_LINE_BYTES
from mechatronikers.session.manager import ArtifactTooLargeError
from mechatronikers.session.store import SessionRecord
from mechatronikers.storage.repositories import EventRepository, RunRepository
from mechatronikers.telemetry.collector import HOST_ONLY_TYPES
from mechatronikers.telemetry.events import Event, Source

HUB_TOKEN_ENV = "MT_HUB_TOKEN"  # the worker token on a sandbox host
BATCH = 500
MAX_BATCHES = 20  # per session and round, so one long session does not starve the others
MAX_REQUEST_BYTES = 4 * 1024 * 1024  # the hub takes 16 MiB per request
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_BACKOFF_S = 60.0
ARTIFACT_ATTEMPTS = 5
REDACTED = "[redacted]"
_SECRET_NAME = re.compile(r"(API_KEY|_KEY|SECRET|PASSWORD|_TOKEN)$")
_ACTIVE = ("running", "awaiting_review")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
_EXPORTS = ("step-ap242", "step-ap214", "stl", "obj", "glb")

# (session_id, path below artifacts/, dest dir, max bytes) → the copied file; refuses larger files
CopyArtifact = Callable[[str, str, Path, int], Path]


class ForwardError(Exception):
    """The hub refused one session's batch; that session is retried next round."""


class HubUnavailableError(ForwardError):
    """The hub is unreachable, failing or refuses the token: the round stops and backs off."""


class _Item(NamedTuple):
    event: Event
    source: Source
    line: str
    stub: bool  # already the `log.truncated` placeholder


@dataclass
class _Pending:
    role: str
    attempts: int
    due: float


def provider_secrets(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of the host's secret-looking variables (provider API keys and the like)."""
    env = os.environ if environ is None else environ
    return sorted(
        {value for name, value in env.items() if _SECRET_NAME.search(name) and len(value) >= 16},
        key=len,
        reverse=True,
    )


def mirror_run_id(session_id: str) -> str:
    """The hub run that mirrors a mode 1 session (one plan per sandbox)."""
    if session_id.startswith("ses-") and _SEGMENT.match(session_id[4:]):
        return f"run-{session_id[4:]}"[:100]
    return "run-" + hashlib.sha256(session_id.encode()).hexdigest()[:16]


def artifact_role(payload: dict[str, Any]) -> str:
    """The hub's artifact role (docs/13 `artifacts.role`) for an `artifact.created` payload."""
    fmt = payload.get("format")
    if payload.get("role") == "sheet":
        return "sheet"
    if fmt in _EXPORTS:
        return "export"
    if fmt == "features-json":
        return "spec"
    if isinstance(fmt, str) and fmt.startswith("netlist"):
        return "netlist"
    return "report"


def artifact_path(session_id: str, payload: dict[str, Any]) -> str | None:
    """The file below the session's artifacts folder that an `artifact.created` names, or None
    for references that are not files to mirror (no format, another session, odd paths)."""
    uri, fmt = payload.get("uri"), payload.get("format")
    prefix = f"artifact://{session_id}/"
    if not isinstance(uri, str) or not isinstance(fmt, str) or not uri.startswith(prefix):
        return None
    parts = uri[len(prefix) :].split("/")
    if not 2 <= len(parts) <= 3 or not all(_SEGMENT.match(p) for p in parts):
        return None
    if not _SEGMENT.match("-".join(parts[:-1])):
        return None
    return "/".join(parts)


class HubForwarder:
    def __init__(
        self,
        events: EventRepository,
        sessions: RunRepository,
        hub_url: str,
        token: str,
        *,
        copy_artifact: CopyArtifact | None = None,
        client: httpx.Client | None = None,
        batch: int = BATCH,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        timeout_s: float = 30.0,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.sessions = sessions
        self.hub_url = hub_url.rstrip("/")
        self.copy_artifact = copy_artifact
        self.batch = batch
        self.max_request_bytes = max_request_bytes
        self._token = token
        # JSON-safe values are replaced inside the line; others drop the line (see `_line`)
        self._provider = [v for v in provider_secrets(environ) if v != token]
        self._clock = clock
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._client = client or httpx.Client(timeout=timeout_s)
        self._auth = {"Authorization": f"Bearer {token}"}
        self._host: dict[str, tuple[int, set[str]]] = {}  # session → (scanned up to, emitters)
        self._last_warning = ""

    # -- helpers ---------------------------------------------------------------------------

    def _warn(self, message: str) -> None:
        if message != self._last_warning:  # one line per new problem, not one per round
            print(f"hub forwarder: {message}"[:500], file=sys.stderr, flush=True)
            self._last_warning = message

    def cursor_key(self, session_id: str) -> str:
        return f"hub:{self.hub_url}:{session_id}"

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                self.hub_url + path,
                headers={**self._auth, **kwargs.pop("headers", {})},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise HubUnavailableError(
                f"hub not reachable at {self.hub_url}: {type(exc).__name__}"
            ) from None
        if response.status_code == 401:
            raise HubUnavailableError(f"the hub refused the worker token ({HUB_TOKEN_ENV})")
        if response.status_code >= 500:
            raise HubUnavailableError(f"hub error {response.status_code} on {path}")
        return response

    def check(self) -> str:
        """The worker name the hub knows this token by (raises ForwardError)."""
        response = self._request("GET", "/api/health")
        response.raise_for_status()
        return str(response.json().get("worker"))

    def _host_emitters(self, session_id: str, upto: int) -> set[str]:
        """Host emitters among the session's events up to ingest id `upto` (rebuilt after a
        restart by reading the local store; nothing is sent for it)."""
        scanned, emitters = self._host.get(session_id, (0, set()))
        while scanned < upto:
            rows = self.events.events(session_id, scanned, self.batch)
            rows = [(i, e) for i, e in rows if i <= upto]
            if not rows:
                break
            emitters |= {e.emitter_id for _, e in rows if e.emitter_id and _host_only(e)}
            scanned = rows[-1][0]
        self._host[session_id] = (max(scanned, upto), emitters)
        return emitters

    # -- forwarding ------------------------------------------------------------------------

    def forward_once(self) -> int:
        """One round over every local session; returns the number of lines the hub stored. A
        session the hub refuses is reported and skipped until the next round; an unreachable
        hub or a refused token (`HubUnavailableError`) ends the round."""
        stored = 0
        for record in self.sessions.all():
            try:
                stored += self.forward_session(record)
            except HubUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - one session must not stop the others
                self._warn(f"{record.id}: {type(exc).__name__}: {exc}")
        return stored

    def forward_session(self, record: SessionRecord) -> int:
        stored = 0
        for _ in range(MAX_BATCHES):
            sent, more = self._batch(record)
            stored += sent
            if not more:
                break
        self._retry_artifacts(record)
        return stored

    def _line(self, event: Event, source: Source, secrets: list[str]) -> _Item | None:
        """The NDJSON envelope of one event, or None if it must not leave the host."""
        text = event.to_json()
        if any(secret in text for secret in secrets):
            return None
        for value in self._provider:
            if value in text:
                if not _JSON_SAFE.match(value):
                    return None
                text = text.replace(value, REDACTED)
        line = json.dumps({"source": source, "event": json.loads(text)})
        size = len(line.encode())
        if size > MAX_LINE_BYTES:
            return _Item(event, source, self._placeholder(event, source, size), True)
        return _Item(event, source, line, False)

    @staticmethod
    def _placeholder(event: Event, source: Source, size: int) -> str:
        """What the hub gets for a line it cannot take: same emitter and sequence number, so
        the cursor moves on and a resend is a duplicate."""
        stub = Event(
            ts=event.ts,
            recv_ts=event.recv_ts,
            mono_ns=event.mono_ns,
            seq=event.seq,
            emitter_id=event.emitter_id,
            session_id=event.session_id,
            run_id=event.run_id,
            component=event.component,
            type="log.truncated",
            level="warning",
            payload={"original_type": event.type[:200], "bytes": size},
        )
        return json.dumps({"source": source, "event": json.loads(stub.to_json())})

    def _batch(self, record: SessionRecord) -> tuple[int, bool]:
        session_id = record.id
        key = self.cursor_key(session_id)
        cursor = self.events.get_cursor(key)
        after = int(cursor) if cursor and cursor.isdigit() else 0
        rows = self.events.events(session_id, after, self.batch)
        if not rows:
            return 0, False
        host = self._host_emitters(session_id, after)
        host |= {e.emitter_id for _, e in rows if e.emitter_id and _host_only(e)}
        run_id = mirror_run_id(session_id)
        # real gateway and hub tokens are long; a short test value would match everything
        secrets = [s for s in (record.token, self._token) if s and len(s) >= 16]
        items: list[_Item] = []
        size, last, full = 0, after, False
        for ingest_id, event in rows:
            item = None
            source: Source = "host" if event.emitter_id in host else "container"
            out = event.model_copy(
                update={"session_id": session_id, "run_id": run_id, "source": source}
            )
            if event.emitter_id:  # the hub refuses lines without an emitter
                item = self._line(out, source, secrets)
                if item is None:
                    self._warn(f"{session_id}: event {ingest_id} holds a secret; not sent")
            if item is not None:
                length = len(item.line.encode()) + 1
                if items and size + length > self.max_request_bytes:
                    full = True
                    break
                items.append(item)
                size += length
            last = ingest_id
        stored = self._send(session_id, items) if items else 0
        self._artifacts(record, [e for i, e in rows if i <= last])
        self.events.set_cursor(key, str(last))
        self._host[session_id] = (last, host)
        return stored, full or len(rows) == self.batch

    def _send(self, session_id: str, items: list[_Item]) -> int:
        """Post the lines; on 413 halve the batch, and a single line still too large goes as a
        placeholder. Returns the number of lines the hub stored."""
        body = ("\n".join(item.line for item in items) + "\n").encode()
        response = self._request(
            "POST", "/api/ingest", content=body, headers={"Content-Type": "application/x-ndjson"}
        )
        if response.status_code == 413:
            if len(items) > 1:
                half = len(items) // 2
                return self._send(session_id, items[:half]) + self._send(session_id, items[half:])
            item = items[0]
            if item.stub:
                raise ForwardError("the hub refuses even the placeholder (413)")
            stub = self._placeholder(item.event, item.source, len(item.line.encode()))
            return self._send(session_id, [_Item(item.event, item.source, stub, True)])
        if response.status_code != 200:
            raise ForwardError(f"ingest refused ({response.status_code}): {response.text[:200]}")
        result = response.json()
        for rejected in result.get("rejected", [])[:5]:
            self._warn(f"{session_id}: the hub refused a line ({rejected.get('reason')})")
        return int(result.get("stored", 0))

    def _artifacts(self, record: SessionRecord, events: list[Event]) -> None:
        paths: dict[str, str] = {}
        for event in events:
            if event.type != "artifact.created":
                continue
            path = artifact_path(record.id, event.payload)
            if path is None:
                continue
            claimed = event.payload.get("bytes")
            if isinstance(claimed, int) and claimed > MAX_ARTIFACT_BYTES:
                self._warn(f"{record.id}: {path} is larger than 256 MiB; not mirrored")
                continue
            paths[path] = artifact_role(event.payload)
        if not paths:
            return
        if self.copy_artifact is None or record.state not in _ACTIVE:
            self._warn(f"{record.id}: {len(paths)} artifacts not mirrored (no running sandbox)")
            return
        for path, role in paths.items():
            self._try_upload(record.id, path, role, attempts=0)

    def _retry_artifacts(self, record: SessionRecord) -> None:
        now = self._clock()
        for (session_id, path), pending in list(self._pending.items()):
            if session_id != record.id or pending.due > now:
                continue
            del self._pending[(session_id, path)]
            if record.state in _ACTIVE and self.copy_artifact is not None:
                try:
                    self._try_upload(session_id, path, pending.role, attempts=pending.attempts)
                except HubUnavailableError:
                    self._pending[(session_id, path)] = pending  # still queued; the round stops
                    raise

    def _try_upload(self, session_id: str, path: str, role: str, *, attempts: int) -> None:
        """Upload one file; a failure that may pass is retried later with back-off."""
        problem = self._upload(session_id, path, role)
        if problem is None:
            return
        attempts += 1
        if attempts >= ARTIFACT_ATTEMPTS:
            self._warn(f"{session_id}: giving up on {path} ({problem})")
            return
        delay = min(2.0 * 2**attempts, 300.0)
        self._pending[(session_id, path)] = _Pending(role, attempts, self._clock() + delay)
        self._warn(f"{session_id}: {path} not mirrored yet ({problem}); retrying")

    def _upload(self, session_id: str, path: str, role: str) -> str | None:
        """None when the hub has the file or must never get it; else why to retry."""
        assert self.copy_artifact is not None
        parts = path.split("/")
        with tempfile.TemporaryDirectory(prefix="mt-hub-") as tmp:
            try:
                copied = self.copy_artifact(session_id, path, Path(tmp), MAX_ARTIFACT_BYTES)
                info = copied.lstat()
            except ArtifactTooLargeError:
                self._warn(f"{session_id}: {path} is larger than 256 MiB; not mirrored")
                return None
            except Exception as exc:  # noqa: BLE001 - one missing file must not stop the session
                return f"cannot copy: {type(exc).__name__}"
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                self._warn(f"{session_id}: {path} is not a regular file of at most 256 MiB")
                return None
            digest = hashlib.sha256()
            with copied.open("rb") as f:
                while chunk := f.read(1 << 20):
                    digest.update(chunk)
            with copied.open("rb") as f:
                response = self._request(
                    "PUT",
                    f"/api/worker/runs/{mirror_run_id(session_id)}/artifacts",
                    params={"role": role, "build": "-".join(parts[:-1]), "name": parts[-1]},
                    content=f,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Sha256": digest.hexdigest(),
                    },
                )
        if response.status_code in (200, 201):
            return None
        if response.status_code == 409:  # another file under this name: retrying cannot help
            self._warn(f"{session_id}: the hub holds another file as {path}")
            return None
        return f"refused with {response.status_code}"

    def run(self, stop: threading.Event, interval_s: float = 2.0) -> None:
        """Forward until `stop` is set; problems are reported and retried, with an exponential
        back-off up to `MAX_BACKOFF_S` while the hub is unreachable."""
        delay = interval_s
        while not stop.is_set():
            try:
                self.forward_once()
                delay = interval_s
            except ForwardError as exc:
                self._warn(str(exc))
                delay = min(delay * 2, MAX_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 - the watcher keeps serving the host
                self._warn(f"{type(exc).__name__}: {exc}")
                delay = min(delay * 2, MAX_BACKOFF_S)
            stop.wait(delay)

    def close(self) -> None:
        self._client.close()


_JSON_SAFE = re.compile(r'^[^"\\\x00-\x1f]+$')


def _host_only(event: Event) -> bool:
    return event.type.startswith(HOST_ONLY_TYPES)
