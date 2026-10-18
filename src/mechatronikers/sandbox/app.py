"""One ASGI app for the session container: /drawing/mcp, /cad/mcp, the review app, the circuit
add-on (/circuit/mcp and its review page, docs/12) and /health.

In a session every request must carry the header the session gateway sets (D95): the gateway
checks the session token, and a caller inside the container (a build script) must not get around
it by calling port 8000 directly.
"""

import contextlib
import hmac
from collections.abc import AsyncIterator
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mechatronikers.cad_mcp.server import create_server as create_cad_server
from mechatronikers.circuit_mcp.app import create_circuit_app
from mechatronikers.drawing_mcp.server import create_server as create_drawing_server
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.review_app.ui import SANDBOX_UI, ui_routes
from mechatronikers.runner.launcher import Isolation, unisolated_builds
from mechatronikers.session.gateway import GATEWAY_HEADER
from mechatronikers.spec import SPEC_VERSION
from mechatronikers.telemetry import EventEmitter


class GatewayOnly:
    """Rejects every request without the gateway's header value (403, `sandbox.request_rejected`
    warning). ASGI middleware, so streamed MCP responses pass through unchanged."""

    def __init__(self, app: ASGIApp, secret: str, emitter: EventEmitter) -> None:
        self.app = app
        self.secret = secret.encode()
        self.header = GATEWAY_HEADER.lower().encode()
        self.emitter = emitter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            sent = dict(scope["headers"]).get(self.header, b"")
            if not hmac.compare_digest(sent, self.secret):
                client = scope.get("client")
                self.emitter.emit(
                    "sandbox.request_rejected",
                    {
                        "method": scope.get("method", "WEBSOCKET"),
                        "path": str(scope.get("path", ""))[:200],
                        "client": client[0] if client else None,
                        "reason": "missing gateway header" if not sent else "wrong gateway header",
                    },
                    level="warning",
                )
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                detail = "requests must come through the session gateway"
                await JSONResponse({"detail": detail}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    session_id: str | None,
    builds_dir: Path,
    isolation: Isolation,
    *,
    gateway_secret: str | None = None,
) -> Starlette:
    """`gateway_secret`: the value of the gateway header (set in a session); without it the app
    accepts every request (tests, the dev server, the root Dockerfile's inspection image)."""
    sandbox_events = EventEmitter("sandbox", session_id=session_id)
    drawing = create_drawing_server(
        EventEmitter("drawing-mcp", session_id=session_id),
        input_root=builds_dir.parent / "input",
        artifacts_dir=builds_dir.parent / "artifacts",
    )
    cad = create_cad_server(EventEmitter("cad-mcp", session_id=session_id), builds_dir, isolation)
    # Review state lives in the session volume next to builds/, so snapshots include it.
    review = ReviewStore(
        builds_dir.parent / "review", EventEmitter("review-app", session_id=session_id)
    )
    circuit_app, circuit = create_circuit_app(
        EventEmitter("circuit-mcp", session_id=session_id),
        input_root=builds_dir.parent / "input",
        artifacts_dir=builds_dir.parent / "artifacts",
    )
    # streamable_http_app() creates the session managers, so build the apps before the lifespan.
    drawing_app, cad_app = drawing.streamable_http_app(), cad.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(drawing.session_manager.run())
            await stack.enter_async_context(cad.session_manager.run())
            await stack.enter_async_context(circuit.session_manager.run())
            servers = ["drawing-mcp", "cad-mcp", "circuit-mcp"]
            sandbox_events.emit(
                "sandbox.started", {"servers": servers, "gateway_only": bool(gateway_secret)}
            )
            yield
            sandbox_events.emit("sandbox.stopping")

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "session_id": session_id, "spec_version": SPEC_VERSION}
        )

    return Starlette(
        routes=[
            *ui_routes(SANDBOX_UI, "Review UI not built (npm run build in frontend/)."),
            Route("/health", health),
            Mount(
                "/api",
                create_review_api(
                    review,
                    builds_dir.parent / "input",
                    builds_dir.parent / "artifacts",
                    lambda: unisolated_builds(builds_dir),
                ),
            ),
            Mount("/drawing", drawing_app),
            Mount("/cad", cad_app),
            Mount("/circuit", circuit_app),
        ],
        lifespan=lifespan,
        middleware=[Middleware(GatewayOnly, secret=gateway_secret, emitter=sandbox_events)]
        if gateway_secret
        else [],
    )
