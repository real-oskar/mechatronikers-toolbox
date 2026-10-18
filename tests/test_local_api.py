"""The host's local APIs refuse browsers from other pages and DNS rebinding (#146, D103)."""

import io
import os
import socket
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mechatronikers.circuit_mcp.dev import SESSION, create_dev_app
from mechatronikers.session.store import SessionStore
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.collector_app import create_collector_app
from mechatronikers.telemetry.forward import CollectorStream
from mechatronikers.telemetry.local_api import ingest_headers, ingest_key
from mechatronikers.telemetry.store import EventStore

LOCAL = "http://127.0.0.1:8765"
EVIL = "http://evil.example"
SANDBOX_PAGE = "http://127.0.0.1:40000"  # another port on 127.0.0.1: same site, other origin


def _line(type: str = "agent.run_finished", session_id: str = "ses-1") -> str:
    buf = io.StringIO()
    EventEmitter("agent", session_id=session_id, stream=buf).emit(type, {"status": "done"})
    return buf.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    events = EventStore(tmp_path / "state.db")
    yield events
    events.close()


@pytest.fixture
def key(tmp_path: Path) -> str:
    return ingest_key(tmp_path / "var")


def _watch(store: EventStore, tmp_path: Path, key: str | None) -> Starlette:
    """The `session watch` app: collector API and run table."""
    sessions = SessionStore(store.connection())
    return create_collector_app(Collector(store, tmp_path / "storage"), sessions, ingest_key=key)


@contextmanager
def _serve(app: Starlette) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


# -- the ingest key --------------------------------------------------------------------------


def test_ingest_key_is_private_and_stable(tmp_path: Path) -> None:
    var = tmp_path / "var"
    key = ingest_key(var)
    path = var / "ingest.key"
    assert len(key) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ingest_key(var) == key  # `session watch` and the run drivers read the same key
    assert [p.name for p in var.iterdir()] == ["ingest.key"]  # no temporary file left
    path.chmod(0o644)
    assert ingest_key(var) == key and stat.S_IMODE(path.stat().st_mode) == 0o600

    other = tmp_path / "other"
    other.mkdir()
    (other / "ingest.key").symlink_to(path)
    with pytest.raises(ValueError, match="regular file"):
        ingest_key(other)
    (other / "ingest.key").unlink()
    (other / "ingest.key").write_text("short\n")
    with pytest.raises(ValueError, match="no valid ingest key"):
        ingest_key(other)
    assert os.access(path, os.R_OK)


# -- /ingest: host processes only ------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        # what any web page can send without a preflight: no-cors text and form posts
        ({"content-type": "text/plain", "origin": EVIL, "sec-fetch-site": "cross-site"}, 403),
        ({"content-type": "application/x-www-form-urlencoded", "origin": EVIL}, 403),
        (
            {"content-type": "text/plain", "origin": SANDBOX_PAGE, "sec-fetch-site": "same-site"},
            403,
        ),
        ({"content-type": "application/json", "origin": EVIL}, 403),
        ({"content-type": "text/plain", "sec-fetch-site": "cross-site"}, 403),
        ({"content-type": "text/plain", "origin": "null"}, 403),
        # without browser headers: the content type and the key decide
        ({"content-type": "text/plain"}, 415),
        ({"content-type": "application/json"}, 415),
        ({"content-type": "application/x-ndjson"}, 401),
        ({"content-type": "application/x-ndjson", "x-mt-ingest-key": "guess"}, 403),
        # even this server's own page needs the key
        ({"content-type": "application/x-ndjson", "origin": LOCAL}, 401),
    ],
)
def test_ingest_refuses_everything_but_the_hosts_run_drivers(
    store: EventStore, tmp_path: Path, key: str, headers: dict[str, str], status: int
) -> None:
    client = TestClient(_watch(store, tmp_path, key), base_url=LOCAL)
    response = client.post("/ingest", content=_line(), headers=headers)
    assert response.status_code == status
    assert store.events("ses-1") == []  # nothing stored, no phase flipped
    assert "access-control-allow-origin" not in response.headers


def test_ingest_takes_the_key_and_nothing_without_one(
    store: EventStore, tmp_path: Path, key: str
) -> None:
    client = TestClient(_watch(store, tmp_path, key), base_url=LOCAL)
    assert client.post("/ingest", content=_line(), headers=ingest_headers(key)).json() == {
        "stored": 1
    }
    unkeyed = TestClient(_watch(store, tmp_path, None), base_url=LOCAL)
    assert unkeyed.post("/ingest", content=_line(), headers=ingest_headers(key)).status_code == 403
    assert len(store.events("ses-1")) == 1


# -- DNS rebinding: loopback Host headers only -----------------------------------------------


def test_every_route_of_session_watch_refuses_a_foreign_host(
    store: EventStore, tmp_path: Path, key: str
) -> None:
    app = _watch(store, tmp_path, key)
    gets = ["/", "/config.json", "/sessions", "/sessions/ses-1/events", "/sessions/ses-1/stream"]
    for host in ("evil.example:8765", "127.0.0.1.evil.example", "localhost.evil", "", "[::1"):
        client = TestClient(app, base_url="http://placeholder", headers={"host": host})
        for path in gets:
            assert client.get(path).status_code == 400, (host, path)
        sent = client.post("/ingest", content=_line(), headers=ingest_headers(key))
        assert sent.status_code == 400, host
    for local in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
        assert TestClient(app, base_url=local).get("/sessions").status_code == 200, local
    assert store.events("ses-1") == []


# -- the run drivers still post (#146 acceptance) ----------------------------------------------


def test_run_drivers_post_with_the_key(store: EventStore, tmp_path: Path) -> None:
    """The agent and the circuit run use `CollectorStream`; it sends the key of their var dir."""
    var = tmp_path / "var"
    with _serve(_watch(store, tmp_path, ingest_key(var))) as url:
        stream = CollectorStream(url, None, key=ingest_key(var))
        EventEmitter("agent", session_id="ses-1", stream=stream).emit("agent.step_started")
        stream.close()
        wrong = CollectorStream(url, Collector(store, tmp_path / "fallback"), key="x" * 43)
        EventEmitter("agent", session_id="ses-2", stream=wrong).emit("agent.step_started")
        wrong.close()
    assert [e.type for _, e in store.events("ses-1")] == ["agent.step_started"]
    # a refused key falls back to the local store, like a collector that is down
    assert [e.type for _, e in store.events("ses-2")] == ["agent.step_started"]


# -- the circuit dev server: the same rules --------------------------------------------------


def test_circuit_dev_server_has_the_same_rules(tmp_path: Path, key: str) -> None:
    (tmp_path / "data").mkdir()
    app, collector = create_dev_app(tmp_path / "data", tmp_path / "out", key)
    foreign = TestClient(app, base_url="http://evil.example:8790")
    for path in ("/", "/events/history", "/api/review/gates", "/circuit/api/files"):
        assert foreign.get(path).status_code == 400, path
    local = TestClient(app, base_url="http://127.0.0.1:8790")
    assert local.get("/api/review/gates").json() == []
    # a cross-site page may not change state; host processes (no browser headers) still may
    cross = local.post("/api/review/items", json={}, headers={"origin": EVIL})
    assert cross.status_code == 403
    assert local.post("/api/review/items", json={}).status_code == 422  # reached the API
    assert local.post("/ingest", content=_line(session_id=SESSION)).status_code == 415
    assert collector.store.events(SESSION) == []

    with _serve(app) as url:  # `circuit_mcp.run --collector-url <dev server> run ...`
        keyed = CollectorStream(url, None, key=key)
        keyed.write(_line("agent.step_started", SESSION))
        keyed.close()
        wrong = CollectorStream(url, None, key="x" * 43)
        wrong.write(_line("agent.step_completed", SESSION))
        wrong.close()
    assert [e.type for _, e in collector.store.events(SESSION)] == ["agent.step_started"]
