"""The two runs recorded for the demo video on 2026-10-18 (main be1e8f4), replayed (#152).

`demo-fuss`: a part run on Fuss through gate 1, the build and "Finish inspection" with four
points left open (they stay open, D70). `demo-rectifier`: a circuit run whose reviewer gave V1
its value at gate 1; ngspice then failed for want of a 1N4148 model. Exported from the bench
collector; tool payloads trimmed, the reviewer's name replaced. The frontend tests replay the
same files.
"""

import json
from pathlib import Path

from mechatronikers.session.runs import open_pois, phase, summarize
from mechatronikers.session.store import SessionRecord
from mechatronikers.telemetry import Event

FIXTURES = Path(__file__).parent / "fixtures" / "events"


def _events(name: str) -> list[Event]:
    lines = (FIXTURES / f"{name}.ndjson").read_text().splitlines()
    return [Event.model_validate(json.loads(line)) for line in lines]


def _until(events: list[Event], type: str) -> list[Event]:
    return events[: next(i for i, e in enumerate(events) if e.type == type) + 1]


def _record(session_id: str) -> SessionRecord:
    now = "2026-10-18T00:00:00Z"
    return SessionRecord(id=session_id, state="shelved", image="x", created_at=now, updated_at=now)


def test_part_run_after_finish_inspection_is_released_with_its_open_points() -> None:
    events = _events("demo-fuss")
    waiting = [e for e in events if not e.type.startswith("session.")]
    at_inspection = waiting[: [e.type for e in waiting].index("review.approved") + 1]
    assert phase("running", at_inspection) == "valid"  # waiting for the inspection
    assert open_pois(at_inspection) == 4  # five points, the reviewer approved one
    done = [e for e in events if e.type not in ("session.shelved", "session.snapshot_started")]
    assert phase("stopped", done) == "released"
    assert open_pois(done) == 4  # released, and the four points stay open (D70)


def test_released_needs_the_hosts_finish_at_gate_2() -> None:
    """Only the run driver's resume from gate 2 with "finish" releases a part: a run that ended
    `done` without it (`--auto-approve`, the old fixture) stays valid, and a container cannot
    forge it (docs/13 §1)."""
    done = [e for e in _events("demo-fuss") if not e.type.startswith("session.s")]
    finish = next(
        i for i, e in enumerate(done)
        if e.type == "agent.resumed" and e.payload.get("action") == "finish"
    )  # fmt: skip
    assert phase("stopped", done[:finish] + done[finish + 1 :]) == "valid"
    forged = [*done[:finish], done[finish].model_copy(update={"source": "container"})]
    assert phase("stopped", forged + done[finish + 1 :]) == "valid"
    hosted = [*done[:finish], done[finish].model_copy(update={"source": "host"})]
    assert phase("stopped", hosted + done[finish + 1 :]) == "released"


def test_circuit_run_keeps_its_recorded_open_point() -> None:
    """Recorded before corrections resolved their findings (#129): "No value found for V1" stayed
    open in circuit-mcp after V1 got its value, and the run table says so."""
    events = _events("demo-rectifier")
    assert open_pois(_until(events, "agent.interrupt")) == 1  # "No value found for V1"
    done = [e for e in events if not e.type.startswith("session.s")]
    assert phase("stopped", done, "circuit") == "valid"  # "netlists ready"
    assert open_pois(done) == 1
    row = summarize(_record("ses-6c8758b5688b"), events, None)
    assert (row.run_kind, row.phase, row.open_pois) == ("circuit", "shelved", 1)


def test_a_resolution_by_correction_closes_the_point() -> None:
    """With #129, circuit-mcp records `review.resolved` for V1's finding: it no longer counts."""
    done = _until(_events("demo-rectifier"), "agent.run_finished")
    resolved = done[-1].model_copy(
        update={"type": "review.resolved", "component": "circuit-mcp",
                "payload": {"item_id": "ri-1", "by": "Anna", "gate_id": "gate-run-82632ed8-1"}}
    )  # fmt: skip
    assert open_pois([*done, resolved]) == 0


def test_points_raised_after_the_end_count_too() -> None:
    done = _until(_events("demo-rectifier"), "agent.run_finished")
    late = done[-1].model_copy(
        update={
            "type": "review.item_created",
            "payload": {"item": {"id": "op-r1", "kind": "unsupported"}, "revision": 1},
        }
    )
    assert open_pois([*done, late]) == 2
