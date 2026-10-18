import io
import json
import re
from pathlib import Path

from mechatronikers.telemetry import EventEmitter


def test_emitter_writes_ndjson_with_increasing_seq() -> None:
    stream = io.StringIO()
    emitter = EventEmitter("cad-mcp", session_id="ses-1", stream=stream)
    emitter.emit("build.started", {"build_id": "b1"})
    emitter.emit("build.completed", {"build_id": "b1"}, duration_ms=12.5)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [e["seq"] for e in lines] == [1, 2]
    assert lines[0]["schema"] == "mechatronikers.event/1"
    assert lines[0]["component"] == "cad-mcp"
    assert lines[0]["session_id"] == "ses-1"
    assert lines[1]["duration_ms"] == 12.5
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", lines[0]["ts"])
    assert lines[1]["mono_ns"] >= lines[0]["mono_ns"]
    assert "recv_ts" not in lines[0]  # set by the collector, not the emitter


def test_agent_events_go_to_the_collector_or_the_local_store(tmp_path: Path) -> None:
    import httpx

    from mechatronikers.telemetry.collector import Collector
    from mechatronikers.telemetry.forward import CollectorStream
    from mechatronikers.telemetry.store import EventStore

    store = EventStore(tmp_path / "events.db")
    collector = Collector(store, tmp_path / "archive")
    posted: list[str] = []

    seen: list[httpx.Headers] = []

    def collector_api(request: httpx.Request) -> httpx.Response:
        posted.append(request.content.decode())
        seen.append(request.headers)
        return httpx.Response(200, json={"stored": 1})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    up = CollectorStream(
        "http://collector", collector, key="host-key", transport=httpx.MockTransport(collector_api)
    )
    EventEmitter("agent", session_id="ses-1", stream=up).emit("agent.step_started")
    assert len(posted) == 1 and '"agent.step_started"' in posted[0]
    # this host's ingest key and a content type no browser may send without a preflight (#146)
    assert seen[0]["x-mt-ingest-key"] == "host-key"
    assert seen[0]["content-type"] == "application/x-ndjson"
    assert store.events("ses-1") == []
    fallback = CollectorStream(
        "http://collector", collector, key="host-key", transport=httpx.MockTransport(down)
    )
    EventEmitter("agent", session_id="ses-1", stream=fallback).emit("agent.step_started")
    assert [e.type for _, e in store.events("ses-1")] == ["agent.step_started"]
    store.close()
