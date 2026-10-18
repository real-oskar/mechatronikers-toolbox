"""Feature list → solid (docs/05). Runs inside the build runner, never in a server process.

Every feature is applied in order. Errors name the feature that caused them, and cutting or
adding features that change nothing are errors too (usually a wrong placement or normal), so a
model can correct exactly that feature.
"""

import math
from typing import Any

import build123d as bd
from pydantic import TypeAdapter

from mechatronikers.spec.part import (
    Arc,
    Boss,
    Chamfer,
    Circle,
    EdgeSelector,
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
    RevolveBase,
    ScriptFeature,
    Slot,
)

_FEATURES: TypeAdapter[list[Feature]] = TypeAdapter(list[Feature])
_ENTRY_OFFSET = 0.01  # mm; cutting tools start this far outside the entry face
_MIN_CHANGE = 1e-6  # relative volume change below which a feature "did nothing"


class FeatureBuildError(RuntimeError):
    def __init__(self, feature_id: str, message: str) -> None:
        super().__init__(f"feature {feature_id}: {message}")
        self.feature_id = feature_id


def plane(placement: Placement) -> bd.Plane:
    """Sketch plane of a placement; without x_dir, world X (or Y if X is parallel) projected."""
    normal = bd.Vector(*placement.normal).normalized()
    if placement.x_dir is not None:
        x_dir = bd.Vector(*placement.x_dir)
    else:
        ref = bd.Vector(1, 0, 0) if abs(normal.X) < 0.9 else bd.Vector(0, 1, 0)
        x_dir = ref - normal * ref.dot(normal)
    return bd.Plane(origin=bd.Vector(*placement.origin), x_dir=x_dir.normalized(), z_dir=normal)


def _wire(profile: Profile, pl: bd.Plane) -> bd.Wire:
    edges: list[bd.Edge] = []
    for s in profile.segments:
        if isinstance(s, Circle):
            center = pl.from_local_coords(bd.Vector(s.center[0], s.center[1], 0))
            edges.append(bd.Edge.make_circle(s.radius, pl.move(bd.Location(center - pl.origin))))
        elif isinstance(s, Arc):
            points = [pl.from_local_coords(bd.Vector(u, v, 0)) for u, v in (s.start, s.mid, s.end)]
            edges.append(bd.Edge.make_three_point_arc(*points))
        else:
            assert isinstance(s, Line)
            a = pl.from_local_coords(bd.Vector(s.start[0], s.start[1], 0))
            b = pl.from_local_coords(bd.Vector(s.end[0], s.end[1], 0))
            edges.append(bd.Edge.make_line(a, b))
    return bd.Wire(edges)


def face(profile: Profile, pl: bd.Plane) -> bd.Face:
    outer = _wire(profile, pl)
    inner = [_wire(loop, pl) for loop in profile.inner]
    return bd.Face(outer, inner) if inner else bd.Face(outer)


def _size(part: bd.Shape[Any]) -> float:
    return max(part.bounding_box().diagonal, 1.0)


def _cutter(params: HoleParams, placement: Placement, reach: float) -> bd.Shape[Any]:
    """Tool solid for one hole; starts slightly outside the entry face."""
    pl = plane(placement)
    start = pl.offset(-_ENTRY_OFFSET)
    length = (params.depth if params.depth is not None else reach) + _ENTRY_OFFSET
    tool: bd.Shape[Any] = bd.extrude(
        bd.Face(bd.Wire([bd.Edge.make_circle(params.diameter / 2, start)])), amount=length
    )
    if params.hole_type == "counterbore":
        assert params.cb_diameter is not None and params.cb_depth is not None
        cb_face = bd.Face(bd.Wire([bd.Edge.make_circle(params.cb_diameter / 2, start)]))
        tool = tool + bd.extrude(cb_face, amount=params.cb_depth + _ENTRY_OFFSET)
    elif params.hole_type == "countersink":
        assert params.cs_diameter is not None
        r_top, r_bottom = params.cs_diameter / 2, params.diameter / 2
        height = (r_top - r_bottom) / math.tan(math.radians(params.cs_angle / 2))
        cone = bd.Solid.make_cone(r_top, r_bottom, height + _ENTRY_OFFSET, start)
        tool = tool + cone
    return tool


def _changed(before: float, after: float) -> bool:
    return abs(after - before) > _MIN_CHANGE * max(before, 1.0)


def _cut(part: bd.Shape[Any], tool: bd.Shape[Any], feature_id: str) -> bd.Shape[Any]:
    before = part.volume
    result = part - tool
    if not _changed(before, result.volume):
        raise FeatureBuildError(feature_id, "removed no material; check placement and normal")
    return result


def _add(part: bd.Shape[Any], tool: bd.Shape[Any], feature_id: str) -> bd.Shape[Any]:
    before = part.volume
    result = part + tool
    if not _changed(before, result.volume):
        raise FeatureBuildError(feature_id, "added no material; check placement and normal")
    return result


def select_edges(part: bd.Shape[Any], selector: EdgeSelector) -> list[bd.Edge]:
    edges = list(part.edges())
    if selector.within is not None:
        lo, hi = selector.within
        edges = [
            e
            for e in edges
            if all(
                lo[i] - 1e-6 <= c <= hi[i] + 1e-6
                for i, c in enumerate((e.center().X, e.center().Y, e.center().Z))
            )
        ]
    if selector.by == "all":
        return edges
    if selector.by == "parallel":
        assert selector.axis is not None
        axis = bd.Vector(*selector.axis).normalized()
        return [
            e
            for e in edges
            if e.geom_type == bd.GeomType.LINE
            and abs(abs((e.end_point() - e.start_point()).normalized().dot(axis)) - 1) < 1e-6
        ]
    chosen: list[bd.Edge] = []
    for point in selector.points:
        target = bd.Vector(*point)
        nearest = min(edges, key=lambda e: e.distance_to(target))
        if nearest not in chosen:
            chosen.append(nearest)
    return chosen


def apply_feature(part: bd.Shape[Any] | None, feature: Feature) -> bd.Shape[Any]:
    if isinstance(feature, ExtrudeBase):
        pl = plane(feature.placement)
        f = face(feature.profile, pl)
        if feature.direction == "symmetric":
            return bd.extrude(f, amount=feature.depth / 2, both=True)
        return bd.extrude(f, amount=feature.depth)
    if isinstance(feature, RevolveBase):
        f = face(feature.profile, bd.Plane.XZ)  # local (u, v) = world (X, Z)
        return bd.revolve(f, axis=bd.Axis.Z, revolution_arc=feature.angle)
    if part is None:
        raise FeatureBuildError(feature.id, "needs a base body first")
    reach = 2 * _size(part)
    if isinstance(feature, Hole):
        return _cut(part, _cutter(feature, feature.placement, reach), feature.id)
    if isinstance(feature, HolePattern):
        for i, placement in enumerate(feature.instances()):
            part = _cut(part, _cutter(feature.seed, placement, reach), f"{feature.id}[{i}]")
        return part
    if isinstance(feature, Pocket):
        pl = plane(feature.placement).offset(-_ENTRY_OFFSET)
        depth = (feature.depth if feature.depth is not None else reach) + _ENTRY_OFFSET
        return _cut(part, bd.extrude(face(feature.profile, pl), amount=depth), feature.id)
    if isinstance(feature, Boss):
        tool = bd.extrude(face(feature.profile, plane(feature.placement)), amount=feature.height)
        return _add(part, tool, feature.id)
    if isinstance(feature, Slot):
        pl = plane(feature.placement).offset(-_ENTRY_OFFSET)
        slot = bd.SlotCenterToCenter(feature.length, feature.width)
        depth = (feature.depth if feature.depth is not None else reach) + _ENTRY_OFFSET
        return _cut(part, bd.extrude(pl * slot, amount=depth), feature.id)
    if isinstance(feature, Fillet | Chamfer):
        edges = select_edges(part, feature.edges)
        if not edges:
            raise FeatureBuildError(feature.id, "edge selector matched no edges")
        if isinstance(feature, Fillet):
            result = bd.fillet(edges, feature.radius)
        else:
            result = bd.chamfer(edges, feature.distance, feature.distance_2)
        if not isinstance(result, bd.Part):
            raise FeatureBuildError(feature.id, "did not produce a solid")
        return result
    if isinstance(feature, ScriptFeature):
        namespace: dict[str, Any] = {"part": part}
        exec("from build123d import *", namespace)  # noqa: S102 - runs in the build runner
        exec(compile(feature.code, f"<feature {feature.id}>", "exec"), namespace)  # noqa: S102
        scripted = namespace.get("part")
        if not isinstance(scripted, bd.Shape):
            raise FeatureBuildError(feature.id, "script must leave a build123d shape in `part`")
        return scripted
    raise FeatureBuildError(feature.id, f"unsupported feature type {feature.type}")


def build_features(features: list[dict[str, Any]]) -> bd.Shape[Any]:
    """Build the solid from a serialized feature list (as sent in a BuildJob)."""
    parsed = _FEATURES.validate_python(features)
    if not parsed:
        raise ValueError("empty feature list")
    part: bd.Shape[Any] | None = None
    for feature in parsed:
        try:
            part = apply_feature(part, feature)
        except FeatureBuildError:
            raise
        except Exception as exc:
            raise FeatureBuildError(feature.id, f"{type(exc).__name__}: {exc}") from exc
    assert part is not None
    return part
