"""Plan coverage: does the planned feature list explain what the drawing shows? (D54)

Deterministic and advisory. Findings are review items with a sheet position, i.e. points of
interest for the reviewer. Evidence:

- Visible circles: full circles drawn at outline width in an orthographic view, converted with
  the view scale. Each needs a planned feature with that diameter (hole, counterbore, countersink,
  thread, boss or pocket circle, revolved diameter, chamfer or fillet edge), and one view cannot
  show more circles of a diameter than the plan has instances of it.
- Printed dimensions: each should be referenced by a feature (`dimension_ids`) or appear in the
  plan as a size, position, step, pitch, radius or angle. Radius, chamfer and angle dimensions
  need evidence of their own type: an R is a fillet, a profile arc, a slot end or a pitch radius
  (never a full circle, which is dimensioned with Ø); a chamfer is a chamfer feature, a chamfered
  profile corner or a countersink; an angle is a sloped edge in the plane of the view it is
  printed in, a pattern step, a countersink or a chamfer. A feature that references such a
  dimension must build it.
- Outline edges seen in two views: straight edges at outline width mark faces of the part. Views
  are mapped to part axes by their extents; an edge position along an axis that two views show
  but no planned feature has (face, vertex, wall, tangent) is a cut-out, pocket, notch or step
  that may be missing.

Covered evidence is no proof that a value is used correctly (validation and review check that);
uncovered evidence is a strong hint that something visible is missing from the plan.
"""

import itertools
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel

from mechatronikers.drawing_mcp.dimensions import parse_dimension_text
from mechatronikers.spec import (
    Arc,
    Boss,
    Chamfer,
    Circle,
    CircularPattern,
    Dimension,
    ExtrudeBase,
    Feature,
    Fillet,
    Hole,
    HoleParams,
    HolePattern,
    Line,
    Placement,
    Pocket,
    Profile,
    ReviewItem,
    RevolveBase,
    ScriptFeature,
    Slot,
    SourceRef,
)
from mechatronikers.spec.drawing import BBox, Primitive, ViewRegion

MIN_SHEET_DIAMETER = 2.0  # sheet mm: smaller circles are symbols, centre marks or hatch dots
WIDTH_TOL = 0.02  # pt, for "drawn at outline width"
SAME_CIRCLE = 0.05  # sheet mm: duplicates drawn on top of each other (e.g. two widths)
CONCENTRIC = 0.2  # sheet mm
POI_MARGIN = 3.0  # sheet mm around the evidence
THREAD_MINOR = 0.75  # minor diameter is drawn at roughly 0.75-0.9 x nominal
ANGLE_TOL = 0.15  # degrees, printed angle vs planned angle
CHAMFER_ANGLE_TOL = 0.5  # degrees
MIN_EDGE = 2.0  # sheet mm: shorter straight edges are not used as two-view evidence
AXIS_TOL = 0.05  # sheet mm: an edge counts as horizontal/vertical on the sheet
JOIN = 0.1  # sheet mm: an outline edge touches another one
LEVEL_TOL = 0.3  # part mm: drawn edge position vs planned face position; also view extent vs
# plan extent: a view maps to a part axis only when it is as long as the plan along it, so a plan
# that is slightly too long or short never reports its own features as missing cut-outs
GRID = 5.0  # sheet mm, cell of the spatial index for the outline join test

Points = list[np.ndarray]
Vec3 = tuple[float, float, float]


def _close(a: float, b: float, rel: float = 0.002, abs_: float = 0.02) -> bool:
    return abs(a - b) <= max(abs_, rel * abs(b))


@dataclass(frozen=True)
class PlannedCircle:
    """A full circle the plan makes visible when looking along its axis."""

    lo: float  # diameter range (part mm); lo == hi except for threads
    hi: float
    count: int
    feature_id: str
    label: str

    def matches(self, d: float) -> bool:
        return self.lo - max(0.05, 0.005 * self.lo) <= d <= self.hi + max(0.05, 0.005 * self.hi)


@dataclass(frozen=True)
class PlannedAngle:
    value: float  # degrees
    normal: Vec3 | None  # plane the angle lies in (part frame); None: seen in any view


@dataclass
class Evidence:
    """What one feature builds that an R, chamfer or angle dimension can refer to."""

    radii: list[float] = field(default_factory=list)  # arcs, fillets, slot ends, pitch radii
    circle_radii: list[float] = field(default_factory=list)  # full circles (explicit links only)
    chamfers: list[tuple[float, float]] = field(default_factory=list)  # legs of sloped edges
    angles: list[PlannedAngle] = field(default_factory=list)
    opaque: bool = False  # script feature: cannot be inspected
    anchors: Points = field(default_factory=list)  # hole axes, circle/arc/slot centres
    axis: np.ndarray | None = None  # feature axis (placement normal): positions lie across it


@dataclass
class PlanFacts:
    """Everything the plan states, as values a printed dimension or a circle can match."""

    circles: list[PlannedCircle] = field(default_factory=list)
    lengths: list[float] = field(default_factory=list)
    diameters: list[float] = field(default_factory=list)
    radii: list[float] = field(default_factory=list)  # rounded edges only, not full circles
    chamfers: list[tuple[float, float]] = field(default_factory=list)
    angles: list[PlannedAngle] = field(default_factory=list)
    threads: list[float] = field(default_factory=list)  # nominal diameters
    referenced: set[str] = field(default_factory=set)
    by_feature: dict[str, Evidence] = field(default_factory=dict)
    anchors: Points = field(default_factory=list)  # all feature positions (part frame)
    levels: Points = field(default_factory=list)  # points where features put faces and edges
    extent: tuple[np.ndarray, np.ndarray] | None = None  # bounding box of the material
    edge_offsets: list[float] = field(default_factory=list)  # fillets/chamfers move edges
    opaque: bool = False  # the plan has a script feature


class CoverageReport(BaseModel):
    circles_checked: int
    dimensions_checked: int
    review_items: list[ReviewItem]
    edges_checked: int = 0  # edge positions compared in views mapped to the part axes


# --------------------------------------------------------------------------- plan side


def _unit(v: tuple[float, float, float] | np.ndarray) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    unit: np.ndarray = a / float(np.linalg.norm(a))
    return unit


def _frame(p: Placement) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """origin, u, v, normal in the part frame (same convention as cad_mcp.build.plane)."""
    n = _unit(p.normal)
    if p.x_dir is not None:
        u = _unit(p.x_dir)
    else:
        ref = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
        u = _unit(ref - n * ref.dot(n))
    return np.asarray(p.origin, dtype=float), u, np.cross(n, u), n


def _arc_centre(
    a: tuple[float, float], m: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float, float] | None:
    """Centre and radius of the circle through three points (None if collinear)."""
    (ax, ay), (bx, by), (cx, cy) = a, m, b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    sa, sb, sc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (sa * (by - cy) + sb * (cy - ay) + sc * (ay - by)) / d
    uy = (sa * (cx - bx) + sb * (ax - cx) + sc * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _diffs(values: list[float]) -> list[float]:
    return [abs(a - b) for a, b in itertools.combinations(values, 2) if abs(a - b) > 1e-6]


def _line_angles(thetas: list[float]) -> list[float]:
    """Angles a drawing may print for lines with these directions (degrees, mod 180)."""
    out: list[float] = []
    for t in thetas:
        out += [t, 180 - t, abs(90 - t)]
    for a, b in itertools.combinations(thetas, 2):
        d = abs(a - b) % 180
        out += [d, 180 - d]
    return [a for a in out if 0.05 < a < 179.95]


def _swept_extremes(arc: Arc, cu: float, cv: float, r: float) -> list[tuple[float, float]]:
    """The axis extremes (±u, ±v) of an arc's circle that the arc actually passes through."""

    def angle(p: tuple[float, float]) -> float:
        return math.atan2(p[1] - cv, p[0] - cu)

    turn = 2 * math.pi
    a0, am, a1 = angle(arc.start), angle(arc.mid), angle(arc.end)
    ccw = (am - a0) % turn <= (a1 - a0) % turn
    span = (a1 - a0) % turn if ccw else (a0 - a1) % turn
    out = []
    for k, (du, dv) in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1))):
        e = k * math.pi / 2
        if ((e - a0) % turn if ccw else (a0 - e) % turn) <= span + 1e-9:
            out.append((cu + r * du, cv + r * dv))
    return out


def _tilt_angles(direction: np.ndarray) -> list[PlannedAngle]:
    """Angles of an inclined axis or direction against the part axes, in the plane they span."""
    out = []
    for e in np.eye(3):
        cos = min(1.0, abs(float(direction.dot(e))))
        tilt = math.degrees(math.acos(cos))
        plane = np.cross(direction, e)
        if 0.05 < tilt < 89.95 and float(np.linalg.norm(plane)) > 1e-9:
            n = plane / float(np.linalg.norm(plane))
            normal = (float(n[0]), float(n[1]), float(n[2]))
            out += [PlannedAngle(tilt, normal), PlannedAngle(90 - tilt, normal)]
    return out


def _sloped(du: float, dv: float) -> bool:
    """Neither along u nor along v: a chamfered corner, a slope or a taper."""
    return abs(du) > 1e-6 and abs(dv) > 1e-6


def _thread_nominal(thread: str | None) -> float | None:
    m = re.match(r"\s*M\s*(\d+(?:[.,]\d+)?)", thread or "", re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


class _Collector:
    def __init__(self, dimensions: list[Dimension]) -> None:
        self.facts = PlanFacts()
        self.thread_dims = {d.id: d.value for d in dimensions if d.kind == "thread"}
        self.anchors: Points = []  # feature positions: hole axes, circle and arc centres
        self.extents: list[np.ndarray] = []  # per feature: (min xyz, max xyz)
        self.base_box: tuple[np.ndarray, np.ndarray] | None = None
        self.solid: Points = []  # points of features that add material

    def evidence(self, f: Feature) -> Evidence:
        return self.facts.by_feature.setdefault(f.id, Evidence())

    def radius(self, ev: Evidence, r: float) -> None:
        self.facts.radii.append(r)
        ev.radii.append(r)

    def angles(self, ev: Evidence, values: list[float], normal: Vec3 | None) -> None:
        planned = [PlannedAngle(a, normal) for a in values]
        self.facts.angles += planned
        ev.angles += planned

    def chamfer(self, ev: Evidence, a: float, b: float) -> None:
        self.facts.chamfers.append((a, b))
        ev.chamfers.append((a, b))

    def place(self, ev: Evidence, placement: Placement, anchors: Points) -> None:
        """Positions and axis of a feature: an inclined axis or direction is an angle, and the
        anchors give radial and polar positions for dimensions the feature references."""
        _, u, _, n = _frame(placement)
        ev.axis = n
        ev.anchors += anchors
        self.anchors += anchors
        tilts = _tilt_angles(n) + (_tilt_angles(u) if placement.x_dir is not None else [])
        self.facts.angles += tilts
        ev.angles += tilts

    def profile(
        self, f: Feature, profile: Profile, placement: Placement, shift: list[float]
    ) -> Points:
        """Local values of one profile; returns its points in the part frame."""
        facts, ev = self.facts, self.evidence(f)
        o, u, v, n = _frame(placement)
        local: list[tuple[float, float]] = []
        centres: list[tuple[float, float]] = []
        thetas: list[float] = []
        loops = [profile, *profile.inner]
        for loop in loops:
            for s in loop.segments:
                if isinstance(s, Circle):
                    (cu, cv), r = s.center, s.radius
                    local += [(cu - r, cv), (cu + r, cv), (cu, cv - r), (cu, cv + r)]
                    centres.append((cu, cv))
                    ev.circle_radii.append(r)
                    facts.diameters.append(2 * r)
                    facts.circles.append(
                        PlannedCircle(2 * r, 2 * r, 1, f.id, f"{f.type} Ø{2 * r:g}")
                    )
                    for dim_id in f.dimension_ids:
                        if dim_id in self.thread_dims and _close(self.thread_dims[dim_id], 2 * r):
                            self._thread(2 * r, 1, f.id)
                elif isinstance(s, Arc):
                    local += [s.start, s.mid, s.end]
                    arc = _arc_centre(s.start, s.mid, s.end)
                    if arc is not None:
                        cu, cv, r = arc
                        centres.append((cu, cv))
                        # tangent positions the arc sweeps are edges in the other views
                        local += _swept_extremes(s, cu, cv, r)
                        self.radius(ev, r)
                        facts.diameters.append(2 * r)
                elif isinstance(s, Line):
                    local += [s.start, s.end]
                    du, dv = s.end[0] - s.start[0], s.end[1] - s.start[1]
                    facts.lengths.append(math.hypot(du, dv))
                    thetas.append(math.degrees(math.atan2(dv, du)) % 180)
                    if _sloped(du, dv):
                        self.chamfer(ev, abs(du), abs(dv))
        normal = (float(n[0]), float(n[1]), float(n[2]))
        self.angles(ev, _line_angles(thetas), normal)
        facts.lengths += _diffs([p[0] for p in local]) + _diffs([p[1] for p in local])
        base = [o + pu * u + pv * v for pu, pv in local]
        self.place(ev, placement, [o + cu * u + cv * v for cu, cv in centres])
        return [p + s * n for p in base for s in shift]

    def _thread(self, nominal: float, count: int, feature_id: str) -> None:
        self.facts.threads.append(nominal)
        self.facts.circles.append(
            PlannedCircle(THREAD_MINOR * nominal, nominal, count, feature_id, f"M{nominal:g}")
        )

    def hole(self, f: Feature, params: HoleParams, placements: list[Placement]) -> Points:
        facts, n, ev = self.facts, len(placements), self.evidence(f)
        diameters = []
        for d, what in (
            (params.diameter, "hole"),
            (params.cb_diameter, "counterbore"),
            (params.cs_diameter, "countersink"),
        ):
            if d:
                facts.circles.append(PlannedCircle(d, d, n, f.id, f"{what} Ø{d:g}"))
                facts.diameters.append(d)
                ev.circle_radii.append(d / 2)
                diameters.append(d)
        facts.lengths += [x for x in (params.depth, params.cb_depth, params.thread_depth) if x]
        if params.hole_type == "countersink" or params.cs_diameter:
            self.angles(ev, [params.cs_angle, params.cs_angle / 2], None)
            if params.cs_diameter and params.cs_diameter > params.diameter:
                leg = (params.cs_diameter - params.diameter) / 2
                self.chamfer(ev, leg, leg / math.tan(math.radians(params.cs_angle / 2)))
        nominal = _thread_nominal(params.thread)
        if nominal:
            self._thread(nominal, n, f.id)
        points = []
        for p in placements:
            o, u, v, normal = _frame(p)
            self.place(ev, p, [o])
            points.append(o)
            ends = [o] + ([o + params.depth * normal] if params.depth else [])
            ends += [o + params.cb_depth * normal] if params.cb_depth else []
            if params.depth:
                points.append(o + params.depth * normal)
            # walls: a hole seen from the side (section) shows edges at axis ± radius
            for e, d, w, s in itertools.product(ends, diameters, (u, v), (-1, 1)):
                points.append(e + s * (d / 2) * w)
        return points

    def pattern(self, f: HolePattern) -> Points:
        facts, p, ev = self.facts, f.pattern, self.evidence(f)
        if isinstance(p, CircularPattern):
            step = p.angle()
            axis, centre = _unit(p.axis), np.asarray(p.center, dtype=float)
            normal = (float(axis[0]), float(axis[1]), float(axis[2]))
            angles = [k * step for k in range(1, p.count)] + [360 / p.count]
            radial = np.asarray(f.seed.placement.origin, dtype=float) - centre
            radial -= axis * radial.dot(axis)
            r = float(np.linalg.norm(radial))
            ev.anchors.append(centre)
            if r > 1e-6:
                self.radius(ev, r)
                facts.diameters.append(2 * r)
                _, u, v, _ = _frame(Placement(normal=tuple(axis)))
                polar = math.degrees(math.atan2(radial.dot(v), radial.dot(u))) % 90
                angles += [polar, 90 - polar]
                angles += [polar + k * step for k in range(1, p.count)]
            self.angles(ev, angles, normal)
        else:
            facts.lengths += [p.pitch_1, p.pitch_2]
            facts.lengths += [(p.count_1 - 1) * p.pitch_1, (p.count_2 - 1) * p.pitch_2]
        return self.hole(f, f.seed, f.instances())

    def revolve(self, f: RevolveBase) -> Points:
        facts, ev = self.facts, self.evidence(f)
        segments = [s for s in f.profile.segments if not isinstance(s, Circle)]
        pts = [pt for s in segments for pt in _ends(s)]
        us = sorted({round(u, 6) for u, _ in pts if u > 1e-6})
        for u in us:
            facts.circles.append(PlannedCircle(2 * u, 2 * u, 1, f.id, f"turned Ø{2 * u:g}"))
            ev.circle_radii.append(u)
        facts.diameters += [2 * u for u in us]
        facts.lengths += [2 * u for u in us] + _diffs([p[0] for p in pts])
        facts.lengths += _diffs([p[1] for p in pts])
        thetas = []
        for s in segments:
            if isinstance(s, Arc) and (arc := _arc_centre(s.start, s.mid, s.end)):
                self.radius(ev, arc[2])
                cu, cv, r = arc
                pts += [(max(pu, 0.0), pv) for pu, pv in _swept_extremes(s, cu, cv, r)]
            elif isinstance(s, Line):
                du, dv = s.end[0] - s.start[0], s.end[1] - s.start[1]
                thetas.append(math.degrees(math.atan2(dv, du)) % 180)
                if _sloped(du, dv):
                    self.chamfer(ev, abs(du), abs(dv))
        # the profile plane contains the axis: it is seen in every side view
        self.angles(ev, _line_angles(thetas), None)
        self.anchors.append(np.zeros(3))
        ev.anchors.append(np.zeros(3))
        ev.axis = np.array([0.0, 0.0, 1.0])
        return [np.array([sx * u, sy * u, z]) for u, z in pts for sx, sy in _SIGNS]

    def add(self, f: Feature) -> None:
        facts = self.facts
        facts.referenced.update(f.dimension_ids)
        ev = self.evidence(f)
        points: Points = []
        if isinstance(f, ExtrudeBase):
            shift = [0.0, f.depth] if f.direction == "positive" else [-f.depth / 2, f.depth / 2]
            facts.lengths.append(f.depth)
            points = self.profile(f, f.profile, f.placement, shift)
        elif isinstance(f, RevolveBase):
            points = self.revolve(f)
        elif isinstance(f, Boss):
            facts.lengths.append(f.height)
            points = self.profile(f, f.profile, f.placement, [0.0, f.height])
        elif isinstance(f, Pocket):
            facts.lengths += [f.depth] if f.depth else []
            depths = [0.0, f.depth] if f.depth else [0.0]
            points = self.profile(f, f.profile, f.placement, depths)
        elif isinstance(f, Slot):
            facts.lengths += [
                f.length,
                f.width,
                f.length + f.width,
                *([f.depth] if f.depth else []),
            ]
            self.radius(ev, f.width / 2)
            centre, along, across, normal = _frame(f.placement)
            ends = [centre + sign * (f.length / 2) * along for sign in (-1, 1)]
            self.place(ev, f.placement, [centre, *ends])
            points = ends + [centre + s * (f.length + f.width) / 2 * along for s in (-1, 1)]
            points += [e + s * (f.width / 2) * across for e in ends for s in (-1, 1)]
            if f.depth:
                points += [p + f.depth * normal for p in points]
        elif isinstance(f, Hole):
            points = self.hole(f, f, [f.placement])
        elif isinstance(f, HolePattern):
            points = self.pattern(f)
        elif isinstance(f, Fillet):
            self.radius(ev, f.radius)
            facts.edge_offsets.append(f.radius)
            self._edge_circles(f.radius)
        elif isinstance(f, Chamfer):
            second = f.distance_2 or f.distance
            facts.lengths += [x for x in (f.distance, f.distance_2) if x]
            facts.edge_offsets += [f.distance, second]
            self.chamfer(ev, f.distance, second)
            slope = math.degrees(math.atan2(second, f.distance))
            self.angles(ev, [slope, 90 - slope], None)
            self._edge_circles(f.distance)
        elif isinstance(f, ScriptFeature):
            ev.opaque = facts.opaque = True
        if points:
            arr = np.array(points)
            self.extents.append(np.stack([arr.min(axis=0), arr.max(axis=0)]))
            facts.levels += points
            if f.type in ("extrude_base", "revolve_base"):
                self.base_box = (arr.min(axis=0), arr.max(axis=0))
            if f.type in ("extrude_base", "revolve_base", "boss"):
                self.solid += points

    def _edge_circles(self, size: float) -> None:
        """A chamfer or fillet on a circular edge shows as a concentric circle ± 2 x size."""
        for c in [c for c in self.facts.circles if c.lo == c.hi]:
            for d in (c.lo - 2 * size, c.lo + 2 * size):
                if d > 0:
                    label = f"edge of {c.label}"
                    self.facts.circles.append(PlannedCircle(d, d, c.count, c.feature_id, label))

    def finish(self) -> PlanFacts:
        facts = self.facts
        for axis in range(3):
            anchors = [float(a[axis]) for a in self.anchors]
            levels = [float(e[i][axis]) for e in self.extents for i in (0, 1)]
            facts.lengths += _diffs(anchors) + _diffs(levels)
            if self.base_box is not None:
                lo, hi = float(self.base_box[0][axis]), float(self.base_box[1][axis])
                facts.lengths += [abs(a - b) for a in anchors for b in (lo, hi)]
        if self.solid:
            arr = np.array(self.solid)
            facts.extent = (arr.min(axis=0), arr.max(axis=0))
        facts.anchors = self.anchors
        return facts


_SIGNS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _ends(s: Line | Arc) -> list[tuple[float, float]]:
    return [s.start, s.mid, s.end] if isinstance(s, Arc) else [s.start, s.end]


def plan_facts(features: list[Feature], dimensions: list[Dimension]) -> PlanFacts:
    collector = _Collector(dimensions)
    for f in features:
        collector.add(f)
    return collector.finish()


# --------------------------------------------------------------------------- drawing side


def visible_circles(
    primitives: list[Primitive], view_ids: set[str], outline_width: float | None
) -> dict[str, list[Primitive]]:
    """Full circles at outline width per orthographic view, without duplicates and symbols."""
    by_view: dict[str, list[Primitive]] = defaultdict(list)
    for p in sorted(primitives, key=lambda p: -p.width):
        if p.kind != "circle" or p.view_id not in view_ids:
            continue
        assert p.center is not None and p.radius is not None
        if 2 * p.radius < MIN_SHEET_DIAMETER:
            continue
        if outline_width is not None and p.width < outline_width - WIDTH_TOL:
            continue
        kept = by_view[p.view_id]
        if not any(
            math.dist(p.center, k.center or (0, 0)) <= SAME_CIRCLE
            and abs(p.radius - (k.radius or 0)) <= SAME_CIRCLE
            for k in kept
        ):
            kept.append(p)
    return dict(by_view)


def _point_to(p: Primitive, x: float, y: float) -> float:
    """Distance from a sheet point to a primitive (line segment, or circle/arc curve)."""
    if p.kind == "line":
        assert p.start is not None and p.end is not None
        (ax, ay), (bx, by) = p.start, p.end
        dx, dy = bx - ax, by - ay
        squared = dx * dx + dy * dy
        t = 0.0 if squared == 0 else ((x - ax) * dx + (y - ay) * dy) / squared
        t = max(0.0, min(1.0, t))
        return math.hypot(x - ax - t * dx, y - ay - t * dy)
    assert p.center is not None and p.radius is not None
    return abs(math.dist((x, y), p.center) - p.radius)


def _joined(prims: list[Primitive]) -> list[Primitive]:
    """Drop straight edges that touch no other primitive (spatial grid, not all pairs)."""
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, q in enumerate(prims):
        x0, y0, x1, y1 = q.bbox()
        for gx in range(int((x0 - JOIN) // GRID), int((x1 + JOIN) // GRID) + 1):
            for gy in range(int((y0 - JOIN) // GRID), int((y1 + JOIN) // GRID) + 1):
                grid[(gx, gy)].append(i)
    kept = []
    for i, p in enumerate(prims):
        ends = (p.start or (0, 0), p.end or (0, 0))
        if p.kind != "line" or any(
            j != i and _point_to(prims[j], x, y) <= JOIN
            for x, y in ends
            for j in grid[(int(x // GRID), int(y // GRID))]
        ):
            kept.append(p)
    return kept


def visible_outline(
    primitives: list[Primitive], views: list[ViewRegion], outline_width: float | None
) -> dict[str, list[Primitive]]:
    """Primitives at outline width inside each orthographic view. Straight edges that touch no
    other outline geometry (cutting-plane marks, symbols) are left out."""
    if outline_width is None:
        return {}
    boxes = {v.id: v.bbox for v in views if v.kind == "orthographic"}
    by_view: dict[str, list[Primitive]] = defaultdict(list)
    for p in primitives:
        box = boxes.get(p.view_id or "")
        if box is None or p.width < outline_width - WIDTH_TOL:
            continue
        x0, y0, x1, y1 = p.bbox()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if box[0] - 1 <= cx <= box[2] + 1 and box[1] - 1 <= cy <= box[3] + 1:
            by_view[p.view_id or ""].append(p)
    return {view_id: _joined(prims) for view_id, prims in by_view.items()}


def _concentric(a: Primitive, b: Primitive) -> bool:
    return math.dist(a.center or (0, 0), b.center or (0, 0)) <= CONCENTRIC


def _bbox(prims: list[Primitive]) -> tuple[float, float, float, float]:
    boxes = [p.bbox() for p in prims]
    return (
        min(b[0] for b in boxes) - POI_MARGIN,
        min(b[1] for b in boxes) - POI_MARGIN,
        max(b[2] for b in boxes) + POI_MARGIN,
        max(b[3] for b in boxes) + POI_MARGIN,
    )


@dataclass
class ViewFrame:
    """How an orthographic view lies relative to the part: which part axes may run along the
    sheet's horizontal and vertical (by extent; both directions are allowed, views may be
    mirrored relative to the part frame)."""

    box: BBox  # sheet mm, outline geometry
    scale: float
    h_axes: list[int]
    v_axes: list[int]

    @property
    def normal(self) -> int | None:
        if len(self.h_axes) == 1 and len(self.v_axes) == 1 and self.h_axes != self.v_axes:
            return 3 - self.h_axes[0] - self.v_axes[0]
        return None


def view_frames(
    outline_by_view: dict[str, list[Primitive]],
    view_scales: dict[str, float],
    extent: tuple[np.ndarray, np.ndarray] | None,
) -> dict[str, ViewFrame]:
    if extent is None:
        return {}
    size = extent[1] - extent[0]
    frames = {}
    for view_id, prims in outline_by_view.items():
        scale = view_scales.get(view_id)
        if not prims or not scale:
            continue
        boxes = [p.bbox() for p in prims]
        box = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        w, h = (box[2] - box[0]) / scale, (box[3] - box[1]) / scale

        def axes(length: float) -> list[int]:
            return [a for a in range(3) if abs(float(size[a]) - length) <= LEVEL_TOL]

        frames[view_id] = ViewFrame(box=box, scale=scale, h_axes=axes(w), v_axes=axes(h))
    return frames


# --------------------------------------------------------------------------- checks


def _chamfer_matches(value: float, angle: float | None, legs: list[tuple[float, float]]) -> bool:
    for a, b in legs:
        for x, y in ((a, b), (b, a)):
            if not _close(x, value):
                continue
            slope = math.degrees(math.atan2(y, x))
            off = min(abs(slope - angle), abs(90 - slope - angle)) if angle is not None else 0.0
            if off <= CHAMFER_ANGLE_TOL:
                return True
    return False


def _angle_matches(value: float, planned: list[PlannedAngle], normal: int | None) -> bool:
    return any(
        abs(value - a.value) <= ANGLE_TOL
        and (normal is None or a.normal is None or abs(a.normal[normal]) > 0.99)
        for a in planned
    )


def _offsets(ev: Evidence, refs: Points) -> list[np.ndarray]:
    """Vectors from reference points (part origin, every feature position) to this feature's
    positions, projected into the plane across the feature axis."""
    out = []
    for a in ev.anchors:
        for r in refs:
            d = a - r
            if ev.axis is not None:
                d = d - ev.axis * float(d.dot(ev.axis))
            if float(np.linalg.norm(d)) > 1e-6:
                out.append(d)
    return out


def _polar(ev: Evidence, d: np.ndarray) -> list[float]:
    """Angles a drawing may print for a position at offset d: against the two part axes that
    span the plane across the feature axis."""
    axis = ev.axis if ev.axis is not None else np.array([0.0, 0.0, 1.0])
    e1, e2 = sorted(np.eye(3), key=lambda e: abs(float(e.dot(axis))))[:2]
    t = math.degrees(math.atan2(float(d.dot(e2)), float(d.dot(e1)))) % 180
    return [a for a in (t, 180 - t, abs(90 - t)) if 0.05 < a < 179.95]


def _builds(ev: Evidence, dim: Dimension, chamfer_angle: float | None, refs: Points) -> bool:
    """Does this feature build or position by the radius, chamfer or angle the dimension states?
    Positional use counts: the radial distance or polar angle of a hole or slot from the part
    origin or another feature (a pitch radius, a kidney slot's centre line, a hole at 30°), and
    the tilt of an inclined axis."""
    if ev.opaque:
        return True
    if dim.kind == "radius":
        radii = ev.radii + ev.circle_radii
        radii += [float(np.linalg.norm(d)) for d in _offsets(ev, refs)]
        return any(_close(dim.value, r) for r in radii)
    if dim.kind == "chamfer":
        return _chamfer_matches(dim.value, chamfer_angle, ev.chamfers)
    if dim.kind == "angle":
        polar = [PlannedAngle(a, None) for d in _offsets(ev, refs) for a in _polar(ev, d)]
        return _angle_matches(dim.value, ev.angles + polar, None)
    return True


# kind: (what the dimension states, evidence the plan lacks, hint for the model)
_TYPED = {
    "radius": (
        "a radius",
        "fillet, profile arc, slot end, pitch radius or radial position of this size (a full "
        "circle of that radius does not count: circles are dimensioned with Ø)",
        "a fillet or rounded edge may be missing; if the radius positions a feature, reference "
        "it from that feature",
    ),
    "chamfer": (
        "a chamfer",
        "chamfer feature, chamfered profile corner or countersink of this size and angle",
        "a chamfer may be missing",
    ),
    "angle": (
        "an angle",
        "sloped edge, inclined axis, pattern step, countersink or chamfer with this angle",
        "a sloped or angled face may be missing; if the angle positions a feature (polar "
        "position, inclined axis), reference it from that feature",
    ),
}


def _edge_findings(
    facts: PlanFacts, frames: dict[str, ViewFrame], outline_by_view: dict[str, list[Primitive]]
) -> tuple[int, list[tuple[str, str, list[Primitive], str]]]:
    """Edge positions two views show but no planned feature has. Returns the number of edge
    positions checked and (message, reason, lines in the first view, first view) per finding."""
    if facts.extent is None or facts.opaque or not facts.levels:
        return 0, []
    lo, hi = facts.extent
    size = hi - lo
    levels = np.array(facts.levels) - lo  # (n, 3), relative to the material's low corner
    offsets = [0.0, *facts.edge_offsets]

    def explained(position: float, axis: int) -> bool:
        planned = levels[:, axis]
        for target in (position, float(size[axis]) - position):  # the view may be mirrored
            gap = np.abs(planned - target)
            if any(bool(np.any(np.abs(gap - off) <= LEVEL_TOL)) for off in offsets):
                return True
        return False

    # (axis, distance from the nearer end, view, edge key, lines)
    open_edges: list[tuple[int, float, str, tuple[str, str, int], list[Primitive]]] = []
    checked = 0
    for view_id, frame in sorted(frames.items()):
        x0, y0, x1, y1 = frame.box
        groups: dict[str, list[tuple[float, Primitive]]] = {"h": [], "v": []}
        for p in outline_by_view[view_id]:
            if p.kind != "line" or p.start is None or p.end is None:
                continue
            (xa, ya), (xb, yb) = p.start, p.end
            if math.hypot(xb - xa, yb - ya) < MIN_EDGE:
                continue
            if abs(xa - xb) <= AXIS_TOL:  # vertical edge: a position along the horizontal
                groups["h"].append(((xa + xb) / 2 - x0, p))
            elif abs(ya - yb) <= AXIS_TOL:
                groups["v"].append(((ya + yb) / 2 - y0, p))
        for direction, candidates in (("h", frame.h_axes), ("v", frame.v_axes)):
            span = (x1 - x0 if direction == "h" else y1 - y0) / frame.scale
            merged: list[tuple[float, list[Primitive]]] = []
            for offset, p in sorted(groups[direction], key=lambda x: x[0]):
                position = offset / frame.scale
                if merged and position - merged[-1][0] <= LEVEL_TOL:
                    merged[-1][1].append(p)
                else:
                    merged.append((position, [p]))
            for index, (position, lines) in enumerate(merged):
                if not candidates or position <= LEVEL_TOL or position >= span - LEVEL_TOL:
                    continue  # the silhouette's own bounds, or a view that does not map
                checked += 1
                if any(explained(position, a) for a in candidates):
                    continue
                for a in candidates:
                    near = min(position, float(size[a]) - position)
                    open_edges.append((a, near, view_id, (view_id, direction, index), lines))
    findings = []
    reported: set[tuple[str, str, int]] = set()
    for axis in range(3):
        edges = sorted((e for e in open_edges if e[0] == axis), key=lambda e: e[1])
        clusters: list[list[tuple[int, float, str, tuple[str, str, int], list[Primitive]]]] = []
        for e in edges:
            if clusters and e[1] - clusters[-1][-1][1] <= LEVEL_TOL:
                clusters[-1].append(e)
            else:
                clusters.append([e])
        for cluster in clusters:
            views = sorted({e[2] for e in cluster})
            keys = {e[3] for e in cluster}
            if len(views) < 2 or keys <= reported:
                continue
            reported |= keys
            first = views[0]
            lines = [p for e in cluster if e[2] == first for p in e[4]]
            findings.append(
                (
                    f"Views {', '.join(views)} show an outline edge {cluster[0][1]:.4g} mm from "
                    f"one end of the part along its {'xyz'[axis]} axis ({float(size[axis]):.4g} "
                    "mm) that no planned feature explains",
                    "an edge seen in two views is a face of the part (cut-out, pocket, notch or "
                    "step); no planned feature has a face, wall or edge at that position, so a "
                    "cut-out or step may be missing or misplaced",
                    lines,
                    first,
                )
            )
    return checked, findings


def check_coverage(
    features: list[Feature],
    dimensions: list[Dimension],
    circles_by_view: dict[str, list[Primitive]],
    view_scales: dict[str, float],
    outline_by_view: dict[str, list[Primitive]] | None = None,
) -> CoverageReport:
    facts = plan_facts(features, dimensions)
    outline_by_view = outline_by_view or {}
    frames = view_frames(outline_by_view, view_scales, facts.extent)
    items: list[ReviewItem] = []

    def item(
        message: str,
        reason: str,
        sheet_ref: SourceRef | None,
        feature_ids: list[str] | None = None,
        dimension_ids: list[str] | None = None,
    ) -> None:
        items.append(
            ReviewItem(
                id=f"cov-{len(items) + 1}",
                kind="possibly_missing",
                severity="warning",
                message=message,
                reason=reason,
                sheet_ref=sheet_ref,
                feature_ids=feature_ids or [],
                dimension_ids=dimension_ids or [],
            )
        )

    checked = 0
    for view_id, circles in sorted(circles_by_view.items()):
        scale = view_scales[view_id]
        covered: dict[tuple[float, float], list[Primitive]] = defaultdict(list)
        uncovered: list[tuple[float, Primitive]] = []
        for c in circles:
            assert c.radius is not None
            d = 2 * c.radius / scale
            checked += 1
            hits = [pc for pc in facts.circles if pc.matches(d)]
            if not hits:
                uncovered.append((d, c))
                continue
            # The closest planned diameter explains the circle (exact diameters before ranges).
            best = min(hits, key=lambda pc: (pc.lo != pc.hi, abs((pc.lo + pc.hi) / 2 - d)))
            covered[(best.lo, best.hi)].append(c)
        # Too many circles of one diameter; concentric groups (e.g. countersink + hole) are one
        # finding: [circles, diameters, planned, feature ids]
        excess: list[tuple[list[Primitive], list[str], int, list[str]]] = []
        for (lo, hi), group in sorted(covered.items(), reverse=True):
            same = [pc for pc in facts.circles if (pc.lo, pc.hi) == (lo, hi)]
            planned = sum(pc.count for pc in same)
            if len(group) <= planned:
                continue
            names = sorted({pc.feature_id for pc in same})
            diameter = f"Ø{lo:g}" if lo == hi else f"Ø{lo:g}–{hi:g}"
            for found, diameters, n, _ in excess:
                if (len(found), n) == (len(group), planned) and all(
                    any(_concentric(c, e) for e in found) for c in group
                ):
                    diameters.append(diameter)
                    break
            else:
                excess.append((group, [diameter], planned, names))
        for found, diameters, planned, names in excess:
            item(
                f"View {view_id} shows {len(found)} circles {' / '.join(diameters)}, the plan "
                f"has {planned} ({', '.join(names)})",
                "every circle seen along its axis is one feature instance: holes, bosses or "
                "pattern instances may be missing",
                SourceRef(view_id=view_id, bbox=_bbox(found)),
                feature_ids=names,
            )
        groups: list[list[tuple[float, Primitive]]] = []
        for d, c in sorted(uncovered, key=lambda x: x[0]):
            if groups and _close(groups[-1][0][0], d, rel=0.005, abs_=0.05):
                groups[-1].append((d, c))
            else:
                groups.append([(d, c)])
        explained = [c for group in covered.values() for c in group]
        for cluster in groups:
            d, prims = cluster[0][0], [c for _, c in cluster]
            hint = ""
            outer = [e for c in prims for e in explained if _concentric(c, e)]
            if outer:
                hint = (
                    f"; concentric with a planned Ø{2 * (outer[0].radius or 0) / scale:.4g}: "
                    "possibly a chamfer, step, counterbore or thread"
                )
            item(
                f"View {view_id}: {len(prims)} visible circle(s) Ø{d:.4g} that no planned "
                "feature explains",
                "full circle at outline width; no planned hole, counterbore, countersink, "
                "thread, boss or pocket circle, turned diameter or edge has this diameter" + hint,
                SourceRef(view_id=view_id, bbox=_bbox(prims)),
            )

    values = {
        "linear": facts.lengths + facts.diameters,
        "diameter": facts.diameters,
        "thread": facts.threads + facts.diameters,
    }
    refs = [np.zeros(3), *facts.anchors]  # part origin and every feature position
    users: dict[str, list[str]] = defaultdict(list)
    for f in features:
        for dim_id in f.dimension_ids:
            users[dim_id].append(f.id)
    dims_checked = 0
    for dim in dimensions:
        if dim.status == "rejected" or dim.kind == "other":
            continue
        dims_checked += 1
        text = dim.stated_text or f"{dim.value:g}"
        chamfer_angle = parse_dimension_text(text).angle if dim.kind == "chamfer" else None
        typed = _TYPED.get(dim.kind)
        if users[dim.id]:
            evidence = [facts.by_feature[f] for f in users[dim.id]]
            if typed and not any(_builds(ev, dim, chamfer_angle, refs) for ev in evidence):
                what, builds, missing = typed
                owners = ", ".join(users[dim.id])
                item(
                    f"Dimension '{text}' ({dim.id}) is referenced by {owners}, but none of them "
                    f"builds or is positioned by {what} of that size",
                    f"the referencing feature has no {builds}; {missing}",
                    dim.source,
                    feature_ids=users[dim.id],
                    dimension_ids=[dim.id],
                )
            continue
        view = dim.source.view_id if dim.source else None
        frame_normal = frames[view].normal if view in frames else None
        if dim.kind == "radius":
            used = any(_close(dim.value, r) for r in facts.radii)
        elif dim.kind == "chamfer":
            used = _chamfer_matches(dim.value, chamfer_angle, facts.chamfers)
        elif dim.kind == "angle":
            used = _angle_matches(dim.value, facts.angles, frame_normal)
        else:
            used = any(_close(dim.value, v) for v in values[dim.kind])
        if used:
            continue
        if typed:
            what, builds, missing = typed
            plane = ""
            if dim.kind == "angle" and frame_normal is not None:
                plane = f" in the plane of view {view}"
            reason = (
                f"not referenced via dimension_ids, and the plan has no {builds}{plane}; {missing}"
            )
        else:
            reason = (
                "neither referenced via dimension_ids nor found in the plan as a size, position, "
                "step, pitch, radius or angle"
            )
        item(
            f"Printed dimension '{text}' ({dim.id}) is not used by any planned feature",
            reason,
            dim.source,
            dimension_ids=[dim.id],
        )

    edges_checked, edge_findings = _edge_findings(facts, frames, outline_by_view)
    for message, reason, lines, view_id in edge_findings:
        item(message, reason, SourceRef(view_id=view_id, bbox=_bbox(lines)))
    return CoverageReport(
        circles_checked=checked,
        dimensions_checked=dims_checked,
        review_items=items,
        edges_checked=edges_checked,
    )
