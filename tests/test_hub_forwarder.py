"""The forwarder of `session watch --hub`: a mode 1 run shows up in the hub, restarts store
nothing twice, sources and tokens follow docs/13 §1 and §5, artifacts are copied out safely."""

import io
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from mechatronikers.hub.app import create_hub_app
from mechatronikers.hub.auth import HubAuth
from mechatronikers.hub.forwarder import (
    MAX_ARTIFACT_BYTES,
    ForwardError,
    HubForwarder,
    HubUnavailableError,
    artifact_path,
    artifact_role,
    mirror_run_id,
    provider_secrets,
)
from mechatronikers.hub.records import HubEvents, HubStore
from mechatronikers.session.manager import ArtifactTooLargeError
from mechatronikers.session.runs import run_table
from mechatronikers.session.store import SessionRecord, SessionStore
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.store import EventStore
from tests.fixtures.hub import (
    BACKENDS,
    BROWSER,
    GATEWAY_TOKEN,
    SHARED,
    WORKERS,
    Host,
    Hub,
    _stores,
    hub,
    mode1_host,
)

RUN = "run-fixture0001"
STEP = b"ISO-10303-21;\nEND-ISO-10303-21;\n"


@pytest.fixture(params=BACKENDS)
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Hub]:
    with hub(tmp_path, request.param) as h:
        yield h


@pytest.fixture
def host(tmp_path: Path) -> Iterator[Host]:
    with mode1_host(tmp_path) as host:
        yield host


def _copy(files: dict[str, bytes]) -> Any:
    """A sandbox's artifacts folder for `copy_artifact`."""

    def copy(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        out = dest / path.rsplit("/", 1)[-1]
        out.write_bytes(files[path])
        return out

    return copy


def _forwarder(h: Hub, host: Host, **kwargs: Any) -> HubForwarder:
    return HubForwarder(
        host.store,
        host.sessions,
        "http://testserver",
        WORKERS["worker-a"],
        client=h.client,
        **kwargs,
    )


def _hub_events(h: Hub, run: str = RUN) -> list[dict[str, Any]]:
    response = h.client.get(f"/api/runs/{run}/events/history", headers=BROWSER)
    assert response.status_code == 200
    rows: list[dict[str, Any]] = response.json()
    return rows


def test_a_mode1_run_shows_up_in_the_hub(h: Hub, host: Host) -> None:
    files = {"build-fx2/Fuss.ap242.step": STEP}
    forwarder = _forwarder(h, host, copy_artifact=_copy(files))
    assert forwarder.check() == "worker-a"
    local = host.store.count(host.record.id)
    assert forwarder.forward_once() == local
    rows = _hub_events(h)
    assert len(rows) == local + 1  # and the hub's own `artifact.uploaded`
    assert rows[-1]["event"]["type"] == "artifact.uploaded"
    assert {r["event"]["session_id"] for r in rows} == {host.record.id}
    assert {r["event"]["run_id"] for r in rows} == {RUN}
    # host processes are trusted, sandbox output is not (D86)
    sources = {r["event"]["component"]: r["event"]["source"] for r in rows}
    assert sources == {
        "hub": "host",
        "collector": "host",
        "session-manager": "host",
        "agent": "host",
        "sandbox": "container",
        "drawing-mcp": "container",
        "cad-mcp": "container",
        "review-app": "container",
    }
    # the overview row equals the host's own run table, except the token link
    hub_row = h.client.get(f"/api/runs/{RUN}/run", headers=BROWSER).json()
    host_row = run_table(host.sessions, host.store)[0].model_dump(mode="json")
    for field in ("phase", "checks_passed", "checks_total", "open_pois", "cost", "revision"):
        assert hub_row[field] == host_row[field], field
    assert hub_row["link"] is None
    # the exported STEP file was copied out and uploaded under a key the hub chose
    artifacts = h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json()
    assert [(a["role"], a["build_id"], a["name"]) for a in artifacts] == [
        ("export", "build-fx2", "Fuss.ap242.step")
    ]
    assert h.blobs.head("artifacts/ses-fixture0001/build-fx2/Fuss.ap242.step") is not None


def test_restarts_store_nothing_twice(h: Hub, host: Host) -> None:
    first = _forwarder(h, host)
    first.forward_once()
    total = len(_hub_events(h))
    # new events after the restart are sent, the old ones are not resent
    EventEmitter("agent", session_id=host.record.id, stream=_Sink(host)).emit(
        "agent.step_started", {"step": "export"}
    )
    second = _forwarder(h, host)
    assert second.forward_once() == 1
    assert len(_hub_events(h)) == total + 1
    assert _forwarder(h, host).forward_once() == 0


def test_a_crash_before_the_cursor_moves_resends_without_duplicates(h: Hub, host: Host) -> None:
    store = host.store
    original = store.set_cursor

    def crash(source: str, last_ts: str) -> None:
        raise RuntimeError("killed")

    store.set_cursor = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):  # the process dies between the hub's reply and the cursor
        _forwarder(h, host, batch=20).forward_session(host.record)
    store.set_cursor = original  # type: ignore[method-assign]
    sent = len(_hub_events(h))
    assert sent == 20  # the hub has the first batch; the host never learnt it
    restarted = _forwarder(h, host, batch=20)
    stored = restarted.forward_once()
    assert stored == store.count(host.record.id) - sent  # the resent batch was all duplicates
    assert len(_hub_events(h)) == store.count(host.record.id)
    seqs = [(r["event"]["emitter_id"], r["event"]["seq"]) for r in _hub_events(h)]
    assert len(seqs) == len(set(seqs))


def test_sources_survive_a_restart_mid_session(h: Hub, host: Host) -> None:
    """After a restart the forwarder still knows the agent is a host process, also for event
    types a sandbox could send too (`review.item_created`)."""
    _forwarder(h, host).forward_once()
    agent = EventEmitter("agent", session_id=host.record.id, stream=_Sink(host))
    agent.emitter_id = "agent-fx"
    agent._seq = 100
    agent.emit("review.item_created", {"origin": "agent", "revision": 1, "item": {"id": "x9"}})
    _forwarder(h, host).forward_once()
    last = _hub_events(h)[-1]["event"]
    assert (last["type"], last["source"]) == ("review.item_created", "host")


def test_tokens_never_leave_the_host(h: Hub, host: Host) -> None:
    box = EventEmitter("cad-mcp", session_id=host.record.id, stream=_Sink(host, "session"))
    box.emit("tool.completed", {"output": f"http://127.0.0.1:40000/?t={GATEWAY_TOKEN}"})
    box.emit("tool.completed", {"output": f"worker token {WORKERS['worker-a']}"})
    box.emit("tool.completed", {"output": "fine"})
    forwarder = _forwarder(h, host)
    forwarder.forward_once()
    sent = [r["event"] for r in _hub_events(h)]
    assert len(sent) == host.store.count(host.record.id) - 2
    assert all(GATEWAY_TOKEN not in str(e) and WORKERS["worker-a"] not in str(e) for e in sent)
    assert sent[-1]["payload"] == {"output": "fine"}


def test_artifacts_need_a_regular_file(h: Hub, host: Host) -> None:
    def symlink(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        out = dest / "Fuss.ap242.step"
        os.symlink("/etc/passwd", out)  # what a hostile sandbox could leave behind
        return out

    _forwarder(h, host, copy_artifact=symlink).forward_once()
    assert len(_hub_events(h)) == host.store.count(host.record.id)  # events still go
    assert h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json() == []
    assert list(h.blobs.list()) == []


def test_a_shelved_sandbox_mirrors_events_only(h: Hub, tmp_path: Path) -> None:
    def copy(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        raise AssertionError("a shelved sandbox has no container to copy from")

    with mode1_host(tmp_path / "shelved", state="shelved") as shelved:
        _forwarder(h, shelved, copy_artifact=copy).forward_once()
        assert len(_hub_events(h)) == shelved.store.count(shelved.record.id)
    assert h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json() == []


def test_another_worker_cannot_mirror_the_same_session(h: Hub, host: Host) -> None:
    _forwarder(h, host).forward_once()
    total = len(_hub_events(h))
    other = HubForwarder(
        host.store, host.sessions, "http://testserver", WORKERS["worker-b"], client=h.client
    )
    host.store.set_cursor(other.cursor_key(host.record.id), "0")
    assert other.forward_once() == 0  # every line refused: not worker b's run
    assert len(_hub_events(h)) == total


def test_unreachable_hub_keeps_the_cursor(host: Host) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    forwarder = HubForwarder(
        host.store,
        host.sessions,
        "http://127.0.0.1:9",
        WORKERS["worker-a"],
        client=httpx.Client(transport=httpx.MockTransport(down)),
    )
    with pytest.raises(ForwardError, match="not reachable"):
        forwarder.forward_once()
    assert host.store.get_cursor(forwarder.cursor_key(host.record.id)) is None
    stop = threading.Event()
    stop.set()
    forwarder.run(stop)  # reports, never raises


def test_artifact_references() -> None:
    sid = "ses-abc"
    assert artifact_path(sid, {"uri": f"artifact://{sid}/build-1/a.stl", "format": "stl"}) == (
        "build-1/a.stl"
    )
    sheet = {"uri": f"artifact://{sid}/drawing/0123abcd/sheet-150.png", "format": "png"}
    assert artifact_path(sid, sheet) == "drawing/0123abcd/sheet-150.png"
    for uri in (
        "artifact://ses-other/build-1/a.stl",
        f"artifact://{sid}/../a.stl",
        f"artifact://{sid}/a.stl",
        f"artifact://{sid}/b/.x",
        f"artifact://{sid}/a/b/c/d.stl",
        f"artifact://{sid}/b/a\\b.stl",
        "/session/artifacts/b/a.stl",
    ):
        assert artifact_path(sid, {"uri": uri, "format": "stl"}) is None, uri
    assert artifact_path(sid, {"uri": f"artifact://{sid}/b/region.png"}) is None  # no format
    assert artifact_role({"format": "png", "role": "sheet"}) == "sheet"
    assert artifact_role({"format": "step-ap214"}) == "export"
    assert artifact_role({"format": "features-json"}) == "spec"
    assert artifact_role({"format": "review-json"}) == "report"
    assert mirror_run_id("ses-0123abcd") == "run-0123abcd"
    assert mirror_run_id("odd id").startswith("run-")


@pytest.mark.parametrize("backend", BACKENDS)
def test_live_over_http(tmp_path: Path, host: Host, backend: str) -> None:
    """End to end on localhost: the forwarder pushes, a browser follows the run's live stream."""
    with _stores(tmp_path / "live", backend) as (events, runs, _):
        _live(events, runs, host)


def _live(events: HubEvents, runs: HubStore, host: Host) -> None:
    app = create_hub_app(events=events, runs=runs, auth=HubAuth.create(SHARED, WORKERS))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    sockets = [config.bind_socket()]
    port = sockets[0].getsockname()[1]
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        forwarder = HubForwarder(host.store, host.sessions, base, WORKERS["worker-a"])
        stop = threading.Event()
        pump = threading.Thread(target=forwarder.run, args=(stop, 0.1), daemon=True)
        pump.start()
        with httpx.Client(base_url=base, headers=BROWSER, timeout=10) as browser:
            history: list[dict[str, Any]] = []
            while len(history) < host.store.count(host.record.id):  # the first push lands
                assert time.monotonic() < deadline + 10
                time.sleep(0.1)
                if browser.get("/api/runs").json()["runs"]:
                    history = browser.get(f"/api/runs/{RUN}/events/history").json()
            last = history[-1]["id"]
            with browser.stream("GET", f"/api/runs/{RUN}/events/stream?after={last}") as live:
                # the step starts on the host after the browser opened the stream
                agent = EventEmitter("agent", session_id=host.record.id, stream=_Sink(host))
                agent.emit("agent.step_started", {"step": "export"})
                data = next(x for x in live.iter_lines() if x.startswith("data:"))
            assert '"agent.step_started"' in data and '"source":"host"' in data
            row = browser.get(f"/api/runs/{RUN}/run").json()
            assert row["phase"] == "valid" or row["phase"] == "building"
        stop.set()
        pump.join(5)
        forwarder.close()
    finally:
        server.should_exit = True
        thread.join(10)


class _Sink:
    """An emitter stream into the host's collector, as `POST /ingest` (agent) or as container
    output of the session (`role`)."""

    def __init__(self, host: Host, role: str = "agent") -> None:
        self.host, self.role = host, role

    def write(self, text: str) -> int:
        for raw in text.splitlines():
            if self.role == "agent":
                self.host.collector.ingest_line(raw, source="agent")
            else:
                self.host.collector.ingest_line(
                    raw, source=self.role, session_id=self.host.record.id
                )
        return len(text)

    def flush(self) -> None:
        pass


# -- review of #137: batches, isolation, back-off, artifacts, secrets ------------------------


def _two_sessions(tmp: Path, big: int = 80) -> Host:
    """The reviewer's probe: session A with `big` sandbox events of 250 kB, session B with one
    host `session.created`."""
    store = EventStore(tmp / "probe" / "state.db")
    sessions = SessionStore(store.connection())
    collector = Collector(store, tmp / "probe" / "storage")
    for sid in ("ses-aaaa", "ses-bbbb"):
        now = "2026-10-17T08:00:00.000000Z"
        sessions.save(
            SessionRecord(
                id=sid, state="running", token="t", created_at=now, updated_at=now, image="x"
            )
        )
    box = EventEmitter("cad-mcp", session_id="ses-aaaa", stream=io.StringIO())
    for _ in range(big):
        line = box.emit("tool.completed", {"output": "x" * 250_000}).to_json()
        collector.ingest_line(line, source="session", session_id="ses-aaaa")
    sm = EventEmitter("session-manager", session_id="ses-bbbb", stream=io.StringIO())
    collector.ingest(sm.emit("session.created", {}))
    return Host(store, sessions, collector, sessions.get("ses-aaaa"))


def test_an_oversized_session_is_sent_in_parts(h: Hub, tmp_path: Path) -> None:
    host = _two_sessions(tmp_path)
    forwarder = _forwarder(h, host)
    forwarder.forward_once()
    for sid in ("ses-aaaa", "ses-bbbb"):
        cursor = host.store.get_cursor(forwarder.cursor_key(sid))
        assert cursor is not None and int(cursor) > 0
    runs = h.client.get("/api/runs", headers=BROWSER).json()["runs"]
    assert {r["run_id"] for r in runs} == {"run-aaaa", "run-bbbb"}
    rows = h.client.get("/api/runs/run-aaaa/events/history", headers=BROWSER).json()
    assert len(rows) == host.store.count("ses-aaaa")
    host.store.close()


def test_a_413_halves_the_batch(h: Hub, tmp_path: Path) -> None:
    """Without the byte cap the whole session goes at once: the hub answers 413 (16 MiB) and the
    forwarder halves until the parts fit."""
    host = _two_sessions(tmp_path)
    _forwarder(h, host, max_request_bytes=1 << 30).forward_once()
    rows = h.client.get("/api/runs/run-aaaa/events/history", headers=BROWSER).json()
    assert len(rows) == host.store.count("ses-aaaa")
    host.store.close()


def _mock_hub(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://hub", transport=httpx.MockTransport(handler))


def _lines(request: httpx.Request) -> list[dict[str, Any]]:
    return [json.loads(x) for x in request.content.decode().splitlines() if x]


def test_a_line_too_large_for_the_hub_becomes_a_placeholder(tmp_path: Path) -> None:
    host = _two_sessions(tmp_path, big=3)
    sent: list[dict[str, Any]] = []

    def hub(request: httpx.Request) -> httpx.Response:
        if len(request.content) > 100_000:  # a hub with a much smaller body limit
            return httpx.Response(413, json={"detail": "too large"})
        sent.extend(_lines(request))
        return httpx.Response(200, json={"stored": len(_lines(request)), "rejected": []})

    forwarder = HubForwarder(
        host.store, host.sessions, "http://hub", "w" * 24, client=_mock_hub(hub)
    )
    forwarder.forward_once()
    stubs = [x["event"] for x in sent if x["event"]["type"] == "log.truncated"]
    assert len(stubs) == 3  # each 250 kB line, alone still too large
    assert stubs[0]["payload"]["original_type"] == "tool.completed"
    assert stubs[0]["emitter_id"] and stubs[0]["seq"] == 1 and stubs[0]["level"] == "warning"
    assert host.store.get_cursor(forwarder.cursor_key("ses-aaaa")) is not None
    # a host line over the hub's per-line limit (the collector cuts sandbox lines before that) is
    # replaced before sending
    big = EventEmitter("agent", session_id="ses-aaaa", stream=io.StringIO())
    line = big.emit("model.response", {"output": "y" * 300_000}).to_json()
    host.collector.ingest_line(line, source="agent")
    sent.clear()
    forwarder.forward_once()
    assert [x["event"]["type"] for x in sent] == ["log.truncated"]
    host.store.close()


def test_one_refused_session_does_not_block_the_others(tmp_path: Path) -> None:
    host = _two_sessions(tmp_path, big=2)

    def hub(request: httpx.Request) -> httpx.Response:
        lines = _lines(request)
        if any(x["event"]["session_id"] == "ses-aaaa" for x in lines):
            return httpx.Response(422, json={"detail": "nope"})
        return httpx.Response(200, json={"stored": len(lines), "rejected": []})

    forwarder = HubForwarder(
        host.store, host.sessions, "http://hub", "w" * 24, client=_mock_hub(hub)
    )
    assert forwarder.forward_once() == 1  # session B went through
    assert host.store.get_cursor(forwarder.cursor_key("ses-aaaa")) is None  # A waits, retried
    assert host.store.get_cursor(forwarder.cursor_key("ses-bbbb")) is not None
    host.store.close()


class _Rounds(threading.Event):
    """A stop event that records the waits of `run` and stops after `n` rounds."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n, self.waits = n, []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.n:
            self.set()
        return self.is_set()


def test_rounds_back_off_while_the_hub_is_down(host: Host) -> None:
    calls = {"n": 0}

    def hub(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 7:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"stored": 0, "rejected": []})

    forwarder = HubForwarder(
        host.store, host.sessions, "http://hub", "w" * 24, client=_mock_hub(hub)
    )
    rounds = _Rounds(9)
    forwarder.run(rounds, interval_s=2.0)
    assert rounds.waits == [4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 2.0, 2.0]


def test_artifacts_are_size_checked_and_retried(h: Hub, host: Host) -> None:
    now = {"t": 0.0}
    calls: list[str] = []

    def flaky(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        calls.append(path)
        if len(calls) == 1:
            raise OSError("podman cp failed")
        out = dest / path.rsplit("/", 1)[-1]
        out.write_bytes(STEP)
        return out

    forwarder = _forwarder(h, host, copy_artifact=flaky, clock=lambda: now["t"])
    forwarder.forward_once()
    assert h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json() == []
    forwarder.forward_once()  # not due yet
    assert len(calls) == 1
    now["t"] = 1000.0
    forwarder.forward_once()  # the retry
    assert len(h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json()) == 1

    def too_large(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        raise ArtifactTooLargeError(path)  # refused in the sandbox, before any copy

    other = _forwarder(h, host, copy_artifact=too_large)
    host.store.set_cursor(other.cursor_key(host.record.id), "0")
    other.forward_once()
    assert other._pending == {}  # never retried


def test_a_claimed_size_over_the_limit_is_not_copied(h: Hub, host: Host) -> None:
    box = EventEmitter("cad-mcp", session_id=host.record.id, stream=_Sink(host, "session"))
    uri = f"artifact://{host.record.id}/build-9/huge.stl"
    box.emit("artifact.created", {"uri": uri, "format": "stl", "bytes": MAX_ARTIFACT_BYTES + 1})
    copies: list[str] = []

    def copy(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        copies.append(path)
        raise OSError("no such file")

    _forwarder(h, host, copy_artifact=copy).forward_once()
    assert "build-9/huge.stl" not in copies


def test_a_retry_stays_queued_while_the_hub_is_down(h: Hub, host: Host) -> None:
    now = {"t": 0.0}

    def broken(session_id: str, path: str, dest: Path, max_bytes: int) -> Path:
        raise OSError("podman cp failed")

    forwarder = _forwarder(h, host, copy_artifact=broken, clock=lambda: now["t"])
    forwarder.forward_once()
    assert len(forwarder._pending) == 1
    forwarder.copy_artifact = _copy({"build-fx2/Fuss.ap242.step": STEP})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    healthy = forwarder._client
    forwarder._client = httpx.Client(transport=httpx.MockTransport(down))
    now["t"] = 1000.0
    with pytest.raises(HubUnavailableError):
        forwarder.forward_once()
    assert len(forwarder._pending) == 1  # kept for the next round
    forwarder._client = healthy
    forwarder.forward_once()
    assert forwarder._pending == {}
    assert len(h.client.get(f"/api/runs/{RUN}/artifacts", headers=BROWSER).json()) == 1


def test_a_conflicting_upload_is_not_retried(h: Hub, host: Host) -> None:
    first = _forwarder(h, host, copy_artifact=_copy({"build-fx2/Fuss.ap242.step": STEP}))
    first.forward_once()
    second = _forwarder(h, host, copy_artifact=_copy({"build-fx2/Fuss.ap242.step": b"other"}))
    host.store.set_cursor(second.cursor_key(host.record.id), "0")
    second.forward_once()
    assert second._pending == {}  # 409: the hub keeps the first file


def test_copy_artifact_checks_the_size_in_the_sandbox(tmp_path: Path) -> None:
    from mechatronikers.session.manager import SessionManager

    class Runtime:
        size = 10
        copied: list[str] = []

        def file_size(self, container: str, path: str) -> int:
            return self.size

        def copy_from(self, container: str, source: str, dest: Path) -> None:
            self.copied.append(source)

    runtime = Runtime()
    manager = SessionManager(runtime, None, None, tmp_path)  # type: ignore[arg-type]
    assert manager.copy_artifact("ses-1", "build-1/a.stl", tmp_path, 100) == tmp_path / "a.stl"
    runtime.size = 101
    with pytest.raises(ArtifactTooLargeError):
        manager.copy_artifact("ses-1", "build-1/a.stl", tmp_path, 100)
    assert runtime.copied == ["/session/artifacts/build-1/a.stl"]
    with pytest.raises(ValueError):
        manager.copy_artifact("ses-1", "../a.stl", tmp_path, 100)


def test_provider_keys_are_redacted(h: Hub, host: Host) -> None:
    key = "sk-or-v1-" + "a1" * 20
    box = EventEmitter("cad-mcp", session_id=host.record.id, stream=_Sink(host, "session"))
    box.emit("tool.completed", {"output": f"env says {key}"})
    forwarder = _forwarder(h, host, environ={"OPENROUTER_API_KEY": key, "HOME": "/home/x" * 4})
    forwarder.forward_once()
    last = _hub_events(h)[-1]["event"]
    assert last["payload"] == {"output": "env says [redacted]"}
    assert provider_secrets({"OPENROUTER_API_KEY": key, "PATH": "/usr/bin" * 4}) == [key]


def test_open_pois_match_the_host_after_reviews_in_the_sandbox(h: Hub, host: Host) -> None:
    """The reviewer approves the run's two open POIs in the sandbox's review app (mode 1): the
    hub's overview follows the host's run table (D99)."""
    review = EventEmitter("review-app", session_id=host.record.id, stream=_Sink(host, "session"))
    for item in ("rev1-r-1", "rev1-a-1"):
        review.emit("review.approved", {"item_id": item, "reviewer": "Anna"})
    _forwarder(h, host).forward_once()
    hub_row = h.client.get(f"/api/runs/{RUN}/run", headers=BROWSER).json()
    host_row = run_table(host.sessions, host.store)[0]
    assert host_row.open_pois == 0 and hub_row["open_pois"] == 0
    assert hub_row["phase"] == host_row.phase == "valid"
