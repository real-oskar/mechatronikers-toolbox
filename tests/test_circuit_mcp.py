import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.circuit_mcp.server import create_server
from mechatronikers.circuit_mcp.session import (
    CircuitSession,
    ComponentUpdate,
    ReviewAppliedError,
    correction_calls,
)
from mechatronikers.spec.circuit import CircuitGatePayload, Correction
from mechatronikers.telemetry import EventEmitter
from tests.fixtures.schematics import make_schematic


def _call(server: Any, name: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(server.call_tool(name, args))
    return result[1] if isinstance(result, tuple) else result


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    return tmp_path


def test_tools_load_correct_check_and_export(workspace: Path) -> None:
    buf = io.StringIO()
    server = create_server(
        EventEmitter("circuit-mcp", stream=buf), workspace / "input", workspace / "artifacts"
    )
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"load_schematic", "update_component", "check_circuit", "export_netlist"} <= names

    overview = _call(server, "load_schematic", {"path": "divider.pdf"})
    sid = overview["schematic_id"]
    assert overview["components"] == 4 and overview["checks_passed"]

    comps = _call(server, "list_components", {"schematic_id": sid})["result"]
    assert [c["refdes"] for c in comps] == ["R1", "R2", "R3", "V1"]
    updated = _call(
        server, "update_component", {"schematic_id": sid, "refdes": "R3", "value": "2k2"}
    )
    assert updated["value"] == "2k2" and updated["status"] == "edited"

    result = _call(server, "export_netlist", {"schematic_id": sid})
    assert [a["format"] for a in result["artifacts"]] == ["spice", "ltspice", "kicad-netlist"]
    cir = Path(result["artifacts"][0]["path"]).read_text()
    assert "R3 VREF 0 2.2k" in cir
    assert result["artifacts"][0]["uri"].startswith("artifact://local/circuit/")
    saved = json.loads((workspace / "artifacts" / "circuit" / sid / "circuit.json").read_text())
    assert saved["components"][2]["value"] == "2k2"

    image = asyncio.run(server.call_tool("render_component", {"schematic_id": sid, "refdes": "R1"}))
    assert image[0].type == "image"
    events = [json.loads(line)["type"] for line in buf.getvalue().splitlines()]
    for kind in ("circuit.loaded", "circuit.component_updated", "circuit.exported"):
        assert kind in events


def test_session_rejects_paths_outside_input_and_unknown_terminals(workspace: Path) -> None:
    session = CircuitSession(
        workspace / "input", workspace / "artifacts", EventEmitter("x", stream=io.StringIO())
    )
    with pytest.raises(ValueError, match="inside"):
        session.load("../escape.pdf")
    sid, _ = session.load("divider.pdf")
    with pytest.raises(KeyError, match="no terminal"):
        session.update_component(sid, "V1", ComponentUpdate(roles={"V1.9": "+"}))
    with pytest.raises(ValueError):
        session.set_net_name(sid, "N1", "bad\nname")


def test_review_actions_are_recorded(workspace: Path) -> None:
    make_schematic(workspace / "input" / "nameless.pdf", ground_name=False)
    session = CircuitSession(
        workspace / "input", workspace / "artifacts", EventEmitter("x", stream=io.StringIO())
    )
    sid, spec = session.load("nameless.pdf")
    item = spec.review_items[0]
    approved = session.review(sid, item.id, "approve", note="ground symbol confirmed", actor="ana")
    assert approved.status == "approved"
    assert approved.history[0].by == "ana" and approved.history[0].action == "approved"


def test_review_events_use_the_review_app_names(workspace: Path) -> None:
    make_schematic(workspace / "input" / "nameless.pdf", ground_name=False)
    buf = io.StringIO()
    session = CircuitSession(
        workspace / "input", workspace / "artifacts", EventEmitter("x", stream=buf)
    )
    sid, spec = session.load("nameless.pdf")
    session.review(sid, spec.review_items[0].id, "note", note="checked the symbol")
    types = [json.loads(line)["type"] for line in buf.getvalue().splitlines()]
    assert types[-1] == "review.note_added"


def test_gate_corrections_map_onto_circuit_mcp_tools(workspace: Path) -> None:
    """What #57's run driver does with a gate decision: one call per component and net."""
    buf = io.StringIO()
    server = create_server(
        EventEmitter("circuit-mcp", stream=buf), workspace / "input", workspace / "artifacts"
    )
    sid = _call(server, "load_schematic", {"path": "divider.pdf"})["schematic_id"]
    net = next(n for n in _call(server, "list_nets", {"schematic_id": sid})["result"]
               if n["name"] == "VREF")  # fmt: skip
    corrections = [
        Correction(target="component", id="R3", field="value", old="1k", new="1k5"),
        Correction(target="terminal", id="R3.1", field="pin", new="7"),
        Correction(target="component", id="R3", field="value", new="2k2"),  # the later one wins
        Correction(target="terminal", id="V1.1", field="role", new="+"),
        Correction(target="net", id=net["id"], field="name", new="REF"),
    ]
    payload = CircuitGatePayload(
        schematic_id=sid,
        components=_call(server, "list_components", {"schematic_id": sid})["result"],
    )
    calls = correction_calls(payload, corrections)
    assert [(name, args.get("refdes") or args.get("net_id")) for name, args in calls] == [
        ("update_component", "R3"),
        ("update_component", "V1"),
        ("set_net_name", net["id"]),
    ]
    assert calls[0][1] == {"schematic_id": sid, "refdes": "R3", "value": "2k2",
                           "pins": {"R3.1": "7"}}  # fmt: skip
    for name, args in calls:
        _call(server, name, args)
    comps = {
        c["refdes"]: c for c in _call(server, "list_components", {"schematic_id": sid})["result"]
    }
    assert comps["R3"]["value"] == "2k2"
    assert [t["pin"] for t in comps["R3"]["terminals"] if t["id"] == "R3.1"] == ["7"]
    nets = _call(server, "list_nets", {"schematic_id": sid})["result"]
    assert [n["name"] for n in nets if n["id"] == net["id"]] == ["REF"]


def test_only_a_decided_gate_records_human_changes(workspace: Path) -> None:
    """The model's tool calls stay `vlm`; a gate's corrections go in all at once or not at all."""
    buf = io.StringIO()
    session = CircuitSession(
        workspace / "input", workspace / "artifacts", EventEmitter("circuit-mcp", stream=buf)
    )
    server = create_server(EventEmitter("circuit-mcp", stream=buf), session=session)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("update_component", "set_net_name"):
        assert "gate_id" not in tools[name].inputSchema["properties"]
    sid = _call(server, "load_schematic", {"path": "divider.pdf"})["schematic_id"]
    model = _call(server, "update_component", {"schematic_id": sid, "refdes": "R1", "value": "12k"})
    assert model["source"] == "vlm"

    spec = session.get(sid)
    fake_r1 = spec.component("R1").model_copy(deep=True)
    fake_r1.terminals[0].id = "R1.9"  # the gate showed a terminal the circuit no longer has
    payload = CircuitGatePayload(
        schematic_id=sid, components=[spec.component("R2"), fake_r1, spec.component("V1")]
    )
    bad = [
        Correction(target="component", id="R2", field="value", new="47k"),
        Correction(target="terminal", id="R1.9", field="pin", new="3"),
    ]
    with pytest.raises(KeyError, match="no terminal"):
        session.apply_corrections(payload, bad, "gate-run-1-1")
    assert spec.component("R2").value == "4k7"  # nothing applied
    with pytest.raises(ValueError, match="did not show"):
        session.apply_corrections(payload, [bad[0].model_copy(update={"id": "R3"})], "gate-x")

    good = [
        bad[0],
        Correction(target="terminal", id="V1.1", field="role", new="-"),
    ]
    assert session.apply_corrections(payload, good, "gate-run-1-1") == 2
    with pytest.raises(ReviewAppliedError):
        session.apply_corrections(payload, good, "gate-run-1-1")
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    updates = [e["payload"] for e in events if e["type"] == "circuit.component_updated"]
    assert [(u["refdes"], u["by"], u.get("gate_id")) for u in updates] == [
        ("R1", "vlm", None),
        ("R2", "human", "gate-run-1-1"),
        ("V1", "human", "gate-run-1-1"),
    ]
    assert updates[1]["old"] == {"value": "4k7"} and updates[2]["old"] == {"V1.1.role": "+"}
