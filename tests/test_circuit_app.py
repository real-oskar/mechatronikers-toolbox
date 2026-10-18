import io
import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mechatronikers.circuit_mcp.app import create_circuit_app
from mechatronikers.circuit_mcp.session import CircuitSession, ComponentUpdate
from mechatronikers.circuit_mcp.simulate import error_lines, op_netlist, parse_op, run_op
from mechatronikers.sandbox.app import create_app
from mechatronikers.spec.circuit import CircuitGatePayload, Correction
from mechatronikers.spec.poi import ReviewItem
from mechatronikers.telemetry import EventEmitter
from tests.fixtures.schematics import make_schematic

# ngspice batch output for `.op`, recorded from ngspice 39 in the sandbox image (shortened).
NGSPICE_OP = """
Note: No compatibility mode selected!

Circuit: * divider.pdf: netlist read by circuit-mcp (spec 0.1.0)

Doing analysis at TEMP = 27.000000 and TNOM = 27.000000

No. of Data Rows : 1
\tNode                                  Voltage
\t----                                  -------
\t----\t-------
\tout                              1.600000e+00
\tvref                             0.000000e+00
\tn2                               5.000000e+00

\tSource\tCurrent
\t------\t-------

\tv1#branch                        -3.40426e-04

"""


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf", ground_name=False)
    app, _ = create_circuit_app(
        EventEmitter("circuit-mcp", stream=io.StringIO()), tmp_path / "input", tmp_path / "out"
    )
    return TestClient(app)


def test_old_page_redirects_and_api_round_trip(client: TestClient) -> None:
    page = client.get("/", follow_redirects=False)  # the circuit panels live in the run view
    assert (page.status_code, page.headers["location"]) == (307, "/")
    # the token link keeps its token: a cross-site hop does not send the SameSite=Strict cookie
    linked = client.get("/?t=abc123", follow_redirects=False)
    assert linked.headers["location"] == "/?t=abc123"
    assert client.get("/api/files").json() == [{"path": "divider.pdf", "schematic_id": None}]

    sid = client.post("/api/schematics", json={"path": "divider.pdf"}).json()["schematic_id"]
    assert client.get("/api/files").json()[0]["schematic_id"] == sid
    spec = client.get(f"/api/schematics/{sid}").json()
    assert [c["refdes"] for c in spec["components"]] == ["R1", "R2", "R3", "V1"]
    sheet = client.get(f"/api/schematics/{sid}/sheet.png")
    assert sheet.headers["content-type"] == "image/png" and sheet.content[:4] == b"\x89PNG"

    item = spec["review_items"][0]["id"]
    done = client.post(f"/api/schematics/{sid}/items/{item}", json={"action": "approve"}).json()
    assert done["status"] == "approved" and done["history"][0]["by"] == "reviewer"
    comp = client.post(f"/api/schematics/{sid}/components/R3", json={"value": "2k2"}).json()
    assert comp["value"] == "2k2" and comp["source"] == "human"
    assert client.get(f"/api/schematics/{sid}/checks").json()["passed"]

    out = client.post(f"/api/schematics/{sid}/export", json={}).json()
    names = [Path(a["path"]).name for a in out["artifacts"]]
    assert names == ["divider.cir", "divider.asc", "divider.net"]
    cir = client.get(f"/api/schematics/{sid}/files/divider.cir")
    assert cir.status_code == 200 and "R3 VREF 0 2.2k" in cir.text


def test_api_rejects_bad_input(client: TestClient) -> None:
    assert client.post("/api/schematics", json={"path": "../x.pdf"}).status_code == 422
    assert client.get("/api/schematics/nope").status_code == 404
    sid = client.post("/api/schematics", json={"path": "divider.pdf"}).json()["schematic_id"]
    for name in ("..%2F..%2Fsecret.cir", "circuit.txt", "missing.cir"):
        assert client.get(f"/api/schematics/{sid}/files/{name}").status_code == 404, name
    bad = client.post(f"/api/schematics/{sid}/items/ri-1", json={"action": "delete"})
    assert bad.status_code == 422
    assert client.post("/api/schematics", content=b"x" * 70_000).status_code == 422


def test_sandbox_mounts_the_circuit_add_on(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    with TestClient(create_app("ses-test", tmp_path / "builds", "subprocess")) as client:
        old = client.get("/circuit/")  # old links land on the run view at /
        assert old.status_code == 200 and str(old.url).endswith("/")
        assert old.history[0].headers["location"] == "/"
        assert client.get("/circuit/api/files").json() == []
        assert client.get("/health").json()["status"] == "ok"


def test_parse_ngspice_operating_point() -> None:
    voltages, currents = parse_op(NGSPICE_OP)
    assert voltages == {"out": 1.6, "vref": 0.0, "n2": 5.0}
    assert currents == {"v1#branch": pytest.approx(-3.40426e-04)}
    assert op_netlist("* x\nR1 a 0 1k\n.tran 1m\n.ac dec 10 1 1k\n.end\n") == (
        "* x\nR1 a 0 1k\n.op\n.end\n"
    )


def test_simulation_needs_a_complete_netlist_and_ngspice(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    buf = io.StringIO()
    session = CircuitSession(tmp_path / "input", tmp_path / "out", EventEmitter("x", stream=buf))
    sid, spec = session.load("divider.pdf")
    spec.component("R1").value = None
    refused = session.simulate(sid)
    assert not refused.ok and "R1" in refused.reason
    assert json.loads(buf.getvalue().splitlines()[-1])["type"] == "circuit.simulated"
    if shutil.which("ngspice") is None:
        result = run_op("R1 a 0 1k\nV1 a 0 DC 1\n.end\n", tmp_path / "sim")
        assert not result.ok and "not installed" in result.reason


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_divider_operating_point_with_ngspice(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    session = CircuitSession(
        tmp_path / "input", tmp_path / "out", EventEmitter("x", stream=io.StringIO())
    )
    sid, _ = session.load("divider.pdf")
    result = session.simulate(sid)
    assert result.ok, result.log_tail
    assert result.node_voltages["out"] == pytest.approx(5 * 4.7 / 14.7, rel=1e-4)


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_mega_ohm_operating_point_with_ngspice(tmp_path: Path) -> None:
    """1 MΩ is 1e6 Ω in the simulation, not 1 mΩ (#143): 5 V over 1 MΩ + 4k7 gives ~23 mV."""
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    session = CircuitSession(
        tmp_path / "input", tmp_path / "out", EventEmitter("x", stream=io.StringIO())
    )
    sid, _ = session.load("divider.pdf")
    session.update_component(sid, "R1", ComponentUpdate(value="1MΩ"), source="human")
    result = session.simulate(sid)
    assert result.ok, result.log_tail
    assert result.node_voltages["out"] == pytest.approx(5 * 4.7e3 / (1e6 + 4.7e3), rel=1e-4)


def test_tool_names_are_unique_across_the_sandbox_servers(tmp_path: Path) -> None:
    # The agent's MCP client merges the tools of all servers by name.
    import asyncio

    from mechatronikers.cad_mcp.server import create_server as create_cad
    from mechatronikers.circuit_mcp.server import create_server as create_circuit
    from mechatronikers.drawing_mcp.server import create_server as create_drawing

    def names(server: object) -> set[str]:
        return {t.name for t in asyncio.run(server.list_tools())}  # type: ignore[attr-defined]

    quiet = EventEmitter("x", stream=io.StringIO())
    drawing, circuit = names(create_drawing(quiet)), names(create_circuit(quiet))
    cad = names(create_cad(quiet, tmp_path, "subprocess"))
    assert not drawing & circuit and not cad & circuit and not drawing & cad


NGSPICE_MODEL_ERROR = """
Circuit: * rectifier_sch.pdf: netlist read by circuit-mcp (spec 0.1.0)

Error on line 5 or its substitute:
  d1 n2 rect_out 1n4148
could not find a valid modelname
Simulation interrupted due to error!
"""


def test_ngspice_errors_keep_what_they_refer_to() -> None:
    """#152: the first line alone ("Error on line 5 or its substitute:") said nothing."""
    assert error_lines(NGSPICE_MODEL_ERROR) == [
        "Error on line 5 or its substitute: d1 n2 rect_out 1n4148",
        "could not find a valid modelname",
        "Simulation interrupted due to error!",
    ]
    assert error_lines("No. of Data Rows : 1\n\tout 1.6\n") == []


def test_no_simulation_without_device_models(tmp_path: Path) -> None:
    """ngspice has no 1N4148 of its own: the reason names the missing model (#152)."""
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    session = CircuitSession(
        tmp_path / "input", tmp_path / "out", EventEmitter("x", stream=io.StringIO())
    )
    sid, spec = session.load("divider.pdf")
    diode = spec.component("R3")
    diode.kind, diode.value = "diode", "1N4148"
    for terminal, role in zip(diode.terminals, ("A", "K"), strict=True):
        terminal.role = role  # type: ignore[assignment]
    result = session.simulate(sid)
    assert not result.ok
    assert result.reason == (
        "no SPICE model for 1N4148: the netlist needs a .model or .subckt for it"
    )


def test_a_value_correction_resolves_the_missing_value_item(tmp_path: Path) -> None:
    """#129, #152: the reviewer gave R1 the value the analysis could not find, so its "No value
    found for R1" is resolved by that correction; other items stay open (D70)."""
    (tmp_path / "input").mkdir()
    make_schematic(tmp_path / "input" / "divider.pdf")
    buf = io.StringIO()
    session = CircuitSession(tmp_path / "input", tmp_path / "out", EventEmitter("x", stream=buf))
    sid, spec = session.load("divider.pdf")
    spec.component("R1").value = None
    spec.review_items += [
        ReviewItem(id="ri-v", kind="possibly_missing", message="No value found for R1",
                   reason="no unassigned text near the symbol", feature_ids=["R1"]),
        ReviewItem(id="ri-r", kind="low_confidence", message="Pin roles of R2 need confirmation",
                   reason="x", feature_ids=["R2"]),
    ]  # fmt: skip
    payload = CircuitGatePayload(schematic_id=sid, components=spec.components, nets=[])
    fix = Correction(target="component", id="R1", field="value", new="10k")
    session.apply_corrections(payload, [fix], "gate-r-1", reviewer="Anna")
    items = {r.id: r for r in session.get(sid).review_items}
    assert (items["ri-v"].status, items["ri-r"].status) == ("resolved", "open")
    assert (items["ri-v"].history[-1].by, items["ri-v"].history[-1].comment) == (
        "Anna",
        "resolved by the correction at gate-r-1: value 10k",
    )
    resolved = [json.loads(line) for line in buf.getvalue().splitlines()]
    resolved = [e["payload"] for e in resolved if e["type"] == "review.resolved"]
    assert resolved == [
        {"schematic_id": sid, "item_id": "ri-v", "by": "Anna", "gate_id": "gate-r-1"}
    ]
    # a value for a part that had one is no reason to close anything
    session.apply_corrections(
        payload, [Correction(target="component", id="R2", field="value", new="4k7")], "gate-r-2"
    )
    assert session.get(sid).review_items[-1].status == "open"
