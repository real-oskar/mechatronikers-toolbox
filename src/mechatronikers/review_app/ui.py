"""Serve the built review UI (`frontend/`, D55): static files only, no Node at runtime.

One build for every mode (D74): the sandbox app and the host collector API (`session watch`) both
serve the same `index.html` at `/` and differ only in `GET /config.json`, which tells the UI its
mode and API bases. Without a build, a short placeholder page is served.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from mechatronikers.session.gateway import COLLECTOR_PORT

# Drawing text and model output are untrusted: no inline scripts, nothing from other origins.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_HEADERS = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",  # the review link carries the token
}


class UiApi(BaseModel):
    """API bases the UI calls: `run` + `events` for a run view at `/`, `runs` for a run table,
    `circuit` for a circuit run's circuit (the sandbox's circuit add-on; else `<run>/circuit`)."""

    model_config = ConfigDict(extra="forbid")

    run: str | None = None
    events: str | None = None
    runs: str | None = None
    circuit: str | None = None


class UiFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: bool = False  # `/` shows the run table instead of a run view
    # the run table offers "finish and shelve" (host only, D22, #52); left out elsewhere
    shelve: bool | None = None


class UiLinks(BaseModel):
    """Pages of other apps the UI links to (plain links, never API bases)."""

    model_config = ConfigDict(extra="forbid")

    runs: str | None = None  # the host's run table, where a sandbox's run view sends "shelve"


class UiConfig(BaseModel):
    """`GET /config.json` (docs/13, D74): a file, not an inline script, so the CSP keeps
    `script-src 'self'`. Mirrored by `frontend/src/config.ts`."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["sandbox", "host", "hub"]
    api: UiApi
    features: UiFeatures = UiFeatures()
    links: UiLinks | None = None


# The run table of `session watch` as the reviewer's browser sees it (its default port, which the
# gateway's TCP route to the collector assumes as well).
HOST_RUN_TABLE = f"http://127.0.0.1:{COLLECTOR_PORT}/"
# Mode 1: the sandbox serves one run view; its events come through the gateway (D61), a circuit
# run's circuit from the circuit add-on mounted at `/circuit` (docs/12). It cannot shelve itself
# (no route out): it links to the host's run table for that (#52).
SANDBOX_UI = UiConfig(
    mode="sandbox",
    api=UiApi(run="/api", events="/events", circuit="/circuit/api"),
    links=UiLinks(runs=HOST_RUN_TABLE),
)
# Mode 1 on the host: `session watch` serves the run table; each row links to its sandbox and
# can be finished and shelved there.
HOST_UI = UiConfig(
    mode="host", api=UiApi(runs="/sessions"), features=UiFeatures(table=True, shelve=True)
)
# Mode 2: the hub serves the overview at `/` and each run's view at `/runs/<run_id>` (docs/13).
HUB_UI = UiConfig(mode="hub", api=UiApi(runs="/api/runs"), features=UiFeatures(table=True))


def ui_dir() -> Path | None:
    """`MT_REVIEW_UI` (set in the sandbox image) or `frontend/dist` of a local build."""
    root = Path(os.environ.get("MT_REVIEW_UI", "frontend/dist"))
    return root if (root / "index.html").is_file() else None


def ui_routes(config: UiConfig, placeholder: str, pages: tuple[str, ...] = ()) -> list[BaseRoute]:
    """`/`, `/config.json` and the built assets; `pages` are further routes of the single-page
    app (e.g. the hub's `/runs/{run_id}`) that get the same `index.html`."""
    root = ui_dir()
    config_body = config.model_dump(mode="json", exclude_none=True)

    async def index(_: Request) -> Response:
        if root is None:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>Drawing2CAD review</title>"
                f"<p>{placeholder}</p>",
                headers=_HEADERS,
            )
        return FileResponse(root / "index.html", headers=_HEADERS)

    async def config_json(_: Request) -> Response:
        return JSONResponse(config_body, headers=_HEADERS)

    routes: list[BaseRoute] = [Route("/", index), Route("/config.json", config_json)]
    routes += [Route(page, index) for page in pages]
    if root is not None and (root / "assets").is_dir():
        routes.append(Mount("/assets", StaticFiles(directory=root / "assets")))
    return routes
