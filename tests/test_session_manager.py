import io
import json
import stat
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import pytest
from starlette.testclient import TestClient

from mechatronikers.session.__main__ import watch_once
from mechatronikers.session.gateway import (
    CollectorRoute,
    caddyfile,
    check_socket_dir,
    collector_route,
)
from mechatronikers.session.manager import SessionManager, ShelveNotConfirmedError
from mechatronikers.session.runtime import (
    ContainerSpec,
    PodmanRuntime,
    SandboxError,
    split_log_line,
)
from mechatronikers.session.snapshot import SnapshotManifest, verify
from mechatronikers.session.store import SessionStore
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.collector_app import Shelve, create_collector_app
from mechatronikers.telemetry.store import EventStore


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.specs: dict[str, ContainerSpec] = {}
        self.log_lines: dict[str, list[str]] = {}
        self.copied: dict[str, str] = {}  # "<container>:<dest>" -> text of the copied file
        self.fail_on: str | None = None

    def _call(self, action: str, name: str) -> None:
        if action == self.fail_on:
            raise RuntimeError(f"{action} failed")
        self.calls.append((action, name))

    def create_network(self, name: str, *, internal: bool, labels: dict[str, str]) -> None:
        assert internal
        self._call("create_network", name)

    def remove_network(self, name: str) -> None:
        self._call("remove_network", name)

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self._call("create_volume", name)

    def remove_volume(self, name: str) -> None:
        self._call("remove_volume", name)

    def export_volume(self, name: str, target: IO[bytes]) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = b"drawing"
            info = tarfile.TarInfo("input/drawing.pdf")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        target.write(buf.getvalue())

    def create_container(self, spec: ContainerSpec) -> None:
        self.specs[spec.name] = spec
        self._call("create_container", spec.name)

    def copy_into(self, container: str, source: Path, dest: str) -> None:
        self._call("copy_into", f"{container}:{dest}")
        if source.suffix != ".pdf":
            self.copied[f"{container}:{dest}"] = source.read_text()

    def copy_from(self, container: str, source: str, dest: Path) -> None:
        self._call("copy_from", f"{container}:{source}")

    def start(self, container: str) -> None:
        self._call("start", container)

    def remove_container(self, container: str) -> None:
        self._call("remove_container", container)

    def host_port(self, container: str, container_port: int) -> int:
        return 40000

    def image_digest(self, image: str) -> str:
        return f"{image}@sha256:abc"

    def logs(self, container: str, since_ns: int | None = None) -> Iterator[str]:
        yield from self.log_lines.get(container, [])

    def follow_logs(self, container: str, since_ns: int | None = None) -> subprocess.Popen[str]:
        raise NotImplementedError


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[SessionManager, FakeRuntime]]:
    store = EventStore(tmp_path / "state.db")
    runtime = FakeRuntime()
    manager = SessionManager(
        runtime,
        Collector(store, tmp_path / "storage"),
        SessionStore(store.connection()),
        tmp_path / "storage",
        health_check=lambda base, token: True,
    )
    yield manager, runtime
    store.close()


def _types(manager: SessionManager, session_id: str) -> list[str]:
    return [e.type for _, e in manager.collector.store.events(session_id)]


def test_start_creates_isolated_session(
    setup: tuple[SessionManager, FakeRuntime], tmp_path: Path
) -> None:
    manager, runtime = setup
    drawing = tmp_path / "Fuss_dwg.pdf"
    drawing.write_bytes(b"%PDF")
    info = manager.start(drawing)
    sid = info.session_id
    assert info.state == "running"
    assert info.link is not None and info.link.startswith("http://127.0.0.1:40000/?t=")
    assert set(info.mcp) == {"drawing", "cad", "circuit"}
    session = runtime.specs[f"mt-{sid}-session"]
    assert session.networks == [f"mt-{sid}-net"]  # internal network only
    assert session.read_only and session.env["MT_RUNNER_ISOLATION"] == "subprocess"
    gateway = runtime.specs[f"mt-{sid}-gateway"]
    assert gateway.publish == ["127.0.0.1::8080"]
    assert gateway.binds == {} and session.binds == {}
    assert ("copy_into", f"mt-{sid}-session:/session/input/Fuss_dwg.pdf") in runtime.calls
    assert _types(manager, sid) == ["session.created", "session.input_added", "session.started"]


def test_failed_start_tears_down(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, runtime = setup
    runtime.fail_on = "start"
    with pytest.raises(RuntimeError):
        manager.start()
    (record,) = manager.sessions.all()
    assert record.state == "failed"
    assert "session.failed" in _types(manager, record.id)
    assert any(action == "remove_network" for action, _ in runtime.calls)


def test_collect_is_incremental(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, runtime = setup
    sid = manager.start().session_id
    buf = io.StringIO()
    emitter = EventEmitter("cad-mcp", session_id=sid, stream=buf)
    emitter.emit("tool.called")
    container = f"mt-{sid}-session"
    runtime.log_lines[container] = [
        f"2026-10-16T23:00:00.000000001+02:00 {buf.getvalue().strip()}",
        "2026-10-16T23:00:00.000000002+02:00 plain line",
    ]
    assert manager.collect(sid) == 2
    assert manager.collect(sid) == 0  # nothing new
    runtime.log_lines[container].append("2026-10-16T23:00:01.000000000+02:00 later line")
    assert manager.collect(sid) == 1


def test_container_output_stays_in_its_session(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, runtime = setup
    own, other = manager.start().session_id, manager.start().session_id
    buf = io.StringIO()
    EventEmitter("cad-mcp", session_id=other, stream=buf).emit("build.completed")
    runtime.log_lines[f"mt-{own}-session"] = [
        f"2026-10-16T23:00:00.000000001+02:00 {buf.getvalue().strip()}"
    ]
    manager.collect(own)
    assert _types(manager, other) == ["session.created", "session.started"]
    assert _types(manager, own)[-2:] == ["log.provenance_mismatch", "log.line"]


def test_reminders_respect_interval(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 16, 12, 0, tzinfo=UTC)]
    store = EventStore(tmp_path / "state.db")
    manager = SessionManager(
        FakeRuntime(),
        Collector(store, tmp_path / "storage"),
        SessionStore(store.connection()),
        tmp_path / "storage",
        health_check=lambda base, token: True,
        clock=lambda: now[0],
    )
    sid = manager.start().session_id
    now[0] = datetime.now(UTC) + timedelta(minutes=5)
    assert manager.remind(timedelta(minutes=15)) == []
    now[0] = datetime.now(UTC) + timedelta(minutes=20)
    assert manager.remind(timedelta(minutes=15)) == [sid]
    assert manager.remind(timedelta(minutes=15)) == []  # just reminded
    now[0] += timedelta(minutes=16)
    assert manager.remind(timedelta(minutes=15)) == [sid]
    store.close()


def test_shelve_needs_confirmation_and_writes_verified_snapshot(
    setup: tuple[SessionManager, FakeRuntime],
) -> None:
    manager, runtime = setup
    sid = manager.start().session_id
    with pytest.raises(ShelveNotConfirmedError):
        manager.shelve(sid, confirmed=False)
    assert manager.sessions.get(sid).state == "running"

    manifest = manager.shelve(sid, confirmed=True)
    directory = manager.storage_dir / "snapshots" / sid / manifest.snapshot_id
    assert verify(directory).snapshot_id == manifest.snapshot_id
    assert {f.path for f in manifest.files} == {"session.tar.zst", "events.ndjson.zst"}
    assert manifest.images["session"].endswith("@sha256:abc")
    assert manifest.event_count >= 3
    record = manager.sessions.get(sid)
    assert record.state == "shelved" and record.snapshot_id == manifest.snapshot_id
    assert ("remove_container", f"mt-{sid}-session") in runtime.calls
    assert ("remove_volume", f"mt-{sid}-data") in runtime.calls
    assert _types(manager, sid)[-4:] == [
        "session.snapshot_started",
        "session.snapshot_completed",
        "session.shelved",
        "session.stopped",
    ]
    assert manager.info(sid).link is None


def test_no_event_carries_the_gateway_token(tmp_path: Path) -> None:
    """Events leave the host (history API, snapshots, the hub in mode 2): the token never does."""
    now = [datetime.now(UTC)]
    store = EventStore(tmp_path / "state.db")
    manager = SessionManager(
        FakeRuntime(),
        Collector(store, tmp_path / "storage"),
        SessionStore(store.connection()),
        tmp_path / "storage",
        health_check=lambda base, token: True,
        clock=lambda: now[0],
    )
    drawing = tmp_path / "Fuss_dwg.pdf"
    drawing.write_bytes(b"%PDF")
    info = manager.start(drawing)
    sid, token = info.session_id, manager.sessions.get(info.session_id).token
    assert info.link is not None and token in info.link  # the user's link has it
    manager.awaiting_review(sid, {"gate_id": "gate-1", "kind": "spec_review"})
    manager.review_done(sid, {"gate_id": "gate-1", "action": "approve"})
    now[0] += timedelta(hours=1)
    assert manager.remind(timedelta(minutes=15)) == [sid]
    manager.shelve(sid, confirmed=True)
    events = manager.collector.store.events(sid)
    assert {"session.started", "session.reminder_sent", "session.shelved"} <= {
        e.type for _, e in events
    }
    assert all(token not in e.to_json() for _, e in events)
    store.close()


def test_tampered_snapshot_fails_verification(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, _ = setup
    sid = manager.start().session_id
    manifest = manager.shelve(sid, confirmed=True)
    directory = manager.storage_dir / "snapshots" / sid / manifest.snapshot_id
    (directory / "events.ndjson.zst").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        verify(directory)


def test_split_log_line_normalises_offsets() -> None:
    a, text = split_log_line("2026-10-16T23:00:00.123456789+02:00 hello world")
    b, _ = split_log_line("2026-10-16T21:00:00.123456789Z x")
    assert a == b and text == "hello world"
    assert split_log_line("no timestamp") == (None, "no timestamp")


def test_caddyfile_requires_alphanumeric_token() -> None:
    assert '"Bearer abc123"' in caddyfile("abc123", "upstream:8000", "ses-abc123")
    with pytest.raises(ValueError):
        caddyfile('x" || true', "upstream:8000", "ses-abc123")


def test_caddyfile_routes_events_to_its_own_session_only() -> None:
    config = caddyfile("abc123", "upstream:8000", "ses-0123abcd", "host.containers.internal:8765")
    assert "rewrite * /sessions/ses-0123abcd/stream?after=" in config
    assert "rewrite * /sessions/ses-0123abcd/events?after=" in config
    assert config.count("/sessions/") == 2  # no route to any other session
    assert "shelve" not in config  # a sandbox can never shelve itself, only the host table (#52)
    assert config.count("header_up -Authorization") == 2  # the token stays at the gateway
    assert config.index("handle /events/stream") < config.index('respond "unauthorized"')
    with pytest.raises(ValueError):
        caddyfile("abc123", "upstream:8000", "ses-../other")


def test_caddyfile_takes_the_token_out_of_the_page_url() -> None:
    """#116: a `?t=` navigation sets the cookie and lands on the same URL without `t`."""
    config = caddyfile("abc123", "upstream:8000", "ses-0123abcd")
    login = config[config.index("@login {") : config.index("handle /events/history")]
    for line in ("method GET HEAD", "query t=abc123", "header Sec-Fetch-Mode navigate"):
        assert line in login  # only browser navigations; agents (Bearer) and POSTs as before
    route = login[login.index("route {") :]  # `route` keeps this order (Caddy would sort it)
    steps = ("uri query -t", "uri path_regexp ^/+ /", "respond @cross_site", "redir {uri} 303")
    assert [route.index(step) for step in steps] == sorted(route.index(step) for step in steps)
    assert "@cross_site header Sec-Fetch-Site cross-site" in route
    assert 'header @cross_site Refresh "0; url={uri}"' in route  # Strict cookie: no 303 there
    assert "header Referrer-Policy no-referrer" in route
    assert "{uri}" not in route[route.index("respond") : route.index("redir")]  # static page
    # The cookie is still set on every `?t=` request (the handlers above run after it).
    assert config.index("header @first Set-Cookie") < config.index("handle @login")


def test_caddyfile_masks_the_token_in_gateway_log_lines() -> None:
    config = caddyfile("abc123", "upstream:8000", "ses-0123abcd")
    options = config[: config.index(":8080 {")]  # global: covers the error log of every handler
    assert "format filter {" in options and "wrap json" in options
    assert "request>uri query {\n\t\t\t\t\treplace t REDACTED\n" in options
    assert "request>headers>Referer delete" in options
    assert "abc123" not in options


def test_no_stored_event_carries_the_gateway_token(
    setup: tuple[SessionManager, FakeRuntime],
) -> None:
    """#116: even an unmasked gateway line (a 502 on a `?t=` reload) loses the token on the way
    into the store; the link with the token goes to the user only."""
    manager, runtime = setup
    info = manager.start()
    sid, token = info.session_id, manager.sessions.get(info.session_id).token
    assert token is not None and info.link is not None and token in info.link
    error = {
        "level": "error",
        "logger": "http.log.error",
        "msg": "dial tcp: lookup mt-x-session: no such host",
        "request": {
            "method": "GET",
            "uri": f"/?t={token}",
            "headers": {"Referer": [f"/?t={token}"]},
        },
        "status": 502,
    }
    runtime.log_lines[f"mt-{sid}-gateway"] = [
        f"2026-10-17T10:00:00.000000001+00:00 {json.dumps(error)}"
    ]
    runtime.log_lines[f"mt-{sid}-session"] = [
        f"2026-10-17T10:00:00.000000001+00:00 GET /circuit/?t={token} 307"
    ]
    assert manager.collect(sid) == 2
    manager.shelve(sid, confirmed=True)
    events = [e for _, e in manager.collector.store.events(sid)]
    assert {"session.started", "log.line", "session.shelved"} <= {e.type for e in events}
    assert all(token not in e.to_json() for e in events)
    lines = [e.to_json() for e in events if e.type == "log.line"]
    assert all("t=REDACTED" in line for line in lines) and len(lines) == 2


def test_caddyfile_proxies_events_to_the_collector_socket() -> None:
    config = caddyfile(
        "abc123", "upstream:8000", "ses-0123abcd", "unix//run/mt-collector/collector.sock"
    )
    assert config.count("reverse_proxy unix//run/mt-collector/collector.sock {") == 2
    for bad in ("x:1 {\n", "unix//run/a b", "unix/relative.sock", "collector"):
        with pytest.raises(ValueError):
            caddyfile("abc123", "upstream:8000", "ses-0123abcd", bad)


def test_collector_route_by_platform(tmp_path: Path) -> None:
    linux = collector_route(tmp_path, {}, "linux")
    assert linux.upstream == "unix//run/mt-collector/collector.sock"
    assert linux.socket == (tmp_path / "run" / "collector" / "collector.sock").absolute()
    assert collector_route(tmp_path, {}, "darwin") == CollectorRoute(
        "host.containers.internal:8765"
    )
    env = {"MT_COLLECTOR_TRANSPORT": "tcp"}
    assert collector_route(tmp_path, env, "linux").socket is None
    env = {"MT_COLLECTOR_TRANSPORT": "unix", "MT_COLLECTOR_SOCKET_DIR": str(tmp_path / "s")}
    assert collector_route(tmp_path, env, "darwin").socket_dir == (tmp_path / "s").absolute()
    with pytest.raises(ValueError):
        collector_route(tmp_path, {"MT_COLLECTOR_TRANSPORT": "udp"}, "linux")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_gateway_reaches_the_collector_per_platform(tmp_path: Path, platform: str) -> None:
    store = EventStore(tmp_path / "state.db")
    runtime = FakeRuntime()
    route = collector_route(tmp_path / "var", {}, platform)
    manager = SessionManager(
        runtime,
        Collector(store, tmp_path / "storage"),
        SessionStore(store.connection()),
        tmp_path / "storage",
        health_check=lambda base, token: True,
        collector_route=route,
    )
    sid = manager.start().session_id
    store.close()
    gateway = runtime.specs[f"mt-{sid}-gateway"]
    config = runtime.copied[f"mt-{sid}-gateway:/etc/caddy/Caddyfile"]
    assert config.count(f"reverse_proxy {route.upstream} {{") == 2
    session = runtime.specs[f"mt-{sid}-session"]
    assert session.binds == {}  # the sandbox never sees the collector
    assert session.security_opt == []  # and keeps its SELinux label
    if platform == "linux":
        assert route.socket_dir is not None
        assert gateway.binds == {route.socket_dir: "/run/mt-collector"}
        # container_t may not connect to the user's socket: only the gateway runs unlabelled
        assert gateway.security_opt == ["label=disable"]
        # ready for `session watch`, even if it starts after the session; only this user enters
        assert stat.S_IMODE(route.socket_dir.stat().st_mode) == 0o700
    else:
        assert gateway.binds == {} and gateway.security_opt == []
        assert "host.containers.internal:8765" in config


def test_socket_dir_must_be_private_and_hold_only_the_socket(
    setup: tuple[SessionManager, FakeRuntime], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, runtime = setup
    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    check_socket_dir(good)
    (tmp_path / "link").symlink_to(good)
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    crowded = tmp_path / "crowded"
    crowded.mkdir(mode=0o700)
    (crowded / "collector.sock").write_text("not a socket")
    for bad in (tmp_path / "link", loose, crowded):
        with pytest.raises(ValueError):
            check_socket_dir(bad)
        manager.collector_route = collector_route(
            tmp_path, {"MT_COLLECTOR_TRANSPORT": "unix", "MT_COLLECTOR_SOCKET_DIR": str(bad)}
        )
        with pytest.raises(ValueError):  # nothing is mounted from such a directory
            manager.start()
    assert not any(spec.binds for spec in runtime.specs.values())
    monkeypatch.setattr("os.getuid", lambda: 4242)
    with pytest.raises(ValueError, match="another user"):
        check_socket_dir(good)


def test_podman_mounts_binds_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PodmanRuntime("podman")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runtime, "_run", lambda *args, input=None: calls.append(args) or "")
    socket_dir = Path("/home/u/mt/var/run/collector")
    spec = ContainerSpec(
        "gw",
        "caddy",
        ["net"],
        binds={socket_dir: "/run/mt-collector"},
        security_opt=["label=disable"],
    )
    runtime.create_container(spec)
    args = list(calls[0])
    assert args[args.index("-v") + 1] == "/home/u/mt/var/run/collector:/run/mt-collector:ro"
    options = [args[i + 1] for i, arg in enumerate(args) if arg == "--security-opt"]
    assert options == ["no-new-privileges", "label=disable"]
    for bad in (Path("relative/dir"), Path("/odd:path")):
        with pytest.raises(SandboxError):
            runtime.create_container(ContainerSpec("gw", "caddy", ["net"], binds={bad: "/x"}))


def test_review_waits_change_the_session_state(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, _ = setup
    sid = manager.start().session_id
    manager.awaiting_review(sid, {"gate_id": "gate-1", "kind": "spec_review"})
    assert manager.sessions.get(sid).state == "awaiting_review"
    assert manager.info(sid).link is not None  # the reviewer still reaches the session
    assert manager.gateway_access(sid)[0] == "http://127.0.0.1:40000"
    manager.review_done(sid, {"gate_id": "gate-1", "action": "approve"})
    assert manager.sessions.get(sid).state == "running"
    assert _types(manager, sid)[-2:] == ["session.awaiting_review", "session.resumed"]


def test_a_poison_line_never_stops_collection(setup: tuple[SessionManager, FakeRuntime]) -> None:
    manager, runtime = setup
    bad, good = manager.start().session_id, manager.start().session_id
    buf = io.StringIO()
    EventEmitter("review-app", session_id=bad, stream=buf).emit("review.item_created")
    poison = json.loads(buf.getvalue())
    poison.update(seq=2, ts="not-a-time")
    runtime.log_lines[f"mt-{bad}-session"] = [
        f"2026-10-17T10:00:00.000000001+00:00 {buf.getvalue().strip()}",
        f"2026-10-17T10:00:01.000000001+00:00 {json.dumps(poison)}",
        "2026-10-17T10:00:02.000000001+00:00 later legit output",
    ]
    runtime.log_lines[f"mt-{good}-session"] = ["2026-10-17T10:00:00.000000001+00:00 hello"]
    assert manager.collect(bad) == 3
    assert manager.collect(bad) == 0  # the cursor moved past the poison line
    assert _types(manager, bad)[-4:] == [
        "review.item_created", "log.invalid_event", "log.line", "log.line"
    ]  # fmt: skip
    assert manager.collect(good) == 1


def test_watch_keeps_running_when_one_session_fails(
    setup: tuple[SessionManager, FakeRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = setup
    bad, good = manager.start().session_id, manager.start().session_id
    followed: set[str] = set()

    def follow(session_id: str, stop: threading.Event) -> list[threading.Thread]:
        if session_id == bad:
            raise RuntimeError("podman logs failed")
        return []

    monkeypatch.setattr(manager, "follow", follow)
    watch_once(manager, threading.Event(), followed, timedelta(minutes=15))
    assert followed == {good}  # the failing session is retried next round


ORIGIN = {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
OK = {"confirmed": True}


def _shelve_app(manager: SessionManager, shelve: Shelve | None = None) -> TestClient:
    def by_manager(session_id: str, confirmed: bool) -> SnapshotManifest:
        return manager.shelve(session_id, confirmed=confirmed)

    app = create_collector_app(manager.collector, manager.sessions, shelve=shelve or by_manager)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_run_table_shelves_after_the_users_ok(setup: tuple[SessionManager, FakeRuntime]) -> None:
    """The run table's "finish and shelve" (#52): confirmed in the dialog, then D22's flow."""
    manager, runtime = setup
    sid = manager.start().session_id
    client = _shelve_app(manager)

    refused = client.post(f"/sessions/{sid}/shelve", json={"confirmed": False}, headers=ORIGIN)
    assert refused.status_code == 428  # the manager itself insists on the OK (D22)
    assert manager.sessions.get(sid).state == "running"

    done = client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN)
    assert done.status_code == 200
    record = manager.sessions.get(sid)
    assert done.json() == {
        "session_id": sid,
        "state": "shelved",
        "snapshot_id": record.snapshot_id,
        "retention_class": "standard",
        "expires_at": record.expires_at,
    }
    assert record.state == "shelved"
    assert ("remove_container", f"mt-{sid}-session") in runtime.calls
    (row,) = client.get("/sessions").json()
    assert (row["state"], row["phase"], row["link"]) == ("shelved", "shelved", None)
    assert row["expires_at"] == record.expires_at and row["retention_days"] == 30

    again = client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN)
    assert again.status_code == 409  # already shelved
    assert (
        client.post("/sessions/ses-unknown000/shelve", json=OK, headers=ORIGIN).status_code == 404
    )
    assert client.post("/sessions/../shelve", json=OK, headers=ORIGIN).status_code in (404, 405)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"host": "127.0.0.1:8765"}, 403),  # neither Origin nor Sec-Fetch-Site: a script
        ({"host": "127.0.0.1:8765", "origin": "http://evil.example"}, 403),
        ({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:40000"}, 403),  # a sandbox page
        ({"host": "127.0.0.1:8765", "origin": "null"}, 403),
        ({"host": "evil.example:8765", "origin": "http://evil.example:8765"}, 400),  # rebinding
        ({"host": "127.0.0.1:8765", "sec-fetch-site": "cross-site"}, 403),
        ({"host": "127.0.0.1:8765", "sec-fetch-site": "same-origin"}, 200),
        ({"host": "localhost:8765", "origin": "http://localhost:8765"}, 200),
        ({"host": "[::1]:8765", "origin": "http://[::1]:8765"}, 200),
    ],
)
def test_shelve_route_accepts_only_its_own_page(
    setup: tuple[SessionManager, FakeRuntime], headers: dict[str, str], status: int
) -> None:
    """CSRF and DNS rebinding: a foreign page can neither post nor read the answer (docs/13)."""
    manager, _ = setup
    sid = manager.start().session_id
    client = _shelve_app(manager)
    response = client.post(f"/sessions/{sid}/shelve", json=OK, headers=headers)
    assert response.status_code == status
    assert manager.sessions.get(sid).state == ("shelved" if status == 200 else "running")
    assert "access-control-allow-origin" not in response.headers


def test_shelve_route_needs_a_json_body(setup: tuple[SessionManager, FakeRuntime]) -> None:
    """A plain cross-site form can only send form or text bodies: those are refused."""
    manager, _ = setup
    sid = manager.start().session_id
    client = _shelve_app(manager)
    url = f"/sessions/{sid}/shelve"
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        body = '{"confirmed": true}'
        sent = client.post(url, content=body, headers={**ORIGIN, "content-type": content_type})
        assert sent.status_code == 415, content_type
    for body in ("", "[]", '{"confirmed": true, "extra": 1}', '{"confirmed": "yes please"}'):
        sent = client.post(
            url, content=body, headers={**ORIGIN, "content-type": "application/json"}
        )
        assert sent.status_code == 422, body
    assert manager.sessions.get(sid).state == "running"


def test_one_shelve_at_a_time(setup: tuple[SessionManager, FakeRuntime]) -> None:
    """A second click while the snapshot is written gets 409, not a second snapshot."""
    manager, _ = setup
    sid = manager.start().session_id
    entered, release = threading.Event(), threading.Event()

    def slow(session_id: str, confirmed: bool) -> SnapshotManifest:
        entered.set()
        assert release.wait(5)
        return manager.shelve(session_id, confirmed=confirmed)

    client = _shelve_app(manager, slow)
    first: list[int] = []
    thread = threading.Thread(
        target=lambda: first.append(
            client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN).status_code
        )
    )
    thread.start()
    assert entered.wait(5)
    assert client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN).status_code == 409
    release.set()
    thread.join(5)
    assert first == [200]


def test_configured_image_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    from mechatronikers.session.manager import DEFAULT_IMAGE, configured_image

    monkeypatch.delenv("MT_IMAGE", raising=False)
    assert configured_image() == DEFAULT_IMAGE
    monkeypatch.setenv("MT_IMAGE", "localhost/mechatronikers:env")
    assert configured_image() == "localhost/mechatronikers:env"
    assert configured_image(None) == "localhost/mechatronikers:env"
    assert configured_image("localhost/mechatronikers:flag") == "localhost/mechatronikers:flag"
    monkeypatch.setenv("MT_IMAGE", "")
    assert configured_image() == DEFAULT_IMAGE


def test_agent_cli_image_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from mechatronikers.agent.__main__ import build_parser
    from mechatronikers.session.manager import configured_image

    monkeypatch.setenv("MT_IMAGE", "x:env")
    parser = build_parser()
    args = parser.parse_args(["gate", "--session", "s", "--approve"])
    assert configured_image(args.image) == "x:env"
    args = parser.parse_args(["--image", "x:flag", "gate", "--session", "s", "--approve"])
    assert configured_image(args.image) == "x:flag"


def test_a_failed_snapshot_names_its_reason(setup: tuple[SessionManager, FakeRuntime]) -> None:
    """The dialog shows why shelving failed, not a bare 500 (#141 review)."""
    manager, _ = setup
    sid = manager.start().session_id
    calls: list[str] = []

    def failing(session_id: str, confirmed: bool) -> SnapshotManifest:
        calls.append(session_id)
        raise OSError("No space left on device")

    client = _shelve_app(manager, failing)
    response = client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN)
    assert response.status_code == 500
    assert response.json() == {"detail": "shelving failed: No space left on device"}
    # unknown sessions and states that cannot be shelved never reach the manager
    assert (
        client.post("/sessions/ses-unknown000/shelve", json=OK, headers=ORIGIN).status_code == 404
    )
    manager.sessions.save(manager.sessions.get(sid).model_copy(update={"state": "snapshotting"}))
    busy = client.post(f"/sessions/{sid}/shelve", json=OK, headers=ORIGIN)
    assert busy.status_code == 409 and "snapshotting" in busy.json()["detail"]
    assert calls == [sid]
