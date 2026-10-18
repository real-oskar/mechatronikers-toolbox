"""Vector path of the drawing analysis: frame, title block, primitives, labels, views (docs/04).

Deterministic and offline. Interpretation (what a line means, what a label says) is left to the
vision model and the human reviewer; this module supplies exact positions and geometry.
"""

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from mechatronikers.drawing_mcp.vector import RawDrawing, SubPath, extract, fit_circle
from mechatronikers.spec.drawing import (
    BBox,
    DrawingAnalysis,
    Label,
    LayerMap,
    Primitive,
    Region,
    ViewRegion,
)

_FRAME_SPAN = 0.9  # frame lines span at least this share of the sheet
_TITLE_LINE_SPAN = 0.3
_CIRCLE_RESIDUAL = 0.005  # max relative radial deviation to accept a circle/arc fit
_MAX_GLYPH = 12.0  # mm; larger filled shapes are not lettering
_ARROW_MAX_VERTICES = 5
_OUTLINE_SHARE = 0.15
_VIEW_CELL = 2.0  # mm grid for view clustering
_VIEW_DILATE = 2  # cells
_MIN_VIEW = 15.0  # mm; smaller clusters are fragments (dimension pieces, symbols)
_FRAGMENT_MERGE = 15.0  # mm; fragments this close to a view are merged into it
_FRAME_CLEARANCE = 3.0  # mm; zone ticks along the frame are not drawing geometry
_LABEL_GAP = 0.45  # × glyph height
_LABEL_MARGIN = 1.0  # mm

SHEET_FORMATS = {
    "A4": (210, 297),
    "A3": (420, 297),
    "A2": (594, 420),
    "A1": (841, 594),
    "A0": (1189, 841),
}


@dataclass
class Layout:
    area: BBox
    title_block: BBox | None


def _sheet_format(width: float, height: float) -> str | None:
    for name, (w, h) in SHEET_FORMATS.items():
        for a, b in ((w, h), (h, w)):
            if abs(width - a) < 2 and abs(height - b) < 2:
                return name
    return None


def _segments(
    raw: RawDrawing, stroked_only: bool = True
) -> list[tuple[float, np.ndarray, np.ndarray]]:
    out = []
    for p in raw.paths:
        if stroked_only and not p.stroked:
            continue
        for s in p.subpaths:
            for a, b in zip(s.points[:-1], s.points[1:], strict=True):
                out.append((p.width, a, b))
    return out


def detect_layout(raw: RawDrawing) -> Layout:
    """Inner frame = innermost lines spanning ≥ 90 % of the sheet; title block = area below the
    topmost long horizontal line that ends at the right frame edge in the lowest quarter."""
    w, h = raw.width_mm, raw.height_mm
    hs, vs = [], []
    segments = _segments(raw)
    for _, a, b in segments:
        if abs(a[1] - b[1]) < 0.01 and abs(a[0] - b[0]) > _FRAME_SPAN * w:
            hs.append(float(a[1]))
        if abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) > _FRAME_SPAN * h:
            vs.append(float(a[0]))
    left = max([x for x in vs if x < w / 2], default=0.0)
    right = min([x for x in vs if x > w / 2], default=w)
    top = max([y for y in hs if y < h / 2], default=0.0)
    bottom = min([y for y in hs if y > h / 2], default=h)
    title: list[tuple[float, float]] = []
    for _, a, b in segments:
        if (
            abs(a[1] - b[1]) < 0.01
            and abs(max(a[0], b[0]) - right) < 0.5
            and 0.75 * h < a[1] < bottom - 0.5
            and abs(a[0] - b[0]) > _TITLE_LINE_SPAN * w
        ):
            title.append((float(min(a[0], b[0])), float(a[1])))
    block = None
    if title:
        block = (min(x for x, _ in title), min(y for _, y in title), right, bottom)
    return Layout(area=(left, top, right, bottom), title_block=block)


def _inside(point: tuple[float, float], box: BBox | None) -> bool:
    return box is not None and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def region_of(point: tuple[float, float], layout: Layout) -> Region:
    if _inside(point, layout.title_block):
        return "title_block"
    if _inside(point, layout.area):
        return "drawing"
    return "border"


def layer_map(raw: RawDrawing, layout: Layout) -> LayerMap:
    lengths: dict[float, float] = defaultdict(float)
    for p in raw.paths:
        if not p.stroked or p.filled:
            continue
        for s in p.subpaths:
            for a, b in zip(s.points[:-1], s.points[1:], strict=True):
                mid = (float((a[0] + b[0]) / 2), float((a[1] + b[1]) / 2))
                if region_of(mid, layout) == "drawing":
                    lengths[p.width] += float(np.linalg.norm(b - a))
    total = sum(lengths.values()) or 1.0
    significant = [w for w, length in lengths.items() if length / total >= _OUTLINE_SHARE]
    outline = max(significant, default=None)
    share = lengths.get(outline, 0.0) / total if outline is not None else 0.0
    notes = []
    if outline is not None and outline < 0.7:
        notes.append(
            f"outline width {outline} pt is thin; visible edges and dimension lines may "
            "share widths"
        )
    return LayerMap(
        lengths_by_width={f"{w:.2f}": round(v, 1) for w, v in sorted(lengths.items())},
        outline_width=outline,
        confidence=round(min(1.0, share / 0.5), 2),
        notes=notes,
    )


def _arc_or_circle(s: SubPath) -> tuple[str, dict[str, object]] | None:
    if not s.curved or len(s.points) < 6:
        return None
    cx, cy, r, residual = fit_circle(s.points)
    if residual > _CIRCLE_RESIDUAL or r <= 0:
        return None
    angles = np.unwrap(np.arctan2(s.points[:, 1] - cy, s.points[:, 0] - cx))
    sweep = math.degrees(float(angles[-1] - angles[0]))
    if abs(sweep) >= 359.0:
        return "circle", {"center": (cx, cy), "radius": r}
    start = math.degrees(float(angles[0]))
    return "arc", {
        "center": (cx, cy),
        "radius": r,
        "start_angle": start,
        "sweep_angle": sweep,
        "start": (float(s.points[0, 0]), float(s.points[0, 1])),
        "end": (float(s.points[-1, 0]), float(s.points[-1, 1])),
    }


def extract_primitives(raw: RawDrawing, layout: Layout) -> list[Primitive]:
    prims: list[Primitive] = []
    for p in raw.paths:
        if not p.stroked or p.filled:
            continue
        for s in p.subpaths:
            fitted = _arc_or_circle(s)
            if fitted is not None:
                kind, data = fitted
                center = data["center"]
                assert isinstance(center, tuple)
                prims.append(
                    Primitive(
                        id=f"p{len(prims)}",
                        kind=kind,
                        width=p.width,
                        region=region_of(center, layout),
                        **data,
                    )
                )
                continue
            for a, b in zip(s.points[:-1], s.points[1:], strict=True):
                if float(np.linalg.norm(b - a)) < 1e-6:
                    continue
                mid = (float((a[0] + b[0]) / 2), float((a[1] + b[1]) / 2))
                prims.append(
                    Primitive(
                        id=f"p{len(prims)}",
                        kind="line",
                        width=p.width,
                        start=(float(a[0]), float(a[1])),
                        end=(float(b[0]), float(b[1])),
                        region=region_of(mid, layout),
                    )
                )
    return prims


def _glyph_boxes(raw: RawDrawing) -> list[BBox]:
    """Bounding boxes of filled shapes that look like lettering (not arrowheads, not fills)."""
    boxes: list[BBox] = []
    for p in raw.paths:
        if not p.filled or p.stroked:
            continue
        for s in p.subpaths:
            x0, y0 = s.points.min(axis=0)
            x1, y1 = s.points.max(axis=0)
            size = max(x1 - x0, y1 - y0)
            if size > _MAX_GLYPH or size < 0.2:
                continue
            if len(s.points) <= _ARROW_MAX_VERTICES and not s.curved:
                continue  # arrowheads and small solid triangles
            boxes.append((float(x0), float(y0), float(x1), float(y1)))
    return boxes


def _merge_boxes(boxes: list[BBox]) -> list[list[int]]:
    """Union-find over glyph boxes expanded by a gap relative to the glyph height."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    heights = sorted(min(b[2] - b[0], b[3] - b[1]) for b in boxes) or [3.5]
    gap = _LABEL_GAP * max(heights[len(heights) // 2], 1.5)
    grown = [(b[0] - gap, b[1] - gap, b[2] + gap, b[3] + gap) for b in boxes]
    order = sorted(range(len(boxes)), key=lambda i: grown[i][0])
    active: list[int] = []
    for i in order:
        active = [j for j in active if grown[j][2] >= grown[i][0]]
        for j in active:
            gi, gj = grown[i], grown[j]
            if gi[1] <= gj[3] and gj[1] <= gi[3]:
                parent[find(i)] = find(j)
        active.append(i)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(boxes)):
        groups[find(i)].append(i)
    return list(groups.values())


def extract_labels(raw: RawDrawing, layout: Layout) -> list[Label]:
    boxes = _glyph_boxes(raw)
    labels: list[Label] = []
    for group in sorted(_merge_boxes(boxes), key=lambda g: min(boxes[i][1] for i in g)):
        gb = [boxes[i] for i in group]
        x0, y0 = min(b[0] for b in gb), min(b[1] for b in gb)
        x1, y1 = max(b[2] for b in gb), max(b[3] for b in gb)
        if len(gb) < 2:
            orientation = "unknown"
        else:
            cx = np.array([(b[0] + b[2]) / 2 for b in gb])
            cy = np.array([(b[1] + b[3]) / 2 for b in gb])
            orientation = "horizontal" if np.ptp(cx) >= np.ptp(cy) else "vertical"
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        m = _LABEL_MARGIN
        labels.append(
            Label(
                id=f"l{len(labels)}",
                bbox=(x0, y0, x1, y1),
                crop_bbox=(x0 - m, y0 - m, x1 + m, y1 + m),
                orientation=orientation,
                glyph_count=len(gb),
                region=region_of(center, layout),
            )
        )
    return labels


def detect_views(
    raw: RawDrawing, layout: Layout, prims: list[Primitive], outline_width: float | None
) -> list[ViewRegion]:
    """Connected clusters of outline geometry in the drawing area, plus embedded images."""
    x0, y0, x1, y1 = layout.area
    nx, ny = int((x1 - x0) / _VIEW_CELL) + 1, int((y1 - y0) / _VIEW_CELL) + 1
    grid = np.zeros((nx, ny), dtype=bool)
    image_boxes = list(raw.images)
    members: list[Primitive] = []

    def near_frame(box: BBox) -> bool:
        return (
            box[0] < x0 + _FRAME_CLEARANCE
            or box[1] < y0 + _FRAME_CLEARANCE
            or box[2] > x1 - _FRAME_CLEARANCE
            or box[3] > y1 - _FRAME_CLEARANCE
        )

    for p in prims:
        if p.region != "drawing" or near_frame(p.bbox()):
            continue
        if outline_width is not None and p.kind == "line" and abs(p.width - outline_width) > 0.01:
            continue
        bx0, by0, bx1, by1 = p.bbox()
        if any(_inside(((bx0 + bx1) / 2, (by0 + by1) / 2), box) for box in image_boxes):
            continue
        members.append(p)
        if p.kind == "line":
            assert p.start is not None and p.end is not None
            n = max(2, int(math.dist(p.start, p.end) / (_VIEW_CELL / 2)) + 1)
            pts = np.linspace(p.start, p.end, n)
        else:
            assert p.center is not None and p.radius is not None
            t = np.linspace(0, 2 * math.pi, 48)
            pts = np.column_stack(
                [p.center[0] + p.radius * np.cos(t), p.center[1] + p.radius * np.sin(t)]
            )
        ix = np.clip(((pts[:, 0] - x0) / _VIEW_CELL).astype(int), 0, nx - 1)
        iy = np.clip(((pts[:, 1] - y0) / _VIEW_CELL).astype(int), 0, ny - 1)
        grid[ix, iy] = True
    from scipy import ndimage

    dilated = ndimage.binary_dilation(grid, iterations=_VIEW_DILATE)
    labelled, count = ndimage.label(dilated)
    boxes: list[BBox] = []
    for k in range(1, count + 1):
        cells = np.argwhere(labelled == k)
        boxes.append(
            (
                x0 + (int(np.min(cells[:, 0])) + _VIEW_DILATE) * _VIEW_CELL,
                y0 + (int(np.min(cells[:, 1])) + _VIEW_DILATE) * _VIEW_CELL,
                x0 + (int(np.max(cells[:, 0])) + 1 - _VIEW_DILATE) * _VIEW_CELL,
                y0 + (int(np.max(cells[:, 1])) + 1 - _VIEW_DILATE) * _VIEW_CELL,
            )
        )
    big = [b for b in boxes if max(b[2] - b[0], b[3] - b[1]) >= _MIN_VIEW]
    for frag in (b for b in boxes if max(b[2] - b[0], b[3] - b[1]) < _MIN_VIEW):
        distances = [_box_distance(frag, b) for b in big]
        if distances and min(distances) <= _FRAGMENT_MERGE:
            i = int(np.argmin(distances))
            b = big[i]
            big[i] = (
                min(b[0], frag[0]),
                min(b[1], frag[1]),
                max(b[2], frag[2]),
                max(b[3], frag[3]),
            )
    views = [ViewRegion(id="", bbox=b, kind="orthographic") for b in _merge_overlapping(big)]
    for box in image_boxes:
        if region_of(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), layout) == "drawing":
            views.append(ViewRegion(id="", bbox=box, kind="image"))
    views.sort(key=lambda v: (v.bbox[1], v.bbox[0]))
    for i, v in enumerate(views):
        v.id = f"v{i}"
    for p in members:
        cx, cy = _center(p.bbox())
        for v in views:
            if v.kind == "orthographic" and _inside((cx, cy), v.bbox):
                p.view_id = v.id
                v.primitive_count += 1
                v.circle_count += p.kind == "circle"
                break
    return views


def _merge_overlapping(boxes: list[BBox]) -> list[BBox]:
    """Merge regions that overlap or nest (e.g. a hole drawn inside its view's outline)."""
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                if _box_distance(merged[i], merged[j]) == 0.0:
                    a, b = merged[i], merged[j]
                    merged[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return merged


def _box_distance(a: BBox, b: BBox) -> float:
    dx = max(b[0] - a[2], 0.0, a[0] - b[2])
    dy = max(b[1] - a[3], 0.0, a[1] - b[3])
    return math.hypot(dx, dy)


def _center(box: BBox) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _nearest_view(point: tuple[float, float], views: list[ViewRegion], reach: float) -> str | None:
    best, best_d = None, reach
    for v in views:
        if v.kind != "orthographic":
            continue
        dx = max(v.bbox[0] - point[0], 0.0, point[0] - v.bbox[2])
        dy = max(v.bbox[1] - point[1], 0.0, point[1] - v.bbox[3])
        d = math.hypot(dx, dy)
        if d < best_d:
            best, best_d = v.id, d
    return best


@dataclass
class Analysis:
    """Full result: the serialisable summary plus the primitives (kept server-side)."""

    raw: RawDrawing
    summary: DrawingAnalysis
    primitives: list[Primitive]


def analyse(pdf_path: Path) -> Analysis:
    raw = extract(pdf_path)
    layout = detect_layout(raw)
    layers = layer_map(raw, layout)
    prims = extract_primitives(raw, layout)
    labels = extract_labels(raw, layout)
    views = detect_views(raw, layout, prims, layers.outline_width)
    for label in labels:
        if label.region == "drawing":
            label.view_id = _nearest_view(_center(label.bbox), views, reach=30.0)
    for p in prims:
        if p.view_id is None and p.region == "drawing":
            p.view_id = _nearest_view(_center(p.bbox()), views, reach=30.0)
    summary = DrawingAnalysis(
        source_file=str(pdf_path),
        source_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        sheet_size=(raw.width_mm, raw.height_mm),
        sheet_format=_sheet_format(raw.width_mm, raw.height_mm),
        drawing_area=layout.area,
        title_block=layout.title_block,
        layer_map=layers,
        views=views,
        labels=labels,
        primitive_count=len(prims),
        circle_count=sum(p.kind == "circle" for p in prims),
    )
    return Analysis(raw=raw, summary=summary, primitives=prims)
