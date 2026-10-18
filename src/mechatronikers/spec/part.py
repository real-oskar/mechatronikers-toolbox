"""Part specification: what was read from the drawing and how the part is built (docs/03).

Conventions:
- Millimetres and degrees. Part frame right-handed, Z up; turned parts rotate about Z.
- Sheet coordinates: millimetres from the top-left of the sheet, at print scale.
- Profiles are 2D sketches in the (u, v) coordinates of their placement plane.
- A hole's placement origin lies on the entry face; its normal points into the material.
"""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# POI types live in spec.poi (shared by every run kind); re-exported here for older imports.
from mechatronikers.spec.poi import ModelRef as ModelRef
from mechatronikers.spec.poi import ReviewAction as ReviewAction
from mechatronikers.spec.poi import ReviewItem as ReviewItem
from mechatronikers.spec.poi import SourceRef as SourceRef
from mechatronikers.spec.poi import Status as Status
from mechatronikers.spec.poi import Vec3 as Vec3
from mechatronikers.spec.validation import ExpectedGeometry, ExpectedHole, Size3

Vec2 = tuple[float, float]

_CLOSE = 1e-6  # mm; profile segments must meet within this distance

# ISO metric coarse threads: nominal → tap drill diameter. NX models tapped holes at tap drill
# diameter (e.g. M5 → Ø4.2 in Traegerplatte), so threaded holes are built that way (T4, D43).
TAP_DRILL = {
    "M2": 1.6, "M2.5": 2.05, "M3": 2.5, "M4": 3.3, "M5": 4.2, "M6": 5.0,
    "M8": 6.8, "M10": 8.5, "M12": 10.2, "M14": 12.0, "M16": 14.0, "M20": 17.5,
}  # fmt: skip


def tap_drill_diameter(thread: str) -> float:
    key = thread.strip().upper().split("X")[0].replace(",", ".")
    if key not in TAP_DRILL:
        raise ValueError(f"unknown thread {thread!r}; known: {', '.join(TAP_DRILL)}")
    return TAP_DRILL[key]


# --------------------------------------------------------------------------- dimensions


class Tolerance(BaseModel):
    plus: float
    minus: float  # positive number: value - minus is the lower limit
    fit: str | None = None  # e.g. "H7"


class Dimension(BaseModel):
    id: str
    kind: Literal["linear", "diameter", "radius", "angle", "thread", "chamfer", "other"]
    stated: float | None = None  # printed value; None = not on the drawing
    stated_text: str | None = None  # raw label, e.g. "3x Ø12", "M10", "5x45°"
    measured: float | None = None  # from drawing geometry, already divided by the view scale
    value: float  # what the part is built with
    tolerance: Tolerance | None = None
    source: SourceRef | None = None
    method: Literal["vector", "ocr", "vlm", "measured", "assumed", "human"]
    confidence: float = Field(ge=0.0, le=1.0)
    status: Status = "auto_ok"
    reason: str | None = None
    feature_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> "Dimension":
        if self.stated is None and self.method not in ("measured", "assumed", "human"):
            raise ValueError(
                f"{self.id}: no printed value, so method must be measured/assumed/human"
            )
        if self.method in ("measured", "assumed") and not self.reason:
            raise ValueError(f"{self.id}: a {self.method} value needs a reason (metric M3)")
        if self.status == "needs_review" and not self.reason:
            raise ValueError(f"{self.id}: needs_review requires a reason")
        if self.method in ("measured", "assumed") and self.status == "auto_ok":
            raise ValueError(f"{self.id}: {self.method} values must be reviewed, not auto_ok")
        return self


# --------------------------------------------------------------------------- geometry


class Placement(BaseModel):
    """A plane/axis in the part frame. `x_dir` fixes the sketch u-axis (default: derived)."""

    origin: Vec3 = (0.0, 0.0, 0.0)
    normal: Vec3 = (0.0, 0.0, 1.0)
    x_dir: Vec3 | None = None

    @model_validator(mode="after")
    def _unit(self) -> "Placement":
        if math.hypot(*self.normal) < 1e-12:
            raise ValueError("placement normal must not be zero")
        if self.x_dir is not None:
            if math.hypot(*self.x_dir) < 1e-12:
                raise ValueError("placement x_dir must not be zero")
            dot = sum(a * b for a, b in zip(self.normal, self.x_dir, strict=True))
            if abs(dot) > 1e-6 * math.hypot(*self.normal) * math.hypot(*self.x_dir):
                raise ValueError("placement x_dir must be perpendicular to normal")
        return self


class Line(BaseModel):
    kind: Literal["line"] = "line"
    start: Vec2
    end: Vec2


class Arc(BaseModel):
    """Three-point arc from `start` through `mid` to `end`."""

    kind: Literal["arc"] = "arc"
    start: Vec2
    mid: Vec2
    end: Vec2


class Circle(BaseModel):
    kind: Literal["circle"] = "circle"
    center: Vec2
    radius: float = Field(gt=0)


Segment = Annotated[Line | Arc | Circle, Field(discriminator="kind")]


class Profile(BaseModel):
    """Closed 2D outline: either one circle, or lines/arcs where each segment starts at the
    previous one's end and the last ends at the first one's start. Optional inner loops cut
    holes into the face (e.g. a ring)."""

    segments: list[Segment] = Field(min_length=1)
    inner: list["Profile"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _closed(self) -> "Profile":
        if any(isinstance(s, Circle) for s in self.segments):
            if len(self.segments) != 1:
                raise ValueError("a circle must be the only segment of its profile")
            return self
        chain: list[Line | Arc] = [s for s in self.segments if not isinstance(s, Circle)]
        for prev, nxt in zip(chain, chain[1:] + chain[:1], strict=True):
            if math.dist(prev.end, nxt.start) > _CLOSE:
                raise ValueError(f"profile not closed: {prev.end} does not meet {nxt.start}")
        return self

    def bounds(self) -> tuple[float, float, float, float]:
        """(umin, vmin, umax, vmax) of the outer loop (arcs approximated by their 3 points)."""
        pts: list[Vec2] = []
        for s in self.segments:
            if isinstance(s, Circle):
                (cu, cv), r = s.center, s.radius
                pts += [(cu - r, cv - r), (cu + r, cv + r)]
            elif isinstance(s, Arc):
                pts += [s.start, s.mid, s.end]
            else:
                pts += [s.start, s.end]
        us, vs = [p[0] for p in pts], [p[1] for p in pts]
        return min(us), min(vs), max(us), max(vs)


def rectangle(width: float, height: float, center: Vec2 = (0.0, 0.0)) -> Profile:
    cu, cv = center
    w, h = width / 2, height / 2
    corners = [(cu - w, cv - h), (cu + w, cv - h), (cu + w, cv + h), (cu - w, cv + h)]
    return Profile(
        segments=[
            Line(start=a, end=b) for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
        ]
    )


def circle(diameter: float, center: Vec2 = (0.0, 0.0)) -> Profile:
    return Profile(segments=[Circle(center=center, radius=diameter / 2)])


# --------------------------------------------------------------------------- features


class _Feature(BaseModel):
    id: str
    dimension_ids: list[str] = Field(default_factory=list)
    status: Status = "auto_ok"
    notes: str | None = None


class ExtrudeBase(_Feature):
    """Base body: `profile` on `placement`, extruded along its normal."""

    type: Literal["extrude_base"] = "extrude_base"
    profile: Profile
    depth: float = Field(gt=0)
    direction: Literal["positive", "symmetric"] = "positive"
    placement: Placement = Placement()


class RevolveBase(_Feature):
    """Turned base body: `profile` in the XZ half-plane (u = radius ≥ 0, v = z), about Z."""

    type: Literal["revolve_base"] = "revolve_base"
    profile: Profile
    angle: float = Field(default=360.0, gt=0, le=360)

    @model_validator(mode="after")
    def _half_plane(self) -> "RevolveBase":
        if self.profile.bounds()[0] < -_CLOSE:
            raise ValueError("revolve profile must lie in u >= 0 (radius)")
        return self


HoleType = Literal["through", "blind", "counterbore", "countersink", "threaded"]


class HoleParams(BaseModel):
    hole_type: HoleType = "through"
    diameter: float = Field(gt=0)  # threaded: tap drill diameter (see tap_drill_diameter)
    depth: float | None = Field(default=None, gt=0)  # None = through all
    cb_diameter: float | None = None
    cb_depth: float | None = None
    cs_diameter: float | None = None
    cs_angle: float = 90.0
    thread: str | None = None  # e.g. "M5"; cosmetic attribute
    thread_depth: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "HoleParams":
        if self.hole_type == "blind" and self.depth is None:
            raise ValueError("blind hole needs a depth")
        if self.hole_type == "counterbore" and not (self.cb_diameter and self.cb_depth):
            raise ValueError("counterbore needs cb_diameter and cb_depth")
        if self.hole_type == "countersink" and not self.cs_diameter:
            raise ValueError("countersink needs cs_diameter")
        if self.hole_type == "threaded" and not self.thread:
            raise ValueError("threaded hole needs a thread designation (e.g. 'M5')")
        for name in ("cb_diameter", "cs_diameter"):
            outer = getattr(self, name)
            if outer is not None and outer <= self.diameter:
                raise ValueError(f"{name} must be larger than the hole diameter")
        return self


class Hole(_Feature, HoleParams):
    type: Literal["hole"] = "hole"
    placement: Placement  # origin on the entry face, normal pointing into the material


class CircularPattern(BaseModel):
    kind: Literal["circular"] = "circular"
    center: Vec3
    axis: Vec3 = (0.0, 0.0, 1.0)
    count: int = Field(ge=2)
    step_angle: float | None = None  # degrees between instances; default 360 / count

    def angle(self) -> float:
        return self.step_angle if self.step_angle is not None else 360.0 / self.count


class LinearPattern(BaseModel):
    kind: Literal["linear"] = "linear"
    direction_1: Vec3 = (1.0, 0.0, 0.0)
    count_1: int = Field(default=1, ge=1)
    pitch_1: float = 0.0
    direction_2: Vec3 = (0.0, 1.0, 0.0)
    count_2: int = Field(default=1, ge=1)
    pitch_2: float = 0.0


class HolePattern(_Feature):
    """`seed` is the first instance; the pattern places the others."""

    type: Literal["hole_pattern"] = "hole_pattern"
    seed: Hole
    pattern: Annotated[CircularPattern | LinearPattern, Field(discriminator="kind")]

    def instances(self) -> list[Placement]:
        base = self.seed.placement
        if isinstance(self.pattern, CircularPattern):
            p = self.pattern
            return [
                Placement(
                    origin=_rotate(base.origin, p.center, p.axis, i * p.angle()),
                    normal=_rotate(base.normal, (0, 0, 0), p.axis, i * p.angle()),
                    x_dir=None
                    if base.x_dir is None
                    else _rotate(base.x_dir, (0, 0, 0), p.axis, i * p.angle()),
                )
                for i in range(p.count)
            ]
        p2 = self.pattern
        d1, d2 = _unit(p2.direction_1), _unit(p2.direction_2)
        return [
            base.model_copy(
                update={
                    "origin": tuple(
                        o + i * p2.pitch_1 * a + j * p2.pitch_2 * b
                        for o, a, b in zip(base.origin, d1, d2, strict=True)
                    )
                }
            )
            for i in range(p2.count_1)
            for j in range(p2.count_2)
        ]


class Pocket(_Feature):
    """Material removal: `profile` on `placement` cut `depth` along the normal (None = through)."""

    type: Literal["pocket"] = "pocket"
    profile: Profile
    depth: float | None = Field(default=None, gt=0)
    placement: Placement


class Boss(_Feature):
    """Material addition: `profile` on `placement` extruded `height` along the normal."""

    type: Literal["boss"] = "boss"
    profile: Profile
    height: float = Field(gt=0)
    placement: Placement


class Slot(_Feature):
    """Straight slot: centre-to-centre `length` along placement x_dir, `width`, `depth`."""

    type: Literal["slot"] = "slot"
    length: float = Field(gt=0)
    width: float = Field(gt=0)
    depth: float | None = Field(default=None, gt=0)  # None = through
    placement: Placement  # origin = slot centre on the entry face


class EdgeSelector(BaseModel):
    """Which edges a fillet/chamfer applies to (T10, D44).

    - "near_points": for each point, the edge closest to it (points taken from the drawing)
    - "parallel": all edges parallel to `axis`, optionally only those inside `within`
    - "all": every edge (optionally inside `within`)
    """

    by: Literal["near_points", "parallel", "all"]
    points: list[Vec3] = Field(default_factory=list)
    axis: Vec3 | None = None
    within: tuple[Vec3, Vec3] | None = None  # axis-aligned box (min corner, max corner)

    @model_validator(mode="after")
    def _args(self) -> "EdgeSelector":
        if self.by == "near_points" and not self.points:
            raise ValueError("near_points selector needs points")
        if self.by == "parallel" and self.axis is None:
            raise ValueError("parallel selector needs an axis")
        return self


class Fillet(_Feature):
    type: Literal["fillet"] = "fillet"
    radius: float = Field(gt=0)
    edges: EdgeSelector


class Chamfer(_Feature):
    type: Literal["chamfer"] = "chamfer"
    distance: float = Field(gt=0)
    distance_2: float | None = Field(default=None, gt=0)  # None = symmetric (e.g. 1x45°)
    edges: EdgeSelector


class ScriptFeature(_Feature):
    """Fallback: build123d code applied to `part` (the solid so far); always needs review."""

    type: Literal["script"] = "script"
    code: str
    reason: str = Field(min_length=1)
    status: Status = "needs_review"


Feature = Annotated[
    ExtrudeBase
    | RevolveBase
    | Hole
    | HolePattern
    | Pocket
    | Boss
    | Slot
    | Fillet
    | Chamfer
    | ScriptFeature,
    Field(discriminator="type"),
]
BASE_TYPES = ("extrude_base", "revolve_base")


# --------------------------------------------------------------------------- drawing context


class View(BaseModel):
    id: str
    kind: Literal["front", "top", "side", "bottom", "back", "section", "detail", "iso", "other"]
    scale: float = 1.0
    bbox: tuple[float, float, float, float] | None = None  # sheet mm
    label: str | None = None  # e.g. "A-A", "A (2:1)"
    # Part-frame axes shown along the sheet's right and up directions, e.g. ("+X", "+Z").
    axes: tuple[str, str] | None = None


class SheetInfo(BaseModel):
    format: str | None = None  # e.g. "A3"
    scale: float = 1.0  # title block scale (2:1 → 2.0)
    title: str | None = None
    title_block: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)  # e.g. "nicht bemaßte Radien R=1"


# --------------------------------------------------------------------------- part spec


class PartSpec(BaseModel):
    spec_version: str
    part_name: str
    source_file: str | None = None
    source_sha256: str | None = None
    sheet: SheetInfo = SheetInfo()
    views: list[View] = Field(default_factory=list)
    # Axis-aligned extents in the part frame; per axis None = no printed overall dimension.
    overall_size: Size3 | None = None
    overall_size_dims: tuple[str | None, str | None, str | None] | None = None  # provenance
    dimensions: list[Dimension] = Field(default_factory=list)
    features: list[Feature] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    general_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> "PartSpec":
        ids = [d.id for d in self.dimensions] + [f.id for f in self.features]
        ids += [r.id for r in self.review_items]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate ids: {sorted(duplicates)}")
        # Dimension.feature_ids are back-links and may name features not built yet.
        dim_ids = {d.id for d in self.dimensions}
        unknown = {i for i in self.overall_size_dims or () if i is not None} - dim_ids
        if unknown:
            raise ValueError(f"overall size references unknown dimensions {sorted(unknown)}")
        for f in self.features:
            missing = set(f.dimension_ids) - dim_ids
            if missing:
                raise ValueError(f"feature {f.id} references unknown dimensions {sorted(missing)}")
        if self.features:
            if self.features[0].type not in BASE_TYPES:
                raise ValueError(
                    "the first feature must be a base body (extrude_base/revolve_base)"
                )
            if any(f.type in BASE_TYPES for f in self.features[1:]):
                raise ValueError("only the first feature may be a base body")
        return self

    def missing_dimensions(self) -> list[Dimension]:
        """Dimensions not printed on the drawing (metric M3: each needs a reason)."""
        return [d for d in self.dimensions if d.stated is None]

    def expected_geometry(self) -> ExpectedGeometry:
        """V2 targets: overall size and every hole the features create (patterns expanded)."""
        holes: list[ExpectedHole] = []
        for f in self.features:
            if isinstance(f, Hole):
                holes += _expected_holes(f.id, f, f.placement, f.id)
            elif isinstance(f, HolePattern):
                for i, placement in enumerate(f.instances()):
                    holes += _expected_holes(f"{f.id}.{i}", f.seed, placement, f.id)
        return ExpectedGeometry(bbox_size=self.overall_size, holes=holes)


def _expected_holes(
    id: str, params: HoleParams, placement: Placement, feature_id: str
) -> list[ExpectedHole]:
    """A counterbore shows up as two cylinders: the hole and the wider counterbore."""
    diameters = [(id, params.diameter)]
    if params.hole_type == "counterbore" and params.cb_diameter is not None:
        diameters.append((f"{id}.cb", params.cb_diameter))
    return [
        ExpectedHole(
            id=hole_id,
            diameter=diameter,
            center=placement.origin,
            axis=placement.normal,
            feature_ids=[feature_id],
        )
        for hole_id, diameter in diameters
    ]


def _unit(v: Vec3) -> Vec3:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n, v[2] / n)


def _rotate(point: Vec3, center: Vec3, axis: Vec3, degrees: float) -> Vec3:
    """Rodrigues rotation of `point` about the line through `center` along `axis`."""
    k = _unit(axis)
    p = tuple(a - c for a, c in zip(point, center, strict=True))
    t = math.radians(degrees)
    cos, sin = math.cos(t), math.sin(t)
    dot = sum(a * b for a, b in zip(k, p, strict=True))
    cross = (k[1] * p[2] - k[2] * p[1], k[2] * p[0] - k[0] * p[2], k[0] * p[1] - k[1] * p[0])
    rotated = tuple(
        p[i] * cos + cross[i] * sin + k[i] * dot * (1 - cos) + center[i] for i in range(3)
    )
    return (rotated[0], rotated[1], rotated[2])
