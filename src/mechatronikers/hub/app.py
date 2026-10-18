"""The hub (mode 2, docs/13), v0: read-only. It serves the review UI in `hub` mode, the overview
and run views, and ingests what workers push (D79: workers dial out; the hub never connects into a
sandbox or a worker host). It holds no model keys, has no Podman access and never parses a drawing.

Browser routes (shared credential, `hub.auth`):

| Route | |
|---|---|
| `GET /`, `GET /runs/{run_id}`, `GET /config.json`, `/assets/*` | UI build, `mode: hub` |
| `GET /api/runs?kind=&phase=&batch=&q=&cursor=&limit=` | Overview page (`RunPage`) |
| `GET /api/runs/{id}/run` | One overview row (`HubRun`) |
| `GET /api/runs/{id}/events/history?after=`, `GET /api/runs/{id}/events/stream` | Events, SSE |
| `GET /api/runs/{id}/review/gates`, `…/review/items` | Empty in v0: gates move here with #76 |
| `GET /api/runs/{id}/artifacts`, `…/artifacts/{artifact_id}`, `…/review/model/{b}.stl` | Files |
| `GET /api/runs/{id}/review/sheet.png?dpi=` | The uploaded sheet render nearest to `dpi` |

Worker routes (worker token): `POST /api/ingest` (NDJSON, `hub.ingest`) and
`PUT /api/worker/runs/{run_id}/artifacts?role=&build=&name=` (header `X-Sha256`; the hub derives
the key `artifacts/<session_id>/<build>/<name>`). `GET /api/health` takes either credential.
"""

import asyncio
import hashlib
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import PurePosixPath
from typing import IO, BinaryIO, cast, get_args

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from mechatronikers.hub.auth import AuthMiddleware, HubAuth
from mechatronikers.hub.ingest import MAX_BODY_BYTES, HubEmitter, Ingestor
from mechatronikers.hub.overview import KINDS, MAX_PAGE_SIZE, PAGE_SIZE, PHASES, Overview
from mechatronikers.hub.records import (
    ID,
    ArtifactRecord,
    ArtifactRole,
    HubEvents,
    HubRunRecord,
    HubStore,
)
from mechatronikers.review_app.ui import HUB_UI, ui_routes
from mechatronikers.storage.blobs import BlobConflictError, BlobIntegrityError, check_key

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
EVENTS_PAGE = 1000  # as the collector's history pages
POLL_INTERVAL_S = 0.5
MAX_POLL_INTERVAL_S = 2.0
SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")  # one blob key segment
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHEET = re.compile(r"^sheet-(\d{2,4})\.png$")
_ROLES: tuple[str, ...] = get_args(ArtifactRole)
# Chosen by the hub from the file name, never taken from the upload; downloads are attachments.
CONTENT_TYPES = {
    ".stl": "model/stl",
    ".step": "model/step",
    ".stp": "model/step",
    ".obj": "model/obj",
    ".glb": "model/gltf-binary",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".net": "text/plain",
    ".cir": "text/plain",
    ".txt": "text/plain",
}
_DOWNLOAD_CSP = "sandbox; default-src 'none'"


class _TooLargeError(Exception):
    pass


def _media(request: Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _read_limited(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    declared = _number(length)
    if declared is not None and declared > limit:
        raise _TooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


_NUMBER = re.compile(r"[0-9]{1,18}")  # fits a signed 64-bit id; no other digit forms


def _number(value: str | None) -> int | None:
    return int(value) if value is not None and _NUMBER.fullmatch(value) else None


def _after(request: Request) -> int:
    """Resume point: `?after=<id>` or the SSE `Last-Event-ID` header (as the collector).
    ValueError for an `after` that is no id; a bad header (sent by the browser) is ignored."""
    query = request.query_params.get("after") or "0"
    after = _number(query)
    if after is None:
        raise ValueError("after must be an event id")
    return max(after, _number(request.headers.get("last-event-id")) or 0)


def _public(record: ArtifactRecord) -> dict[str, object]:
    """An artifact as the UI sees it: downloads go by id, the blob key stays inside."""
    return record.model_dump(exclude={"blob_key"})


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(1 << 16):
            yield chunk


def create_hub_app(
    *,
    events: HubEvents,
    runs: HubStore,
    auth: HubAuth,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
) -> Starlette:
    overview = Overview(runs, events)
    ingestor = Ingestor(events, runs, secrets=auth.secrets_, on_stored=overview.mark)
    emitter = HubEmitter(events)

    def known(request: Request) -> HubRunRecord | None:
        run_id = request.path_params["run_id"]
        return runs.get_run(run_id) if ID.match(run_id) else None

    # -- worker routes ---------------------------------------------------------------------

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "worker": request.state.worker})

    async def ingest(request: Request) -> JSONResponse:
        if _media(request) != "application/x-ndjson":
            return _error(415, "Content-Type must be application/x-ndjson")
        try:
            body = await _read_limited(request, MAX_BODY_BYTES)
            result = await run_in_threadpool(ingestor.ingest, request.state.worker, body)
        except (_TooLargeError, ValueError) as exc:
            return _error(413, str(exc) or "request too large")
        return JSONResponse(result.model_dump())

    async def upload(request: Request) -> JSONResponse:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        if run.worker_id != request.state.worker:
            return _error(403, "the run belongs to another worker")
        if _media(request) != "application/octet-stream":
            return _error(415, "Content-Type must be application/octet-stream")
        sha256 = request.headers.get("x-sha256", "").lower()
        role = request.query_params.get("role", "")
        build = request.query_params.get("build", "")
        name = request.query_params.get("name", "")
        if not _SHA256.match(sha256):
            return _error(400, "X-Sha256 must be the body's SHA-256 in hex")
        if role not in _ROLES:
            return _error(400, f"role must be one of {', '.join(_ROLES)}")
        if not SEGMENT.match(build) or not SEGMENT.match(name) or name in (".", ".."):
            return _error(400, "build and name must be single path segments of [A-Za-z0-9._-]")
        try:
            key = check_key(f"artifacts/{run.session_id}/{build}/{name}")
        except ValueError:
            return _error(400, "invalid artifact path")
        with tempfile.SpooledTemporaryFile(max_size=8 << 20) as spool:
            digest, size = hashlib.sha256(), 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_artifact_bytes:
                    return _error(413, f"artifacts are limited to {max_artifact_bytes} bytes")
                digest.update(chunk)
                spool.write(chunk)
            if digest.hexdigest() != sha256:
                return _error(400, "the body does not match X-Sha256")
            spool.seek(0)
            content_type = CONTENT_TYPES.get(
                PurePosixPath(name).suffix.lower(), "application/octet-stream"
            )
            try:
                stored, new = await run_in_threadpool(
                    lambda: runs.put_artifact(
                        cast(BinaryIO, spool),
                        run=run,
                        key=key,
                        role=cast(ArtifactRole, role),
                        build_id=build,
                        name=name,
                        sha256=sha256,
                        content_type=content_type,
                    )
                )
            except BlobConflictError:
                return _error(409, "another file is stored under this name")
            except BlobIntegrityError:
                return _error(400, "the body does not match X-Sha256")
        if new:
            emitter.emit(
                run.session_id,
                run.run_id,
                "artifact.uploaded",
                {
                    "artifact_id": stored.id,
                    "key": key,
                    "role": role,
                    "build_id": build,
                    "name": name,
                    "sha256": sha256,
                    "bytes": size,
                },
            )
            overview.mark(run.run_id)
        return JSONResponse(_public(stored), status_code=201 if new else 200)

    # -- browser routes --------------------------------------------------------------------

    async def run_list(request: Request) -> JSONResponse:
        params = request.query_params
        kind, phase = params.get("kind") or None, params.get("phase") or None
        if kind is not None and kind not in KINDS:
            return _error(400, f"kind must be one of {', '.join(KINDS)}")
        if phase is not None and phase not in PHASES:
            return _error(400, "unknown phase")
        limit = _number(params.get("limit") or str(PAGE_SIZE))
        if limit is None or not 1 <= limit <= MAX_PAGE_SIZE:
            return _error(400, f"limit must be 1 to {MAX_PAGE_SIZE}")
        try:
            page = await run_in_threadpool(
                overview.page,
                kind=kind,
                phase=phase,
                batch=params.get("batch") or None,
                q=params.get("q") or None,
                cursor=params.get("cursor") or None,
                limit=limit,
            )
        except ValueError as exc:
            return _error(400, str(exc))
        return JSONResponse(page.model_dump(mode="json"))

    async def run_row(request: Request) -> JSONResponse:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        row = await run_in_threadpool(overview.row, run.run_id)
        return JSONResponse(row.model_dump(mode="json") if row else None)

    async def history(request: Request) -> JSONResponse:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        try:
            after = _after(request)
        except ValueError as exc:
            return _error(400, str(exc))
        rows = await run_in_threadpool(events.run_events, run, after, EVENTS_PAGE)
        return JSONResponse(
            [{"id": i, "event": e.model_dump(mode="json", by_alias=True)} for i, e in rows]
        )

    async def stream(request: Request) -> Response:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        try:
            after = _after(request)
        except ValueError as exc:
            return _error(400, str(exc))

        async def generate() -> AsyncIterator[dict[str, str]]:
            last, pause = after, POLL_INTERVAL_S
            yield {"comment": "stream open"}
            while not await request.is_disconnected():
                rows = await run_in_threadpool(events.run_events, run, last, EVENTS_PAGE)
                for ingest_id, event in rows:
                    last = ingest_id
                    yield {"id": str(ingest_id), "data": event.to_json()}
                # never a hot poll: a quiet run is read less often (#122's pool is not fair)
                pause = POLL_INTERVAL_S if rows else min(pause * 1.5, MAX_POLL_INTERVAL_S)
                await asyncio.sleep(pause)

        return EventSourceResponse(generate())

    async def empty(request: Request) -> JSONResponse:
        """Gates and POIs of mirrored runs stay in their sandbox until #76 (D80)."""
        return JSONResponse([]) if known(request) else _error(404, "unknown run")

    async def artifact_list(request: Request) -> JSONResponse:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        records = await run_in_threadpool(runs.artifacts, run.run_id)
        return JSONResponse([_public(r) for r in records])

    def _download(record: ArtifactRecord) -> Response:
        return StreamingResponse(
            _chunks(runs.open_artifact(record)),
            media_type=record.content_type,
            headers={
                "Content-Length": str(record.bytes),
                "Content-Disposition": f'attachment; filename="{record.name}"',
                "Content-Security-Policy": _DOWNLOAD_CSP,
            },
        )

    async def artifact(request: Request) -> Response:
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        wanted = request.path_params["artifact_id"]
        records = await run_in_threadpool(runs.artifacts, run.run_id)
        found = next((r for r in records if r.id == wanted), None)
        return _download(found) if found else _error(404, "unknown artifact")

    async def model(request: Request) -> Response:
        """The run view's 3D model (`R/review/model/{build_id}.stl`) from the uploaded exports."""
        run = known(request)
        file = request.path_params["file"]
        if run is None or not file.endswith(".stl"):
            return _error(404, "unknown model")
        build = file.removesuffix(".stl")
        records = await run_in_threadpool(runs.artifacts, run.run_id)
        found = next((r for r in records if r.build_id == build and r.name.endswith(".stl")), None)
        return _download(found) if found else _error(404, "unknown model")

    async def sheet(request: Request) -> Response:
        """`R/review/sheet.png?dpi=`: the uploaded sheet render nearest to the asked dpi (the
        sandbox renders `sheet-<dpi>.png` at analyse time, docs/13), of the latest drawing."""
        run = known(request)
        if run is None:
            return _error(404, "unknown run")
        wanted = _number(request.query_params.get("dpi")) or 150
        records = await run_in_threadpool(runs.artifacts, run.run_id)
        sheets = [(r, _SHEET.match(r.name)) for r in records if r.role == "sheet"]
        found = [(r, int(m.group(1))) for r, m in sheets if m]
        if not found:
            return _error(404, "no sheet uploaded")
        latest = found[-1][0].build_id
        best = min((d for r, d in found if r.build_id == latest), key=lambda d: abs(d - wanted))
        record = next(r for r, d in found if r.build_id == latest and d == best)
        return _download(record)

    run_base = "/api/runs/{run_id}"
    routes: list[BaseRoute] = [
        Route("/api/health", health),
        Route("/api/ingest", ingest, methods=["POST"]),
        Route("/api/worker/runs/{run_id}/artifacts", upload, methods=["PUT"]),
        Route("/api/runs", run_list),
        Route(f"{run_base}/run", run_row),
        Route(f"{run_base}/events/history", history),
        Route(f"{run_base}/events/stream", stream),
        Route(f"{run_base}/review/gates", empty),
        Route(f"{run_base}/review/items", empty),
        Route(f"{run_base}/review/model/{{file}}", model),
        Route(f"{run_base}/review/sheet.png", sheet),
        Route(f"{run_base}/artifacts", artifact_list),
        Route(f"{run_base}/artifacts/{{artifact_id}}", artifact),
        *ui_routes(HUB_UI, "Hub UI not built (npm run build in frontend/).", ("/runs/{run_id}",)),
    ]
    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware, auth=auth)])
