"""Contract tests of the review contract for both run kinds (D75, D84, #69)."""

import inspect
import json
from typing import Any

import pytest
from pydantic import ValidationError

from mechatronikers import spec
from mechatronikers.spec import ValidationReport, part, poi
from mechatronikers.spec import circuit as circuit_spec
from mechatronikers.spec.circuit import (
    CircuitGatePayload,
    Component,
    Correction,
    ErcCheck,
    ErcReport,
    NetSummary,
    Terminal,
)
from mechatronikers.spec.review import GateDecision, GateRequest, GateState, RunSummary
from mechatronikers.spec.validation import Check

NOW = "2026-10-17T12:00:00.000000Z"

# A gate as the review app stored it before SPEC_VERSION 0.5.0 (no run_kind, no corrections).
STORED_0_4 = {
    "request": {
        "id": "gate-abc",
        "kind": "spec_review",
        "run_id": "run-1",
        "revision": 1,
        "created_at": NOW,
        "part_name": "Fuss",
        "features": ["extrude_base base"],
        "assumptions": [],
        "overall_size_dims": None,
        "review_items": [],
        "possibly_missing": [],
        "build_id": None,
        "validation": None,
        "failures": [],
        "iteration": 0,
        "max_iterations": 3,
        "plan": [],
        "dimensions": [],
        "sheet_size": [420.0, 297.0],
        "drawing": "Fuss_dwg.pdf",
    },
    "decision": {
        "gate_id": "gate-abc",
        "action": "approve",
        "reviewer": "Anna",
        "decided_at": NOW,
        "reason": None,
        "hint": None,
        "update": None,
    },
    "open": False,
}


def _payload() -> CircuitGatePayload:
    q1 = Component(
        refdes="Q1",
        kind="bjt",
        value="Q2N2222",
        bbox=(10, 10, 20, 20),
        terminals=[Terminal(id=f"Q1.{n}", at=(10, 10 + n), direction=(-1, 0)) for n in (1, 2, 3)],
    )
    r1 = Component(refdes="R1", kind="resistor", value="10k", bbox=(30, 10, 40, 14))
    nets = [NetSummary(id="n1", name=None, terminals=["Q1.1"], wires=2)]
    return CircuitGatePayload(
        schematic_id="abc123", sheet_size=(297, 210), components=[q1, r1], nets=nets
    )


def _circuit_gate(**extra: Any) -> GateRequest:
    return GateRequest(
        id="gate-run-1-1",
        kind="spec_review",
        run_id="run-1",
        run_kind="circuit",
        created_at=NOW,
        circuit=_payload(),
        **extra,
    )


def test_poi_types_are_shared_and_circuit_code_does_not_import_part_types() -> None:
    for name in ("ReviewItem", "SourceRef", "ReviewAction", "ModelRef"):
        assert getattr(part, name) is getattr(poi, name) is getattr(spec, name)
    assert part.Status is poi.Status
    assert "spec.part" not in inspect.getsource(circuit_spec)
    assert tuple(int(n) for n in spec.SPEC_VERSION.split(".")) >= (0, 5, 0)  # later bumps add


def test_state_stored_before_0_5_loads_as_a_part_gate() -> None:
    state = GateState.model_validate_json(json.dumps(STORED_0_4))
    assert state.request.run_kind == "part" and state.request.circuit is None
    assert state.decision is not None and state.decision.corrections == []
    assert RunSummary(session_id="s", state="running", phase="valid", started_at=NOW).run_kind == (
        "part"
    )


def test_circuit_payload_round_trips_and_only_on_circuit_gates() -> None:
    gate = _circuit_gate(drawing="divider.pdf")
    dumped = GateRequest.model_validate_json(gate.model_dump_json())
    assert dumped == gate and dumped.circuit is not None
    assert [c.refdes for c in dumped.circuit.components] == ["Q1", "R1"]
    assert gate.part_name is None and gate.features == []  # part fields stay flat and optional
    with pytest.raises(ValidationError, match="belongs to a circuit run"):
        GateRequest(id="g", kind="spec_review", run_id="r", created_at=NOW, circuit=_payload())
    # the payload is optional (additive contract)
    GateRequest(id="g", kind="spec_review", run_id="r", run_kind="circuit", created_at=NOW)


def test_corrections_are_checked_per_target() -> None:
    ok = [
        Correction(target="component", id="R1", field="value", old="10k", new="47k"),
        Correction(target="component", id="Q1", field="kind", new="mosfet"),
        Correction(target="component", id="Q1", field="polarity", new="pnp"),
        Correction(target="terminal", id="Q1.2", field="role", old=None, new="B"),
        Correction(target="terminal", id="Q1.3", field="pin", new="3"),
        Correction(target="net", id="n1", field="name", new="VCC"),
    ]
    bad: list[tuple[dict[str, Any], str]] = [
        ({"target": "net", "id": "n1", "field": "value", "new": "1"}, "net correction changes"),
        ({"target": "component", "id": "R1", "field": "kind", "new": "fuse"}, "kind must be"),
        ({"target": "terminal", "id": "Q1.2", "field": "role", "new": "X"}, "role must be"),
        ({"target": "component", "id": "Q1", "field": "polarity", "new": "x"}, "polarity must"),
        ({"target": "net", "id": "n1", "field": "name", "new": "N" * 65}, "at most 64"),
        ({"target": "component", "id": "R1", "field": "value", "new": "1\n2"}, "printable"),
        ({"target": "component", "id": "R1", "field": "value", "new": ""}, "at least 1"),
        ({"target": "pcb", "id": "R1", "field": "value", "new": "1"}, "target"),
    ]
    for body, message in bad:
        with pytest.raises(ValidationError, match=message):
            Correction.model_validate(body)
    unknown = [*ok, Correction(target="terminal", id="R9.1", field="pin", new="1")]
    assert _payload().unknown_targets(unknown) == ["terminal R9.1"]


def test_corrections_come_with_gate_1_decisions_and_round_trip() -> None:
    fix = Correction(target="component", id="R1", field="value", old="10k", new="47k")
    approve = GateDecision(
        gate_id="g", action="approve", reviewer="Ana", decided_at=NOW, corrections=[fix]
    )
    assert GateDecision.model_validate_json(approve.model_dump_json()).corrections == [fix]
    revise = GateDecision(
        gate_id="g",
        action="reject",
        reviewer="Ana",
        decided_at=NOW,
        reason="R1 is 47k",
        corrections=[fix],
    )
    assert revise.corrections[0].new == "47k"
    with pytest.raises(ValidationError, match="gate 1"):
        GateDecision(
            gate_id="g", action="finish", reviewer="Ana", decided_at=NOW, corrections=[fix]
        )


def test_erc_uses_the_checks_shape_of_part_builds() -> None:
    erc = ErcReport(
        checks=[
            ErcCheck(id="E1.unique_refdes", passed=True),
            ErcCheck(id="E3.connected", passed=False, detail="R3.1", refs=["R3.1"]),
        ],
        advisory=[ErcCheck(id="A3.models_needed", passed=True)],
    )
    report = erc.as_validation("erc-r1")
    assert isinstance(report, ValidationReport) and report.build_id == "erc-r1"
    assert [(c.id, c.category, c.passed, c.feature_ids) for c in report.checks] == [
        ("E1.unique_refdes", "ERC", True, []),
        ("E3.connected", "ERC", False, ["R3.1"]),
    ]
    assert [c.id for c in report.advisory] == ["A3.models_needed"]
    assert (report.passed, report.score) == (erc.passed, erc.score) == (False, 0.5)
    # what the circuit run sent before as a dict validates with the shared model now
    sent = {"build_id": "erc-r1", "checks": [{"id": "E4.ground", "category": "ERC",
            "passed": True, "detail": "", "feature_ids": []}], "advisory": []}  # fmt: skip
    assert ValidationReport.model_validate(sent).checks[0].category == "ERC"
    part_check = Check(id="V2.hole", category="V2_dimensions", passed=True, expected=12.0)
    assert ValidationReport(build_id="b", checks=[part_check]).passed
