"""Schematic analysis, vector path (docs/12): wires, junctions, symbols and texts → CircuitSpec.

Deterministic and offline. Connectivity comes from exact geometry (shared endpoints, T-junctions,
junction dots at crossings), component kinds from the printed reference designators, pin roles from
symbol geometry and printed pin names. What stays unsure becomes a review item, never a guess.

Classification without colours or layers (works on black-and-white prints):
- conductors: open 2-point lines in the dominant line width (wires and pin lines);
- symbol graphics: everything else (closed or curved paths, polylines, other widths, fills);
- a symbol body is a cluster of touching graphics; a reference designator names it;
- conductors lying inside a body are symbol detail (e.g. a diode's centre line), and conductors
  leaving a body are its pins: their inner end is the terminal.
"""

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from scipy.optimize import linear_sum_assignment

from mechatronikers.circuit_mcp.extract import RawSchematic, RawText, extract_schematic
from mechatronikers.drawing_mcp.vector import PT_TO_MM, RawDrawing, fit_circle
from mechatronikers.spec.circuit import (
    GROUND_NAMES,
    BBox,
    CircuitSpec,
    Component,
    ComponentKind,
    Net,
    Port,
    SheetStats,
    Terminal,
    TextItem,
    TextRole,
)
from mechatronikers.spec.poi import ReviewItem, SourceRef

TOL = 0.05  # mm; vector coordinates are exact, so points closer than this coincide
BODY_MARGIN = 0.25  # mm around a symbol body when testing what lies inside it
DOT_MAX = 2.0  # mm; larger filled circles are symbol graphics, not junction dots
DOT_REACH = 0.3  # mm between a crossing and its junction dot
CLUSTER_GAP = 0.4  # mm; symbol graphics closer than this belong to one body
REFDES_REACH = 15.0  # mm from a reference designator to its body
VALUE_REACH_REF = 5.0  # mm from a value text to the component's designator...
VALUE_REACH_BODY = 2.5  # ...or to its symbol body
PORT_TEXT_REACH = 2.5  # mm from a power/ground symbol to its name
LABEL_REACH = 1.6  # mm from a net label to its wire
ON_WIRE_REACH = 1.2  # mm; a text this close to a wire and running along it is a net label
END_ON_REACH = 2.5  # mm; a text centred beyond the end of a line names it (power arrows)
FACING_GAP = 3.0  # mm between parallel plates or coils of one symbol (capacitor, transformer)
ABSORB_GAP = 1.0  # mm; nameless graphics this close to a component belong to it (transformer)
NEAR_BODY = 1.5  # mm; texts this close to a symbol are its value or pin names, not net labels
PIN_NUMBER_REACH = 1.8  # mm from a pin number to its pin line
_CELL = 4.0  # mm; spatial index cell size
_FRAME_SPAN = 0.8  # frame lines span at least this share of the sheet (EDA sheets have zone bands)
_WORD_GAP = 0.35  # × font size; EDA exports write each word as its own text object
_TITLE_SIZE = 1.5  # × median font size; larger texts are titles, never net labels
# Marker symbols that look like power symbols but name no net (KiCad's ERC power flag).
MARKER_NAMES = {"PWR_FLAG"}

# Reference designator prefix → kind (IEC 81346 / IEEE 315 practice, as used by EDA tools).
PREFIX_KIND: dict[str, ComponentKind] = {
    "R": "resistor", "RV": "resistor", "VR": "resistor", "RN": "resistor", "POT": "resistor",
    "C": "capacitor", "CP": "capacitor", "VC": "capacitor",
    "L": "inductor", "FB": "inductor",
    "D": "diode", "LED": "diode", "ZD": "diode", "CR": "diode",
    "Q": "bjt", "M": "mosfet",
    "U": "ic", "IC": "ic", "A": "ic", "N": "ic", "X": "ic", "XU": "ic",
    "V": "voltage_source", "BT": "voltage_source", "BAT": "voltage_source",
    "I": "current_source",
    "T": "transformer", "TR": "transformer",
    "J": "connector", "P": "connector", "CN": "connector", "CON": "connector", "TP": "connector",
    "SW": "switch", "S": "switch",
    "K": "other", "F": "other", "Y": "other",
}  # fmt: skip
_REFDES = re.compile(r"^([A-Z]{1,3})([a-z][A-Za-z_]*)?(\d{1,4})([A-Z])?$")
_UNIT = re.compile(r"^(.*\d)([A-Z])$")  # "U1A" → unit A of U1
_PIN_NUMBER = re.compile(r"^\d{1,3}$")
_FOOTPRINT = re.compile(r"^[\w.+-]+:[\w.+-]{3,}$")
_FIELD = re.compile(r"^[A-Za-z][\w.]*:\s")  # "Sim.Pins: 1=2 2=1": a printed property field
_PIN_NAMES = {"+", "-", "~", "V+", "V-", "IN+", "IN-", "+IN", "-IN", "VCC", "VEE", "VS+", "VS-"}
_DIRECTIVE = re.compile(
    r"^\.(ac|dc|tran|op|noise|tf|param|temp|options|step|meas|four|control|endc|include|inc|lib|"
    r"model|subckt|ends|end|save|probe|ic|nodeset|global|func|sens|disto|pz)\b",
    re.I,
)
# Expected terminal counts per kind (None: any count is plausible).
EXPECTED_TERMINALS: dict[ComponentKind, tuple[int, ...] | None] = {
    "resistor": (2, 3), "capacitor": (2,), "inductor": (2,), "diode": (2,),
    "bjt": (3,), "mosfet": (3, 4), "opamp": (3, 5),
    "voltage_source": (2,), "current_source": (2,),
    "transformer": None, "connector": None, "switch": None, "ic": None, "other": None,
}  # fmt: skip
POLARISED: set[ComponentKind] = {
    "diode", "bjt", "mosfet", "opamp", "voltage_source", "current_source"
}  # fmt: skip

Point = tuple[float, float]


# --------------------------------------------------------------------------- geometry


@dataclass
class Seg:
    id: int
    a: Point
    b: Point

    @property
    def bbox(self) -> BBox:
        return (
            min(self.a[0], self.b[0]),
            min(self.a[1], self.b[1]),
            max(self.a[0], self.b[0]),
            max(self.a[1], self.b[1]),
        )

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)


@dataclass
class Shape:
    id: int
    points: list[Point]
    closed: bool
    filled: bool
    bbox: BBox  # including half the stroke width: thick plates reach their pins

    @property
    def is_closed(self) -> bool:
        return self.closed or math.dist(self.points[0], self.points[-1]) <= TOL

    def vertices(self) -> list[Point]:
        """Distinct corner points of a straight-edged shape (closing point dropped)."""
        out: list[Point] = []
        for p in self.points:
            if not out or math.dist(out[-1], p) > TOL:
                out.append(p)
        if len(out) > 1 and math.dist(out[0], out[-1]) <= TOL:
            out.pop()
        return out


@dataclass
class Body:
    id: int
    shapes: list[int]
    bbox: BBox
    refdes_text: int | None = None
    port_text: int | None = None
    directive_box: bool = False
    implicit_ground: bool = False
    internal: list[int] = field(default_factory=list)  # conductor ids drawn inside the body


def _dist_point_seg(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Distance from p to segment ab and the projection parameter t in [0, 1]."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.dist(p, a), 0.0
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.dist(p, (ax + t * dx, ay + t * dy)), t


def _intersection(s: Seg, o: Seg) -> Point | None:
    """Proper crossing of two segments (interior of both), else None."""
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = s.a, s.b, o.a, o.b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    margin_s, margin_o = TOL / max(s.length, TOL), TOL / max(o.length, TOL)
    if margin_s < t < 1 - margin_s and margin_o < u < 1 - margin_o:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _box_distance(a: BBox, b: BBox) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _expand(box: BBox, m: float) -> BBox:
    return (box[0] - m, box[1] - m, box[2] + m, box[3] + m)


def _inside(p: Point, box: BBox) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def _union_box(boxes: Iterable[BBox]) -> BBox:
    bs = list(boxes)
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )


def _center(box: BBox) -> Point:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _seg_box_distance(s: Seg, box: BBox) -> float:
    if _inside(s.a, box) or _inside(s.b, box):
        return 0.0
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    edges = [Seg(-1, corners[i], corners[(i + 1) % 4]) for i in range(4)]
    if any(_intersection(s, e) for e in edges):
        return 0.0
    d = min(_dist_point_seg(c, s.a, s.b)[0] for c in corners)
    return min(d, *(_dist_point_seg(p, e.a, e.b)[0] for p in (s.a, s.b) for e in edges))


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[max(ri, rj)] = min(ri, rj)


class _Grid:
    """Uniform grid over bounding boxes, for neighbour queries."""

    def __init__(self, cell: float = _CELL) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    def _keys(self, box: BBox) -> Iterator[tuple[int, int]]:
        c = self.cell
        for i in range(math.floor(box[0] / c), math.floor(box[2] / c) + 1):
            for j in range(math.floor(box[1] / c), math.floor(box[3] / c) + 1):
                yield i, j

    def add(self, item: int, box: BBox) -> None:
        for key in self._keys(box):
            self.cells[key].append(item)

    def query(self, box: BBox) -> set[int]:
        out: set[int] = set()
        for key in self._keys(box):
            out.update(self.cells.get(key, ()))
        return out


# --------------------------------------------------------------------------- classification


@dataclass
class Layout:
    area: BBox  # inside the frame
    title_block: BBox | None


def detect_layout(raw: RawDrawing) -> Layout:
    """Inner frame = innermost long lines; title block = the box in the bottom-right corner whose
    horizontal lines end at the right frame edge."""
    w, h = raw.width_mm, raw.height_mm
    hs: list[tuple[float, float, float]] = []  # y, x0, x1
    vs: list[tuple[float, float, float]] = []  # x, y0, y1
    for p in raw.paths:
        if not p.stroked:
            continue
        for sub in p.subpaths:
            for a, b in zip(sub.points[:-1], sub.points[1:], strict=True):
                if abs(a[1] - b[1]) < 0.01:
                    hs.append((float(a[1]), float(min(a[0], b[0])), float(max(a[0], b[0]))))
                elif abs(a[0] - b[0]) < 0.01:
                    vs.append((float(a[0]), float(min(a[1], b[1])), float(max(a[1], b[1]))))
    left = max([x for x, y0, y1 in vs if x < w / 2 and y1 - y0 > _FRAME_SPAN * h], default=0.0)
    right = min([x for x, y0, y1 in vs if x > w / 2 and y1 - y0 > _FRAME_SPAN * h], default=w)
    top = max([y for y, x0, x1 in hs if y < h / 2 and x1 - x0 > _FRAME_SPAN * w], default=0.0)
    bottom = min([y for y, x0, x1 in hs if y > h / 2 and x1 - x0 > _FRAME_SPAN * w], default=h)
    edge = max([x1 for y, x0, x1 in hs if abs(y - bottom) < 0.5 and x1 <= right], default=right)
    title = [
        (x0, y)
        for y, x0, x1 in hs
        if abs(x1 - edge) < 0.5 and 0.6 * h < y < bottom - 0.5 and 0.2 * w < x1 - x0 < 0.6 * w
    ]
    block = (min(x for x, _ in title), min(y for _, y in title), edge, bottom) if title else None
    return Layout(area=(left, top, right, bottom), title_block=block)


@dataclass
class _Sheet:
    layout: Layout
    segs: list[Seg]  # conductor candidates
    shapes: list[Shape]
    dots: list[Point]
    texts: list[TextItem]


def _in_area(box: BBox, layout: Layout) -> bool:
    """Strictly inside the frame (frame and zone lines excluded) and outside the title block."""
    if layout.title_block and _box_distance(box, layout.title_block) == 0:
        return False
    inner = _expand(layout.area, -0.5)
    return _inside(box[:2], inner) and _inside(box[2:], inner)


def _classify(raw: RawSchematic) -> _Sheet:
    layout = detect_layout(raw.drawing)
    lines: list[tuple[float, Point, Point]] = []
    shapes: list[Shape] = []
    dots: list[Point] = []
    for path in raw.drawing.paths:
        if path.dashed:
            continue  # group frames and other decorations, never wires
        for sub in path.subpaths:
            pts = [(float(x), float(y)) for x, y in sub.points]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            box = (min(xs), min(ys), max(xs), max(ys))
            size = max(box[2] - box[0], box[3] - box[1])
            if size <= TOL or not _in_area(box, layout):
                continue  # zero-length strokes (pin ends) carry no geometry
            closed = sub.closed or math.dist(pts[0], pts[-1]) <= TOL
            if path.filled and closed and sub.curved and size <= DOT_MAX:
                cx, cy, _, residual = fit_circle(sub.points)
                if residual < 0.1:
                    dots.append((cx, cy))
                    continue
            open_stroke = path.stroked and not path.filled and not sub.curved and not sub.closed
            if open_stroke and len(pts) == 2:
                lines.append((path.width, pts[0], pts[1]))
                continue
            half = path.width * PT_TO_MM / 2 if path.stroked else 0.0
            shapes.append(Shape(len(shapes), pts, sub.closed, path.filled, _expand(box, half)))
    # Wires and pin lines share the dominant width; other 2-point lines (capacitor plates,
    # thick bars) are symbol graphics.
    widths = Counter(round(w, 2) for w, _, _ in lines)
    wire_width = widths.most_common(1)[0][0] if widths else 0.0
    segs: list[Seg] = []
    for w, a, b in lines:
        if abs(round(w, 2) - wire_width) <= 0.02:
            segs.append(Seg(len(segs), a, b))
        else:
            line_box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            shapes.append(
                Shape(len(shapes), [a, b], False, False, _expand(line_box, w * PT_TO_MM / 2))
            )
    texts: list[TextItem] = []
    for raw_text in merge_words(raw.texts):
        role: TextRole = "other"
        if layout.title_block and _inside(_center(raw_text.bbox), layout.title_block):
            role = "title_block"
        elif not _in_area(raw_text.bbox, layout):
            continue
        texts.append(_text_item(len(texts), raw_text, role))
    return _Sheet(layout, segs, shapes, dots, texts)


def merge_words(texts: list[RawText]) -> list[RawText]:
    """Join words written as separate text objects into runs (same line, size and angle, gap
    below a space width). Separate fields (designator, value) keep a wider gap."""
    runs: list[RawText] = []
    for t in sorted(texts, key=lambda t: t.bbox[0]):
        for i, run in enumerate(runs):
            if run.angle != 0 or t.angle != 0 or abs(run.size - t.size) > 0.01:
                continue
            overlap = min(run.bbox[3], t.bbox[3]) - max(run.bbox[1], t.bbox[1])
            same_line = overlap > 0.6 * min(run.bbox[3] - run.bbox[1], t.bbox[3] - t.bbox[1])
            if same_line and -0.3 * t.size < t.bbox[0] - run.bbox[2] < _WORD_GAP * t.size:
                runs[i] = RawText(
                    text=f"{run.text} {t.text}",
                    bbox=_union_box([run.bbox, t.bbox]),
                    angle=0.0,
                    size=t.size,
                    unmapped=run.unmapped or t.unmapped,
                )
                break
        else:
            runs.append(t)
    return runs


def _text_item(i: int, t: RawText, role: TextRole) -> TextItem:
    return TextItem(
        id=f"t{i + 1}",
        text=t.text,
        bbox=t.bbox,
        angle=t.angle,
        size=t.size,
        role=role,
        unmapped_glyphs=t.unmapped,
    )


def _cluster_bodies(shapes: list[Shape]) -> list[Body]:
    grid = _Grid()
    for s in shapes:
        grid.add(s.id, _expand(s.bbox, CLUSTER_GAP))
    uf = _UnionFind(len(shapes))
    for s in shapes:
        for o in grid.query(_expand(s.bbox, CLUSTER_GAP)):
            if o > s.id and _box_distance(s.bbox, shapes[o].bbox) <= CLUSTER_GAP:
                uf.union(s.id, o)
    groups: dict[int, list[int]] = defaultdict(list)
    for s in shapes:
        groups[uf.find(s.id)].append(s.id)
    bodies = [Body(i, ids, _union_box(shapes[k].bbox for k in ids))
              for i, ids in enumerate(groups.values())]  # fmt: skip
    # Graphics inside another body (e.g. "+" marks in an op-amp) belong to it.
    changed = True
    while changed:
        changed = False
        bodies.sort(key=lambda b: -(b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]))
        for big in bodies:
            for small in bodies:
                if small is big or not small.shapes:
                    continue
                if _inside(small.bbox[:2], _expand(big.bbox, TOL)) and _inside(
                    small.bbox[2:], _expand(big.bbox, TOL)
                ):
                    big.shapes += small.shapes
                    small.shapes = []
                    changed = True
        bodies = [b for b in bodies if b.shapes]
    bodies = _merge_facing(bodies)
    for i, b in enumerate(sorted(bodies, key=lambda b: (b.bbox[1], b.bbox[0]))):
        b.id = i
    return sorted(bodies, key=lambda b: b.id)


def _thin_axis(box: BBox) -> int | None:
    """0 if the box is a thin vertical bar, 1 if a thin horizontal bar, else None."""
    w, h = box[2] - box[0], box[3] - box[1]
    if h >= 4 * max(w, 1e-6):
        return 0
    if w >= 4 * max(h, 1e-6):
        return 1
    return None


def _merge_facing(bodies: list[Body]) -> list[Body]:
    """Join parallel plates facing each other across a small gap (capacitors, batteries)."""
    merged = True
    while merged:
        merged = False
        for a in bodies:
            axis = _thin_axis(a.bbox)
            if axis is None or not a.shapes:
                continue
            for b in bodies:
                if b is a or not b.shapes or _thin_axis(b.bbox) != axis:
                    continue
                if axis == 1:  # horizontal plates overlap along x
                    overlap = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
                    shorter = min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
                else:
                    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
                    shorter = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
                if overlap >= 0.6 * shorter and 0 < _box_distance(a.bbox, b.bbox) <= FACING_GAP:
                    a.shapes += b.shapes
                    a.bbox = _union_box([a.bbox, b.bbox])
                    b.shapes = []
                    merged = True
        bodies = [b for b in bodies if b.shapes]
    return bodies


# --------------------------------------------------------------------------- text roles


def base_ref(refdes: str) -> str:
    """The package designator of a unit: "U1A" → "U1" (KiCad netlists name packages)."""
    m = _UNIT.match(refdes)
    return m.group(1) if m else refdes


def parse_refdes(text: str) -> tuple[str, ComponentKind, str | None] | None:
    """('R', 'resistor', unit) for a plausible reference designator, else None."""
    m = _REFDES.match(text)
    if not m or m.group(1) not in PREFIX_KIND:
        return None
    return m.group(1), PREFIX_KIND[m.group(1)], m.group(4)


def _initial_roles(texts: list[TextItem]) -> None:
    for t in texts:
        if t.role == "title_block":
            continue
        if t.text.startswith(("*", ";")):
            t.owner = "comment"  # commented-out SPICE directive
        elif _DIRECTIVE.match(t.text):
            t.role = "directive"
        elif _FOOTPRINT.match(t.text):
            t.role = "footprint"
        elif _FIELD.match(t.text):
            t.role = "param"
        elif parse_refdes(t.text):
            t.role = "refdes"
        elif _PIN_NUMBER.match(t.text):
            t.role = "pin_number"
        elif t.text.upper() in _PIN_NAMES:
            t.role = "pin_name"
        elif "=" in t.text:
            t.role = "param"


# --------------------------------------------------------------------------- analysis


@dataclass
class _Work:
    sheet: _Sheet
    bodies: list[Body]
    seg_grid: _Grid
    conductors: set[int] = field(default_factory=set)
    review: list[ReviewItem] = field(default_factory=list)

    def item(
        self,
        kind: str,
        message: str,
        reason: str,
        severity: str = "warning",
        refs: list[str] | None = None,
        bbox: BBox | None = None,
    ) -> None:
        self.review.append(
            ReviewItem.model_validate(
                {
                    "id": f"ri-{len(self.review) + 1}",
                    "kind": kind,
                    "severity": severity,
                    "message": message,
                    "reason": reason,
                    "feature_ids": refs or [],
                    "sheet_ref": SourceRef(bbox=bbox) if bbox else None,
                }
            )
        )


def _assign_refdes(w: _Work) -> None:
    texts = w.sheet.texts
    for b in w.bodies:
        # Several designator-like texts inside one symbol are its pin names (e.g. "F1", "F2").
        inside = [t for t in texts
                  if t.role == "refdes" and _box_distance(t.bbox, b.bbox) <= 1.0]  # fmt: skip
        if len(inside) >= 2:
            for t in inside:
                t.role = "pin_name"
    # Optimal one-to-one matching: a designator closer to a neighbour's symbol than to its own
    # must not take the neighbour (greedy matching would).
    cands = [i for i, t in enumerate(texts) if t.role == "refdes"]
    taken_t: set[int] = set()
    if cands and w.bodies:
        # Squared distances: a far match is only taken when it completes the assignment.
        cost = [[_box_distance(texts[i].bbox, b.bbox) for b in w.bodies] for i in cands]
        unmatched = 4 * REFDES_REACH**2
        capped = [[d * d if d <= REFDES_REACH else unmatched for d in row] for row in cost]
        for r, c in zip(*linear_sum_assignment(capped), strict=True):
            if capped[r][c] < unmatched:
                w.bodies[c].refdes_text = cands[r]
                taken_t.add(cands[r])
    for t in texts:
        if t.role == "refdes" and int(t.id[1:]) - 1 not in taken_t:
            # A designator-like text without a symbol is most likely a net label (e.g. "D0").
            t.role = "other"


def _assign_ports(w: _Work) -> None:
    texts = w.sheet.texts
    for b in w.bodies:
        inside = [t for t in texts if _inside(_center(t.bbox), b.bbox)]
        if b.refdes_text is None and any(t.role == "directive" for t in inside):
            b.directive_box = True  # frame around a block of SPICE directives
            for t in inside:
                t.role, t.owner = "directive", t.owner or "directives"
    pairs = []
    for b in w.bodies:
        if b.refdes_text is not None or b.directive_box:
            continue
        bx, by = _center(b.bbox)
        for t in texts:
            if t.role != "other" or t.owner is not None:
                continue
            d = _box_distance(t.bbox, b.bbox)
            if d <= PORT_TEXT_REACH:
                tx, ty = _center(t.bbox)
                # Power symbol names are centred on their symbol.
                offset = abs(tx - bx) if abs(ty - by) > abs(tx - bx) else abs(ty - by)
                pairs.append((d + 0.5 * offset, int(t.id[1:]) - 1, b.id))
    for _, ti, bi in sorted(pairs):
        body, t = w.bodies[bi], texts[ti]
        if body.port_text is not None or t.owner is not None:
            continue
        body.port_text = ti
        t.role, t.owner = "net_name", "port"


def _absorb(w: _Work) -> None:
    """Merge nameless graphics into an adjacent component body (a transformer's second coil and
    core lines). Ground symbols keep their distance: their pin line separates them."""
    changed = True
    while changed:
        changed = False
        for b in w.bodies:
            if b.refdes_text is not None or b.port_text is not None or b.directive_box:
                continue
            for c in w.bodies:
                if c.refdes_text is not None and _box_distance(b.bbox, c.bbox) <= ABSORB_GAP:
                    c.shapes += b.shapes
                    c.bbox = _union_box([c.bbox, b.bbox])
                    b.shapes = []
                    changed = True
                    break
        w.bodies = [b for b in w.bodies if b.shapes]
    for i, b in enumerate(w.bodies):
        b.id = i


def _attach(w: _Work) -> dict[int, list[tuple[int, Point, Point]]]:
    """Split conductors into body detail, pins and wires. Returns per body its pins as
    (segment id, inner point, outer point)."""
    segs = w.sheet.segs
    pins: dict[int, list[tuple[int, Point, Point]]] = defaultdict(list)
    internal: set[int] = set()
    for b in w.bodies:
        if b.refdes_text is None and b.port_text is None:
            continue
        box = _expand(b.bbox, BODY_MARGIN)
        for si in w.seg_grid.query(box):
            s = segs[si]
            ia, ib = _inside(s.a, box), _inside(s.b, box)
            if ia and ib:
                internal.add(si)
                b.internal.append(si)
            elif ia or ib:
                inner, outer = (s.a, s.b) if ia else (s.b, s.a)
                pins[b.id].append((si, inner, outer))
    w.conductors = {s.id for s in segs} - internal
    for bid in pins:
        pins[bid] = [p for p in pins[bid] if p[0] in w.conductors]
    return pins


def _connect(w: _Work) -> tuple[_UnionFind, SheetStats]:
    segs = w.sheet.segs
    uf = _UnionFind(len(segs))
    stats = SheetStats(wires=len(w.conductors), junctions=len(w.sheet.dots))
    dot_grid = _Grid(1.0)
    for i, (x, y) in enumerate(w.sheet.dots):
        dot_grid.add(i, (x, y, x, y))

    def has_dot(p: Point) -> bool:
        cands = dot_grid.query(_expand((p[0], p[1], p[0], p[1]), DOT_REACH))
        return any(math.dist(p, w.sheet.dots[i]) <= DOT_REACH for i in cands)

    seen_pairs: set[tuple[int, int]] = set()
    for si in sorted(w.conductors):
        s = segs[si]
        for oi in w.seg_grid.query(_expand(s.bbox, TOL)):
            if oi <= si or oi not in w.conductors or (si, oi) in seen_pairs:
                continue
            seen_pairs.add((si, oi))
            o = segs[oi]
            if any(math.dist(p, q) <= TOL for p in (s.a, s.b) for q in (o.a, o.b)):
                uf.union(si, oi)
                continue
            touched = False
            for p, other in ((s.a, o), (s.b, o), (o.a, s), (o.b, s)):
                d, t = _dist_point_seg(p, other.a, other.b)
                if d <= TOL and 0 < t < 1:
                    uf.union(si, oi)
                    touched = True
                    if not has_dot(p):
                        stats.t_junctions_without_dot += 1
                    break
            if touched:
                continue
            crossing = _intersection(s, o)
            if crossing is not None:
                if has_dot(crossing):
                    uf.union(si, oi)
                else:
                    stats.crossings_without_junction += 1
    return uf, stats


def _apex(tri: list[Point], axis: tuple[Point, Point] | None = None) -> Point:
    """Tip of a triangle: the vertex on the axis if one is given, else where the two most similar
    edges meet (isosceles apex)."""
    if axis is not None:
        a, b = axis
        return min(tri, key=lambda p: _line_distance(p, a, b))
    best, best_diff = tri[0], math.inf
    for i, p in enumerate(tri):
        e1, e2 = math.dist(p, tri[i - 1]), math.dist(p, tri[(i + 1) % 3])
        diff = abs(e1 - e2) / max(e1, e2)
        if diff < best_diff:
            best, best_diff = p, diff
    return best


def _line_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n == 0:
        return math.dist(p, a)
    return abs(dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / n


def _triangles(w: _Work, body: Body, chevrons: bool = False) -> list[list[Point]]:
    """Closed triangles of a body; with `chevrons`, also open arrowheads drawn as a 3-point
    polyline (returned with the tip in the middle)."""
    out = []
    for si in body.shapes:
        shape = w.sheet.shapes[si]
        v = shape.vertices()
        if len(v) == 3 and (shape.is_closed or shape.filled or chevrons):
            out.append(v)
    return out


def _texts_in(w: _Work, box: BBox, roles: set[str]) -> list[TextItem]:
    return [t for t in w.sheet.texts if t.role in roles and _inside(_center(t.bbox), box)]


def _nearest(terms: list[Terminal], p: Point) -> Terminal:
    return min(terms, key=lambda t: math.dist(t.at, p))


def _assign_roles(w: _Work, comp: Component, body: Body) -> str | None:
    """Set terminal roles from geometry and pin names. Returns a reason if unsure."""
    terms = comp.terminals
    names = _texts_in(w, _expand(body.bbox, 1.0), {"pin_name"})
    tris = _triangles(w, body)
    if comp.kind == "diode" and len(terms) == 2:
        if not tris:
            return "no triangle found in the diode symbol"
        axis = (terms[0].at, terms[1].at)
        cathode = _nearest(terms, _apex(max(tris, key=_tri_area), axis))
        cathode.role = "K"
        next(t for t in terms if t is not cathode).role = "A"
        return None
    if comp.kind in ("voltage_source", "current_source") and len(terms) == 2:
        plus = [t for t in names if t.text == "+"]
        if plus:
            pos = _nearest(terms, _center(plus[0].bbox))
        elif comp.kind == "current_source" and (heads := _triangles(w, body, chevrons=True)):
            # Current flows from n+ through the source to n-: the arrow points at n-.
            neg = _nearest(terms, _apex(max(heads, key=_tri_area), (terms[0].at, terms[1].at)))
            pos = next(t for t in terms if t is not neg)
        else:
            return "no '+' mark or arrow found in the source symbol"
        pos.role = "+"
        next(t for t in terms if t is not pos).role = "-"
        return None
    if comp.kind in ("bjt", "mosfet") and len(terms) == 3:
        return _transistor_roles(comp, terms, tris, body)
    if comp.kind == "opamp":
        return _opamp_roles(terms, tris, names)
    return None


def _tri_area(t: list[Point]) -> float:
    (x1, y1), (x2, y2), (x3, y3) = t
    return abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2


def _transistor_roles(
    comp: Component, terms: list[Terminal], tris: list[list[Point]], body: Body
) -> str | None:
    # The control pin (base, gate) is perpendicular to the two load pins, which point apart.
    pairs = [(0, 1, 2), (0, 2, 1), (1, 2, 0)]
    i, j, k = min(
        pairs,
        key=lambda p: (
            terms[p[0]].direction[0] * terms[p[1]].direction[0]
            + terms[p[0]].direction[1] * terms[p[1]].direction[1]
        ),
    )
    ctrl, loads = terms[k], (terms[i], terms[j])
    if not tris:
        return "no arrow found in the transistor symbol"
    arrow = min(tris, key=_tri_area)
    centroid = (sum(p[0] for p in arrow) / 3, sum(p[1] for p in arrow) / 3)
    tip = _apex(arrow)
    marked = min(loads, key=lambda t: math.dist(t.at, centroid))  # emitter / source
    other = loads[0] if marked is loads[1] else loads[1]
    centre = _center(body.bbox)
    outward = math.dist(tip, centre) > math.dist(centroid, centre)
    if comp.kind == "bjt":
        ctrl.role, marked.role, other.role = "B", "E", "C"
        comp.polarity = "npn" if outward else "pnp"
    else:
        ctrl.role, marked.role, other.role = "G", "S", "D"
        comp.polarity = "p" if outward else "n"
    return None


def _opamp_roles(
    terms: list[Terminal], tris: list[list[Point]], names: list[TextItem]
) -> str | None:
    if not tris or len(terms) not in (3, 5):
        return "op-amp triangle or terminal count not recognised"
    tri = max(tris, key=_tri_area)
    apex = _apex(tri)
    base = [p for p in tri if p is not apex]
    out = _nearest(terms, apex)
    out.role = "out"
    rest = [t for t in terms if t is not out]
    by_base = sorted(rest, key=lambda t: _line_distance(t.at, base[0], base[1]))
    inputs, supplies = by_base[:2], by_base[2:]
    plus = [t for t in names if t.text in ("+", "+IN", "IN+")]
    minus = [t for t in names if t.text in ("-", "-IN", "IN-")]
    if plus or minus:
        ref = plus[0] if plus else minus[0]
        first = _nearest(inputs, _center(ref.bbox))
        first.role = "in+" if plus else "in-"
        next(t for t in inputs if t is not first).role = "in-" if plus else "in+"
    else:
        return "op-amp '+'/'-' inputs not marked"
    if supplies:
        vp = [t for t in names if t.text in ("V+", "VCC", "VS+")]
        vn = [t for t in names if t.text in ("V-", "VEE", "VS-")]
        if vp or vn:
            ref = vp[0] if vp else vn[0]
            first = _nearest(supplies, _center(ref.bbox))
            first.role = "v+" if vp else "v-"
            next(t for t in supplies if t is not first).role = "v-" if vp else "v+"
        else:
            # Convention: positive supply on the left of the signal direction.
            dx, dy = (
                apex[0] - (base[0][0] + base[1][0]) / 2,
                apex[1] - (base[0][1] + base[1][1]) / 2,
            )
            left = max(supplies, key=lambda t: dy * t.at[0] - dx * t.at[1])
            left.role = "v+"
            next(t for t in supplies if t is not left).role = "v-"
            return "op-amp supply pins assigned by convention (V+ on top)"
    return None


def analyse_schematic(pdf_path: Path) -> CircuitSpec:
    raw = extract_schematic(pdf_path)
    return analyse_raw(raw, pdf_path)


def analyse_raw(raw: RawSchematic, pdf_path: Path) -> CircuitSpec:
    sheet = _classify(raw)
    _initial_roles(sheet.texts)
    seg_grid = _Grid()
    for s in sheet.segs:
        seg_grid.add(s.id, _expand(s.bbox, TOL))
    w = _Work(sheet, _cluster_bodies(sheet.shapes), seg_grid)
    _assign_refdes(w)
    _assign_ports(w)
    _absorb(w)
    pins = _attach(w)
    uf, stats = _connect(w)
    stats.texts = len(sheet.texts)
    texts = sheet.texts

    # Components with terminals.
    components: list[Component] = []
    body_of: dict[str, Body] = {}
    for b in w.bodies:
        if b.refdes_text is None:
            continue
        t = texts[b.refdes_text]
        prefix, kind, _ = parse_refdes(t.text) or ("", "other", None)
        t.owner = t.text
        terms: list[Terminal] = []
        for si, inner, outer in pins.get(b.id, []):
            if any(math.dist(inner, x.at) <= TOL for x in terms):
                continue
            length = math.dist(inner, outer)
            direction = ((outer[0] - inner[0]) / length, (outer[1] - inner[1]) / length)
            terms.append(Terminal(id="", at=inner, direction=direction, net_id=str(uf.find(si))))
        if kind == "ic" and _triangles(w, b) and len(terms) in (3, 5):
            kind = "opamp"
        elif kind == "diode" and len(terms) == 4:
            kind = "ic"  # bridge rectifier: a four-pin package
        comp = Component(refdes=t.text, kind=kind, bbox=b.bbox, terminals=terms, text_ids=[t.id])
        components.append(comp)
        body_of[comp.refdes] = b

    _assign_pin_numbers(w, components, pins, body_of)
    # Ports (power/ground symbols) and net labels name the nets. Texts lying on a wire are labels
    # even when a component is near; then values; then the remaining texts close to a wire.
    names: dict[int, set[str]] = defaultdict(set)
    ports: list[Port] = []
    component_boxes = [body_of[c.refdes].bbox for c in components]
    for t in texts:
        hugging = any(_box_distance(t.bbox, box) <= NEAR_BODY for box in component_boxes)
        if t.role == "other" and t.owner is None and not t.text.isdigit() and not hugging:
            _label(w, uf, t, names, ports, on_wire=True)
    _assign_values(w, components, body_of)
    for b in w.bodies:
        if b.refdes_text is None and b.port_text is None and not b.directive_box:
            if (root := _unnamed_ground(w, b, uf)) is not None:
                names[root].add("GND")
                ports.append(
                    Port(
                        id=f"port-{b.id}", kind="ground", name="GND", bbox=b.bbox, net_id=str(root)
                    )
                )
                b.implicit_ground = True
                w.item(
                    "assumption",
                    "Ground symbol without a name read as GND",
                    "the symbol's shape is a ground triangle; its name is hidden",
                    "info",
                    bbox=b.bbox,
                )
            continue
        if b.port_text is None:
            continue
        name = texts[b.port_text].text
        roots = {uf.find(si) for si, _, _ in pins.get(b.id, [])}
        if name.upper() in MARKER_NAMES:
            continue  # an ERC marker on a net, not a name for it
        is_ground = name.upper() in GROUND_NAMES or "GND" in name.upper()
        port = Port(
            id=f"port-{b.id}",
            kind="ground" if is_ground else "power",
            name=name,
            bbox=b.bbox,
            net_id=str(next(iter(roots))) if roots else None,
        )
        ports.append(port)
        for r in roots:
            names[r].add(name)
    sizes = sorted(t.size for t in texts if t.role != "title_block")
    title_size = _TITLE_SIZE * sizes[len(sizes) // 2] if sizes else math.inf
    for t in texts:
        # Bare numbers near a wire are far more often stray pin numbers or values than labels.
        if t.role == "other" and t.owner is None and t.size < title_size and not t.text.isdigit():
            _label(w, uf, t, names, ports, on_wire=False)

    # Same name, same net (connection by name).
    root_by_name: dict[str, int] = {}
    for root, ns in names.items():
        for n in ns:
            if n in root_by_name:
                uf.union(root, root_by_name[n])
            root_by_name.setdefault(n, root)

    nets = _build_nets(w, uf, components, ports, names)
    directives = [t.text for t in texts if t.role == "directive" and _DIRECTIVE.match(t.text)]
    _review(w, components, body_of, stats)

    return CircuitSpec(
        source_file=pdf_path.name,
        source_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        sheet_size=(raw.drawing.width_mm, raw.drawing.height_mm),
        drawing_area=sheet.layout.area,
        title_block=sheet.layout.title_block,
        components=sorted(components, key=lambda c: _refdes_key(c.refdes)),
        nets=nets,
        ports=ports,
        texts=texts,
        directives=directives,
        review_items=w.review,
        stats=stats,
    )


def _in_outline(w: _Work, body: Body, p: Point) -> bool:
    """Point inside one of the body's closed outlines (ray casting)."""
    for k in body.shapes:
        shape = w.sheet.shapes[k]
        if not shape.is_closed or len(shape.points) < 4:
            continue
        pts, inside = shape.points, False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
            if (y1 > p[1]) != (y2 > p[1]) and p[0] < x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        if inside:
            return True
    return False


def _beside(t: TextItem, s: Seg, inner: Point) -> bool:
    """The text sits next to a pin line (or just past its outer end), not next to the symbol
    body the pin leaves from."""
    outer = s.b if math.dist(s.a, inner) <= TOL else s.a
    dx, dy = outer[0] - inner[0], outer[1] - inner[1]
    length2 = dx * dx + dy * dy
    cx, cy = _center(t.bbox)
    along = ((cx - inner[0]) * dx + (cy - inner[1]) * dy) / math.sqrt(length2)
    reach = 1.6 * math.sqrt(length2) + 1.0  # short pins carry their number past the end
    return 0.0 < along < reach and _line_distance((cx, cy), inner, outer) <= PIN_NUMBER_REACH


def _unnamed_ground(w: _Work, b: Body, uf: _UnionFind) -> int | None:
    """Net root if the body is a ground symbol without a name: one wire attached on the symbol's
    axis, and away from it either a triangle with a base line or a stack of bars."""
    box = _expand(b.bbox, BODY_MARGIN)
    attached = []
    for si in w.seg_grid.query(box):
        if si not in w.conductors:
            continue
        s = w.sheet.segs[si]
        ia, ib = _inside(s.a, box), _inside(s.b, box)
        if ia != ib:
            attached.append((si, s.a if ia else s.b, s.b if ia else s.a))
    if len(attached) != 1 or max(b.bbox[2] - b.bbox[0], b.bbox[3] - b.bbox[1]) > 6.0:
        return None
    si, at, outer = attached[0]
    length = math.dist(at, outer)
    axis = ((at[0] - outer[0]) / length, (at[1] - outer[1]) / length)  # into the symbol
    edges = [
        (pts[i], pts[i + 1])
        for k in b.shapes
        for pts in [w.sheet.shapes[k].points]
        for i in range(len(pts) - 1)
        if math.dist(pts[i], pts[i + 1]) > TOL
    ] + [(w.sheet.segs[k].a, w.sheet.segs[k].b) for k in b.internal]

    def along(p: Point) -> float:
        return (p[0] - at[0]) * axis[0] + (p[1] - at[1]) * axis[1]

    def across(p: Point) -> float:
        return (p[0] - at[0]) * -axis[1] + (p[1] - at[1]) * axis[0]

    points = [p for e in edges for p in e]
    if not points or abs(sum(across(p) for p in points)) / len(points) > 0.2:
        return None  # not symmetric about the wire
    bars = [e for e in edges if abs(along(e[0]) - along(e[1])) < TOL and math.dist(*e) > 0.5]
    far = max(along(p) for p in points)
    tip = [p for p in points if abs(along(p) - far) < TOL]
    if all(abs(across(p)) < 0.2 for p in tip):
        # A triangle: the tip on the axis plus a bar across the axis behind it (its base).
        is_ground = any(along(e[0]) < far - 0.3 for e in bars)
    else:
        is_ground = len({round(along(e[0]), 2) for e in bars}) >= 2  # earth: stacked bars
    return uf.find(si) if is_ground else None


def _along(t: TextItem, s: Seg) -> bool:
    """The text runs along the segment and overlaps most of its own length with it."""
    horizontal = abs(s.a[1] - s.b[1]) < TOL
    vertical = abs(s.a[0] - s.b[0]) < TOL
    if horizontal and t.angle in (0.0, 180.0):
        lo, hi = sorted((s.a[0], s.b[0]))
        overlap = min(hi, t.bbox[2]) - max(lo, t.bbox[0])
        return overlap >= 0.8 * (t.bbox[2] - t.bbox[0])
    if vertical and t.angle in (90.0, 270.0):
        lo, hi = sorted((s.a[1], s.b[1]))
        overlap = min(hi, t.bbox[3]) - max(lo, t.bbox[1])
        return overlap >= 0.8 * (t.bbox[3] - t.bbox[1])
    return False


def _end_on(t: TextItem, s: Seg) -> bool:
    """The text is centred on the line's extension, just beyond one of its ends."""
    c = _center(t.bbox)
    for end, start in ((s.a, s.b), (s.b, s.a)):
        dx, dy = end[0] - start[0], end[1] - start[1]
        n = math.hypot(dx, dy)
        ahead = ((c[0] - end[0]) * dx + (c[1] - end[1]) * dy) / n
        near = _box_distance(t.bbox, (end[0], end[1], end[0], end[1])) <= END_ON_REACH
        if ahead > 0 and near and _line_distance(c, start, end) < 0.6:
            return True
    return False


def _label(
    w: _Work,
    uf: _UnionFind,
    t: TextItem,
    names: dict[int, set[str]],
    ports: list[Port],
    on_wire: bool,
) -> None:
    reach = ON_WIRE_REACH if on_wire else LABEL_REACH
    near = [
        (_seg_box_distance(w.sheet.segs[si], t.bbox), si)
        for si in w.seg_grid.query(_expand(t.bbox, max(reach, END_ON_REACH)))
        if si in w.conductors and (not on_wire or _along(t, w.sheet.segs[si]))
    ]
    near = [n for n in near if n[0] <= reach or (not on_wire and _end_on(t, w.sheet.segs[n[1]]))]
    if not near:
        return
    root = uf.find(min(near)[1])
    t.role, t.owner = "net_name", f"label:{root}"
    names[root].add(t.text)
    ports.append(Port(id=f"label-{t.id}", kind="label", name=t.text, bbox=t.bbox, net_id=str(root)))


def _refdes_key(refdes: str) -> tuple[str, int, str]:
    m = re.match(r"^([A-Za-z_]+?)(\d+)(.*)$", refdes)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (refdes, 0, "")


def _assign_pin_numbers(
    w: _Work,
    components: list[Component],
    pins: dict[int, list[tuple[int, Point, Point]]],
    body_of: dict[str, Body],
) -> None:
    candidates = []
    for comp in components:
        body = body_of[comp.refdes]
        for term in comp.terminals:
            pin_segs = [w.sheet.segs[si] for si, inner, _ in pins[body.id]
                        if math.dist(inner, term.at) <= TOL]  # fmt: skip
            for t in w.sheet.texts:
                if t.role != "pin_number" or t.owner is not None:
                    continue
                for s in pin_segs:
                    if _beside(t, s, term.at):
                        d = _seg_box_distance(s, t.bbox)
                        candidates.append((d, t.id, comp.refdes, id(term), term, t))
    used_t: set[str] = set()
    used_term: set[int] = set()
    for _, tid, refdes, term_key, term, t in sorted(candidates, key=lambda c: c[0]):
        if tid in used_t or term_key in used_term:
            continue
        term.pin = t.text
        t.owner = refdes
        used_t.add(tid)
        used_term.add(term_key)
    for t in w.sheet.texts:
        if t.role == "pin_number" and t.owner is None:
            t.role = "other"  # a number next to no pin is a value or a label, e.g. "10", "470"
    # Short texts at a pin and touching the symbol are pin names (tube pins "A", "G", "K" …).
    # Two-pin parts print no pin names; a short text there is their value.
    for comp in components:
        if len(comp.terminals) < 3:
            continue
        body = body_of[comp.refdes]
        for term in comp.terminals:
            pin_segs = [w.sheet.segs[si] for si, inner, _ in pins[body.id]
                        if math.dist(inner, term.at) <= TOL]  # fmt: skip
            for t in w.sheet.texts:
                if (
                    t.owner is None
                    and t.role in ("other", "pin_name")
                    and len(t.text) <= 4
                    and _box_distance(t.bbox, body.bbox) <= 1.0
                    and any(_beside(t, s, term.at) for s in pin_segs)
                    and term.name is None
                ):
                    term.name, t.role, t.owner = t.text, "pin_name", comp.refdes
    # Texts inside an IC's body (pin names "VI", "ADJ") belong to it. Two-pin parts often carry
    # their value inside the body instead.
    for comp in components:
        if len(comp.terminals) < 3:
            continue
        body = body_of[comp.refdes]
        for t in w.sheet.texts:
            free = t.owner is None and t.role in ("other", "pin_name") and not t.text.isdigit()
            if free and _in_outline(w, body, _center(t.bbox)):
                t.role, t.owner = "pin_name", comp.refdes
                term = _nearest(comp.terminals, _center(t.bbox))
                term.name = term.name or t.text
    for comp in components:
        # Stable order: by printed pin number, else top-left first.
        comp.terminals.sort(
            key=lambda x: (x.pin is None, int(x.pin) if x.pin else 0, x.at[1], x.at[0])
        )
        printed = {term.pin for term in comp.terminals if term.pin}
        unused = (str(n) for n in range(1, 1000) if str(n) not in printed)
        for term in comp.terminals:
            term.id = f"{comp.refdes}.{term.pin or next(unused)}"


def _assign_values(w: _Work, components: list[Component], body_of: dict[str, Body]) -> None:
    texts = w.sheet.texts
    by_id = {t.id: t for t in texts}
    pairs = []
    for comp in components:
        ref = by_id[comp.text_ids[0]]
        body = body_of[comp.refdes]
        for t in texts:
            if t.owner is not None or t.role not in ("other", "param"):
                continue
            d_ref, d_body = _box_distance(t.bbox, ref.bbox), _box_distance(t.bbox, body.bbox)
            if d_ref <= VALUE_REACH_REF or d_body <= VALUE_REACH_BODY:
                pairs.append((d_ref + 0.5 * d_body, t.id, comp.refdes))
    comps = {c.refdes: c for c in components}
    for t in texts:
        if t.role == "footprint" and t.owner is None:
            near = min(
                components,
                default=None,
                key=lambda c: _box_distance(t.bbox, body_of[c.refdes].bbox),
            )
            if near is not None and _box_distance(t.bbox, body_of[near.refdes].bbox) <= 6.0:
                near.footprint = near.footprint or t.text
                t.owner = near.refdes
    for _, tid, refdes in sorted(pairs):
        t, comp = by_id[tid], comps[refdes]
        if t.owner is not None:
            continue
        if t.role == "param":
            if comp.params is None:
                comp.params, t.owner = t.text, refdes
                comp.text_ids.append(tid)
        elif comp.value is None:
            comp.value, t.owner, t.role = t.text, refdes, "value"
            comp.text_ids.append(tid)
    for comp in components:
        body = body_of[comp.refdes]
        for t in _texts_in(w, _expand(body.bbox, 1.0), {"pin_name"}):
            t.owner = t.owner or comp.refdes


def _build_nets(
    w: _Work,
    uf: _UnionFind,
    components: list[Component],
    ports: list[Port],
    names: dict[int, set[str]],
) -> list[Net]:
    groups: dict[int, list[Terminal]] = defaultdict(list)
    for comp in components:
        for term in comp.terminals:
            if term.net_id is not None:
                groups[uf.find(int(term.net_id))].append(term)
    segs_of: dict[int, list[int]] = defaultdict(list)
    for si in w.conductors:
        segs_of[uf.find(si)].append(si)
    merged_names: dict[int, set[str]] = defaultdict(set)
    for root, ns in names.items():
        merged_names[uf.find(root)] |= ns
    roots = set(groups) | set(merged_names)

    def anchor(root: int) -> tuple[float, float]:
        pts = [p for si in segs_of[root] for p in (w.sheet.segs[si].a, w.sheet.segs[si].b)]
        return min(((p[1], p[0]) for p in pts), default=(math.inf, math.inf))

    nets: list[Net] = []
    new_id: dict[int, str] = {}
    for n, root in enumerate(sorted(roots, key=anchor), start=1):
        net_id = f"N{n}"
        new_id[root] = net_id
        net_names = sorted(merged_names.get(root, ()))
        ground = [x for x in net_names if x.upper() in GROUND_NAMES]
        name = ground[0] if ground else (net_names[0] if net_names else None)
        if len(net_names) > 1 and not ground:
            w.item(
                "mismatch",
                f"Net {net_id} has several names: {', '.join(net_names)}",
                "two different labels or power symbols are connected",
                refs=[net_id],
            )
        segments = [(w.sheet.segs[si].a, w.sheet.segs[si].b) for si in segs_of[root]]
        nets.append(
            Net(
                id=net_id,
                name=name,
                terminals=[t.id for t in groups.get(root, [])],
                segments=segments,
            )
        )
    for comp in components:
        for term in comp.terminals:
            if term.net_id is not None:
                term.net_id = new_id[uf.find(int(term.net_id))]
    for port in ports:
        if port.net_id is not None:
            port.net_id = new_id.get(uf.find(int(port.net_id)))
    return nets


def _review(
    w: _Work, components: list[Component], body_of: dict[str, Body], stats: SheetStats
) -> None:
    seen: Counter[str] = Counter(c.refdes for c in components)
    for refdes, count in seen.items():
        if count > 1:
            w.item(
                "mismatch",
                f"Reference designator {refdes} appears {count} times",
                "each component needs a unique designator",
                "blocking",
                [refdes],
            )
    for comp in components:
        body = body_of[comp.refdes]
        expected = EXPECTED_TERMINALS[comp.kind]
        if expected is not None and len(comp.terminals) not in expected:
            comp.status, comp.confidence = "needs_review", 0.5
            w.item(
                "mismatch",
                f"{comp.refdes} ({comp.kind}) has {len(comp.terminals)} terminals",
                f"expected {' or '.join(map(str, expected))}",
                "blocking",
                [comp.refdes],
                comp.bbox,
            )
            continue
        if comp.kind in ("other", "transformer", "switch", "connector"):
            w.item(
                "unsupported",
                f"{comp.refdes}: kind '{comp.kind}' is exported as a comment",
                "no SPICE primitive for this symbol",
                "info",
                [comp.refdes],
                comp.bbox,
            )
        reason = _assign_roles(w, comp, body)
        if comp.kind in POLARISED and (
            reason is not None or any(t.role is None for t in comp.terminals)
        ):
            comp.status, comp.confidence = "needs_review", 0.6
            w.item(
                "low_confidence",
                f"Pin roles of {comp.refdes} need confirmation",
                reason or "pin roles not recognised",
                "warning",
                [comp.refdes],
                comp.bbox,
            )
        if comp.value is None and comp.kind not in ("connector", "switch", "other"):
            comp.status = "needs_review"
            w.item(
                "possibly_missing",
                f"No value found for {comp.refdes}",
                "no unassigned text near the symbol",
                "warning",
                [comp.refdes],
                comp.bbox,
            )
    for b in w.bodies:
        explained = b.refdes_text is not None or b.port_text is not None
        if not explained and not b.directive_box and not b.implicit_ground:
            w.item(
                "possibly_missing",
                "Symbol graphics without a designator or net name",
                "no component, power symbol or label explains this graphic",
                "info",
                bbox=b.bbox,
            )
    for t in w.sheet.texts:
        if t.role in ("other", "pin_number", "pin_name") and t.owner is None:
            w.item(
                "possibly_missing",
                f"Text '{t.text}' was not assigned",
                "no component, pin or wire near it",
                "info",
                bbox=t.bbox,
            )
    if stats.t_junctions_without_dot:
        w.item(
            "assumption",
            f"{stats.t_junctions_without_dot} T-junction(s) without a dot treated as connected",
            "a wire ending on another wire connects by convention",
            "info",
        )
