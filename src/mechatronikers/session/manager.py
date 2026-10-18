"""Session manager: owns the sandbox lifecycle on the host (docs/07, D20–D22).

Layout per session (not a Podman pod: pod containers share one network namespace, which would
give the session container the gateway's outbound access):

    mt-<id>-net      internal network, no outbound route
    mt-<id>-session  MCP servers + build runner (subprocess isolation), only on mt-<id>-net
    mt-<id>-gateway  Caddy, on mt-<id>-net and the default network, publishes 127.0.0.1:<port>;
                     on Linux it also mounts the collector socket directory read-only (D83)
    mt-<id>-data     volume mounted at /session
"""

import contextlib
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from mechatronikers.runner.namespaces import POLICY_ENV
from mechatronikers.session.connections import mcp_connections
from mechatronikers.session.gateway import (
    DEFAULT_COLLECTOR,
    GATEWAY_IMAGE,
    GATEWAY_PORT,
    GATEWAY_SECRET_ENV,
    GATEWAY_SOCKET_DIR,
    LOG_MASK,
    CollectorRoute,
    caddyfile,
    ensure_socket_dir,
    gateway_secret,
)
from mechatronikers.session.runtime import ContainerSpec, SandboxRuntime, split_log_line
from mechatronikers.session.snapshot import (
    SnapshotManifest,
    describe,
    verify,
    write_events,
    write_manifest,
    write_volume,
)
from mechatronikers.session.store import (
    RETENTION_DAYS,
    Consent,
    RetentionClass,
    SessionRecord,
    SessionState,
)
from mechatronikers.spec import SPEC_VERSION
from mechatronikers.storage.repositories import RunRepository
from mechatronikers.telemetry import Event
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.events import Level, utc_now

DEFAULT_IMAGE = "localhost/mechatronikers:dev"


def configured_image(override: str | None = None) -> str:
    """Sandbox image tag: explicit flag, else `MT_IMAGE`, else `DEFAULT_IMAGE`."""
    return override or os.environ.get("MT_IMAGE") or DEFAULT_IMAGE


REMINDER_AFTER = timedelta(minutes=15)
HEALTH_TIMEOUT_S = 60.0
_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")  # as a blob key segment


class ArtifactTooLargeError(ValueError):
    """An artifact over the copy limit: refused before anything is copied out (the forwarder)."""


class ShelveNotConfirmedError(PermissionError):
    """Shelving stops the sandbox; it needs the user's explicit OK (D22)."""


class SessionInfo(BaseModel):
    session_id: str
    state: SessionState
    link: str | None
    mcp: dict[str, dict[str, Any]]


def _names(session_id: str) -> dict[str, str]:
    prefix = f"mt-{session_id}"
    return {
        "network": f"{prefix}-net",
        "volume": f"{prefix}-data",
        "session": f"{prefix}-session",
        "gateway": f"{prefix}-gateway",
    }


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def gateway_token(record: SessionRecord) -> str:
    """The session's gateway token. Every record of the session manager has one; only hub
    records (mode 2) carry none, because the token never leaves the worker host (docs/13)."""
    if record.token is None:
        raise ValueError(f"session {record.id} has no gateway token")
    return record.token


def _git_commit() -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return proc.stdout.strip() or None


class SessionManager:
    def __init__(
        self,
        runtime: SandboxRuntime,
        collector: Collector,
        sessions: RunRepository,
        storage_dir: Path,
        *,
        image: str = DEFAULT_IMAGE,
        health_check: Callable[[str, str], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        collector_route: CollectorRoute | None = None,
    ) -> None:
        self.runtime = runtime
        self.collector = collector
        self.sessions = sessions
        self.storage_dir = storage_dir
        self.image = image
        # how the gateway container reaches the host collector (D61, #53)
        self.collector_route = collector_route or CollectorRoute(DEFAULT_COLLECTOR)
        self._health_check = health_check or self._http_health
        self._clock = clock
        self._seq = 0
        self._emitter_id = f"session-manager-{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()

    # -- events --------------------------------------------------------------------------

    def _emit(
        self,
        session_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        level: Level = "info",
    ) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self.collector.ingest(
            Event(
                ts=utc_now(),
                mono_ns=time.monotonic_ns(),
                seq=seq,
                emitter_id=self._emitter_id,
                session_id=session_id,
                component="session-manager",
                type=type,
                level=level,
                payload=payload or {},
            )
        )

    def _transition(self, record: SessionRecord, state: SessionState) -> None:
        record.state = state
        record.updated_at = utc_now()
        self.sessions.save(record)

    # -- lifecycle -----------------------------------------------------------------------

    @staticmethod
    def _http_health(base_url: str, token: str) -> bool:
        try:
            r = httpx.get(
                f"{base_url}/health", headers={"Authorization": f"Bearer {token}"}, timeout=2
            )
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    def info(self, session_id: str) -> SessionInfo:
        record = self.sessions.get(session_id)
        active = record.state in ("running", "awaiting_review") and record.port
        base = f"http://127.0.0.1:{record.port}"
        return SessionInfo(
            session_id=record.id,
            state=record.state,
            link=f"{base}/?t={gateway_token(record)}" if active else None,
            mcp=mcp_connections(base, gateway_token(record)) if active else {},
        )

    def gateway_access(self, session_id: str) -> tuple[str, str]:
        """Base URL and token of an active session's gateway (host-side callers only)."""
        record = self.sessions.get(session_id)
        if record.state not in ("running", "awaiting_review") or not record.port:
            raise ValueError(f"session {session_id} is not running ({record.state})")
        return f"http://127.0.0.1:{record.port}", gateway_token(record)

    def start(
        self,
        drawing: Path | None = None,
        *,
        retention_class: RetentionClass = "standard",
        consent: Consent = "not_allowed",
    ) -> SessionInfo:
        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        record = SessionRecord(
            id=session_id,
            state="created",
            token=secrets.token_hex(24),
            created_at=now,
            updated_at=now,
            image=self.image,
            retention_class=retention_class,
            consent=consent,
        )
        self.sessions.save(record)
        self._emit(
            session_id, "session.created", {"image": self.image, "retention_class": retention_class}
        )
        names = _names(session_id)
        labels = {"mt.session": session_id}
        try:
            self.runtime.create_network(names["network"], internal=True, labels=labels)
            self.runtime.create_volume(names["volume"], labels)
            self.runtime.create_container(
                ContainerSpec(
                    name=names["session"],
                    image=self.image,
                    networks=[names["network"]],
                    labels={**labels, "mt.role": "session"},
                    env={
                        "MT_SESSION_ID": session_id,
                        "MT_RUNNER_ISOLATION": "subprocess",
                        # Builds fall back to running without namespaces, with a warning that
                        # the run view and every gate card show; the host may require them (D95)
                        POLICY_ENV: os.environ.get(POLICY_ENV) or "auto",
                        GATEWAY_SECRET_ENV: gateway_secret(gateway_token(record)),
                    },
                    volumes={names["volume"]: "/session"},
                    memory="4g",
                    cpus=2,
                )
            )
            self.runtime.start(names["session"])
            if drawing is not None:
                self.runtime.copy_into(names["session"], drawing, f"/session/input/{drawing.name}")
                self._emit(session_id, "session.input_added", {"file": drawing.name})
            socket_dir = self.collector_route.socket_dir
            if socket_dir is not None:
                # Exists before `session watch` binds the socket in it; only this user may enter.
                ensure_socket_dir(socket_dir)
            self.runtime.create_container(
                ContainerSpec(
                    name=names["gateway"],
                    image=GATEWAY_IMAGE,
                    networks=[names["network"], "podman"],
                    labels={**labels, "mt.role": "gateway"},
                    publish=[f"127.0.0.1::{GATEWAY_PORT}"],
                    binds={socket_dir: GATEWAY_SOCKET_DIR} if socket_dir else {},
                    # SELinux: a confined container (container_t) may not connect to the user's
                    # own socket, and relabelling does not change that; only the gateway runs
                    # unlabelled, and only on the socket route (D83).
                    security_opt=["label=disable"] if socket_dir else [],
                    tmpfs=["/tmp:rw,size=16m", "/data:rw,size=16m", "/config:rw,size=16m"],
                    memory="256m",
                    cpus=0.5,
                    # The caddy binary carries this file capability; without it exec is refused.
                    cap_add=["NET_BIND_SERVICE"],
                    read_only=False,  # the Caddyfile is copied in before start
                )
            )
            with tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "Caddyfile"
                config.write_text(
                    caddyfile(
                        gateway_token(record),
                        f"{names['session']}:8000",
                        session_id,
                        self.collector_route.upstream,
                    )
                )
                self.runtime.copy_into(names["gateway"], config, "/etc/caddy/Caddyfile")
            self.runtime.start(names["gateway"])
            record.port = self.runtime.host_port(names["gateway"], GATEWAY_PORT)
            self._wait_healthy(f"http://127.0.0.1:{record.port}", gateway_token(record))
        except Exception as exc:
            self._transition(record, "failed")
            with contextlib.suppress(Exception):
                self.collect(session_id)  # keep the containers' output for diagnosis
            self._emit(session_id, "session.failed", {"error": str(exc)}, "error")
            self._teardown(session_id)
            raise
        self._transition(record, "running")
        info = self.info(session_id)
        # The link carries the gateway token: it goes to the user, never into an event, because
        # events leave the host (mode 1 history, the hub in mode 2; docs/13 §5).
        self._emit(session_id, "session.started", {"port": record.port})
        return info

    def _wait_healthy(self, base_url: str, token: str) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._health_check(base_url, token):
                return
            time.sleep(0.5)
        raise TimeoutError(f"session not healthy after {HEALTH_TIMEOUT_S:g} s")

    def _teardown(self, session_id: str) -> None:
        names = _names(session_id)
        for action, name in (
            (self.runtime.remove_container, names["gateway"]),
            (self.runtime.remove_container, names["session"]),
            (self.runtime.remove_network, names["network"]),
            (self.runtime.remove_volume, names["volume"]),
        ):
            # Best effort: parts may not exist after a failed start.
            with contextlib.suppress(Exception):
                action(name)

    def copy_artifacts(self, session_id: str, build_id: str, dest: Path) -> Path:
        """Copy one build's artifacts out of a running session (host side)."""
        if "/" in build_id or ".." in build_id:
            raise ValueError(f"invalid build id {build_id!r}")
        self.runtime.copy_from(
            _names(session_id)["session"], f"/session/artifacts/{build_id}/.", dest
        )
        self._emit(session_id, "artifact.copied_out", {"build_id": build_id, "dest": str(dest)})
        return dest

    def copy_artifact(self, session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        """Copy one file below `/session/artifacts/` of a running session into the directory
        `dest` (the hub forwarder). `path` comes from sandbox events: plain segments only. The
        size is checked inside the sandbox first, so a huge file is never copied to the host."""
        parts = path.split("/")
        if not 1 < len(parts) <= 4 or not all(_SEGMENT.match(p) for p in parts):
            raise ValueError(f"invalid artifact path {path[:200]!r}")
        container, source = _names(session_id)["session"], f"/session/artifacts/{path}"
        if self.runtime.file_size(container, source) > max_bytes:
            raise ArtifactTooLargeError(f"{path} is larger than {max_bytes} bytes")
        self.runtime.copy_from(container, source, dest)
        return dest / parts[-1]

    # -- logs ----------------------------------------------------------------------------

    def collect(self, session_id: str) -> int:
        """Ingest all container output not yet stored. Returns the number of new events."""
        stored = 0
        token = self.sessions.get(session_id).token
        for role in ("session", "gateway"):
            container = _names(session_id)[role]
            cursor_key = f"container:{container}"
            cursor = self.collector.store.get_cursor(cursor_key)
            since = int(cursor) if cursor else None
            newest = since
            try:
                lines = list(self.runtime.logs(container, since))
            except Exception:
                continue
            for line in lines:
                ts_ns, text = split_log_line(line)
                if since is not None and ts_ns is not None and ts_ns <= since:
                    continue
                # The cursor moves past every line, so a poison line is read once, never forever.
                if ts_ns is not None and (newest is None or ts_ns > newest):
                    newest = ts_ns
                if self._ingest(text, role, session_id, ts_ns, token) is not None:
                    stored += 1
            if newest is not None:
                self.collector.store.set_cursor(cursor_key, str(newest))
        return stored

    def _ingest(
        self, text: str, role: str, session_id: str, ts_ns: int | None, token: str | None
    ) -> int | None:
        """One container line into the collector; a line that still fails is reported, skipped.

        The gateway already masks `?t=` in its log lines; the token is removed here as well, so no
        container output can carry it into an event (events leave the host, docs/13 §5, #116)."""
        if token:
            text = text.replace(token, LOG_MASK)
        try:
            return self.collector.ingest_line(
                text, source=role, session_id=session_id, observed_ns=ts_ns
            )
        except Exception as exc:  # noqa: BLE001 - never stop collecting for one line (#78)
            print(f"{session_id}: {role} line not stored: {exc!r}"[:300], file=sys.stderr)
            return None

    def follow(self, session_id: str, stop: threading.Event) -> list[threading.Thread]:
        """Stream both containers' output into the collector until `stop` is set."""
        self.collect(session_id)
        token = self.sessions.get(session_id).token
        threads = []
        for role in ("session", "gateway"):
            container = _names(session_id)[role]
            cursor = self.collector.store.get_cursor(f"container:{container}")

            def pump(
                container: str = container, role: str = role, cursor: str | None = cursor
            ) -> None:
                proc = self.runtime.follow_logs(container, int(cursor) if cursor else None)
                assert proc.stdout is not None
                for line in proc.stdout:
                    if stop.is_set():
                        break
                    ts_ns, text = split_log_line(line)
                    self._ingest(text, role, session_id, ts_ns, token)
                    if ts_ns is not None:
                        self.collector.store.set_cursor(f"container:{container}", str(ts_ns))
                proc.terminate()

            thread = threading.Thread(target=pump, daemon=True, name=f"follow-{container}")
            thread.start()
            threads.append(thread)
        return threads

    # -- review waits --------------------------------------------------------------------

    def awaiting_review(self, session_id: str, payload: dict[str, Any]) -> None:
        """The agent waits at a review gate: reminders now count as waiting for the user."""
        record = self.sessions.get(session_id)
        if record.state == "running":
            self._transition(record, "awaiting_review")
        self._emit(session_id, "session.awaiting_review", payload)

    def review_done(self, session_id: str, payload: dict[str, Any]) -> None:
        record = self.sessions.get(session_id)
        if record.state == "awaiting_review":
            self._transition(record, "running")
        self._emit(session_id, "session.resumed", payload)

    # -- reminders -----------------------------------------------------------------------

    def remind(self, reminder_after: timedelta = REMINDER_AFTER) -> list[str]:
        """Emit reminders for active sessions idle longer than `reminder_after`."""
        now = self._clock()
        reminded = []
        for record in self.sessions.all(("running", "awaiting_review")):
            last = self.collector.store.last_activity(record.id, ("session.reminder_sent",))
            idle_since = _parse(last or record.updated_at)
            last_reminder = _parse(record.last_reminder_at) if record.last_reminder_at else None
            if now - idle_since < reminder_after:
                continue
            if last_reminder and now - last_reminder < reminder_after:
                continue
            running_since = record.created_at
            self._emit(
                record.id,
                "session.reminder_sent",
                {
                    "running_since": running_since,
                    "idle_minutes": round((now - idle_since).total_seconds() / 60, 1),
                    "message": f"Sandbox {record.id} is still running (since {running_since}). "
                    "Shelve it when you are done.",
                },
            )
            record.last_reminder_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.sessions.save(record)
            reminded.append(record.id)
        return reminded

    # -- shelving ------------------------------------------------------------------------

    def shelve(
        self, session_id: str, *, confirmed: bool, reason: str = "user_ok"
    ) -> SnapshotManifest:
        """Snapshot the session, verify it, then stop and remove the sandbox."""
        if not confirmed:
            raise ShelveNotConfirmedError(f"shelving {session_id} needs the user's explicit OK")
        record = self.sessions.get(session_id)
        if record.state not in ("running", "awaiting_review", "failed"):
            raise ValueError(f"cannot shelve a session in state {record.state}")
        self.collect(session_id)
        self._transition(record, "snapshotting")
        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
        self._emit(
            session_id, "session.snapshot_started", {"snapshot_id": snapshot_id, "reason": reason}
        )

        directory = self.storage_dir / "snapshots" / session_id / snapshot_id
        directory.mkdir(parents=True)
        names = _names(session_id)
        write_volume(self.runtime, names["volume"], directory / "session.tar.zst")
        count, last_id, models = write_events(
            self.collector.store, session_id, directory / "events.ndjson.zst"
        )
        expires = self._clock() + timedelta(days=RETENTION_DAYS[record.retention_class])
        images = {"session": self.image, "gateway": GATEWAY_IMAGE}
        for role, image in list(images.items()):
            # Keep the tag if the digest is unavailable.
            with contextlib.suppress(Exception):
                images[role] = self.runtime.image_digest(image)
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            session_id=session_id,
            created_at=utc_now(),
            reason=reason,
            images=images,
            git_commit=_git_commit(),
            spec_version=SPEC_VERSION,
            models_used=models,
            files=describe(directory, ["session.tar.zst", "events.ndjson.zst"]),
            event_count=count,
            last_event_id=last_id,
            retention_class=record.retention_class,
            consent=record.consent,
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        write_manifest(directory, manifest)
        verify(directory)
        self._emit(
            session_id,
            "session.snapshot_completed",
            {"snapshot_id": snapshot_id, "path": str(directory), "expires_at": manifest.expires_at},
        )

        self._teardown(session_id)
        record.snapshot_id = snapshot_id
        record.expires_at = manifest.expires_at
        record.port = None
        self._transition(record, "shelved")
        self._emit(
            session_id,
            "session.shelved",
            {"snapshot_id": snapshot_id, "expires_at": manifest.expires_at},
        )
        self._emit(session_id, "session.stopped")
        self.collector.compress_archives(session_id)
        return manifest
