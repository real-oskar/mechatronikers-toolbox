"""Jobs adapter for CAD add-ins (#119): fake sandboxes, a fake agent launcher and scripted run
events; the STEP comes through the real review API behind a fake gateway. No Podman, no model."""

import contextlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from mechatronikers.jobs.__main__ import main as jobs_main
from mechatronikers.jobs.app import JobsConfig, SubprocessLauncher, create_jobs_app, review_link
from mechatronikers.jobs.status import exported_step, outcome
from mechatronikers.jobs.store import JobStore, safe_filename
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.session.manager import SessionInfo
from mechatronikers.telemetry import Event, EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.store import EventStore

KEY = "test-key-0123456789abcdef"
TOKEN = "a1b2c3d4" * 6  # the sessions' gateway token: must never leave the adapter
PDF = b"%PDF-1.4\n% fake drawing\n%%EOF\n"
AUTH = {"Authorization": f"Bearer {KEY}"}
ROOT = Path(__file__).resolve().parents[1]
ADDIN = "F360_extension/Drawing2CAD/Drawing2CAD.py"
AGENT = "mechatronikers.agent"


def step_file(label: str) -> bytes:
    return f"ISO-10303-21;\nHEADER;\nFILE_NAME('{label}');\nENDSEC;\nEND-ISO-10303-21;\n".encode()


class _Sink:
    """An emitter stream that hands every line to the collector, like the real channels."""

    def __init__(self, collector: Collector, source: str, session_id: str) -> None:
        self.collector, self.source, self.session_id = collector, source, session_id

    def write(self, text: str) -> int:
        for line in text.splitlines():
            self.collector.ingest_line(line, source=self.source, session_id=self.session_id)
        return len(text)

    def flush(self) -> None:
        pass


class FakeSandboxes:
    """The session manager's surface; every session has an artifacts folder and a review API."""

    def __init__(self, root: Path, collector: Collector) -> None:
        self.root, self.collector = root, collector
        self.sessions: list[str] = []
        self.inputs: dict[str, str] = {}
        self.states: dict[str, str] = {}
        self.fail = False
        self.started = threading.Event()  # set when a start begins
        self.release: threading.Event | None = None  # if set, start waits for it
        self.gateway_failures = 0  # the next N gateway requests fail with 502
        self.collected: list[str] = []
        self.on_collect: dict[str, Any] = {}
        self.review_events = io.StringIO()
        self.apps: dict[str, Starlette] = {}

    def start(self, drawing: Path | None = None) -> SessionInfo:
        if self.fail:
            raise RuntimeError("podman network create failed: no space left")
        assert drawing is not None
        self.started.set()
        if self.release is not None:
            assert self.release.wait(10)
        session_id = f"ses-{len(self.sessions) + 1:012d}"
        self.states[session_id] = "running"
        self.sessions.append(session_id)
        self.inputs[session_id] = drawing.name
        folder = self.root / session_id
        (folder / "artifacts").mkdir(parents=True)
        emitter = EventEmitter("review-app", stream=self.review_events)
        store = ReviewStore(folder / "review", emitter)
        api = create_review_api(store, folder / "input", folder / "artifacts")
        self.apps[session_id] = Starlette(routes=[Mount("/api", api)])
        return self.info(session_id)

    def link(self, session_id: str) -> str:
        return f"http://127.0.0.1:{40000 + self.sessions.index(session_id)}/?t={TOKEN}"

    def info(self, session_id: str) -> SessionInfo:
        state = self.states[session_id]
        active = state in ("running", "awaiting_review")
        return SessionInfo(
            session_id=session_id,
            state=state,  # type: ignore[arg-type]
            link=self.link(session_id) if active else None,
            mcp={},
        )

    def gateway_access(self, session_id: str) -> tuple[str, str]:
        if self.states[session_id] not in ("running", "awaiting_review"):
            raise ValueError(f"session {session_id} is not running ({self.states[session_id]})")
        return f"http://gateway.test/{session_id}", TOKEN

    def collect(self, session_id: str) -> int:
        self.collected.append(session_id)
        hook = self.on_collect.pop(session_id, None)
        if hook:
            hook()
        return 0

    async def gateway(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The session gateways: token required, then the session's own app."""
        session_id, _, rest = scope["path"].lstrip("/").partition("/")
        headers = dict(scope["headers"])
        if self.gateway_failures:
            self.gateway_failures -= 1
            await PlainTextResponse("bad gateway", 502)(scope, receive, send)
            return
        if headers.get(b"authorization") != f"Bearer {TOKEN}".encode() or (
            session_id not in self.apps
        ):
            await PlainTextResponse("unauthorized", 401)(scope, receive, send)
            return
        await self.apps[session_id]({**scope, "path": "/" + rest}, receive, send)


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[tuple[str, str, str | None]] = []
        self.dead: set[int] = set()
        self.stopped: list[tuple[int, str]] = []

    def launch(self, session_id: str, drawing: str, lang: str | None, log: Path) -> int:
        self.launched.append((session_id, drawing, lang))
        log.write_text("")
        return 1000 + len(self.launched)

    def alive(self, pid: int, session_id: str) -> bool:
        return pid not in self.dead

    def stop(self, pid: int, session_id: str) -> None:
        self.stopped.append((pid, session_id))
        self.dead.add(pid)


class Run:
    """Scripted events of one agent run (host channel) and its sandbox's cad-mcp."""

    def __init__(self, env: "Env", session_id: str) -> None:
        self.session_id = session_id
        self.artifacts = env.sandboxes.root / session_id / "artifacts"
        self.agent = EventEmitter(
            "agent", session_id=session_id, stream=_Sink(env.collector, "agent", session_id)
        )
        self.cad = EventEmitter(
            "cad-mcp", session_id=session_id, stream=_Sink(env.collector, "session", session_id)
        )

    def step(self, name: str) -> None:
        self.agent.emit("agent.step_started", {"step": name})

    def gate(self, kind: str, build_id: str | None = None) -> None:
        self.step({"spec_review": "review_gate_1", "result_review": "review_gate_2"}[kind])
        self.agent.emit("agent.interrupt", {"gate_id": f"gate-{kind}", "kind": kind})
        self.agent.emit(
            "agent.run_finished",
            {"status": "awaiting_review", "paused_at": kind, "build_id": build_id},
        )

    def finish(self, status: str, **payload: Any) -> None:
        self.agent.emit("agent.run_finished", {"status": status, **payload})

    def export(self, build_id: str, label: str, *, events: bool = True) -> None:
        folder = self.artifacts / build_id
        folder.mkdir(parents=True)
        for fmt, suffix in (("step-ap242", ".ap242.step"), ("step-ap214", ".ap214.step")):
            path = folder / f"Plate{suffix}"
            path.write_bytes(step_file(f"{label}{suffix}"))
            if events:
                self.cad.emit(
                    "artifact.created",
                    {
                        "uri": f"artifact://{self.session_id}/{build_id}/{path.name}",
                        "path": f"/session/artifacts/{build_id}/{path.name}",
                        "format": fmt,
                        "bytes": path.stat().st_size,
                        "sha256": "0" * 64,
                    },
                )

    def approved_run(self, export_build: str, label: str) -> None:
        """Gate 1, build, export, gate 2 and finish; the result names another build (#101)."""
        self.step("analyse")
        self.gate("spec_review")
        self.step("build")
        self.step("export")
        self.export(export_build, label)
        self.gate("result_review", build_id="build-ffffff000000")
        self.finish("done", build_id="build-ffffff000000")


class Env:
    def __init__(self, tmp: Path) -> None:
        self.var = tmp / "var"
        self.events = EventStore(self.var / "state.db")
        self.collector = Collector(self.events, self.var / "storage")
        self.sandboxes = FakeSandboxes(tmp / "sessions", self.collector)
        self.launcher = FakeLauncher()
        self.emitted: list[tuple[str, str | None, dict[str, Any]]] = []

    def app(self, **config: Any) -> Starlette:
        settings: dict[str, Any] = {
            "api_key": KEY,
            "wait_s": 0.2,
            "poll_s": 0.02,
            "fetch_retry_s": 0.0,
        } | config
        return create_jobs_app(
            JobsConfig(**settings),
            JobStore(self.var / "jobs"),
            self.sandboxes,
            self.events,
            self.launcher,
            emit=lambda type, session, payload: self.emitted.append((type, session, payload)),
            gateway_transport=httpx.ASGITransport(app=self.sandboxes.gateway),
        )

    def client(self, **config: Any) -> TestClient:
        return TestClient(self.app(**config))

    def run(self, session_id: str) -> Run:
        return Run(self, session_id)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    env = Env(tmp_path)
    yield env
    env.events.close()


def upload(
    client: TestClient, data: bytes = PDF, name: str = "Plate_dwg.pdf", **kw: Any
) -> httpx.Response:
    files = {"file": (name, data, "application/pdf")}
    return client.post("/jobs", files=files, headers=kw.pop("headers", AUTH), **kw)


def session_of(env: Env, job_id: str) -> str:
    job = JobStore(env.var / "jobs").get(job_id)
    assert job is not None and job.session_id is not None
    return job.session_id


def test_every_route_needs_the_key(env: Env) -> None:
    client = env.client()
    job_id = upload(client).json()["job_id"]
    for headers in ({}, {"Authorization": "Bearer wrong-key-0123456789"}, {"Authorization": KEY}):
        assert upload(client, headers=headers).status_code == 401
        for path in (f"/jobs/{job_id}", f"/jobs/{job_id}/wait", f"/download/{job_id}.step"):
            response = client.get(path, headers=headers)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "Bearer"
    assert len(env.sandboxes.sessions) == 1  # only the authorised upload started a sandbox
    with pytest.raises(ValueError, match="at least 16"):
        create_jobs_app(
            JobsConfig(api_key="short"),
            JobStore(env.var / "jobs"),
            env.sandboxes,
            env.events,
            env.launcher,
        )


def test_upload_limits_and_a_safe_file_name(env: Env) -> None:
    client = env.client(max_upload=1000)
    assert upload(client, PDF + b"x" * 2000).status_code == 413
    too_long = {**AUTH, "Content-Length": str(10**9)}
    assert client.post("/jobs", content=b"x", headers=too_long).status_code == 413
    assert upload(client, b"<html>not a pdf</html>").status_code == 415
    assert upload(client, b"").status_code == 415
    wrong_field = client.post("/jobs", files={"pdf": ("a.pdf", PDF)}, headers=AUTH)
    assert wrong_field.status_code == 400
    assert client.post("/jobs", json={"file": "x"}, headers=AUTH).status_code == 400
    assert env.sandboxes.sessions == []

    response = upload(client, name="../../Fuß plate (rev Ä).pdf")
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    session_id = env.sandboxes.sessions[0]
    assert env.sandboxes.inputs[session_id] == "Fu_plate_rev_A.pdf"
    assert env.launcher.launched == [(session_id, "Fu_plate_rev_A.pdf", None)]
    assert (env.var / "jobs" / job_id / "Fu_plate_rev_A.pdf").read_bytes() == PDF
    assert safe_filename("..pdf") == "drawing.pdf" and safe_filename(None) == "drawing.pdf"
    assert safe_filename("a\\b\\c.PDF") == "c.pdf"


def test_pending_approved_download_flow(env: Env) -> None:
    client = env.client(lang="de")
    response = upload(client)
    assert response.status_code == 200
    body = response.json()
    job_id, session_id = body["job_id"], env.sandboxes.sessions[0]
    assert body == {"job_id": job_id, "review_url": env.sandboxes.link(session_id)}
    assert env.launcher.launched == [(session_id, "Plate_dwg.pdf", "de")]
    run = env.run(session_id)

    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json() == {
        "job_id": job_id,
        "status": "pending",
    }
    run.step("analyse")
    run.gate("spec_review")  # waiting for the reviewer at gate 1
    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()["status"] == "pending"
    status = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert status["review_url"] == env.sandboxes.link(session_id)
    assert status["session_id"] == session_id and status["drawing"] == "Plate_dwg.pdf"
    assert client.get(f"/download/{job_id}.step", headers=AUTH).status_code == 409

    run.step("build")
    run.export("build-abc123abc123", "exported")
    run.gate("result_review", build_id="build-ffffff000000")  # gate 2: still pending
    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()["status"] == "pending"
    run.finish("done", build_id="build-ffffff000000")  # a different build than the export (#101)

    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json() == {
        "job_id": job_id,
        "status": "approved",
        "step_url": f"/download/{job_id}.step",
    }
    download = client.get(f"/download/{job_id}.step", headers=AUTH)
    assert download.status_code == 200
    assert download.content == step_file("exported.ap242.step")  # AP242 of the exported build
    assert download.headers["content-type"] == "application/step"
    assert 'filename="Plate_dwg.step"' in download.headers["content-disposition"]

    reads = [json.loads(line) for line in env.sandboxes.review_events.getvalue().splitlines()]
    assert [r["payload"] for r in reads if r["type"] == "artifact.downloaded"] == [
        {"build_id": "build-abc123abc123", "file": "Plate.ap242.step"}
    ]
    types_ = [(t, s) for t, s, _ in env.emitted]
    assert types_ == [
        ("job.created", session_id),
        ("job.finished", session_id),
        ("job.downloaded", session_id),
    ]
    assert env.emitted[1][2]["status"] == "approved"
    assert env.emitted[1][2]["build_id"] == "build-abc123abc123"
    # The gateway token stays in the session store: not in the job record, not in events.
    stored = (env.var / "jobs" / job_id / "job.json").read_text()
    assert TOKEN not in stored and TOKEN not in json.dumps(env.emitted)
    assert TOKEN not in client.get(f"/jobs/{job_id}", headers=AUTH).text  # no link once done


def test_restart_keeps_jobs(env: Env) -> None:
    client = env.client()
    done_id = upload(client).json()["job_id"]
    waiting_id = upload(client).json()["job_id"]
    env.run(session_of(env, done_id)).approved_run("build-aaaaaa111111", "first")
    assert client.get(f"/jobs/{done_id}/wait", headers=AUTH).json()["status"] == "approved"
    first = client.get(f"/download/{done_id}.step", headers=AUTH).content

    restarted = env.client()  # a new process: new store, same var/jobs
    assert restarted.get(f"/jobs/{done_id}", headers=AUTH).json()["status"] == "approved"
    assert restarted.get(f"/download/{done_id}.step", headers=AUTH).content == first
    assert restarted.get(f"/jobs/{waiting_id}", headers=AUTH).json()["status"] == "pending"
    env.run(session_of(env, waiting_id)).approved_run("build-bbbbbb222222", "second")
    assert restarted.get(f"/jobs/{waiting_id}/wait", headers=AUTH).json()["status"] == "approved"


def test_a_job_serves_only_its_own_export(env: Env) -> None:
    client = env.client()
    first, second = (upload(client).json()["job_id"] for _ in range(2))
    env.run(session_of(env, first)).approved_run("build-aaaaaa111111", "first")
    # The second session's cad-mcp names the first session's build: ignored (not its own).
    other = env.run(session_of(env, second))
    other.cad.emit(
        "artifact.created",
        {
            "uri": f"artifact://{session_of(env, first)}/build-aaaaaa111111/Plate.ap242.step",
            "format": "step-ap242",
        },
    )
    assert client.get(f"/jobs/{first}/wait", headers=AUTH).json()["status"] == "approved"

    refused = client.get(f"/download/{second}.step", headers=AUTH)
    assert refused.status_code == 409 and refused.json()["status"] == "pending"
    assert client.get("/download/0123456789abcdef.step", headers=AUTH).status_code == 404
    assert client.get(f"/download/..%2F{first}.step", headers=AUTH).status_code == 404
    assert client.get(f"/download/{first}.stl", headers=AUTH).status_code == 404

    other.step("export")
    other.export("build-bbbbbb222222", "second")
    other.finish("done")
    assert client.get(f"/jobs/{second}/wait", headers=AUTH).json()["status"] == "approved"
    assert client.get(f"/download/{first}.step", headers=AUTH).content == step_file(
        "first.ap242.step"
    )
    assert client.get(f"/download/{second}.step", headers=AUTH).content == step_file(
        "second.ap242.step"
    )


def test_final_states_map_to_the_add_in_statuses(env: Env) -> None:
    client = env.client()
    cases: list[tuple[str, dict[str, Any], str, str]] = [
        ("rejected", {"error": "plan rejected: hole Ø8 missing"}, "rejected", "hole Ø8 missing"),
        ("aborted", {"error": "aborted by the reviewer"}, "rejected", "fix-round limit"),
        ("budget_exceeded", {"error": "run budget reached"}, "error", "run budget reached"),
        ("error", {"error": "read: timeout"}, "error", "read: timeout"),
        ("build_failed", {}, "error", "fix-round limit"),
    ]
    for run_status, payload, expected, reason in cases:
        job_id = upload(client).json()["job_id"]
        run = env.run(session_of(env, job_id))
        run.step("analyse")
        if run_status in ("aborted", "build_failed"):
            run.step("give_up")
            run.agent.emit("agent.build_failed", {"reason": "cap", "failures": []})
        run.finish(run_status, **payload)
        result = client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()
        assert result["status"] == expected, run_status
        assert reason in result["message"], result
        assert "step_url" not in result
        assert client.get(f"/download/{job_id}.step", headers=AUTH).status_code == 409

    # The agent process ended without a final event (e.g. no model configured).
    job_id = upload(client).json()["job_id"]
    env.launcher.dead.add(1000 + len(env.launcher.launched))
    result = client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()
    assert result["status"] == "error" and "agent.log" in result["message"]

    # Messages in the run language; an update run at gate 2 is not final.
    session = "ses-x"
    agent = EventEmitter("agent", session_id=session, stream=io.StringIO())
    events = [
        agent.emit("agent.run_finished", {"status": "rejected", "error": "plan rejected: zu"}),
    ]
    assert outcome(events, "de") == ("rejected", "Der Plan wurde an Prüfschritt 1 abgelehnt: zu")
    update = [
        agent.emit("agent.run_finished", {"status": "awaiting_review", "paused_at": "x"}),
        agent.emit("agent.step_started", {"step": "revise"}),
    ]
    assert outcome(update) is None


def test_step_found_after_collecting_container_output(env: Env) -> None:
    """Without `session watch`, the export events are read from the container on demand."""
    client = env.client()
    job_id = upload(client).json()["job_id"]
    session_id = session_of(env, job_id)
    run = env.run(session_id)
    run.step("export")
    run.export("build-cccccc333333", "late", events=False)
    run.finish("done")
    env.sandboxes.on_collect[session_id] = lambda: run.cad.emit(
        "artifact.created",
        {
            "uri": f"artifact://{session_id}/build-cccccc333333/Plate.ap214.step",
            "format": "step-ap214",
        },
    )
    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()["status"] == "approved"
    assert env.sandboxes.collected == [session_id]
    content = client.get(f"/download/{job_id}.step", headers=AUTH).content
    assert content == step_file("late.ap214.step")  # AP214 when AP242 was not reported


def test_no_export_sandbox_failure_and_capacity(env: Env) -> None:
    client = env.client(max_active=2)
    job_id = upload(client).json()["job_id"]
    run = env.run(session_of(env, job_id))
    run.step("export")
    run.finish("done")
    result = client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()
    assert result["status"] == "error" and "without a STEP export" in result["message"]

    upload(client)
    upload(client)
    busy = upload(client)
    assert busy.status_code == 429 and len(env.sandboxes.sessions) == 3

    client = env.client(max_active=5)
    env.sandboxes.fail = True
    failed = upload(client)
    assert failed.status_code == 503
    jobs = JobStore(env.var / "jobs").all()
    broken = [j for j in jobs if j.session_id is None]
    assert len(broken) == 1 and broken[0].status == "error"
    assert "no space left" in (broken[0].message or "")


def test_exported_step_parsing_rejects_foreign_and_odd_uris() -> None:
    def created(uri: str, fmt: str = "step-ap242", component: str = "cad-mcp") -> Event:
        emitter = EventEmitter(component, session_id="ses-1", stream=io.StringIO())
        return emitter.emit("artifact.created", {"uri": uri, "format": fmt})

    good = created("artifact://ses-1/build-abcdef/Plate.ap242.step")
    assert exported_step([good], "ses-1") == ("build-abcdef", "Plate.ap242.step", "step-ap242")
    for odd in (
        created("artifact://ses-2/build-abcdef/Plate.ap242.step"),
        created("artifact://ses-1/../Plate.ap242.step"),
        created("artifact://ses-1/build-abcdef/sub/Plate.ap242.step"),
        created("artifact://ses-1/build-abcdef/.ap242.step"),
        created("artifact://ses-1/build-abcdef/Plate.stl"),
        created("artifact://ses-1/build-abcdef/Plate.ap242.step", fmt="stl"),
        created("artifact://ses-1/build-abcdef/Plate.ap242.step", component="drawing-mcp"),
        created("file:///etc/passwd"),
    ):
        assert exported_step([odd], "ses-1") is None, odd.payload


def test_cli_refuses_without_a_key_or_on_a_public_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MT_JOBS_API_KEY", raising=False)
    assert jobs_main(["serve"]) == 2
    monkeypatch.setenv("MT_JOBS_API_KEY", "short")
    assert jobs_main(["serve"]) == 2
    monkeypatch.setenv("MT_JOBS_API_KEY", KEY)
    assert jobs_main(["serve", "--host", "0.0.0.0"]) == 2
    err = capsys.readouterr().err
    assert "MT_JOBS_API_KEY" in err and "refusing to listen on 0.0.0.0" in err and KEY not in err
    assert jobs_main(["serve", "--host", "0.0.0.0", "--allow-remote"]) == 2
    remote = ["serve", "--host", "0.0.0.0", "--allow-remote", "--review-host", "http://cad.example"]
    assert jobs_main(remote) == 2
    assert "--review-host https://" in capsys.readouterr().err


def _wait_until(check: Any, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_subprocess_launcher_runs_the_agent_without_the_adapter_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = SubprocessLauncher(tmp_path / "var", "http://127.0.0.1:9999", python="py")
    assert launcher.command("ses-1", "Plate.pdf", "de") == [
        "py", "-m", "mechatronikers.agent", "--var-dir", str(tmp_path / "var"),
        "--collector-url", "http://127.0.0.1:9999",
        "run", "--session", "ses-1", "--drawing", "Plate.pdf", "--yes", "--lang", "de",
    ]  # fmt: skip
    assert "--lang" not in SubprocessLauncher(tmp_path).command("ses-1", "Plate.pdf", None)

    monkeypatch.setenv("MT_JOBS_API_KEY", KEY)
    script = (
        "import os, sys; print('review link: secret'); "
        "sys.stderr.write('key passed: %s' % ('MT_JOBS_API_KEY' in os.environ))"
    )
    monkeypatch.setattr(launcher, "command", lambda *_: [sys.executable, "-c", script])
    log = tmp_path / "agent.log"
    pid = launcher.launch("ses-1", "Plate.pdf", None, log)
    _wait_until(lambda: not launcher.alive(pid, "ses-1"))
    assert not launcher.alive(pid, "ses-1")
    assert log.read_text() == "key passed: False"  # stdout (with the link) is dropped
    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait()
    assert not launcher.alive(probe.pid, "ses-1")  # a pid from before a restart, now gone


@pytest.mark.skipif(sys.platform == "win32", reason="process groups and ps are POSIX")
def test_subprocess_launcher_reaps_and_stops_its_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = SubprocessLauncher(tmp_path)
    # argv carries the module name and the session id, like a real run
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)", AGENT, "ses-a"]
    quick = [sys.executable, "-c", "pass", AGENT, "ses-q"]
    monkeypatch.setattr(launcher, "command", lambda *_: quick)
    finished = [launcher.launch("ses-q", "x.pdf", None, tmp_path / "q.log") for _ in range(3)]

    def zombies() -> list[str]:
        if Path("/proc").is_dir():  # Linux images may have no `ps`
            states = []
            for p in finished:
                with contextlib.suppress(FileNotFoundError, ProcessLookupError):
                    stat = (Path("/proc") / str(p) / "stat").read_text()
                    states.append(stat.rsplit(")", 1)[1].split()[0])
            return [s for s in states if s.startswith("Z")]
        ps = subprocess.run(
            ["ps", "-o", "stat=", "-p", ",".join(map(str, finished))],
            capture_output=True,
            text=True,
            check=False,
        )
        return [line for line in ps.stdout.split() if line.startswith("Z")]

    _wait_until(lambda: not zombies() and launcher.running() == [])
    assert zombies() == [] and launcher.running() == []  # reaped without an alive() call

    monkeypatch.setattr(launcher, "command", lambda *_: sleeper)
    pid = launcher.launch("ses-a", "x.pdf", None, tmp_path / "a.log")
    assert launcher.alive(pid, "ses-a")
    restarted = SubprocessLauncher(tmp_path)  # knows the run only by its pid
    _wait_until(lambda: restarted.alive(pid, "ses-a"), 5)
    assert restarted.alive(pid, "ses-a")
    assert not restarted.alive(pid, "ses-b")  # the pid is another run (or was reused)
    restarted.stop(pid, "ses-b")  # not that run: left alone
    assert launcher.alive(pid, "ses-a")
    restarted.stop(pid, "ses-a")
    _wait_until(lambda: not launcher.alive(pid, "ses-a"))
    assert not launcher.alive(pid, "ses-a")


def test_a_job_without_a_decision_ends_at_its_deadline_and_frees_its_slot(env: Env) -> None:
    """The reviewer never decides and the client is gone: nobody polls the job any more."""
    client = env.client(max_active=1, job_timeout_s=0.5)
    abandoned = upload(client).json()["job_id"]
    session_id = session_of(env, abandoned)
    env.run(session_id).gate("spec_review")
    assert upload(client).status_code == 429  # the slot is taken
    time.sleep(0.6)
    assert upload(client).status_code == 200  # the deadline freed it
    result = client.get(f"/jobs/{abandoned}", headers=AUTH).json()
    assert result["status"] == "error" and "No decision within" in result["message"]
    assert env.launcher.stopped == [(1001, session_id)]
    assert client.get(f"/download/{abandoned}.step", headers=AUTH).status_code == 409


def test_cancel_stops_the_run(env: Env) -> None:
    client = env.client(max_active=1, lang="de")
    job_id = upload(client).json()["job_id"]
    assert client.delete(f"/jobs/{job_id}").status_code == 401
    assert client.delete("/jobs/0123456789abcdef", headers=AUTH).status_code == 404
    assert client.delete("/jobs/not-a-job", headers=AUTH).status_code == 404
    cancelled = client.delete(f"/jobs/{job_id}", headers=AUTH).json()
    assert cancelled["status"] == "error" and "abgebrochen" in cancelled["message"]
    assert env.launcher.stopped == [(1001, session_of(env, job_id))]
    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json() == cancelled
    assert upload(client).status_code == 200  # the slot is free again

    client = env.client(max_active=5)  # a finished job stays as it is
    done = upload(client).json()["job_id"]
    env.run(session_of(env, done)).approved_run("build-eeeeee555555", "kept")
    assert client.get(f"/jobs/{done}/wait", headers=AUTH).json()["status"] == "approved"
    assert client.delete(f"/jobs/{done}", headers=AUTH).json()["status"] == "approved"


async def _asgi(
    app: Starlette, method: str, path: str, request: httpx.Request | None = None
) -> tuple[int, dict[str, Any]]:
    """One request straight into the app; the client disconnects right after its body."""
    body = request.read() if request is not None else b""
    headers = [(b"authorization", f"Bearer {KEY}".encode())]
    if request is not None:
        headers += [(k.lower().encode(), v.encode()) for k, v in request.headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8000),
    }
    messages: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[arg-type]
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(content)


def _multipart() -> httpx.Request:
    files = {"file": ("Plate_dwg.pdf", PDF, "application/pdf")}
    return httpx.Request("POST", "http://test/jobs", files=files)


def test_no_paid_run_when_the_upload_is_abandoned_or_cancelled_during_sandbox_start(
    env: Env,
) -> None:
    app = env.app()

    async def client_gone() -> tuple[int, dict[str, Any]]:
        return await _asgi(app, "POST", "/jobs", _multipart())

    status, body = anyio.run(client_gone)  # e.g. the add-in's 120 s upload timeout expired
    assert status == 409 and "abandoned" in body["error"]
    assert env.launcher.launched == [] and len(env.sandboxes.sessions) == 1

    env.sandboxes.release = threading.Event()
    env.sandboxes.started.clear()

    async def cancelled_while_starting() -> tuple[int, dict[str, Any]]:
        async with anyio.create_task_group() as tg:
            result: dict[str, tuple[int, dict[str, Any]]] = {}

            async def post() -> None:
                result["post"] = await _asgi(app, "POST", "/jobs", _multipart())

            tg.start_soon(post)
            await anyio.to_thread.run_sync(env.sandboxes.started.wait, 10)
            starting = [j for j in JobStore(env.var / "jobs").all() if j.status == "starting"]
            assert len(starting) == 1
            status, body = await _asgi(app, "DELETE", f"/jobs/{starting[0].job_id}")
            assert status == 200 and body["status"] == "pending"
            assert env.sandboxes.release is not None
            env.sandboxes.release.set()
        return result["post"]

    status, body = anyio.run(cancelled_while_starting)
    assert status == 409 and "cancelled" in body["error"]
    assert env.launcher.launched == []
    jobs = JobStore(env.var / "jobs").all()
    assert sorted(j.status for j in jobs) == ["error", "error"]


def test_a_shelved_sandbox_ends_the_job(env: Env) -> None:
    client = env.client()
    waiting = upload(client).json()["job_id"]
    session_id = session_of(env, waiting)
    env.run(session_id).gate("spec_review")
    env.sandboxes.states[session_id] = "shelved"
    result = client.get(f"/jobs/{waiting}/wait", headers=AUTH).json()
    assert result["status"] == "error" and "shelved" in result["message"]
    assert env.launcher.stopped == [(1001, session_id)]

    # Shelved after gate 2 but before the STEP was read: said so, no retries.
    late = upload(client).json()["job_id"]
    late_session = session_of(env, late)
    env.run(late_session).approved_run("build-ffffff666666", "late")
    env.sandboxes.states[late_session] = "shelved"
    result = client.get(f"/jobs/{late}/wait", headers=AUTH).json()
    assert result["status"] == "error"
    assert "shelved before the STEP could be read" in result["message"]


def test_a_failed_step_read_is_retried_before_the_job_fails(env: Env) -> None:
    client = env.client(fetch_attempts=3)
    job_id = upload(client).json()["job_id"]
    env.run(session_of(env, job_id)).approved_run("build-aaaaaa777777", "retried")
    env.sandboxes.gateway_failures = 2  # e.g. the gateway restarting
    assert client.get(f"/jobs/{job_id}", headers=AUTH).json()["status"] == "pending"
    assert client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()["status"] == "approved"
    failures = [p for t, _, p in env.emitted if t == "job.fetch_failed"]
    assert [f["attempt"] for f in failures] == [1, 2] and "HTTP 502" in failures[0]["error"]
    assert client.get(f"/download/{job_id}.step", headers=AUTH).content == step_file(
        "retried.ap242.step"
    )

    broken = upload(client).json()["job_id"]
    env.run(session_of(env, broken)).approved_run("build-bbbbbb888888", "broken")
    env.sandboxes.gateway_failures = 10
    result = client.get(f"/jobs/{broken}/wait", headers=AUTH).json()
    assert result["status"] == "error" and "(3 attempts)" in result["message"]


def test_an_untrusted_gate_decision_is_never_approved(env: Env) -> None:
    """#104: builds without sandbox isolation could have written the decision themselves."""
    client = env.client()
    job_id = upload(client).json()["job_id"]
    run = env.run(session_of(env, job_id))
    run.agent.emit("agent.decision_untrusted", {"gate_id": "gate-result_review"})
    run.approved_run("build-cccccc999999", "untrusted")
    result = client.get(f"/jobs/{job_id}/wait", headers=AUTH).json()
    assert result["status"] == "error" and "without sandbox isolation" in result["message"]
    assert client.get(f"/download/{job_id}.step", headers=AUTH).status_code == 409


def test_finished_jobs_are_deleted_after_the_retention(env: Env) -> None:
    client = env.client()
    old_done, old_running = (upload(client).json()["job_id"] for _ in range(2))
    run = env.run(session_of(env, old_done))
    run.step("analyse")
    run.finish("error", error="read: timeout")
    assert client.get(f"/jobs/{old_done}", headers=AUTH).json()["status"] == "error"
    store = JobStore(env.var / "jobs")
    for job_id in (old_done, old_running):
        job = store.get(job_id)
        assert job is not None
        store.save(job.model_copy(update={"updated_at": "2020-01-01T00:00:00.000000Z"}))

    restarted = env.client(keep_days=30)
    assert restarted.get(f"/jobs/{old_done}", headers=AUTH).status_code == 404
    assert not (env.var / "jobs" / old_done).exists()
    assert restarted.get(f"/jobs/{old_running}", headers=AUTH).status_code == 200  # still running
    assert ("job.deleted", session_of(env, old_running)) not in [(t, s) for t, s, _ in env.emitted]
    assert [p["job_id"] for t, _, p in env.emitted if t == "job.deleted"] == [old_done]


def test_large_uploads_are_streamed_to_the_job_folder(env: Env) -> None:
    client = env.client(max_upload=4 * 1024 * 1024)
    big = PDF + bytes(range(256)) * 12_000  # about 3 MB: spooled to disk by the parser
    job_id = upload(client, big).json()["job_id"]
    assert (env.var / "jobs" / job_id / "Plate_dwg.pdf").read_bytes() == big
    assert upload(client, big + b"x" * 2 * 1024 * 1024).status_code == 413


def test_review_links_for_remote_clients() -> None:
    link = f"http://127.0.0.1:40123/?t={TOKEN}"
    assert review_link(link, None) == link
    assert (
        review_link(link, "https://cad.example.org") == f"https://cad.example.org:40123/?t={TOKEN}"
    )


# ---------------------------------------------------------------- the add-in's own client


def _addin_source() -> str | None:
    """The Fusion add-in: in the tree once ported (#118), else from the add-in branch."""
    path = ROOT / ADDIN
    if path.is_file():
        return path.read_text(encoding="utf-8")
    for ref in ("origin/siemens_NX_dev", "siemens_NX_dev"):
        try:
            proc = subprocess.run(
                ["git", "show", f"{ref}:{ADDIN}"], cwd=ROOT, capture_output=True, check=False
            )
        except OSError:
            return None
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8")
    return None


@pytest.fixture
def addin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    source = _addin_source()
    if source is None:
        pytest.skip(f"{ADDIN} is neither in the tree nor on the siemens_NX_dev branch")
    path = tmp_path / "addin" / "Drawing2CAD.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")
    adsk = types.ModuleType("adsk")
    for name in ("core", "fusion"):
        stub = types.ModuleType(f"adsk.{name}")
        stub.__getattr__ = lambda attr: type(attr, (), {})  # type: ignore[method-assign]
        setattr(adsk, name, stub)
        monkeypatch.setitem(sys.modules, f"adsk.{name}", stub)
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    spec = importlib.util.spec_from_file_location("drawing2cad_addin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TOTAL_TIMEOUT_S = 30  # a broken contract fails the test instead of waiting 30 min
    return module


@pytest.fixture
def server(env: Env) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = env.app(wait_s=0.3, poll_s=0.02)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_the_add_in_client_works_against_the_adapter(
    addin: types.ModuleType, server: str, env: Env, tmp_path: Path
) -> None:
    cfg = {"server_url": server, "api_key": KEY}
    assert addin.check_server_url(server) is None
    pdf = tmp_path / "Plate_dwg.pdf"
    pdf.write_bytes(PDF)

    job = addin._upload_pdf(str(pdf), cfg)
    session_id = env.sandboxes.sessions[0]
    assert job["review_url"] == env.sandboxes.link(session_id)
    assert addin._headers(cfg, addin._absolute(job["review_url"], cfg)) == {}  # key stays home

    def approve_later() -> None:
        time.sleep(0.5)  # a few long polls answer "pending" first
        env.run(session_id).approved_run("build-dddddd444444", "addin")

    threading.Thread(target=approve_later, daemon=True).start()
    result = addin._wait_for_result(job["job_id"], cfg)
    assert result["status"] == "approved"
    target = tmp_path / "result.step"
    addin._download(addin._absolute(result["step_url"], cfg), str(target), cfg)
    assert target.read_bytes() == step_file("addin.ap242.step")

    rejected = addin._upload_pdf(str(pdf), cfg)
    run = env.run(env.sandboxes.sessions[1])
    run.step("analyse")
    run.finish("rejected", error="plan rejected: wrong part")
    answer = addin._wait_for_result(rejected["job_id"], cfg)
    assert answer["status"] == "rejected" and "wrong part" in answer["message"]

    with pytest.raises(urllib.error.HTTPError) as denied:
        addin._upload_pdf(str(pdf), {"server_url": server, "api_key": "wrong-key-0123456789"})
    assert denied.value.code == 401
