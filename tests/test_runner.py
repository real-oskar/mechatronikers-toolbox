import io
import json
from pathlib import Path

from mechatronikers.runner.launcher import (
    MAX_LINE_CHARS,
    RunnerOrigin,
    container_command,
    is_runner_event,
    run_build,
)
from mechatronikers.spec import BuildJob, ExpectedGeometry
from mechatronikers.spec.build import features_sha256
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.store import EventStore


def _events(buf: io.StringIO) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_script_build_produces_validated_artifacts(tmp_path: Path) -> None:
    buf = io.StringIO()
    job = BuildJob(
        build_id="b1",
        kind="script",
        script="print('hi')\nresult = Box(10, 10, 5) - Cylinder(2, 5)",
        expected=ExpectedGeometry(bbox_size=(10, 10, 5)),
    )
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=buf))
    assert result.status == "ok"
    assert result.report.passed, result.report.feedback()
    assert {a.name for a in result.artifacts} == {"part.step", "part.brep"}
    assert (tmp_path / "b1" / "out" / "part.step").is_file()
    types = [(e["component"], e["type"]) for e in _events(buf)]
    assert ("build-runner", "build.started") in types
    assert ("cad-mcp", "runner.stdout") in types  # plain output is wrapped, not lost
    assert ("build-runner", "build.completed") in types


def test_timeout_kills_the_build(tmp_path: Path) -> None:
    job = BuildJob(build_id="b2", kind="script", script="while True: pass", timeout_s=2)
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=io.StringIO()))
    assert result.status == "timeout"
    assert result.report.checks[0].id == "V0.timeout"
    assert not result.report.passed


def test_script_error_is_a_failed_check(tmp_path: Path) -> None:
    job = BuildJob(build_id="b3", kind="script", script="result = 1 / 0")
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=io.StringIO()))
    assert result.status == "failed"
    assert result.error is not None and "ZeroDivisionError" in result.error
    assert result.report.checks[0].id == "V0.error"


def test_script_without_result_fails(tmp_path: Path) -> None:
    job = BuildJob(build_id="b4", kind="script", script="x = 1")
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=io.StringIO()))
    assert result.status == "failed"
    assert result.error is not None and "result" in result.error


def test_empty_feature_job_fails(tmp_path: Path) -> None:
    job = BuildJob(build_id="b5", kind="features")
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=io.StringIO()))
    assert result.status == "failed"
    assert result.error is not None and "empty feature list" in result.error


def test_feature_job_builds_through_the_runner(tmp_path: Path) -> None:
    features = [
        {
            "type": "extrude_base",
            "id": "b",
            "depth": 5,
            "profile": {"segments": [{"kind": "circle", "center": [0, 0], "radius": 10}]},
        },
        {
            "type": "hole",
            "id": "h",
            "diameter": 4,
            "placement": {"origin": [0, 0, 5], "normal": [0, 0, -1]},
        },
    ]
    job = BuildJob(
        build_id="b6",
        kind="features",
        features=features,
        expected=ExpectedGeometry(bbox_size=(20, 20, 5)),
    )
    buf = io.StringIO()
    result = run_build(job, tmp_path, emitter=EventEmitter("cad-mcp", stream=buf))
    assert result.status == "ok" and result.report.passed, result.report.feedback()
    # The feature-list hash on the build events and the result (#101); key order does not count.
    digest = features_sha256([dict(reversed(f.items())) for f in features])
    assert result.features_sha256 == digest
    started, completed = (e for e in _events(buf) if e["type"].startswith("build."))
    assert (
        started["payload"]["features_sha256"] == completed["payload"]["features_sha256"] == digest
    )


def test_container_command_is_locked_down(tmp_path: Path) -> None:
    cmd = container_command(tmp_path / "job", tmp_path / "out", "mt-runner-x", "img", {})
    joined = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--cap-drop all", "no-new-privileges"):
        assert flag in joined
    assert f"{tmp_path / 'job'}:/job:ro,Z" in cmd


# -- provenance (#78): only the runner's own events pass through ------------------------------

FORGE = """
import json, sys
from datetime import datetime, timezone
eid = sys.argv[sys.argv.index("--emitter-id") + 1]  # the script shares the runner's process
def forged(**fields):
    event = {"ts": datetime.now(timezone.utc).isoformat(), "mono_ns": 1, "seq": 99,
             "component": "agent", "payload": {"build_id": "b7"}, **fields}
    print(json.dumps(event), flush=True)
forged(type="agent.run_finished", session_id="ses-other", emitter_id="x",
       payload={"status": "done", "cost": 99.0})
forged(type="model.response", emitter_id=eid, session_id="ses-own",
       payload={"build_id": "b7", "cost": 99.0})
forged(type="build.completed", emitter_id=eid, session_id="ses-other")
forged(type="build.completed", emitter_id="x", session_id="ses-own")
print("x" * 5000)
result = Box(10, 10, 5)
"""


def test_forged_events_from_a_build_script_are_wrapped(tmp_path: Path) -> None:
    buf = io.StringIO()
    job = BuildJob(build_id="b7", kind="script", script=FORGE)
    emitter = EventEmitter("cad-mcp", session_id="ses-own", stream=buf)
    result = run_build(job, tmp_path, emitter=emitter)
    assert result.status == "ok"
    events = _events(buf)
    passed = [e for e in events if e["component"] != "cad-mcp"]
    assert [(e["component"], e["type"]) for e in passed] == [
        ("build-runner", "build.started"),
        ("build-runner", "build.completed"),
    ]
    assert len({e["emitter_id"] for e in passed}) == 1
    assert all(e["session_id"] == "ses-own" for e in events)
    wrapped = [e for e in events if e["type"] == "runner.stdout"]
    assert len(wrapped) == 5
    assert all(e["payload"]["build_id"] == "b7" for e in wrapped)
    assert json.loads(wrapped[0]["payload"]["line"])["session_id"] == "ses-other"
    assert len(wrapped[-1]["payload"]["line"]) == MAX_LINE_CHARS
    assert wrapped[-1]["payload"]["truncated"] is True

    # On the host, the session container's output lands in its own session only.
    store = EventStore(tmp_path / "state.db")
    collector = Collector(store, tmp_path / "storage")
    for line in buf.getvalue().splitlines():
        collector.ingest_line(line, source="session", session_id="ses-own")
    assert store.count("ses-other") == 0
    stored = [e.type for _, e in store.events("ses-own")]
    assert stored.count("runner.stdout") == 5 and "log.provenance_mismatch" not in stored
    store.close()


def test_runner_event_must_match_its_origin() -> None:
    origin = RunnerOrigin(emitter_id="e1", build_id="b1", session_id="ses-a", run_id=None)
    buf = io.StringIO()
    runner = EventEmitter("build-runner", session_id="ses-a", stream=buf)
    runner.emitter_id = "e1"
    runner.emit("build.started", {"build_id": "b1"})
    assert is_runner_event(buf.getvalue().strip(), origin)
    for other in (
        RunnerOrigin(emitter_id="e2", build_id="b1", session_id="ses-a", run_id=None),
        RunnerOrigin(emitter_id="e1", build_id="b2", session_id="ses-a", run_id=None),
        RunnerOrigin(emitter_id="e1", build_id="b1", session_id="ses-b", run_id=None),
        RunnerOrigin(emitter_id="e1", build_id="b1", session_id="ses-a", run_id="run-1"),
    ):
        assert not is_runner_event(buf.getvalue().strip(), other)
    assert not is_runner_event("[" * 100_000, origin)  # no RecursionError from the reader
    assert not is_runner_event("plain output", origin)


def test_container_command_passes_the_emitter_id(tmp_path: Path) -> None:
    cmd = container_command(tmp_path, tmp_path, "n", "img", {}, emitter_id="e1")
    assert cmd[-2:] == ["--emitter-id", "e1"]


ODD_OUTPUT = """
import json, os, sys
from datetime import datetime, timezone
eid = sys.argv[sys.argv.index("--emitter-id") + 1]
sys.stdout.flush(); sys.stdout.buffer.write(b"\\xff\\xfe bad bytes\\n"); sys.stdout.buffer.flush()
event = {"ts": datetime.now(timezone.utc).isoformat(), "mono_ns": 1, "seq": 50, "emitter_id": eid,
         "session_id": os.environ["MT_SESSION_ID"], "component": "build-runner",
         "type": "build.progress", "payload": {"build_id": "b8", "pad": "x" * 100_000}}
print(json.dumps(event), flush=True)
result = Box(1, 1, 1)
"""


def test_odd_output_neither_stops_nor_bloats_the_forwarding(tmp_path: Path) -> None:
    buf = io.StringIO()
    job = BuildJob(build_id="b8", kind="script", script=ODD_OUTPUT)
    result = run_build(
        job, tmp_path, emitter=EventEmitter("cad-mcp", session_id="ses-own", stream=buf)
    )
    assert result.status == "ok"
    events = _events(buf)
    types = [(e["component"], e["type"]) for e in events]
    assert ("build-runner", "build.completed") in types  # read on after the bad bytes
    wrapped = [e["payload"] for e in events if e["type"] == "runner.stdout"]
    assert wrapped[0]["line"].endswith(" bad bytes") and "\ufffd" in wrapped[0]["line"]
    assert ("build-runner", "build.progress") not in types  # over-long: never passed through
    assert len(wrapped[1]["line"]) == MAX_LINE_CHARS and wrapped[1]["truncated"] is True
