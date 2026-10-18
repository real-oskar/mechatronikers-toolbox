"""Raster path, first stage: text candidates and crops on a scanned or rendered sheet (docs/04).

Deterministic and offline (numpy, scipy, Pillow). Lettering is found as clusters of small
connected ink components, the raster twin of the vector path's glyph clustering, so both paths
report the same label granularity: drawings group glyphs like `analysis.extract_labels` (one
label per glyph cluster), schematics group them into text lines like circuit-mcp's text runs.
A reader (Tesseract, a vision model) then reads the crops. Coordinates are image pixels with the
origin at the top-left corner, x to the right and y down.
"""

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage

MM_PER_INCH = 25.4
MAX_GLYPH_MM = 12.0  # as the vector path: larger shapes are not lettering
MIN_GLYPH_MM = 0.3  # smaller specks are noise or dots that do not change a label's box
MIN_LABEL_MM = 0.6  # a label smaller than this is a speck, not lettering
CLUSTER_GAP = 0.45  # × typical glyph size, as the vector path's label clustering
MIN_CLUSTER_GLYPH_MM = 1.5  # floor of the typical glyph size in the cluster gap (vector path)
LINE_GAP = 1.5  # × glyph height: largest gap between characters and words of one text line
LINE_OVERLAP = 0.5  # share of the smaller glyph two neighbours of one line must overlap
INK_CUT = 0.5  # binarisation cut between the ink and the paper level
LINE_CUT = 0.8  # looser cut that keeps faint thin lines connected (finds line fragments)
BAR_ELONGATION = 4.0  # long axis / short axis of a straight stroke (dash, '1', 'l', '-')
DASH_ANGLE_DEG = 12.0  # strokes this close to the chain direction make a dashed line
SNAP_DEG = 5.0  # baselines this close to upright or vertical count as such
LONE_DASH = 1.6  # × typical glyph: a lone straight stroke this long is a dash

Granularity = Literal["cluster", "line"]
PxBox = tuple[int, int, int, int]  # x0, y0 inclusive; x1, y1 exclusive


@dataclass(frozen=True)
class TextCandidate:
    """A located text item, not read yet."""

    id: str
    bbox: PxBox
    angle: float  # baseline direction in degrees, image frame (y down); 0 = left to right
    glyph_px: float  # typical glyph height in px (scale for crops and readers)
    glyphs: int  # connected components in the cluster
    multiline: bool = False  # cluster spans more than one text line (drawings only)
    parts: tuple[PxBox, ...] = ()  # boxes of the glyph components (crops blank everything else)


@dataclass(frozen=True)
class _Component:
    box: PxBox
    cx: float
    cy: float
    elongation: float  # sqrt of the second-moment eigenvalue ratio; 1 for a dot or a ring
    direction: float  # long-axis angle in degrees, (-90, 90]

    @property
    def w(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def h(self) -> int:
        return self.box[3] - self.box[1]


def load_grey(path: Path) -> tuple[NDArray[np.uint8], float | None]:
    """Greyscale pixels and the resolution stored in the file (None if the header has none)."""
    with Image.open(path) as image:
        info = image.info.get("dpi")
        grey = np.asarray(image.convert("L"), dtype=np.uint8)
    dpi = float(info[0]) if info and info[0] else None
    return grey, dpi


def paper_level(grey: NDArray[np.uint8]) -> int:
    """Grey level of the paper (scans lose contrast, so it is not always 255)."""
    return int(np.percentile(grey, 90))


def otsu_threshold(grey: NDArray[np.uint8]) -> int:
    hist = np.bincount(grey.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    levels = np.arange(256, dtype=np.float64)
    weight = np.cumsum(hist)
    mean = np.cumsum(hist * levels)
    between = np.zeros(256)
    valid = (weight > 0) & (weight < total)
    w0, w1 = weight[valid], total - weight[valid]
    m0 = mean[valid] / w0
    m1 = (mean[-1] - mean[valid]) / w1
    between[valid] = w0 * w1 * (m0 - m1) ** 2
    return int(np.argmax(between))


def ink_levels(grey: NDArray[np.uint8]) -> tuple[float, float]:
    """(ink, paper) grey levels: the median of the pixels Otsu calls dark, and the paper."""
    otsu = otsu_threshold(grey)
    dark = grey[grey <= otsu]
    ink = float(np.median(dark)) if dark.size else float(otsu)
    return ink, float(max(paper_level(grey), ink + 1))


def ink_mask(grey: NDArray[np.uint8], cut: float = INK_CUT) -> NDArray[np.bool_]:
    """Pixels darker than `cut` of the way from the ink to the paper level."""
    ink, paper = ink_levels(grey)
    mask: NDArray[np.bool_] = grey <= ink + cut * (paper - ink)
    return mask


def _components(
    mask: NDArray[np.bool_], px_per_mm: float, wide: NDArray[np.bool_] | None = None
) -> list[_Component]:
    """Glyph-sized components of `mask`. With `wide` (a looser binarisation that keeps faint
    thin lines whole), straight strokes that belong to a long line there are dropped: blur and
    contrast loss break thin lines into dash-like pieces at the normal cut."""
    eight = np.ones((3, 3), dtype=bool)
    labels, count = ndimage.label(mask, structure=eight)
    if count == 0:
        return []
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    in_line = np.zeros(count + 1, dtype=bool)
    if wide is not None:
        wide_labels, wide_count = ndimage.label(wide | mask, structure=eight)
        long = np.zeros(wide_count + 1, dtype=bool)
        limit = MAX_GLYPH_MM * px_per_mm
        for index, sl in enumerate(ndimage.find_objects(wide_labels), start=1):
            if sl is not None:
                long[index] = max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) > limit
        owner = np.zeros(count + 1, dtype=np.int64)
        owner[ids] = wide_labels[ys, xs]
        in_line = long[owner]
    n = np.bincount(ids, minlength=count + 1).astype(np.float64)
    n[n == 0] = 1
    sx = np.bincount(ids, xs, minlength=count + 1) / n
    sy = np.bincount(ids, ys, minlength=count + 1) / n
    xs_f, ys_f = xs.astype(np.float64), ys.astype(np.float64)
    sxx = np.bincount(ids, xs_f * xs_f, minlength=count + 1) / n - sx * sx
    syy = np.bincount(ids, ys_f * ys_f, minlength=count + 1) / n - sy * sy
    sxy = np.bincount(ids, xs_f * ys_f, minlength=count + 1) / n - sx * sy
    largest = MAX_GLYPH_MM * px_per_mm
    smallest = max(2.0, MIN_GLYPH_MM * px_per_mm)
    out: list[_Component] = []
    for index, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        box = (sl[1].start, sl[0].start, sl[1].stop, sl[0].stop)
        size = max(box[2] - box[0], box[3] - box[1])
        if size > largest or size < smallest:
            continue
        # Second moments; +1/12 per axis for the pixel's own extent keeps 1-px strokes finite.
        a, b, c = sxx[index] + 1 / 12, sxy[index], syy[index] + 1 / 12
        spread = math.sqrt(((a - c) / 2) ** 2 + b * b)
        major, minor = (a + c) / 2 + spread, max((a + c) / 2 - spread, 1e-9)
        direction = math.degrees(0.5 * math.atan2(2 * b, a - c))
        if in_line[index] and math.sqrt(major / minor) >= BAR_ELONGATION:
            continue
        out.append(
            _Component(
                box=box,
                cx=float(sx[index]),
                cy=float(sy[index]),
                elongation=math.sqrt(major / minor),
                direction=_fold(direction),
            )
        )
    return out


def _fold(angle: float) -> float:
    """Undirected angle in (-90, 90]."""
    folded = (angle + 90.0) % 180.0 - 90.0
    return 90.0 if folded == -90.0 else folded


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        self.parent[self.find(i)] = self.find(j)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            out.setdefault(self.find(i), []).append(i)
        return list(out.values())


def _near_pairs(boxes: list[PxBox], reach: list[float]) -> list[tuple[int, int]]:
    """Pairs whose boxes, each grown by its reach, overlap (sweep over x)."""
    grown = [(b[0] - r, b[1] - r, b[2] + r, b[3] + r) for b, r in zip(boxes, reach, strict=True)]
    order = sorted(range(len(boxes)), key=lambda i: grown[i][0])
    pairs: list[tuple[int, int]] = []
    active: list[int] = []
    for i in order:
        active = [j for j in active if grown[j][2] >= grown[i][0]]
        gi = grown[i]
        for j in active:
            gj = grown[j]
            if gi[1] <= gj[3] and gj[1] <= gi[3]:
                pairs.append((j, i))
        active.append(i)
    return pairs


def _cluster_groups(comps: list[_Component], px_per_mm: float) -> list[list[int]]:
    """Drawings: the vector path's rule (boxes grown by 0.45 × the typical glyph size)."""
    sizes = sorted(min(c.w, c.h) for c in comps)
    gap = CLUSTER_GAP * max(sizes[len(sizes) // 2], MIN_CLUSTER_GLYPH_MM * px_per_mm)
    uf = _UnionFind(len(comps))
    for i, j in _near_pairs([c.box for c in comps], [gap] * len(comps)):
        uf.union(i, j)
    return uf.groups()


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return min(a1, b1) - max(a0, b0)


def _line_groups(comps: list[_Component], typical: float) -> list[list[int]]:
    """Schematics: text lines, upright or turned by 90°. Neighbours on one baseline are linked
    when the gap is below a word space; every glyph takes the direction most of its links agree
    on, so a turned designator next to an upright value does not fuse with it, and lines stacked
    above each other stay apart."""
    boxes = [c.box for c in comps]
    reach = [LINE_GAP * max(c.h, c.w, typical) for c in comps]
    across: list[tuple[int, int]] = []  # links along an upright baseline
    down: list[tuple[int, int]] = []  # links along a turned baseline
    for i, j in _near_pairs(boxes, reach):
        a, b = boxes[i], boxes[j]
        ha, hb, wa, wb = a[3] - a[1], b[3] - b[1], a[2] - a[0], b[2] - b[0]
        if (
            _overlap(a[1], a[3], b[1], b[3]) >= LINE_OVERLAP * min(ha, hb)
            and max(b[0] - a[2], a[0] - b[2]) <= LINE_GAP * max(ha, hb, typical)
            and _overlap(a[0], a[2], b[0], b[2]) <= 0.5 * min(wa, wb)
        ):
            across.append((i, j))
        elif (
            _overlap(a[0], a[2], b[0], b[2]) >= LINE_OVERLAP * min(wa, wb)
            and max(b[1] - a[3], a[1] - b[3]) <= LINE_GAP * max(wa, wb, typical)
            and _overlap(a[1], a[3], b[1], b[3]) <= 0.5 * min(ha, hb)
        ):
            down.append((i, j))
    votes = np.zeros((len(comps), 2), dtype=int)
    for i, j in across:
        votes[i, 0] += 1
        votes[j, 0] += 1
    for i, j in down:
        votes[i, 1] += 1
        votes[j, 1] += 1
    turned = votes[:, 1] > votes[:, 0]
    uf = _UnionFind(len(comps))
    for i, j in across:
        if not turned[i] and not turned[j]:
            uf.union(i, j)
    for i, j in down:
        if turned[i] and turned[j]:
            uf.union(i, j)
    return uf.groups()


def _direction(members: list[_Component]) -> float:
    """Baseline direction of a cluster from its glyph centres; 0 when it cannot be told."""
    if len(members) < 2:
        return 0.0
    pts = np.array([(c.cx, c.cy) for c in members])
    pts -= pts.mean(axis=0)
    cov = pts.T @ pts / len(members)
    values, vectors = np.linalg.eigh(cov)
    if values[-1] <= 4 * max(values[0], 1e-9):
        return 0.0  # round blob of glyphs (a block of short lines): no clear direction
    vx, vy = vectors[:, -1]
    return _fold(math.degrees(math.atan2(vy, vx)))


def _is_dashed_line(members: list[_Component], direction: float, sheet_glyph: float) -> bool:
    """A chain of straight strokes along the chain's own direction is a dashed or centre line,
    not lettering ('11' has its strokes across the chain). A single stroke clearly longer than
    the sheet's typical glyph is a dash as well."""
    if not all(c.elongation >= BAR_ELONGATION for c in members):
        return False
    if len(members) == 1:
        c = members[0]
        return max(c.w, c.h) > LONE_DASH * sheet_glyph
    return all(abs(_fold(c.direction - direction)) <= DASH_ANGLE_DEG for c in members)


def _typical_glyph(comps: list[_Component]) -> float:
    rounded = [max(c.w, c.h) for c in comps if c.elongation < BAR_ELONGATION]
    return float(np.median(rounded or [max(c.w, c.h) for c in comps]))


def text_candidates(
    grey: NDArray[np.uint8], dpi: float, granularity: Granularity = "cluster"
) -> list[TextCandidate]:
    """Lettering on a sheet image: connected ink components of glyph size, grouped into labels.
    `cluster` mirrors the drawing path (glyphs closer than 0.45 glyph sizes form one label, any
    direction, several lines possible); `line` mirrors schematic text runs (one line each)."""
    px_per_mm = dpi / MM_PER_INCH
    wide = ink_mask(grey, LINE_CUT) if LINE_CUT > INK_CUT else None
    comps = _components(ink_mask(grey), px_per_mm, wide)
    if not comps:
        return []
    sheet_glyph = _typical_glyph(comps)
    if granularity == "cluster":
        groups = _cluster_groups(comps, px_per_mm)
    else:
        groups = _line_groups(comps, sheet_glyph)
    out: list[TextCandidate] = []
    for group in sorted(groups, key=lambda g: min(comps[i].box[1] for i in g)):
        members = [comps[i] for i in group]
        x0 = min(c.box[0] for c in members)
        y0 = min(c.box[1] for c in members)
        x1 = max(c.box[2] for c in members)
        y1 = max(c.box[3] for c in members)
        if max(x1 - x0, y1 - y0) < MIN_LABEL_MM * px_per_mm:
            continue
        direction = _direction(members)
        if granularity == "line":  # text runs are upright or turned by 90°
            direction = 90.0 if len(members) > 1 and (y1 - y0) > (x1 - x0) else 0.0
        elif abs(direction) < SNAP_DEG:
            direction = 0.0  # scan skew: readers cope, no need to turn the crop
        elif abs(direction) > 90.0 - SNAP_DEG:
            direction = 90.0
        across = [
            c.w if abs(direction) >= 45 else c.h
            for c in members
            if c.elongation < BAR_ELONGATION or len(members) == 1
        ]
        glyph_px = float(np.median(across)) if across else sheet_glyph
        if _is_dashed_line(members, direction, sheet_glyph):
            continue
        extent_across = (x1 - x0) if abs(direction) >= 45 else (y1 - y0)
        out.append(
            TextCandidate(
                id=f"c{len(out)}",
                bbox=(x0, y0, x1, y1),
                angle=direction,
                glyph_px=glyph_px,
                glyphs=len(members),
                multiline=granularity == "cluster" and extent_across > 1.8 * glyph_px,
                parts=tuple(c.box for c in members),
            )
        )
    return out


def crop_image(
    grey: NDArray[np.uint8],
    candidate: TextCandidate,
    *,
    glyph_target_px: float = 40.0,
    max_scale: float = 6.0,
    pad_glyphs: float = 0.4,
    rotate_deg: float = 0.0,
    paper: int | None = None,
    isolate: bool = True,
) -> Image.Image:
    """The candidate's region with a margin, scaled so glyphs are about `glyph_target_px` high
    and optionally turned counter-clockwise by `rotate_deg` (to make a baseline horizontal).
    `isolate` blanks ink away from the candidate's glyphs (lines and neighbouring labels in the
    margin); the glyph boxes are grown a little so dots and commas between glyphs stay."""
    paper = paper_level(grey) if paper is None else paper
    pad = max(2, round(pad_glyphs * candidate.glyph_px))
    x0, y0, x1, y1 = candidate.bbox
    ox, oy = x0 - pad, y0 - pad  # canvas origin in the sheet
    height, width = grey.shape
    canvas = np.full((y1 - y0 + 2 * pad, x1 - x0 + 2 * pad), paper, dtype=np.uint8)
    sx0, sy0 = max(0, ox), max(0, oy)
    sx1, sy1 = min(width, x1 + pad), min(height, y1 + pad)
    canvas[sy0 - oy : sy1 - oy, sx0 - ox : sx1 - ox] = grey[sy0:sy1, sx0:sx1]
    if isolate and candidate.parts:
        keep = np.zeros(canvas.shape, dtype=bool)
        grow = max(1, round(0.25 * candidate.glyph_px))
        for px0, py0, px1, py1 in candidate.parts:
            keep[
                max(0, py0 - grow - oy) : max(0, py1 + grow - oy),
                max(0, px0 - grow - ox) : max(0, px1 + grow - ox),
            ] = True
        canvas[~keep] = paper
    image = Image.fromarray(canvas, mode="L")
    scale = min(max_scale, max(0.5, glyph_target_px / max(candidate.glyph_px, 1.0)))
    if abs(scale - 1.0) > 0.05:
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    if rotate_deg:
        image = image.rotate(
            rotate_deg, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=paper
        )
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def upright_rotations(candidate: TextCandidate) -> tuple[float, ...]:
    """Counter-clockwise turns that may make the candidate's text upright, the likelier first.
    The baseline gives the axis, not the reading direction: drawings and schematics write turned
    text to be read from the bottom or the right (ISO 129, EDA tools), so a vertical baseline
    reads bottom to top; the opposite direction is the second guess."""
    if candidate.angle == 0.0:
        return (0.0,)
    preferred = _fold(candidate.angle)
    if preferred == 90.0:
        preferred = -90.0
    return (preferred, preferred + 180.0)
