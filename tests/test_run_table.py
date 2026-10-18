import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mechatronikers.session.runs import phase, summarize
from mechatronikers.session.snapshot import SnapshotManifest
from mechatronikers.session.store import SessionRecord, SessionStore
from mechatronikers.telemetry import Event
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.collector_app import create_collector_app
from mechatronikers.telemetry.store import EventStore

# Hand-made events of one run (also used by the frontend tests): provisioning, gate 1, a failed
# build, one fix, a valid build, export.
FIXTURE = Path(__file__).parent / "fixtures" / "events" / "fuss-run.ndjson"
LINK = "http://127.0.0.1:40000/?t=tok"
LOCAL = "http://127.0.0.1:8765"  # `session watch` answers loopback hosts only (#146)


def _events() -> list[Event]:
    return [Event.model_validate(json.loads(line)) for line in FIXTURE.read_text().splitlines()]


def _record(state: str = "running") -> SessionRecord:
    return SessionRecord(
        id="ses-fixture0001",
        state=state,  # type: ignore[arg-type]
        token="tok",
        created_at="2026-10-17T08:00:00.000000Z",
        updated_at="2026-10-17T08:02:26.100000Z",
        port=40000,
        image="localhost/mechatronikers:dev",
    )


def _until(events: list[Event], type: str, step: str | None = None, nth: int = 1) -> list[Event]:
    """Events up to and including the nth event of a type (and step)."""
    seen = 0
    for i, e in enumerate(events):
        if e.type == type and (step is None or e.payload.get("step") == step):
            seen += 1
            if seen == nth:
                return events[: i + 1]
    raise AssertionError(f"no {type} {step}")


def test_summary_of_a_finished_run() -> None:
    events = _events()
    row = summarize(_record(), events, LINK)
    assert (row.phase, row.checks_passed, row.checks_total) == ("valid", 3, 3)
    assert (row.drawing, row.part_name, row.model) == (
        "Fuss_dwg.pdf",
        "Fuss",
        "vendor/vision-model",
    )
    assert (row.fix_iterations, row.max_fix_iterations, row.revision) == (1, 3, 1)
    assert row.open_pois == 2  # the mismatch and the assumption; failed checks are not POIs
    assert row.cost == pytest.approx(0.042) and row.link == LINK
    assert row.last_activity == events[-1].ts
    # a sandbox clock that runs 10 min behind (Podman VM, T13) must not make the run look idle
    late = events[-1].model_copy(
        update={"ts": "2026-10-17T07:52:00.000000Z", "recv_ts": "2026-10-17T08:02:30.000000Z"}
    )
    assert summarize(_record(), [*events, late], LINK).last_activity == late.recv_ts


def test_run_kind_comes_from_run_started() -> None:
    events = _events()  # recorded before runs sent their kind: a part run
    assert summarize(_record(), events, LINK).run_kind == "part"

    def started(kind: str) -> list[Event]:
        return [
            e.model_copy(update={"payload": {**e.payload, "kind": kind}})
            if e.type == "agent.run_started"
            else e
            for e in events
        ]

    assert summarize(_record(), started("circuit"), LINK).run_kind == "circuit"
    assert summarize(_record(), started("part"), LINK).run_kind == "part"
    assert summarize(_record(), started("rocket"), LINK).run_kind == "part"
    # a later run_started (e.g. printed by a build script in the sandbox) cannot switch the kind
    first = next(e for e in events if e.type == "agent.run_started")
    forged = first.model_copy(update={"payload": {**first.payload, "kind": "circuit"}})
    assert summarize(_record(), [*events, forged], LINK).run_kind == "part"
    assert summarize(_record(), [*started("circuit"), first], LINK).run_kind == "circuit"


def test_circuit_revise_rounds_use_the_circuit_phases() -> None:
    first = next(e for e in _events() if e.type == "agent.run_started")

    def at(step: str, revision: int) -> Event:
        payload = {"step": step, "revision": revision}
        return first.model_copy(update={"type": "agent.step_started", "payload": payload})

    assert phase("running", [first, at("revise", 1)], "circuit") == "analysing"
    assert phase("running", [first, at("check", 2)], "circuit") == "checking"
    assert phase("running", [first, at("review_gate_1", 2)], "circuit") == "waiting_gate_1"
    assert phase("running", [first, at("export", 2)], "circuit") == "exporting"
    assert phase("running", [first, at("export", 2)]) == "update_run"  # parts: an update run


@pytest.mark.parametrize(
    ("type", "step", "nth", "phase"),
    [
        ("session.started", None, 1, "provisioning"),
        ("agent.step_started", "read", 1, "analysing"),
        ("agent.run_finished", None, 1, "waiting_gate_1"),
        ("agent.step_started", "build", 1, "building"),
        ("agent.step_started", "fix", 1, "fixing"),
        ("agent.step_started", "build", 2, "building"),
    ],
)
def test_phase_follows_the_run(type: str, step: str | None, nth: int, phase: str) -> None:
    assert summarize(_record(), _until(_events(), type, step, nth), LINK).phase == phase


def test_ended_runs_shelved_sessions_and_reviewer_decisions() -> None:
    events = _events()
    shelved = summarize(_record("shelved"), events, LINK)
    assert shelved.phase == "shelved" and shelved.link is None
    gave_up = events[:-1] + [
        events[-1].model_copy(update={"payload": {**events[-1].payload, "status": "build_failed"}})
    ]
    assert summarize(_record(), gave_up, LINK).phase == "gave_up"
    approved = events[-1].model_copy(
        update={
            "type": "review.approved",
            "component": "review-app",
            "payload": {"item_id": "rev1-r-1"},
        }
    )
    assert summarize(_record(), [*events, approved], LINK).open_pois == 1


def test_aborted_run_has_ended() -> None:
    """#65: the reviewer aborts after a give-up; the agent re-enters give_up and ends `aborted`."""
    events = _events()
    step = next(e for e in events if e.type == "agent.step_started")
    give_up = step.model_copy(update={"payload": {"step": "give_up"}})
    aborted = events[-1].model_copy(update={"payload": {**events[-1].payload, "status": "aborted"}})
    assert summarize(_record(), [*events[:-1], give_up, aborted], LINK).phase == "aborted"
    # without the run's end the same events still read as an active step
    assert summarize(_record(), [*events[:-1], give_up], LINK).phase == "building"


def _ended(events: list[Event], status: str, paused_at: str | None = None) -> Event:
    last = next(e for e in reversed(events) if e.type == "agent.run_finished")
    return last.model_copy(
        update={"payload": {**last.payload, "status": status, "paused_at": paused_at}}
    )


def _gave_up(payload: dict[str, object]) -> list[Event]:
    """The fixture run up to its last build, then the give_up step (#92)."""
    events = _events()
    step = next(e for e in events if e.type == "agent.step_started")
    give_up = step.model_copy(update={"payload": {"step": "give_up"}})
    failed = step.model_copy(
        update={"type": "agent.build_failed", "level": "error", "payload": payload}
    )
    return [*events[:-1], give_up, failed]


@pytest.mark.parametrize(
    ("status", "paused_at", "phase"),
    [
        ("awaiting_review", "gave_up", "gave_up"),  # waiting for retry or abort
        ("aborted", None, "aborted"),
        ("build_failed", None, "gave_up"),  # test bench: no gate
    ],
)
def test_give_up_shows_its_reason_and_last_failures(
    status: str, paused_at: str | None, phase: str
) -> None:
    events = _gave_up(
        {
            "reason": "cap",
            "checks": ["V2.hole_h1.diameter"],
            "failures": [
                {"feature_id": "h1", "message": "added h1 rolled back"},
                {"feature_id": "*", "message": "validate_part failed: V2.hole_h1.diameter"},
            ],
            "iteration": 3,
            "max_iterations": 3,
        }
    )
    row = summarize(_record(), [*events, _ended(events, status, paused_at)], LINK)
    assert (row.phase, row.stop_reason) == (phase, "cap")
    assert (row.failing_checks, row.failed_features) == (["V2.hole_h1.diameter"], ["h1"])
    # a retry starts a new round: nothing is stopped any more
    retry = events[-2].model_copy(update={"payload": {"step": "fix"}})
    row = summarize(_record(), [*events, _ended(events, status, paused_at), retry], LINK)
    assert (row.stop_reason, row.failing_checks, row.failed_features) == (None, [], [])


def test_stopped_runs_name_the_reason() -> None:
    first_build = _until(_events(), "validation.completed")  # V2.hole_h1.diameter failed
    for status, reason in (("error", "error"), ("budget_exceeded", "budget")):
        row = summarize(_record(), [*first_build, _ended(_events(), status)], LINK)
        assert (row.phase, row.stop_reason) == ("failed", reason)
        assert row.failing_checks == []  # the latest report may be from an earlier round
    plateau = _gave_up({"reason": "plateau", "checks": [], "failures": []})
    row = summarize(_record(), [*plateau, _ended(plateau, "aborted")], LINK)
    assert (row.stop_reason, row.failing_checks) == ("plateau", [])  # base body never built
    # a valid run and a run given up before #92 (no reason recorded)
    assert summarize(_record(), _events(), LINK).stop_reason is None
    old = _gave_up({"failures": []})  # give_up step and agent.build_failed without reason
    row = summarize(_record(), [*first_build, *old[-2:], _ended(old, "aborted")], LINK)
    assert (row.stop_reason, row.failing_checks) == (None, [])  # no stale checks either


def _circuit_run(*steps: str) -> list[Event]:
    """A circuit run (agent.run_started.kind) through the given steps (#110)."""
    events = _events()
    started = next(e for e in events if e.type == "agent.run_started")
    step = next(e for e in events if e.type == "agent.step_started")
    kind = started.model_copy(update={"payload": {**started.payload, "kind": "circuit"}})
    return [kind, *(step.model_copy(update={"payload": {"step": s}}) for s in steps)]


@pytest.mark.parametrize(
    ("step", "phase"),
    [
        ("analyse", "analysing"),
        ("check", "checking"),  # ERC
        ("review_gate_1", "waiting_gate_1"),  # the circuit run waits without a run_finished
        ("export", "exporting"),
        ("simulate", "simulating"),
    ],
)
def test_circuit_runs_have_their_own_phases(step: str, phase: str) -> None:
    row = summarize(_record(), _circuit_run("analyse", step), LINK)
    assert (row.run_kind, row.phase) == ("circuit", phase)


def test_circuit_run_ends_like_a_part_run() -> None:
    run = _circuit_run("analyse", "check", "review_gate_1", "check", "export", "simulate")
    done = _ended(_events(), "done")
    assert summarize(_record(), [*run, done], LINK).phase == "valid"  # "netlists ready" in the UI
    assert summarize(_record(), [*run, _ended(_events(), "rejected")], LINK).phase == "failed"
    # a part run's "check" step is still part of reading the drawing
    part = [e for e in run[:3] if e.type != "agent.run_started"]
    assert summarize(_record(), part, LINK).phase == "analysing"


def test_sessions_endpoint_and_table_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "events.db")
    sessions = SessionStore(store.connection())
    sessions.save(_record())
    for event in _events():
        store.add(event)
    collector = Collector(store, tmp_path / "archive")
    monkeypatch.setenv("MT_REVIEW_UI", str(tmp_path / "missing"))
    client = TestClient(create_collector_app(collector, sessions, lambda sid: LINK), base_url=LOCAL)
    (row,) = client.get("/sessions").json()
    assert (row["session_id"], row["phase"], row["link"]) == ("ses-fixture0001", "valid", LINK)
    assert "not built" in client.get("/").text  # no frontend build: placeholder page

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>review ui</title>")
    (dist / "assets" / "index.js").write_text("console.log(1)")
    monkeypatch.setenv("MT_REVIEW_UI", str(dist))
    built = TestClient(create_collector_app(collector, sessions, lambda sid: LINK), base_url=LOCAL)
    page = built.get("/")
    assert "review ui" in page.text  # the one build of the UI (D74) ...
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert built.get("/assets/index.js").status_code == 200
    config = built.get("/config.json")  # ... shows the run table on the host
    assert config.json() == {
        "mode": "host",
        "api": {"runs": "/sessions"},
        "features": {"table": True},  # no shelve route in this app
    }
    assert config.headers["cache-control"] == "no-cache"

    def shelve(session_id: str, confirmed: bool) -> SnapshotManifest:
        raise AssertionError("not called")

    with_shelve = TestClient(
        create_collector_app(collector, sessions, shelve=shelve), base_url=LOCAL
    )
    assert with_shelve.get("/config.json").json()["features"] == {"table": True, "shelve": True}
    store.close()


def test_sandbox_serves_the_run_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mechatronikers.sandbox.app import create_app

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>review ui</title>")
    monkeypatch.setenv("MT_REVIEW_UI", str(dist))
    app = create_app("ses-fixture0001", tmp_path / "session" / "builds", "subprocess")
    client = TestClient(app)
    page = client.get("/")
    assert "review ui" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    # same build as on the host; only the runtime config differs (D74)
    assert client.get("/config.json").json() == {
        "mode": "sandbox",
        "api": {"run": "/api", "events": "/events", "circuit": "/circuit/api"},
        "features": {"table": False},
        # the sandbox cannot shelve itself: its run view links to the host's run table (#52)
        "links": {"runs": "http://127.0.0.1:8765/"},
    }
    assert client.get("/api/review/gates").json() == []


def test_odd_payload_values_never_break_the_table(tmp_path: Path) -> None:
    """Payloads also come from a session's own sandbox: a bad value degrades that row's field."""
    store = EventStore(tmp_path / "events.db")
    sessions = SessionStore(store.connection())
    sessions.save(_record())
    for event in _events():
        store.add(event)
    odd = _record().model_copy(update={"id": "ses-odd"})
    sessions.save(odd)
    for seq, (type, payload) in enumerate(
        [
            ("build.progress", {"revision": "x"}),
            ("review.item_created", {"revision": [1], "item": "not a dict"}),
            ("validation.completed", {"report": "not a dict"}),
        ],
        1,
    ):
        store.add(
            Event(ts="2026-10-17T08:00:00.000000Z", mono_ns=seq, seq=seq, emitter_id="odd",
                  session_id="ses-odd", component="review-app", type=type, payload=payload)
        )  # fmt: skip
    client = TestClient(
        create_collector_app(Collector(store, tmp_path / "a"), sessions), base_url=LOCAL
    )
    response = client.get("/sessions")
    assert response.status_code == 200
    rows = {r["session_id"]: r for r in response.json()}
    assert rows["ses-fixture0001"]["phase"] == "valid"  # other sessions are unaffected
    assert (rows["ses-odd"]["revision"], rows["ses-odd"]["checks_total"]) == (1, None)


def test_rows_name_the_retention_and_the_expiry_once_shelved() -> None:
    """What "finish and shelve" keeps for how long, and until when once it is shelved (#52)."""
    running = summarize(_record(), _events(), LINK)
    assert (running.retention_class, running.retention_days, running.expires_at) == (
        "standard",
        30,
        None,
    )
    shelved = _record("shelved").model_copy(
        update={"retention_class": "ephemeral", "expires_at": "2026-10-18T08:05:00Z"}
    )
    row = summarize(shelved, _events(), LINK)
    assert (row.state, row.phase, row.link) == ("shelved", "shelved", None)
    assert (row.retention_days, row.expires_at) == (1, "2026-10-18T08:05:00Z")


# Payload keys the run table reads, per event type (session/runs.py).
_READ = {
    "agent.run_started": ("kind", "drawing", "model", "max_fix_iterations"),
    "agent.run_finished": ("paused_at", "status", "part_name"),
    "agent.step_started": ("step",),
    "agent.build_failed": ("reason", "failures", "checks"),
    "validation.completed": ("report",),
    "session.input_added": ("file",),
    "review.item_created": ("revision", "item", "origin"),
    "review.approved": ("item_id",),
    "review.rejected": ("item_id",),
    "model.response": ("cost",),
    "runner.isolation_unavailable": ("build_id",),
}
_ODD = ([], {}, None, 1.5, -1, True, 10**30, "x", "rev", "rev-", [{"id": []}], {"checks": [[]]})


def _odd(type: str, key: str, value: object, seq: int) -> Event:
    return Event(
        ts="2026-10-17T08:03:00.000000Z",
        mono_ns=0,
        seq=seq,
        emitter_id="odd-fx",
        session_id="ses-fixture0001",
        component="sandbox",
        type=type,
        level="warning",
        payload={key: value},
    )


def test_a_malformed_event_never_resets_the_row() -> None:
    """Sandbox output reaches these rules: no payload value may make `summarize` fall back to
    an empty row, which would clear the D95 flag, the checks and the phase (#137 review)."""
    from mechatronikers.session.runs import _summarize

    events = _events()
    flagged = [
        *events,
        _odd("runner.isolation_unavailable", "build_id", "build-fx2", 1),
    ]
    base = summarize(_record(), flagged, LINK)
    assert (base.unisolated_builds, base.phase, base.checks_total) == (1, "valid", 3)
    seq = 2
    for type, keys in _READ.items():
        for key in keys:
            for value in _ODD:
                seq += 1
                odd = [*flagged, _odd(type, key, value, seq)]
                row = _summarize(_record(), odd, LINK)  # raises if a rule chokes on the value
                assert row.unisolated_builds >= 1, (type, key, value)
    # the reviewer's probe: an unhashable build id adds a build, it cannot clear the flag
    probe = _odd("runner.isolation_unavailable", "build_id", [], 99)
    row = summarize(_record(), [*flagged, probe], LINK)
    assert (row.unisolated_builds, row.phase, row.checks_total) == (2, "valid", 3)
