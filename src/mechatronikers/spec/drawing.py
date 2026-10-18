"""DrawingAnalysis: what drawing-mcp extracts, independent of the input path (docs/04).

All coordinates are sheet millimetres from the top-left corner, at print scale (before applying
the drawing scale).
"""

from typing import Literal

from pydantic import BaseModel, Field

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 (sheet mm)
Vec2 = tuple[float, float]
Region = Literal["drawing", "title_block", "border"]


class Primitive(BaseModel):
    """One geometric element of the drawing."""

    id: str
    kind: Literal["line", "arc", "circle"]
    width: float  # effective stroke width (pt)
    start: Vec2 | None = None  # line, arc
    end: Vec2 | None = None  # line, arc
    center: Vec2 | None = None  # arc, circle
    radius: float | None = None  # arc, circle (sheet mm)
    start_angle: float | None = None  # arc, degrees, sheet frame (x right, y down)
    sweep_angle: float | None = None  # arc, degrees
    region: Region = "drawing"
    view_id: str | None = None

    def bbox(self) -> BBox:
        if self.kind == "circle" or self.kind == "arc":
            assert self.center is not None and self.radius is not None
            cx, cy, r = self.center[0], self.center[1], self.radius
            return (cx - r, cy - r, cx + r, cy + r)
        assert self.start is not None and self.end is not None
        return (
            min(self.start[0], self.end[0]),
            min(self.start[1], self.end[1]),
            max(self.start[0], self.end[0]),
            max(self.start[1], self.end[1]),
        )


class Label(BaseModel):
    """A cluster of glyph outlines: one text item such as '60', 'Ø20', 'A-A', 'M10'.

    Glyphs are vector outlines without a text layer, so the text itself is read by a vision
    model from `crop_bbox` (or entered by a human).
    """

    id: str
    bbox: BBox
    crop_bbox: BBox  # bbox with margin, for rendering
    orientation: Literal["horizontal", "vertical", "unknown"]
    glyph_count: int
    region: Region
    view_id: str | None = None
    text: str | None = None  # filled in after reading
    text_source: Literal["vlm", "ocr", "human"] | None = None


class ViewRegion(BaseModel):
    id: str
    bbox: BBox
    kind: Literal["orthographic", "image"]  # image = embedded raster (e.g. shaded iso preview)
    primitive_count: int = 0
    circle_count: int = 0


class LayerMap(BaseModel):
    """Per-sheet interpretation of stroke widths (they differ between sheets, T5/D48)."""

    lengths_by_width: dict[str, float]  # total stroke length (sheet mm) per width, drawing area
    outline_width: float | None  # widest width with a significant share: used to find views
    confidence: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)


class DrawingAnalysis(BaseModel):
    source_file: str
    source_sha256: str
    input_path: Literal["vector", "raster"] = "vector"
    sheet_size: Vec2  # mm
    sheet_format: str | None  # e.g. "A3"
    drawing_area: BBox  # inside the frame
    title_block: BBox | None
    layer_map: LayerMap
    views: list[ViewRegion]
    labels: list[Label]
    primitive_count: int
    circle_count: int
