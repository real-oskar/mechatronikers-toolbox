import asyncio
import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from langchain_mcp_adapters.client import MultiServerMCPClient

from mechatronikers.sandbox.app import create_app
from mechatronikers.session.connections import mcp_connections
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.instrument import instrument


def test_instrument_emits_events_and_keeps_signature() -> None:
    buf = io.StringIO()

    @instrument(EventEmitter("cad-mcp", stream=buf), "cad-mcp")
    def add(a: int, b: int = 2) -> int:
        return a + b

    @instrument(EventEmitter("cad-mcp", stream=buf), "cad-mcp")
    def boom() -> None:
        raise ValueError("no")

    assert add(a=1) == 3
    with pytest.raises(ValueError):
        boom()
    import inspect

    assert list(inspect.signature(add).parameters) == ["a", "b"]
    types = [json.loads(line)["type"] for line in buf.getvalue().splitlines()]
    assert types == ["tool.called", "tool.completed", "tool.called", "tool.failed"]


@pytest.fixture
def server_url(tmp_path: Path) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = create_app("ses-test", tmp_path / "builds", "subprocess")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_agent_lists_tools_and_builds_through_mcp(server_url: str) -> None:
    async def scenario() -> tuple[list[str], dict]:  # type: ignore[type-arg]
        client = MultiServerMCPClient(mcp_connections(server_url, "unused"))  # type: ignore[arg-type]
        tools = {t.name: t for t in await client.get_tools()}
        out = await tools["run_script"].ainvoke({"code": "result = Box(5, 5, 5)", "reason": "test"})
        return sorted(tools), json.loads(out[0]["text"])

    names, result = asyncio.run(scenario())
    assert {
        "cad_ping",
        "drawing_ping",
        "run_script",
        "new_part",
        "create_extrude_base",
        "add_hole",
        "add_hole_pattern",
        "add_fillet",
        "update_feature",
        "export",
        "render_views",
        "validate_part",
        "load_drawing",
        "render_labels",
        "check_coverage",
        "load_schematic",
        "export_netlist",
        "simulate_op",
    } <= set(names)
    assert result["status"] == "ok"
    assert result["report"]["passed"]


def test_agent_builds_a_part_with_feature_tools(server_url: str) -> None:
    async def scenario() -> tuple[dict, dict, dict]:  # type: ignore[type-arg]
        client = MultiServerMCPClient(mcp_connections(server_url, "unused"))  # type: ignore[arg-type]
        tools = {t.name: t for t in await client.get_tools()}

        async def call(name: str, args: dict) -> dict:  # type: ignore[type-arg]
            out = await tools[name].ainvoke(args)
            return json.loads(out[0]["text"])  # type: ignore[no-any-return]

        await call("new_part", {"part_name": "Plate", "overall_size": [40, 30, 8]})
        base = await call(
            "create_extrude_base",
            {
                "feature": {
                    "id": "base",
                    "depth": 8,
                    "profile": {
                        "segments": [
                            {"kind": "line", "start": [-20, -15], "end": [20, -15]},
                            {"kind": "line", "start": [20, -15], "end": [20, 15]},
                            {"kind": "line", "start": [20, 15], "end": [-20, 15]},
                            {"kind": "line", "start": [-20, 15], "end": [-20, -15]},
                        ]
                    },
                }
            },
        )
        bad = await call(
            "add_hole",
            {
                "feature": {
                    "id": "air",
                    "diameter": 5,
                    "placement": {"origin": [99, 0, 8], "normal": [0, 0, -1]},
                }
            },
        )
        hole = await call(
            "add_hole",
            {
                "feature": {
                    "id": "h1",
                    "diameter": 6,
                    "placement": {"origin": [10, 0, 8], "normal": [0, 0, -1]},
                }
            },
        )
        return base, bad, hole

    base, bad, hole = asyncio.run(scenario())
    assert base["applied"] and base["build"]["report"]["passed"]
    assert not bad["applied"] and "air" in bad["message"]
    assert hole["applied"] and hole["build"]["report"]["passed"]
