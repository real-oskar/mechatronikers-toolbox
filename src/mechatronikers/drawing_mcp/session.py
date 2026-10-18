"""State behind the drawing-mcp tools: loaded drawings, label readings, rendered crops."""

import hashlib
import math
from pathlib import Path

from pydantic import BaseModel

from mechatronikers.drawing_mcp.analysis import Analysis, analyse
from mechatronikers.drawing_mcp.coverage import (
    CoverageReport,
    check_coverage,
    visible_circles,
    visible_outline,
)
from mechatronikers.drawing_mcp.dimensions import (
    Arrow,
    ChainClaim,
    ConsistencyReport,
    DimensionClaim,
    GapRequest,
    Measurement,
    check_consistency,
    find_arrows,
    measure_label,
    parse_dimension_text,
)
from mechatronikers.drawing_mcp.vector import render_region, render_sheet
from mechatronikers.spec import Feature
from mechatronikers.spec.drawing import BBox, DrawingAnalysis, Label, Primitive, ViewRegion
from mechatronikers.telemetry import EventEmitter

MAX_PRIMITIVES = 500  # per find_geometry call; ask for a smaller region for more
MAX_RENDER_MM = 500.0
MAX_DPI = 600


class RenderedImage(BaseModel):
    uri: str
    path: str
    bbox: BBox
    dpi: float
    sha256: str
    png: bytes  # returned to the model as MCP image content, not as JSON


class SheetInfoResult(BaseModel):
    drawing_id: str
    sheet_format: str | None
    sheet_size: tuple[float, float]
    title_block: BBox | None
    drawing_area: BBox
    note: str


class GeometryResult(BaseModel):
    primitives: list[Primitive]
    truncated: bool


class BetweenResult(BaseModel):
    kind: str  # "parallel distance", "point distance", "angle", "center to line"
    sheet_value: float  # sheet mm, or degrees for angles
    part_value: float | None  # sheet value / scale (None for angles or without scale)


class DrawingSession:
    def __init__(self, input_root: Path, artifacts_dir: Path, emitter: EventEmitter) -> None:
        self.input_root = input_root.resolve()
        self.artifacts_dir = artifacts_dir
        self.emitter = emitter
        self.drawings: dict[str, Analysis] = {}
        self._arrows: dict[str, list[Arrow]] = {}
        self._reports: dict[str, ConsistencyReport] = {}  # last check per drawing

    def _resolve(self, path: str) -> Path:
        candidate = (self.input_root / path).resolve()
        if not candidate.is_relative_to(self.input_root):
            raise ValueError(f"drawings must be inside {self.input_root}")
        if not candidate.is_file():
            raise FileNotFoundError(f"no such drawing: {path}")
        return candidate

    def get(self, drawing_id: str) -> Analysis:
        if drawing_id not in self.drawings:
            raise KeyError(f"unknown drawing {drawing_id!r}; load it first")
        return self.drawings[drawing_id]

    def load(self, path: str) -> DrawingAnalysis:
        source = self._resolve(path)
        if source.suffix.lower() != ".pdf":
            raise ValueError("only vector PDF drawings are supported so far (raster path: #22)")
        analysis = analyse(source)
        drawing_id = analysis.summary.source_sha256[:12]
        self.drawings[drawing_id] = analysis
        s = analysis.summary
        self.emitter.emit(
            "drawing.loaded",
            {
                "drawing_id": drawing_id,
                "file": source.name,
                "format": s.sheet_format,
                "views": len(s.views),
                "labels": len(s.labels),
                "circles": s.circle_count,
                "outline_width": s.layer_map.outline_width,
            },
        )
        self._sheet_artifacts(source, drawing_id)
        return s

    def _sheet_artifacts(self, source: Path, drawing_id: str) -> None:
        """The sheet as PNG artifacts (role `sheet`), rendered here in the sandbox at analyse
        time, so the review UI never needs the PDF outside the sandbox (docs/13)."""
        out_dir = self.artifacts_dir / "drawing" / drawing_id
        session = self.emitter.session_id or "local"
        try:
            renders = render_sheet(source, out_dir)
        except Exception as exc:  # a convenience for the hub: never fail the load for it
            self.emitter.emit(
                "artifact.render_failed",
                {"role": "sheet", "drawing_id": drawing_id, "error": str(exc)[:300]},
                level="warning",
            )
            return
        for dpi, out in renders:
            data = out.read_bytes()
            self.emitter.emit(
                "artifact.created",
                {
                    "uri": f"artifact://{session}/drawing/{drawing_id}/{out.name}",
                    "path": str(out),
                    "format": "png",
                    "role": "sheet",
                    "dpi": dpi,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "drawing_id": drawing_id,
                },
            )

    def drawing_id_of(self, summary: DrawingAnalysis) -> str:
        return summary.source_sha256[:12]

    def sheet_info(self, drawing_id: str) -> SheetInfoResult:
        s = self.get(drawing_id).summary
        return SheetInfoResult(
            drawing_id=drawing_id,
            sheet_format=s.sheet_format,
            sheet_size=s.sheet_size,
            title_block=s.title_block,
            drawing_area=s.drawing_area,
            note="Title block text (part name, scale, notes) is vector lettering: read it from "
            "render_region(title_block); record the scale with set_label_text on its label.",
        )

    def views(self, drawing_id: str) -> list[ViewRegion]:
        return self.get(drawing_id).summary.views

    def labels(
        self, drawing_id: str, view_id: str | None = None, region: str | None = None
    ) -> list[Label]:
        return [
            label
            for label in self.get(drawing_id).summary.labels
            if (view_id is None or label.view_id == view_id)
            and (region is None or label.region == region)
        ]

    def set_label_text(self, drawing_id: str, label_id: str, text: str, source: str) -> Label:
        for label in self.get(drawing_id).summary.labels:
            if label.id == label_id:
                label.text = text
                label.text_source = source  # type: ignore[assignment]
                self.emitter.emit(
                    "drawing.label_read",
                    {
                        "drawing_id": drawing_id,
                        "label_id": label_id,
                        "text": text,
                        "source": source,
                    },
                )
                return label
        raise KeyError(f"unknown label {label_id!r}")

    def render(self, drawing_id: str, bbox: BBox, dpi: float = 300) -> RenderedImage:
        analysis = self.get(drawing_id)
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError("bbox must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0")
        if max(x1 - x0, y1 - y0) > MAX_RENDER_MM:
            raise ValueError(f"region larger than {MAX_RENDER_MM} mm")
        dpi = min(dpi, MAX_DPI)
        name = hashlib.sha256(f"{bbox}{dpi}".encode()).hexdigest()[:12]
        out = self.artifacts_dir / "drawing" / drawing_id / f"region-{name}.png"
        render_region(analysis.raw.path, bbox, out, dpi)
        data = out.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        session = self.emitter.session_id or "local"
        image = RenderedImage(
            uri=f"artifact://{session}/drawing/{drawing_id}/{out.name}",
            path=str(out),
            bbox=bbox,
            dpi=dpi,
            sha256=digest,
            png=data,
        )
        self.emitter.emit(
            "artifact.created",
            {"uri": image.uri, "sha256": digest, "bytes": len(data), "bbox": bbox, "dpi": dpi},
        )
        return image

    def arrows(self, drawing_id: str) -> list[Arrow]:
        if drawing_id not in self._arrows:
            self._arrows[drawing_id] = find_arrows(self.get(drawing_id).raw)
        return self._arrows[drawing_id]

    def _label(self, drawing_id: str, label_id: str) -> Label:
        for label in self.get(drawing_id).summary.labels:
            if label.id == label_id:
                return label
        raise KeyError(f"unknown label {label_id!r}")

    def measure_label(self, drawing_id: str, label_id: str, text: str | None = None) -> Measurement:
        label = self._label(drawing_id, label_id)
        reading = text or label.text
        if not reading:
            raise ValueError(f"label {label_id} has no text yet; read it and call set_label_text")
        kind = parse_dimension_text(reading).kind
        return measure_label(label, kind, self.arrows(drawing_id), self.get(drawing_id).primitives)

    def measure_between(
        self, drawing_id: str, a_id: str, b_id: str, scale: float | None = None
    ) -> BetweenResult:
        prims = {p.id: p for p in self.get(drawing_id).primitives}
        a, b = prims[a_id], prims[b_id]
        kind, value = _between(a, b)
        part = None if kind == "angle" or not scale else value / scale
        return BetweenResult(kind=kind, sheet_value=round(value, 4), part_value=part)

    def check(
        self,
        drawing_id: str,
        sheet_scale: float | None = None,
        claims: list[DimensionClaim] | None = None,
        chains: list[ChainClaim] | None = None,
        gaps: list[GapRequest] | None = None,
    ) -> ConsistencyReport:
        analysis = self.get(drawing_id)
        labels = {label.id: label for label in analysis.summary.labels}
        if claims is None:
            claims = [
                DimensionClaim(
                    label_id=label.id,
                    text=label.text,
                    reading_confidence=1.0 if label.text_source == "human" else 0.9,
                )
                for label in analysis.summary.labels
                if label.text and label.region == "drawing"
            ]
        report = check_consistency(
            labels, self.arrows(drawing_id), analysis.primitives, claims,
            sheet_scale=sheet_scale, chains=chains, gaps=gaps,
        )  # fmt: skip
        self._reports[drawing_id] = report
        self.emitter.emit(
            "drawing.consistency_checked",
            {
                "drawing_id": drawing_id,
                "dimensions": len(report.dimensions),
                "review_items": len(report.review_items),
                "scales": [s.model_dump() for s in report.scales],
            },
        )
        return report

    def coverage(self, drawing_id: str, features: list[Feature]) -> CoverageReport:
        """Compare the planned features with the drawing (uses the last consistency check)."""
        analysis = self.get(drawing_id)
        if drawing_id not in self._reports:
            raise ValueError("run check_consistency first (coverage needs scales and dimensions)")
        report = self._reports[drawing_id]
        sheet_scale = next((s.sheet_scale for s in report.scales if s.sheet_scale), None)
        estimated = {s.view_id: s.scale for s in report.scales if s.view_id and s.scale}
        views = {v.id for v in analysis.summary.views if v.kind == "orthographic"}
        scales = {v: estimated.get(v) or sheet_scale or 1.0 for v in views}
        outline_width = analysis.summary.layer_map.outline_width
        result = check_coverage(
            features,
            [d for d in report.dimensions if d.status != "rejected"],
            visible_circles(analysis.primitives, views, outline_width),
            scales,
            visible_outline(analysis.primitives, analysis.summary.views, outline_width),
        )
        self.emitter.emit(
            "drawing.coverage_checked",
            {
                "drawing_id": drawing_id,
                "features": len(features),
                "circles": result.circles_checked,
                "dimensions": result.dimensions_checked,
                "edges": result.edges_checked,
                "possibly_missing": len(result.review_items),
            },
        )
        return result

    def find_geometry(
        self,
        drawing_id: str,
        bbox: BBox,
        kinds: list[str] | None = None,
        min_width: float | None = None,
    ) -> GeometryResult:
        x0, y0, x1, y1 = bbox
        hits = []
        for p in self.get(drawing_id).primitives:
            bx0, by0, bx1, by1 = p.bbox()
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if kinds and p.kind not in kinds:
                continue
            if min_width is not None and p.width < min_width:
                continue
            hits.append(p)
        hits.sort(key=lambda p: -(p.radius or 0) if p.kind != "line" else -_length(p))
        return GeometryResult(
            primitives=hits[:MAX_PRIMITIVES], truncated=len(hits) > MAX_PRIMITIVES
        )


def _point(p: Primitive) -> tuple[float, float] | None:
    return p.center if p.kind in ("circle", "arc") else None


def _between(a: Primitive, b: Primitive) -> tuple[str, float]:
    """Distance (or angle) between two primitives in sheet units."""
    pa, pb = _point(a), _point(b)
    if pa is not None and pb is not None:
        return "point distance", math.dist(pa, pb)
    if a.kind == "line" and b.kind == "line":
        assert a.start and a.end and b.start and b.end
        ua = (a.end[0] - a.start[0], a.end[1] - a.start[1])
        ub = (b.end[0] - b.start[0], b.end[1] - b.start[1])
        la, lb = math.hypot(*ua), math.hypot(*ub)
        cross = (ua[0] * ub[1] - ua[1] * ub[0]) / (la * lb)
        if abs(cross) < 1e-3:  # parallel: perpendicular distance
            dx, dy = b.start[0] - a.start[0], b.start[1] - a.start[1]
            return "parallel distance", abs(dx * ua[1] - dy * ua[0]) / la
        dot = (ua[0] * ub[0] + ua[1] * ub[1]) / (la * lb)
        return "angle", math.degrees(math.acos(max(-1.0, min(1.0, abs(dot)))))
    line, other = (a, b) if a.kind == "line" else (b, a)
    point = _point(other)
    assert line.start and line.end and point is not None
    ux, uy = line.end[0] - line.start[0], line.end[1] - line.start[1]
    dx, dy = point[0] - line.start[0], point[1] - line.start[1]
    return "center to line", abs(dx * uy - dy * ux) / math.hypot(ux, uy)


def _length(p: Primitive) -> float:
    assert p.start is not None and p.end is not None
    return math.dist(p.start, p.end)
