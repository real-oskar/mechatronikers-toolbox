import io
import json
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import zstandard
from starlette.testclient import TestClient

from mechatronikers.review_app.store import ReviewStore
from mechatronikers.session.runs import summarize
from mechatronikers.session.store import SessionRecord
from mechatronikers.spec.review import GateDecision, GateRequest
from mechatronikers.telemetry import Event, EventEmitter
from mechatronikers.telemetry.collector import MAX_LINE_CHARS, Collector
from mechatronikers.telemetry.collector_app import (
    bind_unix_socket,
    collector_server,
    create_collector_app,
)
from mechatronikers.telemetry.events import utc_now
from mechatronikers.telemetry.local_api import ingest_headers
from mechatronikers.telemetry.store import EventStore


@pytest.fixture
def collector(tmp_path: Path) -> Iterator[Collector]:
    store = EventStore(tmp_path / "state.db")
    yield Collector(store, tmp_path / "storage")
    store.close()


def _line(emitter: EventEmitter, type: str = "x") -> str:
    buf = io.StringIO()
    emitter._stream = buf
    emitter.emit(type)
    return buf.getvalue()


def _types(collector: Collector, session: str = "ses-1") -> list[str]:
    return [e.type for _, e in collector.store.events(session)]


def test_valid_events_are_stored_with_recv_ts(collector: Collector) -> None:
    emitter = EventEmitter("cad-mcp", session_id="ses-1")
    assert collector.ingest_line(_line(emitter, "build.started"), source="session") is not None
    ((_, event),) = collector.store.events("ses-1")
    assert event.type == "build.started"
    assert event.recv_ts is not None


def test_plain_lines_become_log_events(collector: Collector) -> None:
    collector.ingest_line(
        "Traceback (most recent call last):", source="session", session_id="ses-1"
    )
    ((_, event),) = collector.store.events("ses-1")
    assert event.type == "log.line"
    assert event.level == "warning"
    assert event.payload == {"source": "session", "line": "Traceback (most recent call last):"}


def test_duplicates_are_ignored(collector: Collector) -> None:
    line = _line(EventEmitter("cad-mcp", session_id="ses-1"))
    assert collector.ingest_line(line, source="session") is not None
    assert collector.ingest_line(line, source="session") is None
    assert collector.store.count("ses-1") == 1


def test_sequence_gap_is_reported(collector: Collector) -> None:
    emitter = EventEmitter("cad-mcp", session_id="ses-1")
    collector.ingest_line(_line(emitter), source="session")
    _line(emitter)  # seq 2 never arrives
    collector.ingest_line(_line(emitter), source="session")
    gaps = [e for _, e in collector.store.events("ses-1") if e.type == "collector.seq_gap"]
    assert len(gaps) == 1
    assert gaps[0].payload["expected"] == 2 and gaps[0].payload["got"] == 3


def test_clock_skew_uses_the_runtime_timestamp(collector: Collector) -> None:
    emitter = EventEmitter("cad-mcp", session_id="ses-1")
    line = _line(emitter)
    event = Event.model_validate_json(line)
    from datetime import datetime

    written_ns = int(datetime.fromisoformat(event.ts.replace("Z", "+00:00")).timestamp() * 1e9)
    collector.ingest_line(
        line, source="session", observed_ns=written_ns
    )  # late collection: no skew
    assert "collector.clock_skew" not in _types(collector)
    collector.ingest_line(_line(emitter), source="session", observed_ns=written_ns + 5 * 10**9)
    assert "collector.clock_skew" in _types(collector)


def test_archive_is_written_and_compressed(collector: Collector, tmp_path: Path) -> None:
    collector.ingest_line(_line(EventEmitter("cad-mcp", session_id="ses-1")), source="session")
    (archive,) = (tmp_path / "storage" / "logs" / "ses-1").glob("*/events.ndjson")
    (compressed,) = collector.compress_archives("ses-1")
    assert not archive.exists()
    text = zstandard.ZstdDecompressor().decompress(compressed.read_bytes()).decode()
    assert '"component":"cad-mcp"' in text


KEY = "k" * 43  # an ingest key as `local_api.ingest_key` makes them
LOCAL = "http://127.0.0.1:8765"


def _host_client(collector: Collector) -> TestClient:
    """`session watch` as a run driver reaches it: loopback host, this host's ingest key."""
    client = TestClient(create_collector_app(collector, ingest_key=KEY), base_url=LOCAL)
    client.headers.update(ingest_headers(KEY))
    return client


def test_http_ingest_and_query(collector: Collector) -> None:
    client = _host_client(collector)
    line = _line(EventEmitter("agent", session_id="ses-1"), "model.request")
    assert client.post("/ingest", content=line).json() == {"stored": 1}
    rows = client.get("/sessions/ses-1/events").json()
    assert [r["event"]["type"] for r in rows] == ["model.request"]
    assert client.get(f"/sessions/ses-1/events?after={rows[0]['id']}").json() == []


# -- provenance (#78): sandbox lines belong to the session being read -------------------------


def _record(session_id: str = "ses-1") -> SessionRecord:
    now = utc_now()
    return SessionRecord(
        id=session_id, state="running", token="t", created_at=now, updated_at=now, image="img"
    )


def test_sandbox_line_for_another_session_stays_in_its_own(
    collector: Collector, tmp_path: Path
) -> None:
    line = _line(EventEmitter("cad-mcp", session_id="ses-other"), "review.approved")
    assert collector.ingest_line(line, source="session", session_id="ses-1") is not None
    assert collector.store.count("ses-other") == 0
    assert not (tmp_path / "storage" / "logs" / "ses-other").exists()
    warning, log = (e for _, e in collector.store.events("ses-1"))
    assert (warning.type, warning.level) == ("log.provenance_mismatch", "warning")
    assert warning.payload["reason"] == "session_mismatch"
    assert warning.payload["claimed_session_id"] == "ses-other"
    assert log.type == "log.line" and log.payload == {"source": "session", "line": line.strip()}


def test_sandbox_line_without_session_gets_the_session_read(collector: Collector) -> None:
    line = _line(EventEmitter("cad-mcp"), "tool.called")
    collector.ingest_line(line, source="session", session_id="ses-1")
    assert _types(collector) == ["tool.called"]


def test_cost_from_a_sandbox_does_not_count(collector: Collector) -> None:
    forged = _line(EventEmitter("agent", session_id="ses-1"), "model.request")
    collector.ingest_line(forged, source="session", session_id="ses-1")
    buf = io.StringIO()
    EventEmitter("agent", session_id="ses-1", stream=buf).emit("model.response", {"cost": 5.0})
    collector.ingest_line(buf.getvalue(), source="session", session_id="ses-1")
    events = [e for _, e in collector.store.events("ses-1")]
    assert [e.type for e in events] == ["log.provenance_mismatch", "log.line"] * 2
    assert {e.payload.get("reason") for e in events if e.level == "warning"} == {"host_only_type"}
    assert summarize(_record(), events, None).cost == 0.0

    # The agent's own events (POST /ingest on the host) still count.
    client = _host_client(collector)
    agent = EventEmitter("agent", session_id="ses-1")
    assert client.post("/ingest", content=_line(agent, "model.request")).json() == {"stored": 1}
    buf = io.StringIO()
    agent._stream = buf
    agent.emit("model.response", {"cost": 0.25})
    client.post("/ingest", content=buf.getvalue())
    row = summarize(_record(), [e for _, e in collector.store.events("ses-1")], None)
    assert row.cost == 0.25


def test_sandbox_line_cannot_take_a_host_emitters_sequence(collector: Collector) -> None:
    def host(seq: int) -> Event:
        return Event(
            ts=utc_now(),
            mono_ns=seq,
            seq=seq,
            emitter_id="session-manager-1",
            session_id="ses-2",
            component="session-manager",
            type="session.started" if seq == 1 else "session.stopped",
        )

    collector.ingest(host(1))
    claim = host(2).model_copy(update={"session_id": "ses-1", "type": "tool.called"})
    collector.ingest_line(claim.to_json(), source="session", session_id="ses-1")
    assert _types(collector) == ["log.provenance_mismatch", "log.line"]
    assert collector.ingest(host(2)) is not None  # not taken by the sandbox line
    assert _types(collector, "ses-2") == ["session.started", "session.stopped"]


def test_sandbox_cannot_set_the_receive_time(collector: Collector) -> None:
    event = Event.model_validate_json(_line(EventEmitter("cad-mcp", session_id="ses-1")))
    event.recv_ts = "2000-01-01T00:00:00.000000Z"
    collector.ingest_line(event.to_json(), source="session", session_id="ses-1")
    ((_, stored),) = collector.store.events("ses-1")
    assert stored.recv_ts is not None and not stored.recv_ts.startswith("2000")


def test_deeply_nested_line_is_a_log_line(collector: Collector) -> None:
    collector.ingest_line("[" * 100_000, source="session", session_id="ses-1")
    assert _types(collector) == ["log.line"]


def test_review_decisions_from_the_sandbox_land_in_their_session(
    collector: Collector, tmp_path: Path
) -> None:
    """Mode 1: the review app runs in the sandbox; its gate and item events are legitimate."""
    buf = io.StringIO()
    emitter = EventEmitter("review-app", session_id="ses-1", stream=buf)
    review = ReviewStore(tmp_path / "review", emitter)
    request = GateRequest(id="gate-1", kind="spec_review", run_id="run-1", created_at=utc_now())
    review.open_gate(request)
    review.decide(
        "gate-1",
        GateDecision(gate_id="gate-1", action="approve", reviewer="Anna", decided_at=utc_now()),
    )
    for line in buf.getvalue().splitlines():
        collector.ingest_line(line, source="session", session_id="ses-1")
    assert _types(collector) == ["review.gate_opened", "review.gate_passed"]
    events = [e for _, e in collector.store.events("ses-1")]
    assert all(e.session_id == "ses-1" and e.component == "review-app" for e in events)
    assert events[-1].payload["action"] == "approve"


# -- review of #84: no line may stop collection; host-only types; long lines ------------------


def _event_line(**fields: object) -> str:
    base: dict[str, object] = {
        "ts": utc_now(), "mono_ns": 1, "seq": 1, "emitter_id": "e1", "component": "cad-mcp",
        "type": "tool.called",
    }  # fmt: skip
    return json.dumps({**base, **fields})


@pytest.mark.parametrize(
    "line",
    [
        _event_line(ts="yesterday"),
        _event_line(ts="2026-10-17T00:00:00"),  # no time zone
        _event_line(seq=2**63),
        _event_line(seq=-1),
        _event_line(payload={"a": json.loads("[" * 300 + "1" + "]" * 300)}),
        _event_line(payload={"x": "\ud800"}),  # lone surrogate (JSON escape)
    ],
    ids=["bad-ts", "naive-ts", "seq-int64", "seq-negative", "deep-payload", "lone-surrogate"],
)
def test_poison_lines_become_log_lines(collector: Collector, line: str) -> None:
    assert collector.ingest_line(line, source="session", session_id="ses-1") is not None
    assert collector.ingest_line(line, source="session", session_id="ses-1", observed_ns=1)
    warning, log, *_ = (e for _, e in collector.store.events("ses-1"))
    assert (warning.type, warning.level, log.type) == ("log.invalid_event", "warning", "log.line")
    later = _line(EventEmitter("cad-mcp", session_id="ses-1"), "tool.called")
    collector.ingest_line(later, source="session", session_id="ses-1")
    assert _types(collector)[-1] == "tool.called"  # collection goes on


def test_long_lines_are_cut(collector: Collector) -> None:
    for line in (_event_line(payload={"x": "y" * 5_000_000}), "z" * 5_000_000):
        collector.ingest_line(line, source="session", session_id="ses-1")
    for _, event in collector.store.events("ses-1"):
        assert event.type == "log.line" and event.payload["truncated"] is True
        assert len(event.payload["line"]) == MAX_LINE_CHARS


@pytest.mark.parametrize(
    "type", ["agent.run_finished", "session.stopped", "collector.seq_gap", "log.line"]
)
def test_host_only_types_from_a_sandbox_are_log_lines(collector: Collector, type: str) -> None:
    line = _event_line(type=type, session_id="ses-1", payload={"status": "done"})
    collector.ingest_line(line, source="session", session_id="ses-1")
    warning, log = (e for _, e in collector.store.events("ses-1"))
    assert (warning.payload["reason"], log.type) == ("host_only_type", "log.line")
    assert summarize(_record(), [warning, log], None).phase == "provisioning"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """AF_UNIX paths are limited to about 100 bytes; pytest's tmp_path is longer on macOS."""
    path = Path(tempfile.mkdtemp(prefix="mt-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_unix_socket_is_private_and_replaces_a_stale_one(short_dir: Path) -> None:
    path = short_dir / "run" / "collector.sock"
    bind_unix_socket(path).close()  # a stopped collector leaves its socket file behind
    assert path.is_socket()
    sock = bind_unix_socket(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        sock.listen()
        with pytest.raises(OSError, match="another collector"):
            bind_unix_socket(path)
    finally:
        sock.close()


def test_collector_serves_loopback_and_the_gateway_socket(
    collector: Collector, short_dir: Path
) -> None:
    emitter = EventEmitter("cad-mcp", session_id="ses-1")
    collector.ingest_line(_line(emitter, "build.started"), source="session")
    path = short_dir / "collector.sock"
    server, sockets = collector_server(create_collector_app(collector), 0, path)
    host, port = sockets[0].getsockname()
    assert host == "127.0.0.1"  # never 0.0.0.0: not reachable from other machines
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), timeout=5) as client:
            # Caddy passes the browser's Host (the gateway's 127.0.0.1:<port>) through (#146)
            gateway = "http://127.0.0.1:40000"
            rows = client.get(f"{gateway}/sessions/ses-1/events").json()
            assert [r["event"]["type"] for r in rows] == ["build.started"]
            assert client.get("http://collector/sessions/ses-1/events").status_code == 400
            with client.stream("GET", f"{gateway}/sessions/ses-1/stream") as stream:
                data = next(line for line in stream.iter_lines() if line.startswith("data:"))
            assert '"build.started"' in data
        tcp = httpx.get(f"http://127.0.0.1:{port}/sessions/ses-1/events", timeout=5)
        assert tcp.json() == rows
    finally:
        server.should_exit = True
        thread.join(10)
