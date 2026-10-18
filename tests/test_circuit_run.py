import asyncio
import io
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from mechatronikers.circuit_mcp.dev import SESSION, CollectorSink, create_dev_app
from mechatronikers.circuit_mcp.run import CircuitRun, RunError, _circuit_link, main
from mechatronikers.session.runs import open_pois
from mechatronikers.spec import ValidationReport
from mechatronikers.spec.circuit import CircuitGatePayload, Correction
from mechatronikers.spec.review import GateDecision
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.collector_app import create_collector_app
from mechatronikers.telemetry.local_api import ingest_key
from mechatronikers.telemetry.store import EventStore
from tests.fixtures.schematics import make_schematic


@pytest.fixture
def dev(tmp_path: Path) -> Iterator[tuple[str, Collector]]:
    """The dev server: the same routes as a session gateway, without a sandbox."""
    (tmp_path / "data").mkdir()
    make_schematic(tmp_path / "data" / "divider.pdf")
    app, collector = create_dev_app(tmp_path / "data", tmp_path / "out")
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
    yield f"http://127.0.0.1:{port}", collector
    server.should_exit = True
    thread.join(timeout=10)


def _run(url: str, collector: Collector, max_rounds: int = 3, **kwargs: Any) -> dict[str, Any]:
    sink = CollectorSink(collector, "agent")
    emitter = EventEmitter("agent", session_id=SESSION, run_id="run-test", stream=sink)
    run = CircuitRun(url, "dev", emitter, poll_s=0.1, max_rounds=max_rounds)
    return asyncio.run(run.execute("divider.pdf", **kwargs))


def _types(collector: Collector) -> list[str]:
    return [e.type for _, e in collector.store.events(SESSION)]


def test_auto_approved_run_has_the_drawing2cad_steps(dev: tuple[str, Collector]) -> None:
    url, collector = dev
    outcome = _run(url, collector, auto_approve=True)
    assert outcome["status"] == "done" and outcome["checks_passed"] == outcome["checks_total"]
    assert [a["format"] for a in outcome["artifacts"]] == ["spice", "ltspice", "kicad-netlist"]
    events = collector.store.events(SESSION)
    steps = [e.payload["step"] for _, e in events if e.type == "agent.step_started"]
    assert steps == ["analyse", "check", "review_gate_1", "check", "export", "simulate"]
    types = _types(collector)
    assert types[0] == "agent.run_started" and types[-1] == "agent.run_finished"
    assert "validation.completed" in types and "review.gate_passed" in types
    passed = next(e for _, e in events if e.type == "review.gate_passed")
    assert passed.payload["auto"] is True and passed.payload["run_kind"] == "circuit"
    history = httpx.get(f"{url}/events/history").json()
    assert history[0]["event"]["type"] == "agent.run_started"
    assert history[0]["event"]["payload"]["kind"] == "circuit"


def test_checks_and_sheet_use_the_shared_contract(dev: tuple[str, Collector]) -> None:
    url, collector = dev
    _run(url, collector, auto_approve=True)
    events = [e for _, e in collector.store.events(SESSION)]
    validation = next(e.payload for e in events if e.type == "validation.completed")
    report = ValidationReport.model_validate(validation["report"])  # the part builds' shape
    assert {c.category for c in report.checks + report.advisory} == {"ERC"}
    assert [c.id.split(".")[0] for c in report.checks] == [f"E{n}" for n in range(1, 8)]
    assert [c.id.split(".")[0] for c in report.advisory] == ["A1", "A2", "A3"]
    assert report.passed is validation["passed"] is True
    sheets = [e.payload for e in events if e.type == "artifact.created" and e.payload.get("role")]
    assert [(s["role"], s["dpi"], s["format"]) for s in sheets] == [
        ("sheet", 150, "png"),
        ("sheet", 200, "png"),
    ]
    assert all(Path(s["path"]).is_file() and s["uri"].endswith(".png") for s in sheets)


Decide = Callable[[dict[str, Any]], dict[str, Any]]


def _reviewer(url: str, *decisions: Decide) -> threading.Thread:
    """Decides the open gates one after another; each decision sees the gate request."""

    def work() -> None:
        done: set[str] = set()
        for _ in range(400):
            gates = [g for g in httpx.get(f"{url}/api/review/gates").json() if g["open"]]
            for gate in (g for g in gates if g["request"]["id"] not in done):
                request = gate["request"]
                body = {"gate_id": request["id"], "reviewer": "ana",
                        "decided_at": "2026-10-17T12:00:00.000000Z"}  # fmt: skip
                body |= decisions[len(done)](request)
                httpx.post(f"{url}/api/review/gates/{request['id']}/decision", json=body)
                done.add(request["id"])
            if len(done) == len(decisions):
                return
            time.sleep(0.05)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def _approve(_: dict[str, Any]) -> dict[str, Any]:
    return {"action": "approve"}


def _net_id(request: dict[str, Any], name: str) -> str:
    return next(n["id"] for n in request["circuit"]["nets"] if n["name"] == name)


def _exports(tmp_path: Path) -> dict[str, str]:
    folder = next((tmp_path / "out" / "artifacts" / "circuit").iterdir())
    return {ext: (folder / f"divider{ext}").read_text() for ext in (".cir", ".asc", ".net")}


def test_gate_1_waits_for_the_reviewer(dev: tuple[str, Collector]) -> None:
    url, collector = dev
    decider = _reviewer(url, _approve)
    outcome = _run(url, collector)
    decider.join(timeout=5)
    assert outcome["status"] == "done" and outcome["revision"] == 1
    types = _types(collector)
    order = [types.index(t) for t in ("agent.interrupt", "review.gate_passed", "agent.resumed")]
    assert order == sorted(order)
    gate = httpx.get(f"{url}/api/review/gates").json()[0]["request"]
    assert gate["kind"] == "spec_review" and "R1 · resistor · 10k" in gate["features"]
    assert (gate["run_kind"], gate["drawing"]) == ("circuit", "divider.pdf")
    circuit = gate["circuit"]
    assert [c["refdes"] for c in circuit["components"]] == ["R1", "R2", "R3", "V1"]
    assert {n["name"] for n in circuit["nets"]} >= {"OUT", "GND", "VREF"}
    assert circuit["sheet_size"] == gate["sheet_size"] and circuit["schematic_id"]
    opened = next(e for _, e in collector.store.events(SESSION) if e.type == "review.gate_opened")
    assert opened.payload["run_kind"] == "circuit"


def test_approved_corrections_are_applied_and_exported(
    dev: tuple[str, Collector], tmp_path: Path
) -> None:
    url, collector = dev
    fix = {"target": "component", "id": "R2", "field": "value", "old": "4k7", "new": "47k"}
    decider = _reviewer(url, lambda _: {"action": "approve", "corrections": [fix]})
    assert _run(url, collector)["status"] == "done"
    decider.join(timeout=5)
    gate = httpx.get(f"{url}/api/review/gates").json()[0]
    events = [e for _, e in collector.store.events(SESSION)]
    updated = next(e.payload for e in events if e.type == "circuit.component_updated")
    assert (updated["refdes"], updated["changes"], updated["by"], updated["gate_id"]) == (
        "R2",
        {"value": "47k"},
        "human",
        gate["request"]["id"],
    )
    assert updated["old"] == {"value": "4k7"}
    applied = next(e.payload for e in events if e.type == "agent.corrections_applied")
    assert applied["corrections"] == [fix] and applied["gate_id"] == gate["request"]["id"]
    assert "agent.corrections_not_applied" not in _types(collector)
    exports = _exports(tmp_path)
    assert "R2 OUT 0 47k" in exports[".cir"] and "47k" in exports[".asc"]
    assert '"47k"' in exports[".net"] and "4k7" not in exports[".net"]


def test_reject_with_notes_revises_the_circuit(dev: tuple[str, Collector], tmp_path: Path) -> None:
    url, collector = dev

    def reject(request: dict[str, Any]) -> dict[str, Any]:
        rename = {"target": "net", "id": _net_id(request, "VREF"), "field": "name", "new": "REF"}
        value = {"target": "component", "id": "R2", "field": "value", "new": "47k"}
        return {"action": "reject", "reason": "R2 is 47k, the label reads REF",
                "corrections": [value, rename]}  # fmt: skip

    decider = _reviewer(url, reject, _approve)
    outcome = _run(url, collector)
    decider.join(timeout=5)
    assert (outcome["status"], outcome["revision"], outcome["build_id"]) == ("done", 2, "erc-r2")
    first, second = (g["request"] for g in httpx.get(f"{url}/api/review/gates").json())
    assert (first["revision"], second["revision"]) == (1, 2)
    assert second["id"] == f"{first['id']}-r2"
    assert {n["name"] for n in second["circuit"]["nets"]} >= {"REF", "OUT"}
    r2 = next(c for c in second["circuit"]["components"] if c["refdes"] == "R2")
    assert (r2["value"], r2["source"], r2["status"]) == ("47k", "human", "edited")

    events = [e for _, e in collector.store.events(SESSION)]
    steps = [e.payload["step"] for e in events if e.type == "agent.step_started"]
    assert steps == ["analyse", "check", "review_gate_1", "revise", "check", "review_gate_1",
                     "check", "export", "simulate"]  # fmt: skip
    started = next(e.payload for e in events if e.type == "agent.revision_started")
    assert (started["revision"], started["gate_id"], started["corrections"]) == (2, first["id"], 2)
    renamed = next(e.payload for e in events if e.type == "circuit.net_renamed")
    assert (renamed["old"], renamed["new"], renamed["by"], renamed["gate_id"]) == (
        "VREF",
        "REF",
        "human",
        first["id"],
    )
    builds = [e.payload["build_id"] for e in events if e.type == "validation.completed"]
    assert builds == ["erc-r1", "erc-r2", "erc-r2"]
    assert "review.rejected" in _types(collector) and events[-1].type == "agent.run_finished"
    exports = _exports(tmp_path)
    assert "R2 OUT 0 47k" in exports[".cir"] and "R3 REF" in exports[".cir"]
    assert "VREF" not in exports[".cir"] and "REF" in exports[".net"]


def test_kind_and_role_corrections_reach_the_exports(
    dev: tuple[str, Collector], tmp_path: Path
) -> None:
    url, collector = dev
    fixes = [
        {"target": "terminal", "id": "V1.1", "field": "role", "new": "-"},
        {"target": "terminal", "id": "V1.2", "field": "role", "new": "+"},
        {"target": "component", "id": "R3", "field": "kind", "new": "capacitor"},
        {"target": "component", "id": "R3", "field": "value", "new": "100n"},
    ]
    decider = _reviewer(url, lambda _: {"action": "approve", "corrections": fixes})
    assert _run(url, collector)["status"] == "done"
    decider.join(timeout=5)
    exports = _exports(tmp_path)
    assert "V1 0 N2 DC 5" in exports[".cir"]  # the swapped source
    assert "CR3 VREF 0 100n" in exports[".cir"] and "SYMBOL cap" in exports[".asc"]
    assert '(comp (ref "R3") (value "100n"))' in exports[".net"]


def test_corrections_route_applies_only_a_decided_gate(dev: tuple[str, Collector]) -> None:
    url, _ = dev
    sid = httpx.post(f"{url}/circuit/api/schematics", json={"path": "divider.pdf"}).json()[
        "schematic_id"
    ]
    spec = httpx.get(f"{url}/circuit/api/schematics/{sid}").json()
    payload = {"schematic_id": sid, "components": spec["components"], "nets": []}
    gate = {"id": "gate-x-1", "kind": "spec_review", "run_id": "x", "run_kind": "circuit",
            "created_at": "2026-10-17T12:00:00.000000Z", "circuit": payload}  # fmt: skip
    assert httpx.post(f"{url}/api/review/gates", json=gate).status_code == 201
    route = f"{url}/circuit/api/schematics/{sid}/corrections"
    assert httpx.post(route, json={"gate_id": "gate-x-1"}).status_code == 422  # undecided
    assert httpx.post(route, json={"gate_id": "gate-nope"}).status_code == 404
    assert httpx.post(route, json={"gate_id": "../etc"}).status_code == 422
    fix = {"target": "component", "id": "R1", "field": "value", "new": "22k"}
    decision = {"gate_id": "gate-x-1", "action": "approve", "reviewer": "ana",
                "decided_at": "2026-10-17T12:00:00.000000Z", "corrections": [fix]}  # fmt: skip
    httpx.post(f"{url}/api/review/gates/gate-x-1/decision", json=decision).raise_for_status()
    other = f"{url}/circuit/api/schematics/ffffffffffff/corrections"
    assert httpx.post(other, json={"gate_id": "gate-x-1"}).status_code == 422
    applied = httpx.post(route, json={"gate_id": "gate-x-1"})
    assert applied.json() == {"gate_id": "gate-x-1", "applied": 1}
    assert httpx.post(route, json={"gate_id": "gate-x-1"}).status_code == 409  # once per gate
    r1 = next(c for c in httpx.get(f"{url}/circuit/api/schematics/{sid}").json()["components"]
              if c["refdes"] == "R1")  # fmt: skip
    assert (r1["value"], r1["source"]) == ("22k", "human")


def test_driver_checks_every_correction_before_applying() -> None:
    run = CircuitRun("http://127.0.0.1:9", "dev", EventEmitter("agent", stream=io.StringIO()))
    payload = CircuitGatePayload(schematic_id="abc", components=[])
    decision = GateDecision(
        gate_id="gate-r-1", action="approve", reviewer="ana", decided_at="2026-10-17T12:00:00Z",
        corrections=[Correction(target="component", id="R9", field="value", new="1k")],
    )  # fmt: skip
    with pytest.raises(RunError, match="nothing applied"):
        asyncio.run(run._apply(payload, decision))  # fails before any request


def test_max_rounds_is_checked_before_a_sandbox_starts() -> None:
    with pytest.raises(SystemExit):
        main(["start", "--schematic", "x.pdf", "--max-rounds", "0"])


def test_revise_rounds_are_bounded(dev: tuple[str, Collector]) -> None:
    url, collector = dev

    def reject(_: dict[str, Any]) -> dict[str, Any]:
        return {"action": "reject", "reason": "still wrong"}

    decider = _reviewer(url, reject, reject)
    outcome = _run(url, collector, max_rounds=2)
    decider.join(timeout=5)
    assert outcome == {"status": "rejected", "reason": "still wrong", "part_name": "divider",
                       "revision": 2, "rounds_exhausted": True}  # fmt: skip
    assert len(httpx.get(f"{url}/api/review/gates").json()) == 2
    events = [e for _, e in collector.store.events(SESSION)]
    assert "export" not in [e.payload.get("step") for e in events]
    assert events[-1].type == "agent.run_finished" and events[-1].payload == outcome
    with pytest.raises(ValueError, match="at least 1"):
        CircuitRun(url, "dev", EventEmitter("agent"), max_rounds=0)


def test_the_review_ui_replaces_the_circuit_page(dev: tuple[str, Collector]) -> None:
    url, collector = dev
    old = httpx.get(f"{url}/circuit/?t=tok", follow_redirects=False)  # links printed before #71
    assert (old.status_code, old.headers["location"]) == (307, "/?t=tok")
    assert httpx.get(f"{url}/circuit/review.js").status_code == 404
    page = httpx.get(f"{url}/")  # the shared UI (or its placeholder without a build)
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    config = httpx.get(f"{url}/config.json").json()  # what a sandbox serves
    assert config["api"] == {"run": "/api", "events": "/events", "circuit": "/circuit/api"}

    _run(url, collector, auto_approve=True)
    with httpx.stream("GET", f"{url}/events/stream?after=1", timeout=10) as stream:
        lines = stream.iter_lines()
        first = next(line for line in lines if line.startswith("id:"))
    assert first == "id: 2"  # live events resume after the history, like the collector's


def test_circuit_link_keeps_the_token() -> None:
    assert _circuit_link("http://127.0.0.1:1234/?t=abc") == "http://127.0.0.1:1234/circuit/?t=abc"


def test_an_approval_leaves_findings_open_and_a_failed_op_is_one(
    dev: tuple[str, Collector], tmp_path: Path
) -> None:
    """#152 (option B, D70): approving gate 1 accepts nothing on the reviewer's behalf, so the
    netlist header still lists the open item; a failed operating point is a finding too."""
    url, collector = dev
    make_schematic(tmp_path / "data" / "unnamed.pdf", ground_name=False)  # has a review item
    decider = _reviewer(url, _approve)
    sink = CollectorSink(collector, "agent")
    emitter = EventEmitter("agent", session_id=SESSION, run_id="run-items", stream=sink)
    outcome = asyncio.run(CircuitRun(url, "dev", emitter, poll_s=0.1).execute("unnamed.pdf"))
    decider.join(timeout=5)
    assert outcome["status"] == "done"
    shown = httpx.get(f"{url}/api/review/gates").json()[0]["request"]["review_items"]
    events = [e for _, e in collector.store.events(SESSION)]
    assert shown and not [e for e in events if e.type in ("review.approved", "review.resolved")]
    spec = httpx.get(f"{url}/circuit/api/schematics/{outcome['schematic_id']}").json()
    assert {r["status"] for r in spec["review_items"] if r["id"] in {i["id"] for i in shown}} == {
        "open"
    }
    folder = tmp_path / "out" / "artifacts" / "circuit" / outcome["schematic_id"]
    assert f"* {len(shown)} open review item(s)" in (folder / "unnamed.cir").read_text()
    simulated = next(
        e for e in events if e.type == "agent.step_completed" and e.payload["step"] == "simulate"
    )
    finding = [
        e.payload["item"]
        for e in events
        if e.type == "review.item_created" and e.payload["item"]["id"] == "op-r1"
    ]
    if outcome["simulated"]:
        assert simulated.level == "info" and finding == []
        assert open_pois(events) == len(shown)
    else:  # no ngspice here, or it failed: a warning and a finding with the full reason
        assert simulated.level == "warning"
        assert (
            finding[0]["message"] == f"Operating point not simulated: {simulated.payload['reason']}"
        )
        assert open_pois(events) == len(shown) + 1


@pytest.fixture
def host_collector(tmp_path: Path) -> Iterator[tuple[str, EventStore]]:
    """`session watch` on the host, with the ingest key of `tmp_path / "var"` (#146)."""
    store = EventStore(tmp_path / "host.db")
    app = create_collector_app(
        Collector(store, tmp_path / "host-storage"), ingest_key=ingest_key(tmp_path / "var")
    )
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
    yield f"http://127.0.0.1:{port}", store
    server.should_exit = True
    thread.join(timeout=10)
    store.close()


def _run_cli(url: str, var: Path, collector_url: str) -> int:
    """`circuit_mcp.run run` against a session (the dev server stands in for the sandbox)."""
    return main(
        ["--var-dir", str(var), "--collector-url", collector_url, "run", "--url", url]
        + ["--session-id", "ses-runcli0001", "--schematic", "divider.pdf", "--auto-approve"]
    )


def test_run_command_sends_its_events_to_the_host_collector(
    dev: tuple[str, Collector], host_collector: tuple[str, EventStore], tmp_path: Path
) -> None:
    """A sandbox's gateway has no `/ingest`: `run` posts to the host collector with the key,
    like `start` and the agent, instead of losing or crashing on its events (#146 review)."""
    url, sandbox = dev
    collector_url, host = host_collector
    assert _run_cli(url, tmp_path / "var", collector_url) == 0
    types = [e.type for _, e in host.events("ses-runcli0001")]
    assert types[0] == "agent.run_started" and types[-1] == "agent.run_finished"
    # nothing went to the session's own URL (the dev server's /ingest has no key and refuses)
    assert not [e for _, e in sandbox.store.events("ses-runcli0001")]


def test_run_command_keeps_its_events_while_the_collector_is_down(
    dev: tuple[str, Collector], tmp_path: Path
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()
    assert _run_cli(dev[0], tmp_path / "var", closed) == 0
    local = EventStore(tmp_path / "var" / "state.db")  # the store `session watch` reads
    try:
        types = [e.type for _, e in local.events("ses-runcli0001")]
    finally:
        local.close()
    assert types[0] == "agent.run_started" and types[-1] == "agent.run_finished"
