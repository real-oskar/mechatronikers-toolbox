"""Local dev server for circuit runs in the review UI: no sandbox, no model, no API key (docs/12).

    uv run python -m mechatronikers.circuit_mcp.dev [--run sallen_key/sallen_key_sch.pdf]
                                                     [--auto-approve] [--port 8790]

It serves the routes the session gateway serves, backed by the real components: the review UI
at `/` with the sandbox's runtime config (the build in `frontend/dist`, `npm run build`), the
circuit add-on at `/circuit/` (API, MCP), the review API at `/api/review`, and this session's
events at `/events/history` and `/events/stream` (collector and event store in
`var/circuit-dev/`, `/ingest` for run drivers). With `--run` a circuit run starts like
`circuit_mcp.run start` would, so the UI shows the same routing sheet and gate 1 as in a sandbox.
Inputs are the KiCad test cases; references are not read.
Like `session watch` it answers loopback `Host` headers only, refuses cross-site state changes,
and `/ingest` needs this host's key from `--var-dir` (`telemetry.local_api`, #146).
"""

import argparse
import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mechatronikers.circuit_mcp.app import create_circuit_app
from mechatronikers.circuit_mcp.run import CircuitRun
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.review_app.ui import SANDBOX_UI, ui_routes
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.collector_app import POLL_INTERVAL_S
from mechatronikers.telemetry.local_api import LocalOnly, ingest_key, refuse_ingest
from mechatronikers.telemetry.store import EventStore

SESSION = "ses-circuit-dev"


class CollectorSink:
    """Text stream for in-process emitters: each line goes straight into the collector."""

    def __init__(self, collector: Collector, source: str) -> None:
        self.collector = collector
        self.source = source
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.collector.ingest_line(line, source=self.source, session_id=SESSION)
        return len(text)

    def flush(self) -> None:
        pass


def create_dev_app(data: Path, out: Path, key: str | None = None) -> tuple[Starlette, Collector]:
    """`key`: this host's ingest key; without one `/ingest` refuses every line."""
    collector = Collector(EventStore(out / "events.db"), out / "archive")

    def emitter(component: str) -> EventEmitter:
        sink = CollectorSink(collector, component)
        return EventEmitter(component, session_id=SESSION, stream=sink)

    circuit_app, server = create_circuit_app(emitter("circuit-mcp"), data, out / "artifacts")
    review = ReviewStore(out / "review", emitter("review-app"))

    def after(request: Request) -> int:
        values = [request.query_params.get("after") or "0", request.headers.get("last-event-id")]
        return max((int(v) for v in values if v and v.isdigit()), default=0)

    async def history(request: Request) -> JSONResponse:
        rows = collector.store.events(SESSION, after(request))
        return JSONResponse(
            [{"id": i, "event": e.model_dump(mode="json", by_alias=True)} for i, e in rows]
        )

    async def stream(request: Request) -> EventSourceResponse:
        """Live events for the UI, like the collector's stream behind the gateway."""
        start = after(request)

        async def generate() -> AsyncIterator[dict[str, str]]:
            last = start
            yield {"comment": "stream open"}
            while not await request.is_disconnected():
                for ingest_id, event in collector.store.events(SESSION, last):
                    last = ingest_id
                    yield {"id": str(ingest_id), "data": event.to_json()}
                await asyncio.sleep(POLL_INTERVAL_S)

        return EventSourceResponse(generate())

    async def ingest(request: Request) -> JSONResponse:
        refused = refuse_ingest(request, key)
        if refused is not None:
            return refused
        lines = (await request.body()).decode().splitlines()
        stored = [collector.ingest_line(line, source="agent", session_id=SESSION) for line in lines]
        return JSONResponse({"stored": sum(i is not None for i in stored)})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = Starlette(
        routes=[
            *ui_routes(SANDBOX_UI, "Review UI not built (npm run build in frontend/)."),
            Route("/events/history", history),
            Route("/events/stream", stream),
            Route("/ingest", ingest, methods=["POST"]),
            Mount("/api", create_review_api(review, data, out / "artifacts")),
            Mount("/circuit", circuit_app),
        ],
        lifespan=lifespan,
        middleware=[Middleware(LocalOnly)],
    )
    return app, collector


def start_run(
    url: str, collector: Collector, schematic: str, auto_approve: bool
) -> threading.Thread:
    """A circuit run in the background, once the server answers."""

    def work() -> None:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/circuit/api/files", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        emitter = EventEmitter(
            "agent", session_id=SESSION, run_id=run_id, stream=CollectorSink(collector, "agent")
        )
        asyncio.run(CircuitRun(url, "dev", emitter).execute(schematic, auto_approve=auto_approve))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.circuit_mcp.dev")
    parser.add_argument("--data", type=Path, default=Path("data/circuits"))
    parser.add_argument("--out", type=Path, default=Path("var/circuit-dev"))
    parser.add_argument("--var-dir", type=Path, default=Path("var"), help="holds ingest.key")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--run", default="sallen_key/sallen_key_sch.pdf", help="'' for no run")
    parser.add_argument("--auto-approve", action="store_true")
    args = parser.parse_args(argv)
    # A fresh state per start: old gates and events would mix with the new run.
    out = args.out / time.strftime("%Y%m%d-%H%M%S")
    app, collector = create_dev_app(args.data, out, ingest_key(args.var_dir))
    url = f"http://127.0.0.1:{args.port}"
    if args.run:
        start_run(url, collector, args.run, args.auto_approve)
    print(f"review UI: {url}/   (state in {out})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
