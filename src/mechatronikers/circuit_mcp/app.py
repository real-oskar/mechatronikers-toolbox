"""circuit-mcp inside the session container, mounted at `/circuit` (docs/12): the MCP endpoint
`/circuit/mcp` for the agent and a small JSON API that the review UI's circuit panels read
(docs/13). Both work on the same CircuitSession, so the panels show what the agent changed.

The review UI is the shared frontend at `/` (D74); the former page at `/circuit/` redirects there.
The write routes (`components`, `items`, `export`, `simulate`) stay for MCP-less dev use: the UI
records corrections in the gate 1 decision instead (D75).
"""

import json
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mechatronikers.circuit_mcp.server import create_server
from mechatronikers.circuit_mcp.session import (
    CircuitSession,
    ComponentUpdate,
    ExportFormat,
    ReviewAppliedError,
)
from mechatronikers.review_app.store import check_id
from mechatronikers.spec.review import GateState
from mechatronikers.telemetry import EventEmitter

MAX_BODY = 64_000
DOWNLOADS = {".cir", ".asc", ".net", ".json", ".png", ".log"}
SHEET_DPI = 200  # sharp enough to zoom into pin numbers


class LoadRequest(BaseModel):
    path: str


class ItemAction(BaseModel):
    action: Literal["approve", "reject", "resolve", "note"]
    note: str = ""


class CorrectionsRequest(BaseModel):
    gate_id: str


class ExportRequest(BaseModel):
    formats: list[ExportFormat] = ["spice", "ltspice", "kicad-netlist"]


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _body(request: Request, model: type[BaseModel]) -> Any:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("request too large")
    return model.model_validate(json.loads(raw or b"{}"))


def create_circuit_app(
    emitter: EventEmitter, input_root: Path, artifacts_dir: Path, review_root: Path | None = None
) -> tuple[Starlette, FastMCP]:
    """The `/circuit` sub-app and its MCP server (whose session manager the caller runs).
    `review_root`: the review app's state, read-only here (default: next to the artifacts, as
    in the session volume)."""
    reviews = review_root or artifacts_dir.parent / "review"
    session = CircuitSession(input_root, artifacts_dir, emitter)
    server = create_server(emitter, session=session)
    mcp_app = server.streamable_http_app()  # creates the session manager

    def guarded(handler: Any) -> Any:
        async def wrapped(request: Request) -> Response:
            try:
                response: Response = await handler(request)
                return response
            except (KeyError, FileNotFoundError) as e:
                return _error(404, str(e).strip("'\""))
            except (ValueError, ValidationError) as e:
                return _error(422, str(e))

        return wrapped

    async def page(request: Request) -> RedirectResponse:
        """Old links to the circuit page (`<link>/circuit/?t=…`): the run view is at `/` now. The
        query (the gateway token) goes along: opened from another site, the redirected request
        would not carry the gateway's `SameSite=Strict` cookie."""
        query = request.url.query
        target = f"/?{query}" if query else "/"
        return RedirectResponse(target, headers={"Cache-Control": "no-store"})

    async def files(_: Request) -> JSONResponse:
        loaded = {src.resolve(): sid for sid, src in session.sources.items()}
        root = session.input_root
        pdfs = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file())
        return JSONResponse(
            [{"path": str(p.relative_to(root)), "schematic_id": loaded.get(p)} for p in pdfs]
        )

    async def load(request: Request) -> JSONResponse:
        req = await _body(request, LoadRequest)
        sid, spec = session.load(req.path)
        return JSONResponse({"schematic_id": sid, "components": len(spec.components)})

    async def get_spec(request: Request) -> JSONResponse:
        return JSONResponse(session.get(request.path_params["sid"]).model_dump(mode="json"))

    async def sheet(request: Request) -> Response:
        sid = request.path_params["sid"]
        spec = session.get(sid)
        w, h = spec.sheet_size
        image = session.render(sid, (0.0, 0.0, w, h), SHEET_DPI)
        return Response(image.png, media_type="image/png")

    async def checks(request: Request) -> JSONResponse:
        return JSONResponse(session.check(request.path_params["sid"]).model_dump(mode="json"))

    async def item(request: Request) -> JSONResponse:
        req = await _body(request, ItemAction)
        sid, item_id = request.path_params["sid"], request.path_params["item_id"]
        updated = session.review(sid, item_id, req.action, req.note)
        return JSONResponse(updated.model_dump(mode="json"))

    async def component(request: Request) -> JSONResponse:
        update = await _body(request, ComponentUpdate)
        sid, refdes = request.path_params["sid"], request.path_params["refdes"]
        comp = session.update_component(sid, refdes, update, source="human")
        return JSONResponse(comp.model_dump(mode="json"))

    async def corrections(request: Request) -> JSONResponse:
        """For the run driver: apply a decided gate's corrections as the reviewer's changes. The
        corrections come from the recorded decision, never from the caller, so no client can
        record its own edit as human (#57). MCP tools stay the model's (`vlm`). A correction
        resolves the review item that named exactly what it fixed (#129)."""
        gate_id = check_id((await _body(request, CorrectionsRequest)).gate_id)
        path = reviews / "gates" / f"{gate_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no gate {gate_id!r}")
        state = GateState.model_validate_json(path.read_text())
        payload = state.request.circuit
        if state.decision is None:
            raise ValueError(f"gate {gate_id} is not decided yet")
        if payload is None or payload.schematic_id != request.path_params["sid"]:
            raise ValueError(f"gate {gate_id} is not about this schematic")
        decision = state.decision
        try:
            applied = session.apply_corrections(
                payload, decision.corrections, gate_id, reviewer=decision.reviewer
            )
        except ReviewAppliedError as e:
            return _error(409, str(e))
        return JSONResponse({"gate_id": gate_id, "applied": applied})

    async def export(request: Request) -> JSONResponse:
        req = await _body(request, ExportRequest)
        result = session.export(request.path_params["sid"], list(req.formats))
        return JSONResponse(result.model_dump(mode="json"))

    async def download(request: Request) -> Response:
        sid, name = request.path_params["sid"], request.path_params["name"]
        session.get(sid)
        folder = (session.artifacts_dir / "circuit" / sid).resolve()
        target = (folder / name).resolve()
        if target.parent != folder or target.suffix not in DOWNLOADS or not target.is_file():
            raise FileNotFoundError(f"no file {name!r}")
        return FileResponse(target, filename=name)

    async def simulate(request: Request) -> JSONResponse:
        result = session.simulate(request.path_params["sid"])
        return JSONResponse(result.model_dump(mode="json"))

    api = "/api/schematics/{sid}"
    routes = [
        Route("/", page),
        Route("/api/files", guarded(files)),
        Route("/api/schematics", guarded(load), methods=["POST"]),
        Route(api, guarded(get_spec)),
        Route(f"{api}/sheet.png", guarded(sheet)),
        Route(f"{api}/checks", guarded(checks)),
        Route(f"{api}/items/{{item_id}}", guarded(item), methods=["POST"]),
        Route(f"{api}/components/{{refdes}}", guarded(component), methods=["POST"]),
        Route(f"{api}/corrections", guarded(corrections), methods=["POST"]),
        Route(f"{api}/export", guarded(export), methods=["POST"]),
        Route(f"{api}/files/{{name}}", guarded(download)),
        Route(f"{api}/simulate", guarded(simulate), methods=["POST"]),
        *mcp_app.routes,  # /mcp
    ]
    app = Starlette(routes=routes)
    app.state.circuit_session = session  # for the dev server and tests
    return app, server
