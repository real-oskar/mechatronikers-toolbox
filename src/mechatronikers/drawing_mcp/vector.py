"""Raw vector extraction from PDF drawings with PDFium (pypdfium2, permissive licence; D47).

Coordinates: sheet millimetres from the top-left corner (docs/03). Widths: effective stroke width
in points (stroke width × transformation scale), rounded to 0.01.
"""

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from numpy.typing import NDArray

PT_TO_MM = 25.4 / 72
_BEZIER_STEPS = 12

Points = NDArray[np.float64]


@dataclass
class SubPath:
    """One continuous run of a path, flattened to a polyline (sheet mm)."""

    points: Points
    closed: bool
    curved: bool  # contains Bézier segments (arcs, circles)
    line_segments: list[tuple[Points, Points]] = field(default_factory=list)


@dataclass
class VectorPath:
    width: float  # effective stroke width (pt)
    filled: bool
    stroked: bool
    subpaths: list[SubPath]
    bbox: tuple[float, float, float, float]  # sheet mm x0, y0, x1, y1
    dashed: bool = False  # stroked with a dash pattern (frames, centre lines)

    @property
    def size(self) -> float:
        return max(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1])

    @property
    def vertex_count(self) -> int:
        return sum(len(s.points) for s in self.subpaths)


@dataclass
class RawDrawing:
    path: Path
    width_mm: float
    height_mm: float
    paths: list[VectorPath]
    images: list[tuple[float, float, float, float]]  # sheet mm bboxes of embedded images


def _bezier(p0: Points, p1: Points, p2: Points, p3: Points) -> Points:
    t = np.linspace(0, 1, _BEZIER_STEPS + 1)[1:, None]
    curve: Points = (
        (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t**2 * p2 + t**3 * p3
    )
    return curve


def _subpaths(obj: pdfium.PdfObject, height_pt: float) -> list[SubPath]:
    m = obj.get_matrix()

    def to_sheet(x: float, y: float) -> Points:
        px, py = m.a * x + m.c * y + m.e, m.b * x + m.d * y + m.f
        return np.array([px * PT_TO_MM, (height_pt - py) * PT_TO_MM])

    result: list[SubPath] = []
    pts: list[Points] = []
    lines: list[tuple[Points, Points]] = []
    curved = closed = False
    pending: list[Points] = []

    def flush() -> None:
        nonlocal pts, lines, curved, closed
        if len(pts) >= 2:
            result.append(SubPath(np.array(pts), closed, curved, lines))
        pts, lines, curved, closed = [], [], False, False

    count = raw.FPDFPath_CountSegments(obj)
    for i in range(count):
        seg = raw.FPDFPath_GetPathSegment(obj, i)
        x, y = raw.FS_FLOAT(), raw.FS_FLOAT()
        raw.FPDFPathSegment_GetPoint(seg, x, y)
        point = to_sheet(x.value, y.value)
        kind = raw.FPDFPathSegment_GetType(seg)
        if kind == raw.FPDF_SEGMENT_MOVETO:
            flush()
            pts = [point]
        elif kind == raw.FPDF_SEGMENT_LINETO:
            if pts:
                lines.append((pts[-1], point))
            pts.append(point)
        elif kind == raw.FPDF_SEGMENT_BEZIERTO:
            pending.append(point)
            if len(pending) == 3:
                start = pts[-1] if pts else pending[0]
                pts.extend(_bezier(start, pending[0], pending[1], pending[2]))
                pending = []
                curved = True
        if raw.FPDFPathSegment_GetClose(seg) and pts:
            if np.linalg.norm(pts[-1] - pts[0]) > 1e-9:
                lines.append((pts[-1], pts[0]))
                pts.append(pts[0])
            closed = True
    flush()
    return result


def extract(pdf_path: Path, page_index: int = 0) -> RawDrawing:
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf[page_index]
    width_pt, height_pt = page.get_size()
    paths: list[VectorPath] = []
    images: list[tuple[float, float, float, float]] = []
    for obj in page.get_objects(max_depth=5):
        if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            images.append(
                (left * PT_TO_MM, (height_pt - top) * PT_TO_MM, right * PT_TO_MM,
                 (height_pt - bottom) * PT_TO_MM)
            )  # fmt: skip
            continue
        if obj.type != raw.FPDF_PAGEOBJ_PATH:
            continue
        subpaths = _subpaths(obj, height_pt)
        if not subpaths:
            continue
        width = raw.FS_FLOAT()
        raw.FPDFPageObj_GetStrokeWidth(obj, width)
        m = obj.get_matrix()
        mode, stroke = raw.c_int(), raw.FPDF_BOOL()
        raw.FPDFPath_GetDrawMode(obj, mode, stroke)
        allpts = np.concatenate([s.points for s in subpaths])
        paths.append(
            VectorPath(
                width=round(width.value * math.sqrt(abs(m.a * m.d - m.b * m.c)), 2),
                filled=mode.value != 0,
                stroked=bool(stroke.value),
                subpaths=subpaths,
                bbox=(
                    float(allpts[:, 0].min()),
                    float(allpts[:, 1].min()),
                    float(allpts[:, 0].max()),
                    float(allpts[:, 1].max()),
                ),
                dashed=raw.FPDFPageObj_GetDashCount(obj) > 0,
            )
        )
    return RawDrawing(
        path=pdf_path,
        width_mm=width_pt * PT_TO_MM,
        height_mm=height_pt * PT_TO_MM,
        paths=paths,
        images=images,
    )


def sheet_size_mm(pdf_path: Path, page_index: int = 0) -> tuple[float, float]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    width_pt, height_pt = pdf[page_index].get_size()
    return width_pt * PT_TO_MM, height_pt * PT_TO_MM


def fit_circle(points: Points) -> tuple[float, float, float, float]:
    """Algebraic least-squares circle: (cx, cy, r, max relative residual)."""
    x, y = points[:, 0], points[:, 1]
    a = np.column_stack([x, y, np.ones_like(x)])
    b = x**2 + y**2
    (c0, c1, c2), *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = c0 / 2, c1 / 2
    r = math.sqrt(max(c2 + cx**2 + cy**2, 0.0))
    residual = float(np.max(np.abs(np.hypot(x - cx, y - cy) - r)) / r) if r > 0 else math.inf
    return float(cx), float(cy), r, residual


def render_region(
    pdf_path: Path,
    bbox: tuple[float, float, float, float],
    out: Path,
    dpi: float = 300,
    page_index: int = 0,
) -> Path:
    """Render a sheet region (mm, top-left origin) to PNG."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf[page_index]
    width_pt, height_pt = page.get_size()
    x0, y0, x1, y1 = (v / PT_TO_MM for v in bbox)
    crop = (max(x0, 0), max(height_pt - y1, 0), max(width_pt - x1, 0), max(y0, 0))
    image = page.render(scale=dpi / 72, crop=crop).to_pil()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


SHEET_DPIS = (150, 200)  # the sheet view of the review UI (docs/13: rendered at analyse time)
MAX_SHEET_PIXELS = 40_000_000  # per image (~160 MB as a bitmap); large pages get a lower dpi


def sheet_dpi(size_mm: tuple[float, float], dpi: int, max_pixels: int = MAX_SHEET_PIXELS) -> int:
    """`dpi`, lowered so the whole sheet stays within `max_pixels` (PDF pages reach 5 m)."""
    inches = (size_mm[0] / 25.4) * (size_mm[1] / 25.4)
    return max(1, min(dpi, int(math.sqrt(max_pixels / inches)))) if inches > 0 else dpi


def render_sheet(
    pdf_path: Path,
    out_dir: Path,
    dpis: tuple[int, ...] = SHEET_DPIS,
    max_pixels: int = MAX_SHEET_PIXELS,
) -> list[tuple[int, Path]]:
    """The whole first sheet as `sheet-<dpi>.png` (dpi capped by `max_pixels`), so a hub can
    serve it without the PDF."""
    width, height = sheet_size_mm(pdf_path)
    capped = dict.fromkeys(sheet_dpi((width, height), dpi, max_pixels) for dpi in dpis)
    return [
        (dpi, render_region(pdf_path, (0, 0, width, height), out_dir / f"sheet-{dpi}.png", dpi))
        for dpi in capped
    ]
