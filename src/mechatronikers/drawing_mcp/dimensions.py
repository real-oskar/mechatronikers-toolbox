"""Dimension text parsing, measuring labels against the geometry, consistency checks (docs/04).

The printed value is authoritative (D12). Measurements are used to associate labels with
geometry, to estimate the scale per view, to flag mismatches, and to fill missing dimensions
(with a reason, metric M3).
"""

import math
import re
import statistics
from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from mechatronikers.drawing_mcp.vector import RawDrawing
from mechatronikers.spec.drawing import Label, Primitive
from mechatronikers.spec.part import Dimension, ReviewItem, SourceRef, Tolerance

DimKind = Literal["linear", "diameter", "radius", "angle", "thread", "chamfer", "other"]

_ARROW_LEN = (1.5, 6.0)  # mm, length of one arrowhead flank
_ARROW_MAX_ANGLE = 40.0  # degrees between the two flanks
_COLLINEAR = 0.3  # mm, perpendicular tolerance for arrows on one dimension line
_ON_CIRCLE = 0.3  # mm, arrow tip on a circle/arc
_LABEL_REACH = 25.0  # mm, how far from a label its dimension line may be
_LEADER_GAP = 2.5  # mm, a leader runs onto its label (or right beside it)
MISMATCH_REL = 0.01  # stated vs measured (after scale) relative tolerance …
MISMATCH_ABS = 0.2  # … or absolute in part mm, whichever is larger (T6)


# --------------------------------------------------------------------------- text


class ParsedText(BaseModel):
    kind: DimKind
    value: float | None
    count: int = 1  # "3x Ø12" → 3
    angle: float | None = None  # chamfer angle ("5x45°") or the value of an angle dimension
    thread: str | None = None  # "M10"
    tolerance: Tolerance | None = None
    raw: str


_NUM = r"(\d+(?:[.,]\d+)?)"


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_dimension_text(text: str) -> ParsedText:
    """Parse a dimension label as printed: '60', 'Ø20', '3x Ø12', 'R9,5', 'M10', '5x45°',
    '25,5°', 'Ø20 H7', '10 ±0,1'. Unknown formats give kind 'other' with value None."""
    raw = text
    t = text.strip().replace("⌀", "Ø").replace("∅", "Ø").replace("×", "x")
    count = 1
    if m := re.match(rf"^(\d+)\s*x\s*(?=[ØRM]|{_NUM}\s*$)", t, re.I):
        count, t = int(m.group(1)), t[m.end() :].strip()
    tolerance = None
    if m := re.search(rf"±\s*{_NUM}\s*$", t):
        tol = _num(m.group(1))
        tolerance, t = Tolerance(plus=tol, minus=tol), t[: m.start()].strip()
    elif m := re.search(r"\s+([A-Za-z]{1,2}\d{1,2})\s*$", t):
        tolerance, t = Tolerance(plus=0, minus=0, fit=m.group(1)), t[: m.start()].strip()
    if m := re.fullmatch(rf"M\s*{_NUM}(?:\s*x\s*{_NUM})?", t, re.I):
        return ParsedText(
            kind="thread", value=_num(m.group(1)), count=count, thread=t.upper(), raw=raw
        )
    if m := re.fullmatch(rf"{_NUM}\s*x\s*{_NUM}\s*°", t):
        return ParsedText(
            kind="chamfer", value=_num(m.group(1)), angle=_num(m.group(2)), count=count, raw=raw
        )
    if m := re.fullmatch(rf"{_NUM}\s*°", t):
        value = _num(m.group(1))
        return ParsedText(kind="angle", value=value, angle=value, count=count, raw=raw)
    if m := re.fullmatch(rf"Ø\s*{_NUM}", t):
        return ParsedText(
            kind="diameter", value=_num(m.group(1)), count=count, tolerance=tolerance, raw=raw
        )
    if m := re.fullmatch(rf"R\s*{_NUM}", t, re.I):
        return ParsedText(
            kind="radius", value=_num(m.group(1)), count=count, tolerance=tolerance, raw=raw
        )
    if m := re.fullmatch(_NUM, t):
        return ParsedText(
            kind="linear", value=_num(m.group(1)), count=count, tolerance=tolerance, raw=raw
        )
    return ParsedText(kind="other", value=None, raw=raw)


# --------------------------------------------------------------------------- arrows


@dataclass
class Arrow:
    tip: np.ndarray  # sheet mm
    direction: np.ndarray  # unit vector, pointing towards the tip


def find_arrows(raw: RawDrawing) -> list[Arrow]:
    """Arrowheads drawn as two short strokes meeting at the tip (as NX writes them), or as
    small filled triangles."""
    flanks: list[tuple[np.ndarray, np.ndarray]] = []
    arrows: list[Arrow] = []
    for p in raw.paths:
        for s in p.subpaths:
            pts = s.points
            if p.filled and not p.stroked and not s.curved and 3 <= len(pts) <= 5:
                size = float(np.max(np.ptp(pts, axis=0)))
                if _ARROW_LEN[0] <= size <= _ARROW_LEN[1]:
                    arrows.extend(_triangle_arrow(pts))
                continue
            if p.stroked and len(pts) == 2:
                length = float(np.linalg.norm(pts[1] - pts[0]))
                if _ARROW_LEN[0] <= length <= _ARROW_LEN[1]:
                    flanks.append((pts[0], pts[1]))
    # Two flanks sharing an endpoint at an acute angle form an arrow.
    index: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in enumerate(flanks):
        for q in (a, b):
            index.setdefault((round(q[0] * 50), round(q[1] * 50)), []).append(i)
    seen: set[tuple[int, int]] = set()
    for key, members in index.items():
        if len(members) < 2:
            continue
        tip = np.array([key[0] / 50, key[1] / 50])
        ends = []
        for i in members:
            a, b = flanks[i]
            other = b if np.linalg.norm(a - tip) < np.linalg.norm(b - tip) else a
            ends.append((i, other - tip))
        for x in range(len(ends)):
            for y in range(x + 1, len(ends)):
                (i, u), (j, v) = ends[x], ends[y]
                pair = (min(i, j), max(i, j))
                if pair in seen:
                    continue
                cos = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v)))
                angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
                if (
                    2.0 < angle < _ARROW_MAX_ANGLE
                    and abs(np.linalg.norm(u) - np.linalg.norm(v)) < 0.3
                ):
                    seen.add(pair)
                    back = (u / np.linalg.norm(u) + v / np.linalg.norm(v)) / 2
                    arrows.append(Arrow(tip=tip, direction=-back / np.linalg.norm(back)))
    return _dedupe(arrows)


def _dedupe(arrows: list[Arrow]) -> list[Arrow]:
    """NX hatches arrowheads, so one arrow yields several flank pairs at the same tip."""
    unique: list[Arrow] = []
    for a in arrows:
        if not any(
            np.linalg.norm(a.tip - b.tip) < 0.05 and float(np.dot(a.direction, b.direction)) > 0.98
            for b in unique
        ):
            unique.append(a)
    return unique


def _triangle_arrow(pts: np.ndarray) -> list[Arrow]:
    unique = [q for i, q in enumerate(pts) if all(np.linalg.norm(q - r) > 1e-6 for r in pts[:i])]
    if len(unique) != 3:
        return []
    sides = [np.linalg.norm(unique[(k + 1) % 3] - unique[(k + 2) % 3]) for k in range(3)]
    k = int(np.argmin(sides))  # tip is opposite the shortest side
    tip = unique[k]
    base = (unique[(k + 1) % 3] + unique[(k + 2) % 3]) / 2
    direction = tip - base
    return [Arrow(tip=tip, direction=direction / np.linalg.norm(direction))]


# --------------------------------------------------------------------------- measuring


class Measurement(BaseModel):
    label_id: str
    kind: DimKind
    sheet_value: float | None  # sheet mm (or degrees for angles); None = not found
    method: str  # how it was found, e.g. "arrow pair", "circle through arrow tip"
    primitive_ids: list[str] = Field(default_factory=list)
    candidates: int = 0  # >1 means the association was ambiguous
    evidence_distance: float | None = None  # mm from the label to the geometry used
    leader: bool = False  # bound through the label's own leader: not ambiguous, never re-matched


def _center(box: tuple[float, float, float, float]) -> np.ndarray:
    return np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2])


def _point_box(q: np.ndarray, box: tuple[float, float, float, float]) -> float:
    dx = max(box[0] - q[0], 0.0, q[0] - box[2])
    dy = max(box[1] - q[1], 0.0, q[1] - box[3])
    return math.hypot(dx, dy)


def _point_segment(q: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    t = min(1.0, max(0.0, float(np.dot(q - a, ab) / np.dot(ab, ab))))
    return float(np.linalg.norm(q - (a + t * ab)))


def _crosses(a: np.ndarray, b: np.ndarray, box: tuple[float, float, float, float]) -> bool:
    """Whether segment a-b enters the box (Liang-Barsky clipping)."""
    d = b - a
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-d[0], a[0] - box[0]),
        (d[0], box[2] - a[0]),
        (-d[1], a[1] - box[1]),
        (d[1], box[3] - a[1]),
    ):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return True


def _segment_box(a: np.ndarray, b: np.ndarray, box: tuple[float, float, float, float]) -> float:
    if _crosses(a, b, box):
        return 0.0
    corners = [np.array([x, y]) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return min(_point_box(a, box), _point_box(b, box), *(_point_segment(q, a, b) for q in corners))


Segment = tuple[np.ndarray, np.ndarray]


def drawn_leader(arrow: Arrow, lines: list[Primitive]) -> list[Segment]:
    """The leader as drawn: collinear strokes running back from the arrow along its shaft, plus
    any short stroke at its far end (the shoulder text often sits on). Empty without a stroke.

    Only drawn strokes count: a shaft line extended past its end would cross unrelated labels.
    """
    if not lines:
        return []
    starts = np.array([q.start for q in lines])
    ends = np.array([q.end for q in lines])
    back = -arrow.direction
    normal = np.array([-back[1], back[0]])
    t0, t1 = (starts - arrow.tip) @ back, (ends - arrow.tip) @ back
    on = (np.abs((starts - arrow.tip) @ normal) < _ON_CIRCLE) & (
        np.abs((ends - arrow.tip) @ normal) < _ON_CIRCLE
    )
    lo, hi = np.minimum(t0, t1), np.maximum(t0, t1)
    reach, limit = 0.0, _ARROW_LEN[1]  # the first stroke may start at the arrow's base
    while True:
        more = on & (lo <= limit + _ON_CIRCLE) & (hi > reach + _ON_CIRCLE)
        if not more.any():
            break
        reach = float(hi[more].max())
        limit = reach
    if reach < _ARROW_LEN[1]:
        return []
    end = arrow.tip + back * reach
    segments: list[Segment] = [(arrow.tip, end)]
    near = (np.linalg.norm(starts - end, axis=1) < _ON_CIRCLE * 2) | (
        np.linalg.norm(ends - end, axis=1) < _ON_CIRCLE * 2
    )
    short = np.linalg.norm(ends - starts, axis=1) <= _LABEL_REACH
    for k in np.flatnonzero(near & short & ~on):
        segments.append((starts[k], ends[k]))
    return segments


def leader_gap(
    arrow: Arrow, box: tuple[float, float, float, float], lines: list[Primitive]
) -> float | None:
    """Distance from a label box to the arrow's drawn leader; None if the arrow has none.

    A radius or diameter leader starts at its arrow and ends at its text, so the text sits on the
    leader (0) or right beside it. Arrows of other dimensions near the label lead elsewhere.
    """
    segments = drawn_leader(arrow, lines)
    if not segments:
        return None
    return min(_segment_box(a, b, box) for a, b in segments)


def measure_label(
    label: Label,
    kind: DimKind,
    arrows: list[Arrow],
    primitives: list[Primitive],
    expected: float | None = None,
) -> Measurement:
    """Find the geometry a label dimensions. With `expected` (printed value × scale, sheet mm),
    ambiguous candidates are resolved towards it; only used after a first-pass mismatch."""
    c = _center(label.bbox)
    near = [a for a in arrows if np.linalg.norm(a.tip - c) < _LABEL_REACH * 3]
    if kind in ("diameter", "radius"):
        return _measure_round(label, kind, c, near, primitives, expected)
    if kind in ("linear", "chamfer", "thread"):
        return _measure_linear(label, kind, c, near, expected)
    return Measurement(label_id=label.id, kind=kind, sheet_value=None, method="not supported")


def _measure_linear(
    label: Label, kind: DimKind, c: np.ndarray, arrows: list[Arrow], expected: float | None = None
) -> Measurement:
    best: tuple[float, float, int] | None = None  # (distance to label, value, candidates)
    candidates = 0
    for i in range(len(arrows)):
        for j in range(i + 1, len(arrows)):
            a, b = arrows[i], arrows[j]
            axis = b.tip - a.tip
            length = float(np.linalg.norm(axis))
            if length < 0.5:
                continue
            u = axis / length
            # both arrows lie on the line a-b and point along it, in opposite directions
            if abs(abs(float(np.dot(a.direction, u))) - 1) > 0.02:
                continue
            if abs(abs(float(np.dot(b.direction, u))) - 1) > 0.02:
                continue
            if float(np.dot(a.direction, b.direction)) > -0.98:
                continue
            # label must sit next to this dimension line
            t = float(np.dot(c - a.tip, u))
            perp = float(np.linalg.norm((c - a.tip) - t * u))
            outside = max(0.0, -t, t - length)
            distance = perp + outside
            if perp > _LABEL_REACH or outside > _LABEL_REACH:
                continue
            candidates += 1
            rank = distance if expected is None else abs(length - expected) * 100 + distance
            if best is None or rank < best[0]:
                best = (rank, length, candidates)
    if best is None:
        return Measurement(label_id=label.id, kind=kind, sheet_value=None, method="no arrow pair")
    return Measurement(
        label_id=label.id,
        kind=kind,
        sheet_value=best[1],
        method="arrow pair",
        candidates=candidates,
        evidence_distance=best[0],
    )


def _measure_round(
    label: Label,
    kind: DimKind,
    c: np.ndarray,
    arrows: list[Arrow],
    primitives: list[Primitive],
    expected: float | None = None,
) -> Measurement:
    wanted = ("circle",) if kind == "diameter" else ("arc", "circle")
    if kind == "radius":
        bound = _radius_by_leader(label, arrows, primitives)
        if bound is not None:
            return bound
    scored: list[tuple[int, float, Primitive]] = []
    for p in primitives:
        if p.kind not in wanted or p.center is None or p.radius is None:
            continue
        center = np.array(p.center)
        hits = []
        for a in arrows:
            radial = a.tip - center
            distance = float(np.linalg.norm(radial))
            if abs(distance - p.radius) >= _ON_CIRCLE or distance < 1e-9:
                continue
            # the arrow must point along the radius (towards or away from the centre)
            if abs(float(np.dot(a.direction, radial / distance))) > 0.98:
                hits.append(a)
        if not hits:
            continue
        reach = min(float(np.linalg.norm(a.tip - c)) for a in hits)
        value = 2 * p.radius if kind == "diameter" else p.radius
        if expected is not None:
            reach += abs(value - expected) * 100  # prefer the candidate matching the print
        scored.append((-min(len(hits), 2) if expected is None else 0, reach, p))
    # Small holes are often dimensioned between extension lines: an arrow pair whose length
    # equals the diameter of a nearby circle.
    pair = _measure_linear(label, kind, c, arrows, expected)
    if kind == "diameter" and pair.sheet_value is not None:
        matches = [
            q for q in primitives
            if q.kind == "circle" and q.radius is not None
            and abs(2 * q.radius - pair.sheet_value) < _ON_CIRCLE
            and float(np.linalg.norm(np.array(q.center) - c)) < _LABEL_REACH * 2
        ]  # fmt: skip
        radial_reach = min((x[1] for x in scored), default=math.inf)
        if matches and (pair.evidence_distance or 0.0) <= radial_reach:
            return pair.model_copy(
                update={"method": "arrow pair = circle diameter", "primitive_ids": [matches[0].id]}
            )
    if not scored and kind == "diameter" and pair.sheet_value is not None:
        # Section views show a hole as two lines, not a circle: the arrow pair across it is the
        # diameter measurement.
        return pair.model_copy(update={"method": "arrow pair (section view, no circle)"})
    if not scored:
        return Measurement(
            label_id=label.id, kind=kind, sheet_value=None, method="no circle at arrow tip"
        )
    scored.sort(key=lambda x: (x[0], x[1]))
    p = scored[0][2]
    assert p.radius is not None
    value = 2 * p.radius if kind == "diameter" else p.radius
    return Measurement(
        label_id=label.id,
        kind=kind,
        sheet_value=value,
        method=f"{p.kind} through arrow tip",
        primitive_ids=[p.id],
        candidates=len(scored),
        evidence_distance=scored[0][1],
    )


def _on_round(arrow: Arrow, p: Primitive) -> bool:
    """The arrow tip lies on the arc/circle and the arrow points along its radius."""
    assert p.center is not None and p.radius is not None
    radial = arrow.tip - np.array(p.center)
    distance = float(np.linalg.norm(radial))
    if abs(distance - p.radius) >= _ON_CIRCLE or distance < 1e-9:
        return False
    return abs(float(np.dot(arrow.direction, radial / distance))) > 0.98


def _in_span(p: Primitive, point: np.ndarray, tol: float = 3.0) -> bool:
    """Whether the point lies within the arc's angular span (a circle spans everything).
    NX splits one circle into several arcs; the tip belongs to the piece it touches."""
    if p.kind != "arc" or p.start_angle is None or p.sweep_angle is None or p.center is None:
        return True
    angle = math.degrees(math.atan2(point[1] - p.center[1], point[0] - p.center[0]))
    delta = (angle - p.start_angle) % 360 if p.sweep_angle >= 0 else (p.start_angle - angle) % 360
    return delta <= abs(p.sweep_angle) + tol or delta >= 360 - tol


def _radius_by_leader(
    label: Label, arrows: list[Arrow], primitives: list[Primitive]
) -> Measurement | None:
    """The arc a radius label points to: its leader runs from the label to an arrow whose tip
    sits radially on that arc. Proximity alone binds a label to a neighbouring arc when two radii
    are close together, so the leader wins whenever there is one (#91)."""
    lines = [q for q in primitives if q.kind == "line" and q.start and q.end]
    leaders: dict[int, list[Segment]] = {}
    # Ranking: on the leader (0.5 mm steps), then the leader ending at the label rather than
    # passing it, then the arc piece holding the tip, then the fit. Never fit noise alone.
    hits: list[tuple[float, float, bool, float, Primitive, float]] = []
    for p in primitives:
        if p.kind not in ("arc", "circle") or p.center is None or p.radius is None:
            continue
        for i, a in enumerate(arrows):
            if not _on_round(a, p):
                continue
            if i not in leaders:
                leaders[i] = drawn_leader(a, lines)
            if not leaders[i]:
                continue
            gap = min(_segment_box(x, y, label.bbox) for x, y in leaders[i])
            if gap > _LEADER_GAP:
                continue
            far = min(_point_box(q, label.bbox) for seg in leaders[i] for q in seg[1:])
            off = abs(float(np.linalg.norm(a.tip - np.array(p.center))) - p.radius)
            hits.append(
                (round(gap * 2) / 2, round(far * 2) / 2, not _in_span(p, a.tip), off, p, gap)
            )
    if not hits:
        return None
    *_, p, gap = min(hits, key=lambda h: h[:4])
    assert p.radius is not None
    return Measurement(
        label_id=label.id,
        kind="radius",
        sheet_value=p.radius,
        method=f"{p.kind} at the label's leader arrow",
        primitive_ids=[p.id],
        candidates=len({h[4].id for h in hits}),
        evidence_distance=gap,
        leader=True,
    )


# --------------------------------------------------------------------------- consistency


class DimensionClaim(BaseModel):
    """What the agent read for one label."""

    label_id: str
    text: str
    reading_confidence: float = Field(default=0.9, ge=0, le=1)
    underlined: bool = False  # "not to scale" by convention: never used for the scale estimate


class ChainClaim(BaseModel):
    """Partial dimensions that should add up to a total (e.g. 15 + 15 + 15 = 45)."""

    part_label_ids: list[str] = Field(min_length=2)
    total_label_id: str


class GapRequest(BaseModel):
    """A dimension that is needed but not printed: measured from the drawing geometry."""

    id: str
    kind: Literal["linear", "diameter", "radius"]
    view_id: str | None = None
    sheet_value: float = Field(gt=0)  # from measure/find_geometry, sheet mm
    reason: str = Field(min_length=1)


class ScaleEstimate(BaseModel):
    view_id: str | None
    scale: float | None  # sheet / part; 2.0 for a 2:1 view
    samples: int
    spread: float | None  # relative standard deviation of the ratios
    sheet_scale: float | None  # from the title block, if given
    agrees_with_sheet: bool | None


class ConsistencyReport(BaseModel):
    scales: list[ScaleEstimate]
    measurements: list[Measurement]
    dimensions: list[Dimension]
    review_items: list[ReviewItem]


def _snap_scale(value: float) -> float:
    """Snap to a standard drawing scale when close (ISO 5455)."""
    standard = [50, 20, 10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50]
    nearest = min(standard, key=lambda s: abs(math.log(value / s)))
    return nearest if abs(value / nearest - 1) < 0.02 else value


def check_consistency(
    labels: dict[str, Label],
    arrows: list[Arrow],
    primitives: list[Primitive],
    claims: list[DimensionClaim],
    *,
    sheet_scale: float | None = None,
    chains: list[ChainClaim] | None = None,
    gaps: list[GapRequest] | None = None,
) -> ConsistencyReport:
    items: list[ReviewItem] = []
    measurements: list[Measurement] = []
    parsed: dict[str, ParsedText] = {}

    def review(kind: str, severity: str, message: str, reason: str, dims: list[str]) -> None:
        items.append(
            ReviewItem(
                id=f"r-{len(items) + 1}",
                kind=kind,
                severity=severity,
                message=message,
                reason=reason,
                dimension_ids=dims,
            )
        )

    for claim in claims:
        if claim.label_id not in labels:
            raise KeyError(f"unknown label {claim.label_id!r}")
        p = parse_dimension_text(claim.text)
        parsed[claim.label_id] = p
        if p.kind == "other":
            if not any(ch.isdigit() for ch in claim.text):
                continue  # lettering (view names like A-A, note words), not a dimension
            review(
                "unreadable",
                "warning",
                f"Could not interpret '{claim.text}'",
                "text is not a known dimension format",
                [f"d-{claim.label_id}"],
            )
            continue
        measurements.append(measure_label(labels[claim.label_id], p.kind, arrows, primitives))
    by_label = {m.label_id: m for m in measurements}

    # scale per view from linear/diameter/radius dimensions that were measured unambiguously
    ratios: dict[str | None, list[float]] = {}
    for claim in claims:
        p, m = parsed[claim.label_id], by_label.get(claim.label_id)
        if claim.underlined or m is None or m.sheet_value is None or not p.value:
            continue
        if p.kind in ("linear", "diameter", "radius") and m.candidates <= 3:
            ratios.setdefault(labels[claim.label_id].view_id, []).append(m.sheet_value / p.value)
    scales: dict[str | None, float | None] = {}
    estimates = []
    for view_id, values in sorted(ratios.items(), key=lambda kv: str(kv[0])):
        median = statistics.median(values)
        inliers = [v for v in values if abs(v / median - 1) < 0.02]
        scale = _snap_scale(statistics.mean(inliers)) if inliers else None
        spread = statistics.pstdev(values) / median if len(values) > 1 else 0.0
        agrees = (
            None if sheet_scale is None or scale is None else abs(scale / sheet_scale - 1) < 0.02
        )
        scales[view_id] = scale
        estimates.append(
            ScaleEstimate(
                view_id=view_id, scale=scale, samples=len(values), spread=round(spread, 4),
                sheet_scale=sheet_scale, agrees_with_sheet=agrees,
            )
        )  # fmt: skip
        if agrees is False:
            review(
                "mismatch",
                "warning",
                f"View {view_id} measures at scale {scale:.3g}",
                f"title block scale is {sheet_scale:g}; detail views may differ (check label)",
                [],
            )

    # Second pass: where several geometries were plausible and the first pick disagrees with the
    # print, choose among the candidates the one matching the printed value (association only;
    # the printed value stays authoritative, D12).
    rematched: set[str] = set()
    for claim in claims:
        p, m = parsed[claim.label_id], by_label.get(claim.label_id)
        label = labels[claim.label_id]
        scale = scales.get(label.view_id) or sheet_scale
        if m is None or m.sheet_value is None or not p.value or not scale or m.candidates < 2:
            continue
        if m.leader:
            continue  # the label's own leader names the geometry: a mismatch stays a mismatch
        if p.kind not in ("linear", "diameter", "radius"):
            continue
        tol = max(MISMATCH_REL * p.value, MISMATCH_ABS)
        if abs(m.sheet_value / scale - p.value) <= tol:
            continue
        again = measure_label(label, p.kind, arrows, primitives, expected=p.value * scale)
        if again.sheet_value is not None and abs(again.sheet_value / scale - p.value) <= tol:
            again.method += f" (matched to print among {again.candidates} candidates)"
            by_label[claim.label_id] = again
            measurements[measurements.index(m)] = again
            rematched.add(claim.label_id)

    dimensions: list[Dimension] = []
    for claim in claims:
        p, m = parsed[claim.label_id], by_label.get(claim.label_id)
        if p.kind == "other" or p.value is None:
            continue
        label = labels[claim.label_id]
        dim_id = f"d-{claim.label_id}"
        scale = scales.get(label.view_id) or sheet_scale
        measured = None
        if m is not None and m.sheet_value is not None and scale:
            measured = m.sheet_value if p.kind == "angle" else m.sheet_value / scale
        confidence = 0.9 * claim.reading_confidence  # vector input path, as read
        status, reason = "auto_ok", None
        if measured is None:
            confidence *= 0.8
            if p.kind in ("linear", "diameter", "radius"):
                status, reason = (
                    "needs_review",
                    f"no matching geometry found ({m.method if m else 'n/a'})",
                )
        elif not claim.underlined:
            tol = max(MISMATCH_REL * p.value, MISMATCH_ABS)
            if abs(measured - p.value) > tol:
                confidence *= 0.5
                status = "needs_review"
                if m is not None and m.leader:
                    # The feature this label points to has another size: building the printed
                    # value there, or on the arc that does match, would both be a guess.
                    reason = (
                        f"printed R{p.value:g} but the arc its leader points to measures "
                        f"R{measured:.3g} (tol ±{tol:.2g})"
                    )
                    message = f"Radius '{claim.text}' does not match the arc it points to"
                else:
                    reason = (
                        f"printed {p.value:g} but the geometry measures {measured:.3g} "
                        f"(tol ±{tol:.2g})"
                    )
                    message = f"Dimension '{claim.text}' disagrees with the drawing"
                review(
                    "mismatch",
                    "warning",
                    message,
                    reason + "; printed value is used (D12)",
                    [dim_id],
                )
            elif claim.label_id in rematched:
                confidence *= 0.95  # confirmed, but the association needed the printed value
            else:
                confidence = min(1.0, confidence * 1.1)
        if claim.underlined:
            review(
                "assumption",
                "info",
                f"'{claim.text}' is underlined (not to scale)",
                "excluded from the scale estimate",
                [dim_id],
            )
        if m is not None and m.candidates > 3:
            confidence *= 0.8
        dimensions.append(
            Dimension(
                id=dim_id,
                kind=p.kind,
                stated=p.value,
                stated_text=claim.text,
                measured=None if measured is None else round(measured, 4),
                value=p.value,
                tolerance=p.tolerance,
                source=SourceRef(view_id=label.view_id, bbox=label.bbox),
                method="vlm",
                confidence=round(confidence, 3),
                status=status,
                reason=reason,
            )
        )
    dims_by_label = {d.id.removeprefix("d-"): d for d in dimensions}

    for chain in chains or []:
        parts = [dims_by_label.get(i) for i in chain.part_label_ids]
        total = dims_by_label.get(chain.total_label_id)
        if total is None or any(d is None for d in parts):
            review(
                "unreadable",
                "warning",
                "Chain check skipped",
                "a dimension of the chain is missing",
                [],
            )
            continue
        s = sum(d.value for d in parts if d is not None)
        if abs(s - total.value) > max(MISMATCH_REL * total.value, MISMATCH_ABS):
            review(
                "mismatch",
                "warning",
                f"Chain sums to {s:g}, total says {total.value:g}",
                "partial dimensions do not add up to the overall dimension",
                [d.id for d in parts if d is not None] + [total.id],
            )

    for claim in claims:
        p = parsed[claim.label_id]
        if p.count > 1 and p.kind == "diameter" and p.value:
            label = labels[claim.label_id]
            scale = scales.get(label.view_id) or sheet_scale or 1.0
            found = sum(
                1 for q in primitives
                if q.kind == "circle" and q.radius is not None and q.view_id == label.view_id
                and abs(2 * q.radius / scale - p.value) <= max(MISMATCH_REL * p.value, MISMATCH_ABS)
            )  # fmt: skip
            if found != p.count:
                review(
                    "mismatch",
                    "warning",
                    f"'{claim.text}': {found} circles of Ø{p.value:g} found in view",
                    f"pattern count says {p.count}",
                    [f"d-{claim.label_id}"],
                )

    for gap in gaps or []:
        scale = scales.get(gap.view_id) or sheet_scale
        if not scale:
            review(
                "unreadable",
                "blocking",
                f"Cannot fill {gap.id}: unknown scale",
                "read the title block scale or more dimensions of this view first",
                [gap.id],
            )
            continue
        value = round(gap.sheet_value / scale, 3)
        dimensions.append(
            Dimension(
                id=gap.id,
                kind=gap.kind,
                stated=None,
                measured=value,
                value=value,
                method="measured",
                confidence=0.6,
                status="needs_review",
                reason=f"not printed on the drawing; measured from geometry at scale {scale:g}: "
                f"{gap.reason}",
                source=SourceRef(view_id=gap.view_id, bbox=(0, 0, 0, 0)),
            )
        )
        review(
            "assumption",
            "warning",
            f"{gap.id} = {value:g} mm measured, not printed",
            gap.reason,
            [gap.id],
        )

    # A label in the drawing area nobody read cannot be used by the plan: if it is a dimension,
    # its feature may be missing or built without it, so it must not pass silently.
    claimed = {claim.label_id for claim in claims}
    for label in labels.values():
        if label.region != "drawing" or label.id in claimed:
            continue
        items.append(
            ReviewItem(
                id=f"r-{len(items) + 1}",
                kind="unreadable",
                severity="warning",
                message=f"Label {label.id} was not read"
                + (f" ('{label.text}' was not checked)" if label.text else ""),
                reason="no reading of this label was checked, so no feature can use it; if it is "
                "a dimension, the feature it sizes may be missing or built without it",
                dimension_ids=[f"d-{label.id}"],
                sheet_ref=SourceRef(view_id=label.view_id, bbox=label.crop_bbox),
            )
        )

    return ConsistencyReport(
        scales=estimates, measurements=measurements, dimensions=dimensions, review_items=items
    )
