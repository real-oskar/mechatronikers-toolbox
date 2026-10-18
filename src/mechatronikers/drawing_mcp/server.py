"""drawing-mcp server: analyse technical drawings for the agent (docs/04).

Deterministic and offline. The drawing has no text layer: lettering is returned as labels with
positions, and images of label regions are returned so a vision model can read them.
"""

import functools
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from mechatronikers.drawing_mcp.coverage import CoverageReport
from mechatronikers.drawing_mcp.dimensions import (
    ChainClaim,
    ConsistencyReport,
    DimensionClaim,
    GapRequest,
    Measurement,
)
from mechatronikers.drawing_mcp.session import (
    BetweenResult,
    DrawingSession,
    GeometryResult,
    SheetInfoResult,
)
from mechatronikers.spec import SPEC_VERSION, Feature
from mechatronikers.spec.drawing import BBox, DrawingAnalysis, Label, ViewRegion
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.instrument import instrument

MAX_LABELS_PER_RENDER = 24


def create_server(
    emitter: EventEmitter,
    input_root: Path | None = None,
    artifacts_dir: Path | None = None,
) -> FastMCP:
    server = FastMCP(
        "drawing-mcp",
        instructions=(
            "Analyse a technical drawing: load_drawing, then list_views and list_labels. Labels "
            "are vector lettering without text: read them from render_labels / render_region "
            "images and store readings with set_label_text. find_geometry returns exact lines, "
            "arcs and circles (sheet mm, top-left origin, print scale: divide by the drawing "
            "scale for part dimensions)."
        ),
        stateless_http=True,
        log_level="WARNING",
        # The session gateway authenticates every request; the container is unreachable otherwise.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    session = DrawingSession(
        input_root or Path("/session/input"),
        artifacts_dir or Path("/session/artifacts"),
        emitter,
    )

    def tool(fn: Any) -> Any:
        return server.tool()(instrument(emitter, "drawing-mcp")(fn))

    def image_tool(fn: Any) -> Any:
        # Images are returned as MCP image content, not as structured JSON output.
        return server.tool(structured_output=False)(instrument(emitter, "drawing-mcp")(fn))

    @tool
    def drawing_ping() -> dict[str, str]:
        """Health check: returns the server name and spec version."""
        return {"server": "drawing-mcp", "spec_version": SPEC_VERSION}

    @tool
    async def load_drawing(path: str) -> dict[str, Any]:
        """Load a PDF drawing (path relative to the session input folder). Returns the
        drawing_id and an overview: sheet format, views, label count, circles, layer map."""
        summary: DrawingAnalysis = await anyio.to_thread.run_sync(lambda: session.load(path))
        return {
            "drawing_id": session.drawing_id_of(summary),
            "sheet_format": summary.sheet_format,
            "sheet_size": summary.sheet_size,
            "title_block": summary.title_block,
            "views": [v.model_dump() for v in summary.views],
            "label_count": len(summary.labels),
            "circle_count": summary.circle_count,
            "layer_map": summary.layer_map.model_dump(),
        }

    @tool
    def get_sheet_info(drawing_id: str) -> SheetInfoResult:
        """Sheet format, size, frame and title block region."""
        return session.sheet_info(drawing_id)

    @tool
    def list_views(drawing_id: str) -> list[ViewRegion]:
        """View regions (sheet mm). kind=image is the embedded shaded preview."""
        return session.views(drawing_id)

    @tool
    def list_labels(
        drawing_id: str,
        view_id: str | None = None,
        region: Literal["drawing", "title_block", "border"] | None = None,
    ) -> list[Label]:
        """Text items (dimensions, notes, view names) with positions; text is filled in once
        read via set_label_text."""
        return session.labels(drawing_id, view_id, region)

    @image_tool
    async def render_region(drawing_id: str, bbox: BBox, dpi: float = 200) -> Image:
        """Image of a sheet region (x0, y0, x1, y1 in sheet mm), e.g. a view or the title block."""
        rendered = await anyio.to_thread.run_sync(lambda: session.render(drawing_id, bbox, dpi))
        return Image(data=rendered.png, format="png")

    @image_tool
    async def render_labels(drawing_id: str, label_ids: list[str], dpi: float = 400) -> list[Any]:
        """Images of label crops, in the given order, each preceded by its label id."""
        if len(label_ids) > MAX_LABELS_PER_RENDER:
            raise ValueError(f"at most {MAX_LABELS_PER_RENDER} labels per call")
        labels = {label.id: label for label in session.labels(drawing_id)}
        out: list[Any] = []
        for label_id in label_ids:
            if label_id not in labels:
                raise KeyError(f"unknown label {label_id!r}")
            crop = labels[label_id].crop_bbox
            render = functools.partial(session.render, drawing_id, crop, dpi)
            rendered = await anyio.to_thread.run_sync(render)
            out += [f"label {label_id}", Image(data=rendered.png, format="png")]
        return out

    @tool
    def set_label_text(
        drawing_id: str,
        label_id: str,
        text: str,
        source: Literal["vlm", "ocr", "human"] = "vlm",
    ) -> Label:
        """Record what a label says (exactly as printed, e.g. 'Ø20', '3x Ø12', 'M10', '2:1')."""
        return session.set_label_text(drawing_id, label_id, text, source)

    @tool
    def find_geometry(
        drawing_id: str,
        bbox: BBox,
        kinds: list[Literal["line", "arc", "circle"]] | None = None,
        min_width: float | None = None,
    ) -> GeometryResult:
        """Lines, arcs and circles inside a region (sheet mm, print scale), largest first."""
        return session.find_geometry(drawing_id, bbox, list(kinds) if kinds else None, min_width)

    @tool
    def measure_label(drawing_id: str, label_id: str) -> Measurement:
        """Find the geometry a read label dimensions (arrow pair, circle or arc) and measure it
        in sheet mm. The label's text must be set first (set_label_text)."""
        return session.measure_label(drawing_id, label_id)

    @tool
    def measure_between(
        drawing_id: str, primitive_a: str, primitive_b: str, scale: float | None = None
    ) -> BetweenResult:
        """Distance between two primitives (parallel lines, circle centres, centre to line) or
        the angle between two lines. With `scale` (2.0 for 2:1) also in part mm."""
        return session.measure_between(drawing_id, primitive_a, primitive_b, scale)

    @tool
    async def check_consistency(
        drawing_id: str,
        sheet_scale: float | None = None,
        claims: list[DimensionClaim] | None = None,
        chains: list[ChainClaim] | None = None,
        gaps: list[GapRequest] | None = None,
    ) -> ConsistencyReport:
        """Check read dimensions against the drawing: scale per view, mismatches (printed value
        wins), chains, pattern counts; gaps = needed but unprinted dimensions measured from the
        geometry with a reason. Without `claims`, all labels with text are used. Returns
        spec-ready dimensions with computed confidence and review items."""
        return await anyio.to_thread.run_sync(
            lambda: session.check(drawing_id, sheet_scale, claims, chains, gaps)
        )

    @tool
    async def check_coverage(drawing_id: str, features: list[Feature]) -> CoverageReport:
        """Does a planned feature list explain the drawing? Run check_consistency first. Returns
        review items (points of interest with sheet position) for visible circles no feature
        explains, views showing more circles of a diameter than planned, and printed
        dimensions no feature uses. Advisory: covered evidence is no proof of correctness."""
        return await anyio.to_thread.run_sync(lambda: session.coverage(drawing_id, features))

    return server
