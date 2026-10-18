import io
import math
from pathlib import Path

import pytest

from mechatronikers.drawing_mcp.coverage import (
    check_coverage,
    plan_facts,
    visible_circles,
    visible_outline,
)
from mechatronikers.drawing_mcp.session import DrawingSession
from mechatronikers.spec import (
    Arc,
    Boss,
    Chamfer,
    CircularPattern,
    Dimension,
    EdgeSelector,
    ExtrudeBase,
    Feature,
    Fillet,
    Hole,
    HolePattern,
    Line,
    Placement,
    Pocket,
    Profile,
    SourceRef,
    circle,
    rectangle,
)
from mechatronikers.spec.drawing import Primitive, ViewRegion
from mechatronikers.telemetry import EventEmitter
from tests.fixtures.drawings import make_drawing, make_notched_block

PLATE = ExtrudeBase(id="base", profile=rectangle(100, 60), depth=10)


def _circle(pid: str, x: float, y: float, d: float, width: float = 0.99) -> Primitive:
    return Primitive(id=pid, kind="circle", width=width, center=(x, y), radius=d / 2, view_id="v0")


def _hole(hid: str, x: float, y: float, d: float = 8, **kw: object) -> Hole:
    placement = Placement(origin=(x, y, 10), normal=(0, 0, -1))
    return Hole(id=hid, diameter=d, placement=placement, **kw)  # type: ignore[arg-type]


def _dim(did: str, kind: str, value: float, status: str = "auto_ok") -> Dimension:
    return Dimension(
        id=did,
        kind=kind,  # type: ignore[arg-type]
        stated=value,
        stated_text=f"{value:g}",
        value=value,
        method="vlm",
        confidence=0.9,
        status=status,  # type: ignore[arg-type]
        reason=None if status == "auto_ok" else "test",
    )


def _messages(
    features: list[Feature], circles: list[Primitive], dims: list[Dimension]
) -> list[str]:
    report = check_coverage(features, dims, {"v0": circles}, {"v0": 2.0})
    return [i.message + " | " + i.reason for i in report.review_items]


def test_missing_holes_and_unexplained_circles_are_flagged() -> None:
    features: list[Feature] = [PLATE, _hole("h1", -30, 0), _hole("h2", 30, 0)]
    # view at 2:1: three Ø8 holes (sheet Ø16) and a Ø12 ring around one of them
    circles = [_circle("c1", 40, 60, 16), _circle("c2", 160, 60, 16), _circle("c3", 100, 60, 16)]
    circles.append(_circle("c4", 40, 60, 24))
    messages = _messages(features, circles, [])
    assert len(messages) == 2
    assert messages[0].startswith("View v0 shows 3 circles Ø8, the plan has 2 (h1, h2)")
    assert (
        "1 visible circle(s) Ø12" in messages[1] and "concentric with a planned Ø8" in messages[1]
    )
    with_cb = [PLATE, _hole("h1", -30, 0, hole_type="counterbore", cb_diameter=12, cb_depth=3)]
    with_cb += [_hole("h2", 30, 0), _hole("h3", 0, 0)]
    assert _messages(with_cb, circles, []) == []


def test_chamfer_edges_threads_and_turned_diameters_explain_circles() -> None:
    boss = Boss(id="hub", profile=circle(50), height=15, placement=Placement(origin=(0, 0, 10)))
    chamfer = Chamfer(id="ch", distance=2, edges=EdgeSelector(by="all"))
    tapped = _hole("m10", 30, 0, d=8.5, hole_type="threaded", thread="M10")
    circles = [
        _circle("c1", 100, 60, 100),  # Ø50 boss
        _circle("c2", 100, 60, 92),  # Ø46: chamfer edge of the boss
        _circle("c3", 160, 60, 17),  # tap drill Ø8.5
        _circle("c4", 160, 60, 18),  # Ø9: inside the thread band of M10
    ]
    assert _messages([PLATE, boss, tapped], circles, [])[0].startswith("View v0: 1 visible")
    assert _messages([PLATE, boss, chamfer, tapped], circles, []) == []


def test_visible_circles_drop_thin_tiny_and_duplicate_circles() -> None:
    circles = [
        _circle("a", 10, 10, 8),
        _circle("dup", 10.01, 10, 8.02),  # same circle drawn twice
        _circle("thin", 30, 10, 8, width=0.37),  # centre line or thread band
        _circle("tiny", 50, 10, 1.5),  # symbol or hatch dot
        Primitive(id="other", kind="circle", width=0.99, center=(0, 0), radius=5, view_id="v9"),
    ]
    kept = visible_circles(circles, {"v0"}, outline_width=0.99)
    assert [p.id for p in kept["v0"]] == ["a"] and "v9" not in kept


def test_printed_dimensions_must_be_used_by_the_plan() -> None:
    seed = _hole("p", 15, 0, d=5)
    pattern = HolePattern(id="ring", seed=seed, pattern=CircularPattern(center=(0, 0, 0), count=8))
    referenced = Fillet(id="round", radius=5, edges=EdgeSelector(by="all"), dimension_ids=["d-r5"])
    dims = [
        _dim("d-100", "linear", 100),  # plate length (profile)
        _dim("d-20", "linear", 20),  # hole h: 20 from the right edge (x = 30, edge at 50)
        _dim("d-pcd", "diameter", 30),  # pitch circle of the ring
        _dim("d-45", "angle", 45),  # step angle of the ring
        _dim("d-r5", "radius", 5),  # referenced by the fillet that builds it
        _dim("d-37", "linear", 37),  # nowhere in the plan
        _dim("d-r3", "radius", 3),  # nowhere in the plan
        _dim("d-x", "linear", 37, status="rejected"),
    ]
    report = check_coverage([PLATE, pattern, _hole("h", 30, 20), referenced], dims, {}, {})
    flagged = [i.dimension_ids for i in report.review_items]
    assert flagged == [["d-37"], ["d-r3"]] and report.dimensions_checked == 7
    assert all(i.kind == "possibly_missing" for i in report.review_items)


def test_session_coverage_uses_the_last_consistency_check(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    make_drawing(tmp_path / "input" / "sheet.pdf")
    session = DrawingSession(
        tmp_path / "input", tmp_path / "artifacts", EventEmitter("drawing", stream=io.StringIO())
    )
    drawing_id = session.load("sheet.pdf").source_sha256[:12]
    with pytest.raises(ValueError, match="check_consistency first"):
        session.coverage(drawing_id, [PLATE])
    session.check(drawing_id, sheet_scale=1.0, claims=[])
    missing = session.coverage(drawing_id, [PLATE])
    assert missing.circles_checked == 1 and len(missing.review_items) == 1
    item = missing.review_items[0]
    assert "Ø20" in item.message and item.sheet_ref is not None
    assert item.sheet_ref.bbox == pytest.approx((97, 57, 123, 83), abs=0.5)  # Ø20 at (110, 70)
    assert session.coverage(drawing_id, [PLATE, _hole("bore", 0, 0, d=20)]).review_items == []


# --------------------------------------------------------------------------- #89: typed evidence
# Hand-written fixtures (a made-up block), never values of the sample parts.

BLOCK = ExtrudeBase(id="base", profile=rectangle(100, 60), depth=20)  # x ±50, y ±30, z 0..20
# Notch from the top: x 10..30, through in y, 8 deep (floor at z = 12)
NOTCH = Pocket(
    id="notch",
    profile=rectangle(20, 60),
    depth=8,
    placement=Placement(origin=(20, 0, 20), normal=(0, 0, -1)),
)


def _printed(did: str, kind: str, value: float, text: str, view: str | None = "v0") -> Dimension:
    return Dimension(
        id=did,
        kind=kind,  # type: ignore[arg-type]
        stated=value,
        stated_text=text,
        value=value,
        method="vlm",
        confidence=0.9,
        source=SourceRef(view_id=view, bbox=(60, 130, 70, 135)),
    )


def _segments(view: str, *points: tuple[float, float], closed: bool = False) -> list[Primitive]:
    pairs = list(zip(points, points[1:] + (points[:1] if closed else ()), strict=False))
    return [
        Primitive(id=f"{view}-{i}", kind="line", width=0.7, start=a, end=b, view_id=view)
        for i, (a, b) in enumerate(pairs)
    ]


def _three_views(notch_in_top: bool = True) -> dict[str, list[Primitive]]:
    """Scale 1. Front v0 (x right, z up), top v1 (x right, y down), side v2 (y right, z up).
    The notch shows in the front view (walls and floor) and in the top view (floor edges); in
    the side view it is hidden behind the full-height ends."""
    front = _segments(
        "v0", (50, 170), (150, 170), (150, 150), (130, 150), (130, 158), (110, 158),
        (110, 150), (50, 150), closed=True,
    )  # fmt: skip
    top = _segments("v1", (50, 60), (150, 60), (150, 120), (50, 120), closed=True)
    if notch_in_top:
        top += _segments("v1", (110, 60), (110, 120)) + _segments("v1", (130, 60), (130, 120))
        top[-2:] = [p.model_copy(update={"id": f"v1-n{i}"}) for i, p in enumerate(top[-2:])]
    side = _segments("v2", (200, 150), (260, 150), (260, 170), (200, 170), closed=True)
    return {"v0": front, "v1": top, "v2": side}


SCALES = {"v0": 1.0, "v1": 1.0, "v2": 1.0}


def _mirror(p: Primitive, axis_x2: float) -> Primitive:
    """The line mirrored about x = axis_x2 / 2 (sheet mm)."""
    assert p.start is not None and p.end is not None
    start, end = (axis_x2 - p.start[0], p.start[1]), (axis_x2 - p.end[0], p.end[1])
    return p.model_copy(update={"start": start, "end": end})


def _report(
    features: list[Feature], dims: list[Dimension], outline: dict[str, list[Primitive]] | None
) -> list[tuple[str, list[str]]]:
    report = check_coverage(features, dims, {}, SCALES, outline)
    return [(i.message + " | " + i.reason, i.dimension_ids) for i in report.review_items]


def test_cutout_seen_in_two_views_is_reported() -> None:
    views = _three_views()
    found = _report([BLOCK], [], views)
    assert len(found) == 2  # the two notch walls (x = 10 and x = 30); the floor shows in v0 only
    assert all("Views v0, v1 show an outline edge" in m and "x axis" in m for m, _ in found)
    assert "cut-out" in found[0][0]
    assert _report([BLOCK, NOTCH], [], views) == []
    # one view alone is no evidence (the floor edges of the top view are left out)
    assert _report([BLOCK], [], _three_views(notch_in_top=False)) == []
    # a mirrored front view shows the same edges from the other end
    mirrored = {**views, "v0": [_mirror(p, 200) for p in views["v0"]]}
    assert len(_report([BLOCK], [], mirrored)) == 2 and _report([BLOCK, NOTCH], [], mirrored) == []
    # a view whose extent fits no axis of the plan (detail view, merged views) is not used
    assert check_coverage([BLOCK], [], {}, {**SCALES, "v1": 2.0}, views).review_items == []
    # a plan slightly too long maps no view along x: its planned notch is not reported missing
    long = BLOCK.model_copy(update={"profile": rectangle(100.8, 60)})
    assert _report([long, NOTCH], [], views) == []


def test_visible_outline_keeps_connected_outline_edges_inside_the_view() -> None:
    views = [ViewRegion(id="v0", bbox=(45, 145, 155, 175), kind="orthographic")]
    lines = _three_views()["v0"] + [
        Primitive(id="mark", kind="line", width=0.7, start=(60, 140), end=(60, 146), view_id="v0"),
        Primitive(id="thin", kind="line", width=0.35, start=(60, 150), end=(60, 170), view_id="v0"),
        Primitive(id="far", kind="line", width=0.7, start=(300, 10), end=(310, 10), view_id="v0"),
    ]
    kept = visible_outline(lines, views, outline_width=0.7)
    assert sorted(p.id for p in kept["v0"]) == sorted(p.id for p in _three_views()["v0"])
    assert visible_outline(lines, views, outline_width=None) == {}


def test_radius_dimensions_need_a_rounded_edge() -> None:
    r7 = _printed("d-r7", "radius", 7, "R7")
    pin = Boss(id="pin", profile=circle(14), height=5, placement=Placement(origin=(0, 0, 20)))
    # an R7 is a fillet or an arc: a Ø14 circle elsewhere does not use it
    [(message, ids)] = _report([BLOCK, pin], [r7], None)
    assert ids == ["d-r7"] and "not used" in message and "full circle" in message
    fillet = Fillet(id="round", radius=7, edges=EdgeSelector(by="parallel", axis=(0, 0, 1)))
    assert _report([BLOCK, pin, fillet], [r7], None) == []
    rounded = ExtrudeBase(id="base", depth=20, profile=Profile(segments=[
        Line(start=(-50, -30), end=(43, -30)),
        Arc(start=(43, -30), mid=(47.9497, -27.9497), end=(50, -23)),
        Line(start=(50, -23), end=(50, 30)),
        Line(start=(50, 30), end=(-50, 30)),
        Line(start=(-50, 30), end=(-50, -30)),
    ]))  # fmt: skip
    assert _report([rounded], [r7], None) == []
    # a feature that references the R7 must build or be positioned by it: ids dumped onto the
    # base body are caught
    dumped = BLOCK.model_copy(update={"dimension_ids": ["d-r7"]})
    [(message, ids)] = _report([dumped, pin], [r7], None)
    assert "referenced by base, but none of them builds or is positioned by a radius" in message
    linked = fillet.model_copy(update={"dimension_ids": ["d-r7"]})
    assert _report([BLOCK, linked], [r7], None) == []


def test_positional_angles_and_radii_are_used_by_the_features_they_place() -> None:
    """A polar hole position, a kidney slot's centre-line radius and an inclined hole axis are
    positions, not shapes: referencing them from the feature they place is enough (#89)."""
    plate = ExtrudeBase(id="base", profile=rectangle(100, 60), depth=10)
    a30 = _printed("d-30", "angle", 30, "30°", view=None)
    r20 = _printed("d-r20", "radius", 20, "R20", view=None)
    polar = [  # two holes at R20, ±30° from the x axis
        _hole(h, 17.3205, y, d=5).model_copy(update={"dimension_ids": ["d-30", "d-r20"]})
        for h, y in (("h_up", 10), ("h_down", -10))
    ]
    assert _report([plate, *polar], [a30, r20], None) == []

    # kidney slot about the origin: R20 inner and R30 outer arcs over ±20°, R5 end caps whose
    # centres lie on the R25 centre line
    def at(r: float, deg: float) -> tuple[float, float]:
        return (r * math.cos(math.radians(deg)), r * math.sin(math.radians(deg)))

    def cap(deg: float, side: float) -> tuple[float, float]:
        cx, cy = at(25, deg)
        dx, dy = at(5, deg + side * 90)
        return (cx + dx, cy + dy)

    kidney = Pocket(
        id="kidney", dimension_ids=["d-r25"], depth=None,
        placement=Placement(origin=(0, 0, 10), normal=(0, 0, -1)),
        profile=Profile(segments=[
            Arc(start=at(30, -20), mid=at(30, 0), end=at(30, 20)),
            Arc(start=at(30, 20), mid=cap(20, 1), end=at(20, 20)),
            Arc(start=at(20, 20), mid=at(20, 0), end=at(20, -20)),
            Arc(start=at(20, -20), mid=cap(-20, -1), end=at(30, -20)),
        ]),
    )  # fmt: skip
    r25 = _printed("d-r25", "radius", 25, "R25", view=None)
    assert _report([plate, kidney], [r25], None) == []
    # a hole tilted 30° in the x-z plane
    tilted = Hole(
        id="tilted", diameter=5, dimension_ids=["d-30"],
        placement=Placement(origin=(0, 0, 10), normal=(0.5, 0, -0.866025)),
    )  # fmt: skip
    assert _report([plate, tilted], [a30], None) == []
    # unreferenced, the tilt still explains a 30° printed in the front view (x-z plane)
    front = _printed("d-30f", "angle", 30, "30°", view="v0")
    untied = tilted.model_copy(update={"dimension_ids": []})
    assert _report([BLOCK, NOTCH, untied], [front], _three_views()) == []


def test_plan_extent_follows_what_arcs_sweep_and_symmetric_depth() -> None:
    notched = ExtrudeBase(id="base", depth=20, profile=Profile(segments=[
        Line(start=(-50, -30), end=(50, -30)),
        Line(start=(50, -30), end=(50, 30)),
        Line(start=(50, 30), end=(10, 30)),
        Arc(start=(10, 30), mid=(0, 20), end=(-10, 30)),  # concave R10 notch in the top edge
        Line(start=(-10, 30), end=(-50, 30)),
        Line(start=(-50, 30), end=(-50, -30)),
    ]))  # fmt: skip
    facts = plan_facts([notched], [])
    assert facts.extent is not None
    assert facts.extent[0].tolist() == [-50, -30, 0] and facts.extent[1].tolist() == [50, 30, 20]
    symmetric = BLOCK.model_copy(update={"direction": "symmetric"})
    facts = plan_facts([symmetric], [])
    assert facts.extent is not None and facts.extent[0][2] == -10 and facts.extent[1][2] == 10
    assert sorted({round(float(p[2]), 6) for p in facts.levels}) == [-10, 10]


def test_chamfer_dimensions_need_a_chamfer() -> None:
    c2 = _printed("d-c2", "chamfer", 2, "2x45°")
    top = Placement(origin=(0, 0, 20))
    step = Boss(id="step", profile=rectangle(20, 20), height=2, placement=top)
    # a 2 mm step is a length, not a chamfer
    [(message, ids)] = _report([BLOCK, step], [c2], None)
    assert ids == ["d-c2"] and "chamfer" in message
    chamfer = Chamfer(id="ch", distance=2, edges=EdgeSelector(by="all"))
    assert _report([BLOCK, step, chamfer], [c2], None) == []
    corner = ExtrudeBase(id="base", depth=20, profile=Profile(segments=[
        Line(start=(-50, -30), end=(48, -30)),
        Line(start=(48, -30), end=(50, -28)),  # 2 x 45° chamfered corner in the profile
        Line(start=(50, -28), end=(50, 30)),
        Line(start=(50, 30), end=(-50, 30)),
        Line(start=(-50, 30), end=(-50, -30)),
    ]))  # fmt: skip
    assert _report([corner], [c2], None) == []
    sunk = _hole("cs", 0, 0, d=8, hole_type="countersink", cs_diameter=12, cs_angle=90)
    assert _report([BLOCK, sunk], [c2], None) == []
    # the angle counts too: a 45° chamfer does not build "2x30°"
    c30 = _printed("d-c30", "chamfer", 2, "2x30°")
    assert [ids for _, ids in _report([BLOCK, chamfer], [c30], None)] == [["d-c30"]]


def test_slope_angles_need_a_sloped_edge_in_the_plane_of_their_view() -> None:
    slope = _printed("d-15", "angle", 15, "15°")  # printed in the front view v0 (x-z plane)
    seed = _hole("s", 40, 0, d=4)
    ring = HolePattern(id="ring", seed=seed, pattern=CircularPattern(center=(0, 0, 0), count=24))
    views = _three_views()
    # the 15° pitch of a hole ring lies in the x-y plane: it does not explain a slope seen in
    # the front view (without view geometry the plane is unknown, so it still counts)
    flagged = [(m, ids) for m, ids in _report([BLOCK, NOTCH, ring], [slope], views) if ids]
    assert [ids for _, ids in flagged] == [["d-15"]] and "plane of view v0" in flagged[0][0]
    assert _report([BLOCK, NOTCH, ring], [slope], None) == []
    # a wedge cut in the x-z plane: hypotenuse at 15° to the top face
    wedge = Pocket(
        id="wedge",
        placement=Placement(origin=(0, -30, 0), normal=(0, 1, 0)),  # local v = -z
        profile=Profile(segments=[
            Line(start=(-50, -10), end=(-12.679, -20)),
            Line(start=(-12.679, -20), end=(-50, -20)),
            Line(start=(-50, -20), end=(-50, -10)),
        ]),
    )  # fmt: skip
    assert [ids for _, ids in _report([BLOCK, NOTCH, wedge], [slope], views) if ids] == []
    dumped = BLOCK.model_copy(update={"dimension_ids": ["d-15"]})
    [(message, ids)] = [(m, i) for m, i in _report([dumped, NOTCH, ring], [slope], views) if i]
    assert "referenced by base, but none of them builds or is positioned by an angle" in message


def test_session_reports_a_cutout_missing_from_the_plan(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    make_notched_block(tmp_path / "input" / "block.pdf")
    session = DrawingSession(
        tmp_path / "input", tmp_path / "artifacts", EventEmitter("drawing", stream=io.StringIO())
    )
    drawing_id = session.load("block.pdf").source_sha256[:12]
    session.check(drawing_id, sheet_scale=1.0, claims=[])
    missing = session.coverage(drawing_id, [BLOCK])
    assert missing.edges_checked > 0 and len(missing.review_items) == 2
    assert all("x axis" in i.message and i.sheet_ref for i in missing.review_items)
    assert session.coverage(drawing_id, [BLOCK, NOTCH]).review_items == []
