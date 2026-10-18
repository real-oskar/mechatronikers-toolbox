"""Review API inside the session sandbox, mounted at `/api` behind the session gateway (#20).

Callers: the agent on the host (opens gates, polls for decisions, with the bearer token) and the
reviewer's browser (same origin, token cookie). Every payload is validated with the shared Pydantic
models; the API records decisions and POIs only.
"""

import re
import tempfile
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import IO, TypeVar

import anyio
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mechatronikers.drawing_mcp.vector import render_region, sheet_size_mm
from mechatronikers.review_app.store import (
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewStore,
    check_id,
)
from mechatronikers.spec.review import (
    GateDecision,
    GateRequest,
    GateState,
    ItemActionRequest,
    NewItem,
)

MAX_GATE_BODY = 4_000_000  # a gate carries the validation report and all agent POIs
MAX_BODY = 64_000
_BUILD = re.compile(r"^build-[0-9a-f]{6,32}$")
# What cad-mcp's `export` writes into /session/artifacts/<build> (docs/05); nothing else is served.
_EXPORTS = {
    ".ap242.step": "application/step",
    ".ap214.step": "application/step",
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".features.json": "application/json",
    ".review.json": "application/json",
}
_EXPORT_NAME = re.compile(r"^[^./\\\x00-\x1f][^/\\\x00-\x1f]{0,199}$")
_REVISION = re.compile(r"^[1-9][0-9]{0,3}$")
MAX_ZIP_BYTES = 1 << 30  # one build's exports are far smaller; the ZIP is written to the volume

_M = TypeVar("_M", bound=BaseModel)
Handler = Callable[[Request], Awaitable[Response]]


def _json(model: BaseModel | list[BaseModel], status: int = 200) -> JSONResponse:
    if isinstance(model, list):
        return JSONResponse([m.model_dump(mode="json") for m in model], status_code=status)
    return JSONResponse(model.model_dump(mode="json"), status_code=status)


class _BadRequestError(Exception):
    def __init__(self, status: int, detail: object) -> None:
        self.status, self.detail = status, detail


async def _body(request: Request, model: type[_M], limit: int = MAX_BODY) -> _M:
    raw = await request.body()
    if len(raw) > limit:
        raise _BadRequestError(413, "payload too large")
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise _BadRequestError(422, errors) from exc


def _guarded(handler: Handler) -> Handler:
    """Map store and validation errors to HTTP status codes."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except _BadRequestError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        except ReviewNotFoundError as exc:
            return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)
        except ReviewConflictError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    return wrapped


def _drawing(store: ReviewStore, input_root: Path) -> Path:
    """The drawing under review: named by the latest gate, else the only PDF in the input."""
    named = [s.request.drawing for s in store.gates() if s.request.drawing]
    candidates = [input_root / named[-1]] if named else sorted(input_root.glob("*.pdf"))
    if len(candidates) != 1 or not candidates[0].is_file():
        raise ReviewNotFoundError("drawing")
    path = candidates[0].resolve()
    if not path.is_relative_to(input_root.resolve()):
        raise ValueError("the drawing must be inside the session input folder")
    return path


def _media(name: str) -> str | None:
    return next((m for s, m in _EXPORTS.items() if name.endswith(s)), None)


def _revision(request: Request) -> dict[str, int]:
    """`?revision=<n>`: the revision the reviewer downloaded from (the run view knows it; a build
    id alone does not say which revision exported it). Recorded in `artifact.downloaded` only."""
    raw = request.query_params.get("revision")
    if not raw:
        return {}
    if not _REVISION.match(raw):
        raise ValueError("revision must be a positive integer")
    return {"revision": int(raw)}


def _build_exports(artifacts_dir: Path, build_id: str) -> list[tuple[str, Path]]:
    """Every export of one build, by name: regular files with an export suffix directly in the
    build's own folder (the same rules as a single download)."""
    root = artifacts_dir.resolve()
    folder = (root / build_id).resolve()
    if folder.parent != root or not folder.is_dir():
        return []
    found = []
    for entry in sorted(folder.iterdir()):
        path = entry.resolve()
        if (
            _EXPORT_NAME.match(entry.name)
            and _media(entry.name)
            and path.parent == folder
            and path.is_file()
        ):
            found.append((entry.name, path))
    return found


def _write_zip(files: list[tuple[str, Path]], directory: Path) -> IO[bytes]:
    """The exports as one ZIP in an anonymous temporary file on the session volume (the sandbox's
    /tmp is a small tmpfs); it disappears when closed."""
    directory.mkdir(parents=True, exist_ok=True)
    out = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115 - the response closes it
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
            for name, path in files:
                archive.write(path, arcname=name)
        out.seek(0)
    except BaseException:
        out.close()
        raise
    return out


def create_review_api(
    store: ReviewStore,
    input_root: Path | None = None,
    artifacts_dir: Path | None = None,
    unisolated_builds: Callable[[], int] = lambda: 0,
) -> Starlette:
    """`unisolated_builds`: how many builds of this session ran without runner namespaces; every
    gate state carries it (D95)."""

    def marked(state: GateState) -> GateState:
        return state.model_copy(update={"unisolated_builds": unisolated_builds()})

    @_guarded
    async def list_gates(request: Request) -> JSONResponse:
        return _json([marked(s) for s in store.gates()])

    @_guarded
    async def open_gate(request: Request) -> JSONResponse:
        gate = await _body(request, GateRequest, MAX_GATE_BODY)
        check_id(gate.id)
        return _json(marked(store.open_gate(gate)), status=201)

    @_guarded
    async def get_gate(request: Request) -> JSONResponse:
        return _json(marked(store.gate(request.path_params["gate_id"])))

    @_guarded
    async def decide(request: Request) -> JSONResponse:
        decision = await _body(request, GateDecision)
        return _json(marked(store.decide(request.path_params["gate_id"], decision)))

    @_guarded
    async def list_items(request: Request) -> JSONResponse:
        revision = request.query_params.get("revision")
        return _json(list(store.items(int(revision) if revision else None)))

    @_guarded
    async def add_item(request: Request) -> JSONResponse:
        return _json(store.add_item(await _body(request, NewItem)), status=201)

    @_guarded
    async def act(request: Request) -> JSONResponse:
        action = await _body(request, ItemActionRequest)
        return _json(store.act(request.path_params["item_id"], action))

    @_guarded
    async def sheet(request: Request) -> Response:
        """The drawing as PNG (rendered in process, offline), for the ballooned sheet view."""
        if input_root is None:
            raise ReviewNotFoundError("drawing")
        dpi = min(max(int(request.query_params.get("dpi") or 150), 50), 300)
        pdf = _drawing(store, input_root)
        out = store.root / f"sheet-{pdf.stem}-{dpi}.png"
        if not out.is_file():
            width, height = sheet_size_mm(pdf)
            await anyio.to_thread.run_sync(
                lambda: render_region(pdf, (0, 0, width, height), out, dpi=dpi)
            )
        return FileResponse(out, media_type="image/png")

    @_guarded
    async def model(request: Request) -> Response:
        """The exported STL of one build, for the 3D view (exports only, read-only)."""
        build_id = request.path_params["build_id"]
        if artifacts_dir is None or not _BUILD.match(build_id):
            raise ReviewNotFoundError(build_id)
        files = sorted((artifacts_dir / build_id).glob("*.stl"))
        if not files:
            raise ReviewNotFoundError(f"{build_id}.stl")
        return FileResponse(files[0], media_type="model/stl")

    @_guarded
    async def export(request: Request) -> Response:
        """One exported file of a build (downloads, #52; the jobs adapter, #119): exports only,
        read-only, and only from the build's own folder."""
        build_id, name = request.path_params["build_id"], request.path_params["name"]
        media = _media(name)
        revision = _revision(request)
        if artifacts_dir is None or not _BUILD.match(build_id) or not _EXPORT_NAME.match(name):
            raise ReviewNotFoundError(f"{build_id}/{name}")
        root = artifacts_dir.resolve()
        folder = (root / build_id).resolve()
        path = (folder / name).resolve()
        if media is None or folder.parent != root or path.parent != folder or not path.is_file():
            raise ReviewNotFoundError(f"{build_id}/{name}")
        # Starlette answers HEAD on GET routes too: link previews and download managers probe
        # with it, and only a GET is a download.
        if request.method == "GET":
            store.emitter.emit(
                "artifact.downloaded", {"build_id": build_id, "file": name, **revision}
            )
        return FileResponse(path, media_type=media, filename=name)

    @_guarded
    async def export_zip(request: Request) -> Response:
        """All exports of one build as one ZIP (downloads, #52), with the same checks."""
        build_id = request.path_params["build_id"]
        revision = _revision(request)
        if artifacts_dir is None or not _BUILD.match(build_id):
            raise ReviewNotFoundError(f"{build_id}.zip")
        files = _build_exports(artifacts_dir, build_id)
        if not files:
            raise ReviewNotFoundError(f"{build_id}.zip")
        if sum(path.stat().st_size for _, path in files) > MAX_ZIP_BYTES:
            raise _BadRequestError(413, "the exports are too large for one ZIP")
        name = f"{build_id}.zip"
        disposition = {"Content-Disposition": f'attachment; filename="{name}"'}
        if request.method == "HEAD":  # no download, and no ZIP built for nothing
            return Response(media_type="application/zip", headers=disposition)
        out = await anyio.to_thread.run_sync(_write_zip, files, store.root / "tmp")
        size = out.seek(0, 2)
        out.seek(0)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while chunk := await anyio.to_thread.run_sync(out.read, 1 << 16):
                    yield chunk
            finally:
                out.close()

        store.emitter.emit(
            "artifact.downloaded",
            {"build_id": build_id, "file": name, "files": [n for n, _ in files], **revision},
        )
        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={**disposition, "Content-Length": str(size)},
        )

    return Starlette(
        routes=[
            Route("/review/sheet.png", sheet, methods=["GET"]),
            Route("/review/model/{build_id}.stl", model, methods=["GET"]),
            Route("/review/artifacts/{build_id}.zip", export_zip, methods=["GET"]),
            Route("/review/artifacts/{build_id}/{name}", export, methods=["GET"]),
            Route("/review/gates", list_gates, methods=["GET"]),
            Route("/review/gates", open_gate, methods=["POST"]),
            Route("/review/gates/{gate_id}", get_gate, methods=["GET"]),
            Route("/review/gates/{gate_id}/decision", decide, methods=["POST"]),
            Route("/review/items", list_items, methods=["GET"]),
            Route("/review/items", add_item, methods=["POST"]),
            Route("/review/items/{item_id}/actions", act, methods=["POST"]),
        ]
    )
