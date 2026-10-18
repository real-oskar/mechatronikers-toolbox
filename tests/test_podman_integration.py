"""Real sandbox test. Opt-in: MT_PODMAN_TESTS=1 (needs Podman and the image from `build-image`;
`MT_IMAGE` picks another tag)."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from mechatronikers.session.__main__ import build_manager
from mechatronikers.session.gateway import CollectorRoute
from mechatronikers.session.manager import DEFAULT_IMAGE
from mechatronikers.spec.review import GateRequest
from mechatronikers.telemetry.collector_app import collector_server, create_collector_app
from mechatronikers.telemetry.events import utc_now

pytestmark = pytest.mark.skipif(
    os.environ.get("MT_PODMAN_TESTS") != "1", reason="set MT_PODMAN_TESTS=1 to run"
)
IMAGE = os.environ.get("MT_IMAGE", DEFAULT_IMAGE)
TEST_COLLECTOR_PORT = 8753  # not 8765, which a running `session watch` may hold


def test_full_session_lifecycle(tmp_path: Path) -> None:
    manager = build_manager(tmp_path, IMAGE)
    drawing = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"%PDF-1.4")
    info = manager.start(drawing)
    sid = info.session_id
    try:
        record = manager.sessions.get(sid)
        base = f"http://127.0.0.1:{record.port}"
        assert httpx.get(f"{base}/health").status_code == 401
        auth = {"Authorization": f"Bearer {record.token}"}
        assert httpx.get(f"{base}/health", headers=auth).status_code == 200

        probe = subprocess.run(
            [
                "podman",
                "exec",
                f"mt-{sid}-session",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)",
            ],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0, "session container must not reach the internet"

        async def build() -> dict:  # type: ignore[type-arg]
            client = MultiServerMCPClient(info.mcp)  # type: ignore[arg-type]
            tools = {t.name: t for t in await client.get_tools()}
            out = await tools["run_script"].ainvoke(
                {"code": "result = Box(5, 5, 5)", "reason": "it"}
            )
            return json.loads(out[0]["text"])

        result = asyncio.run(build())
        assert result["status"] == "ok" and result["report"]["passed"]
        assert manager.collect(sid) > 0
        types = {e.type for _, e in manager.collector.store.events(sid)}
        assert {"sandbox.started", "tool.called", "build.completed"} <= types
    finally:
        manifest = manager.shelve(sid, confirmed=True)
    assert manifest.event_count > 0
    leftovers = subprocess.run(
        ["podman", "ps", "-a", "--filter", f"label=mt.session={sid}", "-q"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert leftovers == ""


PROBE = """
import json, os, urllib.error, urllib.request
results = {"secret": "MT_GATEWAY_SECRET" in os.environ}
decision = {"gate_id": "gate-live", "action": "approve", "reviewer": "script", "decided_at": "%s"}
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/review/gates/gate-live/decision",
    data=json.dumps(decision).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    urllib.request.urlopen(request, timeout=5)
    results["api"] = "accepted"
except urllib.error.HTTPError as exc:
    results["api"] = exc.code
except OSError as exc:
    results["api"] = type(exc).__name__
try:
    open("/session/review/gates/gate-live.json", "a").close()
    results["file"] = "writable"
except OSError as exc:
    results["file"] = type(exc).__name__
results["session"] = sorted(os.listdir("/session"))
print("PROBE " + json.dumps(results))
result = Box(5, 5, 5)
"""

INSIDE = """
import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/review/gates", timeout=5)
    print("200")
except urllib.error.HTTPError as exc:
    print(exc.code)
try:
    open("/proc/1/environ").read()
    print("environ readable")
except OSError:
    print("environ closed")
"""


def test_build_script_cannot_approve_a_gate_in_a_real_session(tmp_path: Path) -> None:
    """#99, D95: in the session container a build script reaches neither the review API nor the
    review files; a request from inside the container gets 403; the reviewer's decision through
    the gateway still works."""
    manager = build_manager(tmp_path, IMAGE)
    drawing = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"%PDF-1.4")
    info = manager.start(drawing)
    sid = info.session_id
    try:
        base, token = manager.gateway_access(sid)
        auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        gate = GateRequest(id="gate-live", kind="spec_review", run_id="run-1", created_at=utc_now())
        gates = f"{base}/api/review/gates"
        assert httpx.post(gates, content=gate.model_dump_json(), headers=auth).status_code == 201

        async def build() -> dict:  # type: ignore[type-arg]
            client = MultiServerMCPClient(info.mcp)  # type: ignore[arg-type]
            tools = {t.name: t for t in await client.get_tools()}
            code = PROBE % utc_now()
            out = await tools["run_script"].ainvoke({"code": code, "reason": "probe #99"})
            return json.loads(out[0]["text"])

        assert asyncio.run(build())["status"] == "ok"
        inside = subprocess.run(
            ["podman", "exec", f"mt-{sid}-session", "python", "-c", INSIDE],
            capture_output=True,
            text=True,
            check=True,
        )
        assert inside.stdout.split("\n")[:2] == ["403", "environ closed"]
        assert httpx.get(f"{gates}/gate-live", headers=auth).json()["decision"] is None
        decision = {"gate_id": "gate-live", "action": "approve", "reviewer": "Anna"}
        decided = httpx.post(
            f"{gates}/gate-live/decision",
            json={**decision, "decided_at": utc_now()},
            headers=auth,
        )
        assert decided.status_code == 200 and decided.json()["decision"]["reviewer"] == "Anna"
        manager.collect(sid)
        events = [e for _, e in manager.collector.store.events(sid)]
        lines = [e.payload["line"] for e in events if e.type == "runner.stdout"]
        probe = json.loads(next(x for x in lines if x.startswith("PROBE "))[6:])
        assert probe == {
            "secret": False,
            "api": "URLError",
            "file": "FileNotFoundError",
            "session": ["builds"],
        }
        assert not [e for e in events if e.type == "runner.isolation_unavailable"]
        assert [e.level for e in events if e.type == "runner.isolated"] == ["info"]
        gate_state = httpx.get(f"{gates}/gate-live", headers=auth).json()
        assert gate_state["unisolated_builds"] == 0
        rejected = [e for e in events if e.type == "sandbox.request_rejected"]
        assert [(e.level, e.payload["path"]) for e in rejected] == [
            ("warning", "/api/review/gates")
        ]
        started = next(e for e in events if e.type == "sandbox.started")
        assert started.payload["gateway_only"] is True
    finally:
        manager.shelve(sid, confirmed=True)


def test_gateway_serves_the_sessions_events_from_the_host_collector(tmp_path: Path) -> None:
    """D61, #53: rootless Linux through the mounted Unix socket, macOS through
    host.containers.internal. The collector itself listens on 127.0.0.1 and the socket only."""
    manager = build_manager(tmp_path, IMAGE)
    short = Path(tempfile.mkdtemp(prefix="mt-"))  # AF_UNIX paths are limited to ~100 bytes
    route = manager.collector_route
    manager.collector_route = (
        CollectorRoute(route.upstream, short)
        if route.socket_dir
        else CollectorRoute(f"host.containers.internal:{TEST_COLLECTOR_PORT}")
    )
    server, sockets = collector_server(
        create_collector_app(manager.collector),
        TEST_COLLECTOR_PORT,
        manager.collector_route.socket,
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    sid = None
    try:
        sid = manager.start().session_id
        record = manager.sessions.get(sid)
        base = f"http://127.0.0.1:{record.port}"
        auth = {"Authorization": f"Bearer {record.token}"}
        assert httpx.get(f"{base}/events/history").status_code == 401
        deadline = time.monotonic() + 30
        while True:  # session.started is stored right after the health check
            rows = httpx.get(f"{base}/events/history", headers=auth, timeout=10).json()
            types = {r["event"]["type"] for r in rows}
            if "session.started" in types or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert {"session.created", "session.started"} <= types
        assert {r["event"]["session_id"] for r in rows} == {sid}
        with httpx.stream("GET", f"{base}/events/stream", headers=auth, timeout=10) as stream:
            data = next(line for line in stream.iter_lines() if line.startswith("data:"))
        assert sid in data
    finally:
        if sid is not None:
            manager.shelve(sid, confirmed=True)
        server.should_exit = True
        thread.join(10)
        shutil.rmtree(short, ignore_errors=True)


NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def test_the_token_link_leaves_no_token_in_the_url_or_the_logs(tmp_path: Path) -> None:
    """#116: the review link (`?t=`) ends on a token-free URL with the cookie, also for old
    `/circuit/?t=` links and links opened from another site; a gateway error logs no token."""
    manager = build_manager(tmp_path, IMAGE)
    sid = manager.start().session_id
    try:
        record = manager.sessions.get(sid)
        base, token = f"http://127.0.0.1:{record.port}", record.token
        assert token is not None
        for path in ("/?view=run", "/circuit/"):  # the printed link; links from before #71
            with httpx.Client(follow_redirects=True, timeout=10) as browser:
                url = f"{base}{path}{'&' if '?' in path else '?'}t={token}"
                page = browser.get(url, headers={**NAVIGATE, "Sec-Fetch-Site": "none"})
                assert page.history[0].status_code == 303
                assert page.status_code == 200 and "t=" not in str(page.url)
                assert browser.cookies.get("mt_token") == token
        assert str(page.url).endswith(("/circuit/", "/"))
        with httpx.Client(timeout=10) as browser:  # from another site: Strict cookie, no 303
            first = browser.get(
                f"{base}/?t={token}", headers={**NAVIGATE, "Sec-Fetch-Site": "cross-site"}
            )
            assert (first.status_code, first.headers["refresh"]) == (200, "0; url=/")
            assert token not in first.text
            assert browser.get(f"{base}/", headers=NAVIGATE).status_code == 200  # by cookie
        assert httpx.get(f"{base}/health?t={token}").status_code == 200  # non-navigation: as before

        # `session watch` follows both containers, the gateway's stderr (Caddy's log) included.
        stop = threading.Event()
        manager.follow(sid, stop)
        subprocess.run(["podman", "stop", "-t", "1", f"mt-{sid}-session"], check=True)
        assert httpx.get(f"{base}/?t={token}", timeout=10).status_code == 502  # a reload, forced
        gateway_log = subprocess.run(
            ["podman", "logs", f"mt-{sid}-gateway"], capture_output=True, text=True, check=True
        )
        output = gateway_log.stdout + gateway_log.stderr
        assert '"status":502' in output and "t=REDACTED" in output
        assert token not in output
        deadline = time.monotonic() + 30
        while True:
            events = [e for _, e in manager.collector.store.events(sid)]
            lines = [e.to_json() for e in events if e.type == "log.line"]
            if any("t=REDACTED" in line for line in lines) or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        stop.set()
        assert any("502" in line and "t=REDACTED" in line for line in lines)
        assert all(token not in e.to_json() for e in events)
    finally:
        manager.shelve(sid, confirmed=True)
