import math

import pytest

from mechatronikers.cad_mcp.build import FeatureBuildError, build_features
from mechatronikers.cad_mcp.measure import bbox_size, find_holes
from mechatronikers.cad_mcp.validation import validate
from mechatronikers.spec import (
    SPEC_VERSION,
    Boss,
    Chamfer,
    CircularPattern,
    EdgeSelector,
    ExtrudeBase,
    Feature,
    Fillet,
    Hole,
    HolePattern,
    Line,
    LinearPattern,
    PartSpec,
    Placement,
    Pocket,
    Profile,
    RevolveBase,
    ScriptFeature,
    Slot,
    circle,
    rectangle,
)
from tests.fixtures.specs import fuss

PLATE = ExtrudeBase(id="base", profile=rectangle(60, 40), depth=10)
TOP = (0, 0, -1)  # drilling down from the top face at z = 10


def _build(*features: Feature):  # type: ignore[no-untyped-def]
    return build_features([f.model_dump(mode="json") for f in features])


def test_fixture_part_builds_and_validates() -> None:
    spec = fuss()
    part = _build(*spec.features)
    report = validate(part, build_id="fuss", expected=spec.expected_geometry())
    assert report.passed, report.feedback()


def test_extrude_symmetric_and_revolve() -> None:
    sym = _build(ExtrudeBase(id="b", profile=rectangle(10, 10), depth=6, direction="symmetric"))
    assert pytest.approx(-3) == sym.bounding_box().min.Z
    shaft = Profile(
        segments=[
            Line(start=(0, 0), end=(10, 0)),
            Line(start=(10, 0), end=(10, 30)),
            Line(start=(10, 30), end=(6, 30)),
            Line(start=(6, 30), end=(6, 50)),
            Line(start=(6, 50), end=(0, 50)),
            Line(start=(0, 50), end=(0, 0)),
        ]
    )
    turned = _build(RevolveBase(id="r", profile=shaft))
    assert bbox_size(turned) == pytest.approx((20, 20, 50), abs=1e-6)
    assert turned.volume == pytest.approx(math.pi * (10**2 * 30 + 6**2 * 20), rel=1e-6)


def test_hole_types_produce_expected_cylinders() -> None:
    part = _build(
        PLATE,
        Hole(id="through", diameter=5, placement=Placement(origin=(-20, 0, 10), normal=TOP)),
        Hole(
            id="blind",
            hole_type="blind",
            diameter=4,
            depth=5,
            placement=Placement(origin=(-10, 0, 10), normal=TOP),
        ),
        Hole(
            id="cb",
            hole_type="counterbore",
            diameter=6,
            cb_diameter=11,
            cb_depth=4,
            placement=Placement(origin=(0, 0, 10), normal=TOP),
        ),
        Hole(
            id="cs",
            hole_type="countersink",
            diameter=5,
            cs_diameter=10,
            placement=Placement(origin=(10, 0, 10), normal=TOP),
        ),
        Hole(
            id="tap",
            hole_type="threaded",
            diameter=4.2,
            thread="M5",
            placement=Placement(origin=(20, 0, 10), normal=TOP),
        ),
    )
    diameters = sorted(round(h.diameter, 6) for h in find_holes(part))
    assert diameters == [4.0, 4.2, 5.0, 5.0, 6.0, 11.0]


def test_patterns_pocket_boss_slot() -> None:
    part = _build(
        PLATE,
        HolePattern(
            id="circ",
            seed=Hole(id="s1", diameter=3, placement=Placement(origin=(12, 0, 10), normal=TOP)),
            pattern=CircularPattern(center=(0, 0, 0), count=6),
        ),
        HolePattern(
            id="lin",
            seed=Hole(id="s2", diameter=2, placement=Placement(origin=(-26, -16, 10), normal=TOP)),
            pattern=LinearPattern(count_1=2, pitch_1=52, count_2=2, pitch_2=32),
        ),
        Pocket(
            id="pocket",
            profile=rectangle(6, 6, center=(-20, 10)),
            depth=3,
            placement=Placement(origin=(0, 0, 10), normal=TOP),
        ),
        Boss(
            id="boss",
            profile=circle(8, center=(20, 10)),
            height=5,
            placement=Placement(origin=(0, 0, 10)),
        ),
        Slot(
            id="slot",
            length=10,
            width=4,
            depth=2,
            placement=Placement(origin=(0, -12, 10), normal=TOP, x_dir=(1, 0, 0)),
        ),
    )
    assert len(find_holes(part)) == 10
    assert bbox_size(part)[2] == pytest.approx(15)


def test_fillet_and_chamfer_selectors() -> None:
    rounded = _build(
        PLATE,
        Fillet(id="corners", radius=5, edges=EdgeSelector(by="parallel", axis=(0, 0, 1))),
    )
    assert rounded.volume == pytest.approx(
        60 * 40 * 10 - 4 * (25 - math.pi * 25 / 4) * 10, rel=1e-6
    )
    chamfered = _build(
        PLATE,
        Chamfer(id="edge", distance=2, edges=EdgeSelector(by="near_points", points=[(0, 20, 10)])),
    )
    assert chamfered.volume == pytest.approx(60 * 40 * 10 - 0.5 * 2 * 2 * 60, rel=1e-6)


def test_errors_name_the_feature() -> None:
    with pytest.raises(FeatureBuildError, match="feature air: removed no material"):
        _build(
            PLATE, Hole(id="air", diameter=5, placement=Placement(origin=(100, 0, 10), normal=TOP))
        )
    with pytest.raises(FeatureBuildError, match=r"feature pat\[1\]: removed no material"):
        _build(
            PLATE,
            HolePattern(
                id="pat",
                seed=Hole(id="s", diameter=2, placement=Placement(origin=(0, 0, 10), normal=TOP)),
                pattern=LinearPattern(count_1=2, pitch_1=200),
            ),
        )
    with pytest.raises(FeatureBuildError, match="feature f: edge selector matched no edges"):
        _build(
            PLATE,
            Fillet(
                id="f",
                radius=1,
                edges=EdgeSelector(by="all", within=((99, 99, 99), (100, 100, 100))),
            ),
        )


def test_script_feature_modifies_part() -> None:
    part = _build(
        PLATE,
        ScriptFeature(id="sc", code="part = part - Pos(0, 0, 10) * Box(4, 4, 4)", reason="test"),
    )
    assert part.volume == pytest.approx(60 * 40 * 10 - 4 * 4 * 2)


def test_expected_geometry_validates_patterns_in_built_part() -> None:
    features = [
        PLATE,
        HolePattern(
            id="circ",
            seed=Hole(id="s", diameter=3, placement=Placement(origin=(12, 0, 10), normal=TOP)),
            pattern=CircularPattern(center=(0, 0, 0), count=4),
        ),
    ]
    spec = PartSpec(
        spec_version=SPEC_VERSION, part_name="p", overall_size=(60, 40, 10), features=features
    )
    report = validate(_build(*features), build_id="p", expected=spec.expected_geometry())
    assert report.passed, report.feedback()
