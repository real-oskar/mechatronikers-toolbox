"""`GateClient`: the run drivers' only way to the review API, at a configurable run base."""

import argparse
import asyncio
import io
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mechatronikers.agent.__main__ import decide_gate
from mechatronikers.agent.runner import HttpReviewClient
from mechatronikers.circuit_mcp.dev import SESSION, CollectorSink, create_dev_app
from mechatronikers.circuit_mcp.run import CircuitRun
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.client import GateClient, gateway_review_url, review_access
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.spec.review import GateDecision, GateRequest
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now
from tests.fixtures.schematics import make_schematic

HUB_BASE = "/api/runs/run-1"  # docs/13: the run base R of mode 2


@pytest.fixture
def store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "review", EventEmitter("review-app", stream=io.StringIO()))


def _app(store: ReviewStore, base: str) -> Starlette:
    return Starlette(routes=[Mount(base, create_review_api(store))])


def _request(id: str = "gate-1") -> GateRequest:
    return GateRequest(id=id, kind="spec_review", run_id="run-1", created_at=utc_now())


def _decision(gate_id: str = "gate-1", action: str = "approve", **extra: str) -> GateDecision:
    fields = {"gate_id": gate_id, "action": action, "reviewer": "Anna", "decided_at": utc_now()}
    return GateDecision.model_validate(fields | extra)


def test_gateway_review_url() -> None:
    assert gateway_review_url("http://127.0.0.1:40000") == "http://127.0.0.1:40000/api"
    assert gateway_review_url("http://127.0.0.1:40000/") == "http://127.0.0.1:40000/api"


@pytest.mark.parametrize("base", ["/api", HUB_BASE])
def test_gate_client_at_any_run_base(store: ReviewStore, base: str) -> None:
    transport = httpx.ASGITransport(app=_app(store, base))

    async def scenario() -> None:
        async with GateClient(f"http://review{base}/", "tok", transport=transport) as client:
            opened = await client.open(_request())
            assert opened.open and (await client.open(_request())) == opened  # idempotent
            assert [g.request.id for g in await client.gates()] == ["gate-1"]
            decided = await client.submit(_decision())
            assert decided.decision is not None and (await client.get("gate-1")) == decided
            with pytest.raises(httpx.HTTPStatusError) as conflict:
                await client.submit(_decision())
            assert conflict.value.response.status_code == 409
            with pytest.raises(httpx.HTTPStatusError):
                await client.get("gate-unknown")

    asyncio.run(scenario())
    assert store.gate("gate-1").decision is not None


def test_decide_opens_the_gate_and_waits(store: ReviewStore) -> None:
    transport = httpx.ASGITransport(app=_app(store, HUB_BASE))

    async def scenario() -> GateDecision:
        client = GateClient(f"http://hub{HUB_BASE}", "tok", poll_s=0.01, transport=transport)
        waiting = asyncio.create_task(client.decide(_request()))
        while not store.gates():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert not waiting.done()  # still polling
        store.decide("gate-1", _decision(action="reject", reason="R2"))
        decision = await waiting
        await client.aclose()
        return decision

    assert asyncio.run(scenario()).reason == "R2"


def test_decided_gate_returns_at_once(store: ReviewStore) -> None:
    """A restarted driver re-opens a gate that was decided meanwhile (D80)."""
    store.open_gate(_request())
    store.decide("gate-1", _decision())
    transport = httpx.ASGITransport(app=_app(store, "/api"))

    async def scenario() -> GateDecision:
        async with GateClient("http://sandbox/api", "tok", poll_s=60, transport=transport) as c:
            return await asyncio.wait_for(c.decide(_request()), timeout=5)

    assert asyncio.run(scenario()).action == "approve"


def test_wait_keeps_polling_through_outages(store: ReviewStore) -> None:
    """A gate can wait for hours; the hub or the gateway may be away meanwhile."""
    seen: list[str] = []
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("hub down")
        if calls["n"] == 2:
            return httpx.Response(502)
        if calls["n"] == 4:  # still open on the third poll, decided before the fourth
            store.decide("gate-1", _decision())
        return httpx.Response(200, json=store.gate("gate-1").model_dump(mode="json"))

    store.open_gate(_request())

    async def scenario() -> GateDecision:
        transport = httpx.MockTransport(flaky)
        async with GateClient("https://hub/api/runs/run-1", "run-token", poll_s=0.01,
                              transport=transport) as client:  # fmt: skip
            return await client.wait("gate-1")

    assert asyncio.run(scenario()).action == "approve"
    assert calls["n"] == 4 and set(seen) == {"Bearer run-token"}


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_wait_stops_when_the_run_is_gone_or_not_ours(status: int) -> None:
    """A revoked token or a deleted run cannot be waited out (#76): no endless polling."""
    calls = {"n": 0}

    def gone(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"detail": "gone"})

    async def scenario() -> None:
        transport = httpx.MockTransport(gone)
        async with GateClient(
            "https://hub/api/runs/run-1", "t", poll_s=0.01, transport=transport
        ) as client:
            await asyncio.wait_for(client.wait("gate-1"), timeout=5)

    with pytest.raises(httpx.HTTPStatusError) as refused:
        asyncio.run(scenario())
    assert refused.value.response.status_code == status and calls["n"] == 1


def test_open_rides_out_a_short_outage(
    store: ReviewStore, capsys: pytest.CaptureFixture[str]
) -> None:
    app = httpx.ASGITransport(app=_app(store, HUB_BASE))
    calls = {"n": 0}

    async def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("hub restarting")
        if calls["n"] == 2:
            return httpx.Response(503)
        return await app.handle_async_request(request)

    async def scenario() -> bool:
        transport = httpx.MockTransport(flaky)
        async with GateClient(f"http://hub{HUB_BASE}", "t", backoff_s=0.01,
                              transport=transport) as client:  # fmt: skip
            return (await client.open(_request())).open

    assert asyncio.run(scenario()) and calls["n"] == 3
    assert store.gate("gate-1").open
    assert "retrying in 0.01 s" in capsys.readouterr().err


def test_open_gives_up_and_never_retries_a_refusal() -> None:
    def respond(status: int) -> tuple[dict[str, int], httpx.MockTransport]:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(status, json={"detail": "no"})

        return calls, httpx.MockTransport(handler)

    async def open_with(transport: httpx.MockTransport) -> None:
        async with GateClient("http://hub/api", "t", open_attempts=3, backoff_s=0.001,
                              transport=transport) as client:  # fmt: skip
            await client.open(_request())

    for status, attempts in ((502, 3), (422, 1), (401, 1)):
        calls, transport = respond(status)
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(open_with(transport))
        assert calls["n"] == attempts


def test_review_access_never_sends_the_gateway_token_elsewhere() -> None:
    gateway = "http://127.0.0.1:40000"
    assert review_access(gateway, "gw-token") == (f"{gateway}/api", "gw-token")
    hub = "https://hub/api/runs/run-1"
    assert review_access(gateway, "gw-token", hub, "run-token") == (hub, "run-token")
    for token in (None, ""):
        with pytest.raises(ValueError, match="own token"):
            review_access(gateway, "gw-token", hub, token)
    with pytest.raises(ValueError, match="review URL"):
        review_access(gateway, "gw-token", None, "run-token")
    emitter = EventEmitter("agent", stream=io.StringIO())
    with pytest.raises(ValueError, match="own token"):
        CircuitRun(gateway, "gw-token", emitter, review_url=hub)
    run = CircuitRun(gateway, "gw-token", emitter)
    assert (run.review_url, run.review_token) == (f"{gateway}/api", "gw-token")


def test_http_review_client_is_a_gate_client_on_the_gateway(store: ReviewStore) -> None:
    client = HttpReviewClient("http://sandbox", "tok")
    assert isinstance(client, GateClient) and client.base_url == "http://sandbox/api"
    asyncio.run(client.aclose())


def _args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**({"approve": False, "reject": None, "reviewer": "cli"} | kw))


def test_agent_gate_command_decides_through_the_gate_client(
    store: ReviewStore, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = httpx.ASGITransport(app=_app(store, "/api"))

    def run(**kw: object) -> int:
        client = GateClient("http://sandbox/api", "tok", transport=transport)
        return asyncio.run(decide_gate(client, _args(**kw)))

    assert run() == 1  # no open gate
    store.open_gate(_request())
    store.open_gate(_request("gate-2"))
    assert run(approve=True) == 2  # two open gates: nothing decided
    store.decide("gate-2", _decision("gate-2"))
    assert run() == 0
    assert run(reject="R2 is 47k") == 0
    assert store.gate("gate-1").decision is not None
    assert store.gate("gate-1").decision.reason == "R2 is 47k"  # type: ignore[union-attr]
    out = capsys.readouterr().out
    assert "gate-1: spec_review rev 1" in out and "200 {" in out


def test_agent_gate_command_reports_a_refused_decision(
    store: ReviewStore, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refuse(request: Request) -> Response:
        return JSONResponse({"detail": "gate gate-1 is already decided"}, status_code=409)

    store.open_gate(_request())
    app = _app(store, "/api")
    app.router.routes.insert(0, Route("/api/review/gates/{id}/decision", refuse, methods=["POST"]))
    transport = httpx.ASGITransport(app=app)
    client = GateClient("http://sandbox/api", "tok", transport=transport)
    assert asyncio.run(decide_gate(client, _args(approve=True))) == 1
    assert "409 {'detail': 'gate gate-1 is already decided'}" in capsys.readouterr().out


# -- the circuit run driver reaches its gate through the configured base --------------------


@contextmanager
def _serve(app: Starlette) -> Iterator[str]:
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
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_circuit_run_opens_gate_1_at_the_configured_review_base(
    store: ReviewStore, tmp_path: Path
) -> None:
    """The dev server stands in for the sandbox (MCP); a second app plays the hub (docs/13)."""
    (tmp_path / "data").mkdir()
    make_schematic(tmp_path / "data" / "divider.pdf")
    sandbox, collector = create_dev_app(tmp_path / "data", tmp_path / "out")
    tokens: list[str] = []

    async def record_token(request: Request, call_next: RequestResponseEndpoint) -> Response:
        tokens.append(request.headers.get("authorization", ""))
        return await call_next(request)

    hub = _app(store, HUB_BASE)
    hub.add_middleware(BaseHTTPMiddleware, dispatch=record_token)

    def decide_when_open() -> None:
        for _ in range(200):
            if store.gates():
                store.decide(store.gates()[0].request.id, _decision(store.gates()[0].request.id))
                return
            time.sleep(0.05)

    with _serve(sandbox) as sandbox_url, _serve(hub) as hub_url:
        decider = threading.Thread(target=decide_when_open, daemon=True)
        decider.start()
        sink = CollectorSink(collector, "agent")
        emitter = EventEmitter("agent", session_id=SESSION, run_id="run-1", stream=sink)
        run = CircuitRun(
            sandbox_url,
            "dev",
            emitter,
            poll_s=0.1,
            review_url=f"{hub_url}{HUB_BASE}",
            review_token="run-token",
        )
        outcome = asyncio.run(run.execute("divider.pdf"))
        decider.join(timeout=5)
        assert httpx.get(f"{sandbox_url}/api/review/gates").json() == []  # not in the sandbox
    assert outcome["status"] == "done"
    assert [g.request.id for g in store.gates()] == ["gate-run-1-1"]
    assert tokens and set(tokens) == {"Bearer run-token"}
