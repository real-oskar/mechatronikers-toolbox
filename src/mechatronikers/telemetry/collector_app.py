"""Collector HTTP API on the host (localhost only): agent ingest, event queries, live SSE, and
the run table (`GET /sessions` plus the built UI in host mode, D56, D74) with its "finish and
shelve" action (`POST /sessions/<id>/shelve`, D22, #52).

Loopback `Host` headers only, no cross-site state changes, and `/ingest` only with this host's
key (`telemetry.local_api`, #146, D103)."""

import asyncio
import re
import socket
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import anyio
import uvicorn
from pydantic import BaseModel, ConfigDict, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from mechatronikers.review_app.ui import HOST_UI, UiFeatures, ui_routes
from mechatronikers.session.gateway import SOCKET_DIR_ENV, ensure_socket_dir
from mechatronikers.session.manager import ShelveNotConfirmedError
from mechatronikers.session.runs import run_table
from mechatronikers.session.snapshot import SnapshotManifest
from mechatronikers.storage.repositories import RunRepository
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.local_api import LocalOnly, from_own_page, refuse_ingest

POLL_INTERVAL_S = 0.5
_SESSION_ID = re.compile(r"^ses-[a-z0-9]{6,32}$")
SHELVABLE = ("running", "awaiting_review", "failed")  # as `SessionManager.shelve` allows
# `SessionManager.shelve(session_id, confirmed=...)`: snapshot, verify, stop (D22).
Shelve = Callable[[str, bool], SnapshotManifest]


class ShelveRequest(BaseModel):
    """The run table's confirmation dialog sends `{"confirmed": true}` once the user said OK."""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool


def bind_unix_socket(path: Path) -> socket.socket:
    """Bind the collector's Unix socket (rootless Linux, D83): directory 0700, socket 0600.

    Only this user can connect. The gateway still can: it runs as container root, which rootless
    Podman maps to this user. A leftover socket of a stopped collector is replaced; a live one is
    an error (another `session watch` serves it).
    """
    ensure_socket_dir(path.parent)
    if path.is_socket():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise OSError(f"another collector is listening on {path}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        path.chmod(0o600)
    except OSError as exc:  # e.g. the path is too long for AF_UNIX (about 100 bytes)
        sock.close()
        raise OSError(f"cannot listen on {path}: {exc} (see {SOCKET_DIR_ENV})") from exc
    return sock


def collector_server(
    app: Starlette, port: int, unix_socket: Path | None = None
) -> tuple[uvicorn.Server, list[socket.socket]]:
    """A server for `app` and its bound sockets: 127.0.0.1:<port> (never 0.0.0.0) and, if given,
    `unix_socket` for the gateway. Run it with `server.run(sockets=sockets)`."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    sockets = [config.bind_socket()]
    if unix_socket is not None:
        try:
            sockets.append(bind_unix_socket(unix_socket))
        except OSError:
            sockets[0].close()
            raise
    return uvicorn.Server(config), sockets


def _after(request: Request) -> int:
    """Resume point: `?after=<id>` (empty allowed) or the SSE `Last-Event-ID` header."""
    values = [request.query_params.get("after") or "0", request.headers.get("last-event-id", "0")]
    return max(int(v) if v.isdigit() else 0 for v in values)


def create_collector_app(
    collector: Collector,
    sessions: RunRepository | None = None,
    link: Callable[[str], str | None] | None = None,
    shelve: Shelve | None = None,
    *,
    ingest_key: str | None = None,
) -> Starlette:
    """`ingest_key`: this host's key (`local_api.ingest_key`); without one `/ingest` refuses all."""

    async def ingest(request: Request) -> JSONResponse:
        refused = refuse_ingest(request, ingest_key)
        if refused is not None:
            return refused
        body = (await request.body()).decode()
        ids = [collector.ingest_line(line, source="agent") for line in body.splitlines()]
        return JSONResponse({"stored": sum(i is not None for i in ids)})

    async def events(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        after = _after(request)
        rows = collector.store.events(session_id, after)
        return JSONResponse(
            [{"id": i, "event": e.model_dump(mode="json", by_alias=True)} for i, e in rows]
        )

    async def stream(request: Request) -> EventSourceResponse:
        session_id = request.path_params["session_id"]
        after = _after(request)

        async def generate() -> AsyncIterator[dict[str, str]]:
            last = after
            yield {"comment": "stream open"}  # flushes at once, so the browser sees it is open
            while not await request.is_disconnected():
                for ingest_id, event in collector.store.events(session_id, last):
                    last = ingest_id
                    # Unnamed messages: a browser EventSource gets every type via onmessage.
                    yield {"id": str(ingest_id), "data": event.to_json()}
                await asyncio.sleep(POLL_INTERVAL_S)

        return EventSourceResponse(generate())

    async def runs(_: Request) -> JSONResponse:
        """One row per session, newest first (read-only)."""
        assert sessions is not None
        rows = run_table(sessions, collector.store, link)
        return JSONResponse([row.model_dump(mode="json") for row in rows])

    busy: set[str] = set()
    busy_lock = threading.Lock()

    async def shelve_session(request: Request) -> JSONResponse:
        """Finish and shelve one session after the user's OK in the run table's dialog (D22).
        Takes as long as the snapshot; a second request for the same session meanwhile is 409."""
        assert shelve is not None and sessions is not None
        session_id = request.path_params["session_id"]
        # `LocalOnly` already refused foreign hosts and cross-site pages; this route also wants
        # the browser to say the request comes from the run table itself (docs/13, "CSRF").
        if not from_own_page(request.headers):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "expected application/json"}, status_code=415)
        try:
            body = ShelveRequest.model_validate_json(await request.body())
        except ValidationError:
            return JSONResponse({"detail": 'expected {"confirmed": true}'}, status_code=422)
        try:
            if not _SESSION_ID.match(session_id):
                raise KeyError(session_id)
            state = sessions.get(session_id).state
        except KeyError:
            return JSONResponse({"detail": f"unknown session: {session_id}"}, status_code=404)
        if state not in SHELVABLE:
            return JSONResponse(
                {"detail": f"cannot shelve a session in state {state}"}, status_code=409
            )
        with busy_lock:
            if session_id in busy:
                return JSONResponse({"detail": "already shelving"}, status_code=409)
            busy.add(session_id)
        try:
            manifest = await anyio.to_thread.run_sync(shelve, session_id, body.confirmed)
        except ShelveNotConfirmedError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=428)
        except Exception as exc:  # noqa: BLE001 - the dialog shows why the snapshot failed
            reason = str(exc) or type(exc).__name__
            print(f"shelving {session_id} failed: {exc!r}"[:300], file=sys.stderr, flush=True)
            return JSONResponse({"detail": f"shelving failed: {reason}"}, status_code=500)
        finally:
            with busy_lock:
                busy.discard(session_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "state": "shelved",
                "snapshot_id": manifest.snapshot_id,
                "retention_class": manifest.retention_class,
                "expires_at": manifest.expires_at,
            }
        )

    routes: list[BaseRoute] = [
        Route("/ingest", ingest, methods=["POST"]),
        Route("/sessions/{session_id}/events", events),
        Route("/sessions/{session_id}/stream", stream),
    ]
    if sessions is not None:
        routes.append(Route("/sessions", runs))
        if shelve is not None:
            routes.append(Route("/sessions/{session_id}/shelve", shelve_session, methods=["POST"]))
        # the UI offers "finish and shelve" only where the route exists
        ui = HOST_UI if shelve else HOST_UI.model_copy(update={"features": UiFeatures(table=True)})
        routes += ui_routes(ui, "Run table not built (npm run build in frontend/).")
    return Starlette(routes=routes, middleware=[Middleware(LocalOnly)])
