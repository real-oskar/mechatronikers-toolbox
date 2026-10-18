"""A build script must not approve its own review gates (#99, D95).

Two layers: the runner's namespaces (no network, only its build directory of the session folder)
and the gateway header that every request to the session app needs.
"""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from mechatronikers.agent.runner import HttpReviewClient
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.review_app.trust import warn_if_untrusted
from mechatronikers.runner import namespaces
from mechatronikers.runner.launcher import run_build
from mechatronikers.sandbox.__main__ import app_from_env
from mechatronikers.sandbox.app import create_app
from mechatronikers.session.gateway import (
    GATEWAY_HEADER,
    GATEWAY_SECRET_ENV,
    caddyfile,
    gateway_secret,
)
from mechatronikers.session.manager import SessionManager
from mechatronikers.session.runs import unisolated_builds as run_unisolated
from mechatronikers.session.store import SessionStore
from mechatronikers.spec import BuildJob
from mechatronikers.spec.review import GateDecision, GateRequest, GateState
from mechatronikers.telemetry import Event, EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.events import utc_now
from mechatronikers.telemetry.store import EventStore
from tests.test_session_manager import FakeRuntime

SECRET = gateway_secret("abc123")
GATEWAY = {GATEWAY_HEADER: SECRET}  # what Caddy adds to every request it forwards
GATE = "gate-probe"
# Every write route of the review API, with a body that would pass validation.
WRITES: list[tuple[str, dict[str, Any]]] = [
    (
        f"/api/review/gates/{GATE}/decision",
        {"gate_id": GATE, "action": "approve", "reviewer": "script", "decided_at": utc_now()},
    ),
    (
        "/api/review/gates",
        GateRequest(
            id="gate-forged", kind="spec_review", run_id="run-1", created_at=utc_now()
        ).model_dump(mode="json"),
    ),
    ("/api/review/items", {"message": "fine", "reviewer": "script", "revision": 1}),
    ("/api/review/items/poi-1/actions", {"action": "approved", "reviewer": "script"}),
]


def _events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def _session(tmp_path: Path) -> Path:
    root = tmp_path / "session"
    for sub in ("input", "builds"):
        (root / sub).mkdir(parents=True)
    return root


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """The session app with a gateway secret on 127.0.0.1, and its session folder."""
    root = _session(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = create_app("ses-probe", root / "builds", "subprocess", gateway_secret=SECRET)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    request = GateRequest(id=GATE, kind="spec_review", run_id="run-1", created_at=utc_now())
    opened = httpx.post(
        f"{base}/api/review/gates", content=request.model_dump_json(), headers=GATEWAY
    )
    assert opened.status_code == 201
    yield base, root
    server.should_exit = True
    thread.join(timeout=10)


def _attack(base: str, root: Path) -> str:
    """A build script that tries every review write route and the gate file, then builds."""
    return f"""
import json, os, urllib.error, urllib.request
results = {{}}
for path, body in {WRITES!r}:
    request = urllib.request.Request({base!r} + path, data=json.dumps(body).encode(),
                                     headers={{"Content-Type": "application/json"}})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            results[path] = response.status
    except urllib.error.HTTPError as exc:
        results[path] = exc.code
    except OSError as exc:
        results[path] = type(exc).__name__
gate_file = {str(root / "review" / "gates" / f"{GATE}.json")!r}
try:
    state = json.load(open(gate_file))
    state["decision"] = {WRITES[0][1]!r}
    json.dump(state, open(gate_file, "w"))
    results["file"] = "written"
except OSError as exc:
    results["file"] = type(exc).__name__
results["session"] = sorted(os.listdir({str(root)!r}))
results["secret"] = {GATEWAY_SECRET_ENV!r} in os.environ
print("PROBE " + json.dumps(results))
result = Box(1, 1, 1)
"""


def _probe(events: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [e["payload"]["line"] for e in events if e["type"] == "runner.stdout"]
    result: dict[str, Any] = json.loads(next(x for x in lines if x.startswith("PROBE "))[6:])
    return result


def _decision(base: str) -> Any:
    return httpx.get(f"{base}/api/review/gates/{GATE}", headers=GATEWAY).json()["decision"]


def test_the_app_takes_no_request_that_bypassed_the_gateway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _session(tmp_path)
    app = create_app("ses-probe", root / "builds", "subprocess", gateway_secret=SECRET)
    others = [("/circuit/api/schematics", {}), ("/cad/mcp", {}), ("/health", None)]
    with TestClient(app) as client:
        capsys.readouterr()
        for path, body in [*WRITES, *others]:
            for headers in ({}, {GATEWAY_HEADER: "0" * len(SECRET)}):
                if body is None:
                    response = client.get(path, headers=headers)
                else:
                    response = client.post(path, json=body, headers=headers)
                assert response.status_code == 403, path
        rejected = [e for e in _events(capsys.readouterr().out) if e["component"] == "sandbox"]
        assert {e["type"] for e in rejected} == {"sandbox.request_rejected"}
        assert {e["level"] for e in rejected} == {"warning"}
        assert [e["payload"]["path"] for e in rejected][::2] == [p for p, _ in WRITES + others]
        assert {e["payload"]["reason"] for e in rejected} == {
            "missing gateway header",
            "wrong gateway header",
        }
        # through the gateway everything works as before
        assert client.get("/health", headers=GATEWAY).status_code == 200
        gate, decision = WRITES[1][1], WRITES[0][1]
        assert client.post("/api/review/gates", json=gate, headers=GATEWAY).status_code == 201
        decide = client.post(
            "/api/review/gates/gate-forged/decision",
            json={**decision, "gate_id": "gate-forged"},
            headers=GATEWAY,
        )
        assert decide.status_code == 200 and decide.json()["decision"]["action"] == "approve"
    # without a secret (tests, dev server, inspection image) the app is open as before
    with TestClient(create_app("ses-probe", root / "builds", "subprocess")) as client:
        assert client.get("/health").status_code == 200


def test_build_script_gets_403_from_every_review_write_route(
    served: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without namespaces (policy auto, kernel without support) the gateway header still keeps
    a build script out of the review API."""
    base, root = served
    monkeypatch.setenv(namespaces.POLICY_ENV, "auto")
    monkeypatch.setattr(namespaces, "kernel_support", lambda: "disabled in this test")
    runner = io.StringIO()
    job = BuildJob(build_id="build-probe1", kind="script", script=_attack(base, root))
    result = run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=runner))
    assert result.status == "ok", result.error
    events = _events(runner.getvalue())
    probe = _probe(events)
    assert [probe[path] for path, _ in WRITES] == [403] * len(WRITES)
    assert probe["secret"] is False  # the runner does not inherit the gateway secret
    rejected = [
        e for e in _events(capsys.readouterr().out) if e["type"] == "sandbox.request_rejected"
    ]
    assert [e["payload"]["path"] for e in rejected] == [path for path, _ in WRITES]
    assert {e["level"] for e in rejected} == {"warning"}
    (warning,) = [e for e in events if e["type"] == "runner.isolation_unavailable"]
    assert warning["level"] == "warning" and warning["payload"]["policy"] == "auto"
    assert "runner.isolated" not in [e["type"] for e in events]
    # This runner shares the session folder: only the namespaces close the gate file (below).
    # So the fallback is flagged on every gate state, and the run summary counts it.
    assert probe["file"] == "written"
    flagged = httpx.get(f"{base}/api/review/gates/{GATE}", headers=GATEWAY).json()
    assert flagged["unisolated_builds"] == 1 and flagged["decision"]["reviewer"] == "script"
    gates = httpx.get(f"{base}/api/review/gates", headers=GATEWAY).json()
    assert [g["unisolated_builds"] for g in gates] == [1]
    stored = Event.model_validate(warning)
    assert run_unisolated([stored, stored.model_copy(update={"level": "error"})]) == 1


@pytest.mark.skipif(
    namespaces.kernel_support() is not None, reason=f"{namespaces.kernel_support()}"
)
def test_isolated_build_script_reaches_neither_the_api_nor_the_review_files(
    served: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, root = served
    monkeypatch.setenv(namespaces.POLICY_ENV, "required")
    runner = io.StringIO()
    job = BuildJob(build_id="build-probe2", kind="script", script=_attack(base, root))
    result = run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=runner))
    assert result.status == "ok", result.error
    assert (root / "builds" / "build-probe2" / "out" / "part.step").is_file()
    events = _events(runner.getvalue())
    probe = _probe(events)
    assert {probe[path] for path, _ in WRITES} == {"URLError"}  # no network at all
    assert probe["file"] == "FileNotFoundError" and probe["session"] == ["builds"]
    assert _decision(base) is None
    # confirmed by the trampoline before the runner started, so audits need no absence check
    (isolated,) = [e for e in events if e["type"] == "runner.isolated"]
    assert isolated["level"] == "info" and isolated["payload"]["build_id"] == "build-probe2"
    types = [e["type"] for e in events]
    assert types.index("runner.isolated") < types.index("build.started")
    assert (
        httpx.get(f"{base}/api/review/gates", headers=GATEWAY).json()[0]["unisolated_builds"] == 0
    )
    assert "sandbox.request_rejected" not in capsys.readouterr().out  # nothing reached the app


def test_required_namespaces_refuse_the_build_where_unavailable(
    served: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, root = served
    monkeypatch.setenv(namespaces.POLICY_ENV, "required")
    monkeypatch.setattr(namespaces, "kernel_support", lambda: "unshare: Operation not permitted")
    runner = io.StringIO()
    job = BuildJob(build_id="build-probe3", kind="script", script=_attack(base, root))
    result = run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=runner))
    assert result.status == "failed" and "build refused" in (result.error or "")
    assert "Operation not permitted" in (result.error or "")
    events = _events(runner.getvalue())
    assert [(e["type"], e["level"]) for e in events] == [("runner.isolation_unavailable", "error")]
    assert _decision(base) is None  # the script never ran
    monkeypatch.setenv(namespaces.POLICY_ENV, "sometimes")
    with pytest.raises(ValueError, match="MT_RUNNER_NAMESPACES"):
        run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=runner))


def test_a_symlinked_builds_folder_does_not_hide_the_fallback(
    served: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unisolated build swaps `builds/` for a symlink; the gate state still counts it."""
    base, root = served
    monkeypatch.setenv(namespaces.POLICY_ENV, "auto")
    monkeypatch.setattr(namespaces, "kernel_support", lambda: "disabled in this test")
    swap = f"""
import os
os.rename({str(root / "builds")!r}, {str(root / "builds-real")!r})
os.symlink({str(root / "builds-real")!r}, {str(root / "builds")!r})
result = Box(1, 1, 1)
"""
    job = BuildJob(build_id="build-swap1", kind="script", script=swap)
    emitter = EventEmitter("cad-mcp", stream=io.StringIO())
    assert run_build(job, root / "builds", emitter=emitter).status == "ok"
    assert (root / "builds").is_symlink()
    flagged = httpx.get(f"{base}/api/review/gates/{GATE}", headers=GATEWAY).json()
    assert flagged["unisolated_builds"] == 1


def test_the_trampoline_pipe_is_closed_when_the_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jails: list[namespaces.Jail] = []

    class Recorded(namespaces.Jail):
        def __init__(self, build_dir: Path, hidden: Path) -> None:
            super().__init__(build_dir, hidden)
            jails.append(self)

    def no_start(*args: Any, **kwargs: Any) -> None:
        raise OSError("cannot start")

    root = _session(tmp_path)
    monkeypatch.setenv(namespaces.POLICY_ENV, "auto")
    monkeypatch.setattr(namespaces, "kernel_support", lambda: None)
    monkeypatch.setattr(namespaces, "Jail", Recorded)
    monkeypatch.setattr(subprocess, "Popen", no_start)
    job = BuildJob(build_id="build-nostart", kind="script", script="result = Box(1, 1, 1)")
    with pytest.raises(OSError, match="cannot start"):
        run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=io.StringIO()))
    (jail,) = jails
    for fd in (*jail.pass_fds, jail._fds[0]):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_every_gate_client_warns_about_an_untrusted_gate_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = GateRequest(id="gate-w", kind="spec_review", run_id="r", created_at=utc_now())
    decision = GateDecision(gate_id="gate-w", action="approve", reviewer="x", decided_at=utc_now())
    events = io.StringIO()
    emitter = EventEmitter("agent", stream=events)
    assert not warn_if_untrusted(GateState(request=request, decision=decision), emitter)
    waiting = GateState(request=request, unisolated_builds=1)
    assert warn_if_untrusted(waiting)  # `agent gate` lists it: a warning, no event
    decided = GateState(request=request, decision=decision, unisolated_builds=1)
    assert warn_if_untrusted(decided, emitter)  # the agent's and the circuit run's clients
    err = capsys.readouterr().err
    assert "gate gate-w waiting while 1 build(s)" in err and "gate gate-w decided while" in err
    assert [e["type"] for e in _events(events.getvalue())] == ["agent.decision_untrusted"]


def test_namespaces_that_fail_at_run_time_fail_the_build_with_the_isolation_event(
    served: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe passed, but entering the namespaces fails for this build: no generic exit 125."""
    base, root = served

    class Broken(namespaces.Jail):
        def __init__(self, build_dir: Path, hidden: Path) -> None:
            super().__init__(build_dir, hidden / "missing")  # mounting on it fails

    monkeypatch.setenv(namespaces.POLICY_ENV, "auto")
    monkeypatch.setattr(namespaces, "kernel_support", lambda: None)
    monkeypatch.setattr(namespaces, "Jail", Broken)
    runner = io.StringIO()
    job = BuildJob(build_id="build-probe4", kind="script", script=_attack(base, root))
    result = run_build(job, root / "builds", emitter=EventEmitter("cad-mcp", stream=runner))
    assert result.status == "failed"
    assert "the runner namespaces could not be entered" in (result.error or "")
    events = _events(runner.getvalue())
    (failed,) = [e for e in events if e["type"] == "runner.isolation_unavailable"]
    assert failed["level"] == "error" and failed["payload"]["reason"] in (result.error or "")
    assert "runner.isolated" not in [e["type"] for e in events]
    assert "build.started" not in [e["type"] for e in events]  # the script never ran
    assert _decision(base) is None


def test_the_session_folder_must_not_hold_the_python_environment(tmp_path: Path) -> None:
    if namespaces.kernel_support() is None:
        assert namespaces.unavailable(Path(sys.prefix).resolve().parent) is not None
        assert namespaces.unavailable(tmp_path.resolve()) is None
    else:  # e.g. macOS: the reason is the kernel's
        assert namespaces.unavailable(tmp_path) == namespaces.kernel_support()


def test_the_secret_stays_out_of_the_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _session(tmp_path)
    monkeypatch.setenv(GATEWAY_SECRET_ENV, SECRET)
    monkeypatch.setenv("MT_BUILDS_DIR", str(root / "builds"))
    app = app_from_env()
    assert GATEWAY_SECRET_ENV not in os.environ  # so no child process inherits it
    with TestClient(app) as client:
        assert client.get("/health").status_code == 403
        assert client.get("/health", headers=GATEWAY).status_code == 200


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_the_sandbox_process_is_not_dumpable() -> None:
    """With ptrace_scope 0 a same-user child could read the app's memory and environment."""
    code = (
        "import subprocess, sys\n"
        "from mechatronikers.sandbox.__main__ import protect_process\n"
        "protect_process()\n"
        "read = 'import os; open(f\"/proc/{os.getppid()}/environ\").read()'\n"
        "sys.exit(subprocess.run([sys.executable, '-c', read]).returncode)\n"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert child.returncode != 0 and "PermissionError" in child.stderr


def test_sessions_get_the_secret_and_fall_back_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EventStore(tmp_path / "state.db")
    runtime = FakeRuntime()
    manager = SessionManager(
        runtime,
        Collector(store, tmp_path / "storage"),
        SessionStore(store.connection()),
        tmp_path / "storage",
        health_check=lambda base, token: True,
    )
    monkeypatch.delenv(namespaces.POLICY_ENV, raising=False)
    sid = manager.start().session_id
    env = runtime.specs[f"mt-{sid}-session"].env
    token = manager.sessions.get(sid).token
    assert token is not None
    assert env[GATEWAY_SECRET_ENV] == gateway_secret(token) != token
    assert env[namespaces.POLICY_ENV] == "auto"  # decided 2026-10-17: fall back, flagged
    assert all(token not in value for value in env.values())  # the token stays at the gateway
    monkeypatch.setenv(namespaces.POLICY_ENV, "required")  # the host may require namespaces
    sid = manager.start().session_id
    assert runtime.specs[f"mt-{sid}-session"].env[namespaces.POLICY_ENV] == "required"
    store.close()


def test_the_agent_warns_about_a_decision_in_an_unisolated_run(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review", EventEmitter("review-app", stream=io.StringIO()))
    unisolated = [0]
    app = Starlette(
        routes=[Mount("/api", create_review_api(store, unisolated_builds=lambda: unisolated[0]))]
    )
    agent = io.StringIO()

    async def decide(gate_id: str) -> None:
        client = HttpReviewClient(
            "http://sandbox",
            "tok",
            poll_s=0.01,
            transport=httpx.ASGITransport(app=app),
            emitter=EventEmitter("agent", stream=agent),
        )
        request = GateRequest(id=gate_id, kind="spec_review", run_id="r", created_at=utc_now())
        waiting = asyncio.create_task(client.decide(request))
        while not [g for g in store.gates() if g.request.id == gate_id]:
            await asyncio.sleep(0.01)
        decision = GateDecision(
            gate_id=gate_id, action="approve", reviewer="x", decided_at=utc_now()
        )
        store.decide(gate_id, decision)
        await waiting
        await client.aclose()

    asyncio.run(decide("gate-a"))
    assert agent.getvalue() == ""  # every build isolated: nothing to say
    unisolated[0] = 2
    asyncio.run(decide("gate-b"))
    (warning,) = _events(agent.getvalue())
    assert (warning["type"], warning["level"]) == ("agent.decision_untrusted", "warning")
    assert warning["payload"] == {"gate_id": "gate-b", "action": "approve", "unisolated_builds": 2}


def test_caddyfile_sets_the_header_for_the_session_container_only() -> None:
    config = caddyfile("abc123", "upstream:8000", "ses-0123abcd")
    line = f"header_up {GATEWAY_HEADER} {SECRET}"
    assert config.count(line) == 1
    upstream = config.index("reverse_proxy upstream:8000")
    assert upstream < config.index(line) < config.index('respond "unauthorized"')
    assert gateway_secret("abc124") != SECRET
