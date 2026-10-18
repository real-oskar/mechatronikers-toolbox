import math

import pytest
from pydantic import TypeAdapter, ValidationError

from mechatronikers.spec import (
    SPEC_VERSION,
    Arc,
    CircularPattern,
    Dimension,
    EdgeSelector,
    ExtrudeBase,
    Feature,
    Hole,
    HoleParams,
    HolePattern,
    Line,
    LinearPattern,
    PartSpec,
    Placement,
    Profile,
    RevolveBase,
    circle,
    rectangle,
    tap_drill_diameter,
)
from tests.fixtures.specs import fuss


def test_fixture_spec_is_valid_and_round_trips() -> None:
    spec = fuss()
    again = PartSpec.model_validate_json(spec.model_dump_json())
    assert again == spec
    assert [f.type for f in again.features].count("hole_pattern") == 1


def test_dimension_rules() -> None:
    base = {"id": "d1", "kind": "linear", "value": 5.0, "confidence": 0.9}
    Dimension(**base, stated=5.0, method="vector")
    with pytest.raises(ValidationError, match="must be measured/assumed/human"):
        Dimension(**base, method="vector")
    with pytest.raises(ValidationError, match="needs a reason"):
        Dimension(**base, method="measured", status="needs_review")
    with pytest.raises(ValidationError, match="must be reviewed"):
        Dimension(**base, method="assumed", reason="note: unspecified radii R=1")
    ok = Dimension(**base, method="measured", status="needs_review", reason="not printed")
    assert ok.stated is None


def test_profiles_must_be_closed() -> None:
    rectangle(10, 5)
    circle(4)
    Profile(
        segments=[
            Line(start=(0, 0), end=(10, 0)),
            Arc(start=(10, 0), mid=(15, 5), end=(10, 10)),
            Line(start=(10, 10), end=(0, 10)),
            Line(start=(0, 10), end=(0, 0)),
        ]
    )
    with pytest.raises(ValidationError, match="not closed"):
        Profile(segments=[Line(start=(0, 0), end=(1, 0)), Line(start=(1, 0), end=(1, 1))])
    with pytest.raises(ValidationError, match="only segment"):
        Profile(segments=[*circle(4).segments, Line(start=(0, 0), end=(0, 0))])


def test_revolve_profile_stays_in_half_plane() -> None:
    with pytest.raises(ValidationError, match="u >= 0"):
        RevolveBase(id="r", profile=rectangle(10, 10))
    RevolveBase(id="r", profile=rectangle(10, 10, center=(5, 5)))


def test_hole_parameter_rules_and_threads() -> None:
    with pytest.raises(ValidationError, match="blind hole needs a depth"):
        HoleParams(hole_type="blind", diameter=5)
    with pytest.raises(ValidationError, match="counterbore needs"):
        HoleParams(hole_type="counterbore", diameter=5)
    with pytest.raises(ValidationError, match="larger than the hole"):
        HoleParams(hole_type="counterbore", diameter=5, cb_diameter=4, cb_depth=2)
    with pytest.raises(ValidationError, match="thread designation"):
        HoleParams(hole_type="threaded", diameter=4.2)
    assert tap_drill_diameter("M5") == 4.2
    assert tap_drill_diameter("m10x1,5") == 8.5
    with pytest.raises(ValueError):
        tap_drill_diameter("M7")


def test_circular_pattern_instances() -> None:
    seed = Hole(id="h", diameter=10, placement=Placement(origin=(38, 0, 4), normal=(0, 0, -1)))
    pattern = HolePattern(id="p", seed=seed, pattern=CircularPattern(center=(0, 0, 0), count=12))
    origins = [p.origin for p in pattern.instances()]
    assert len(origins) == 12
    assert all(math.isclose(math.hypot(o[0], o[1]), 38) for o in origins)
    assert origins[3] == pytest.approx((0, 38, 4), abs=1e-9)


def test_linear_pattern_instances() -> None:
    seed = Hole(id="h", diameter=5, placement=Placement(origin=(0, 0, 0)))
    pattern = HolePattern(
        id="p", seed=seed, pattern=LinearPattern(count_1=2, pitch_1=40, count_2=3, pitch_2=10)
    )
    assert sorted(p.origin for p in pattern.instances()) == sorted(
        (x, y, 0.0) for x in (0.0, 40.0) for y in (0.0, 10.0, 20.0)
    )


def test_partspec_reference_rules() -> None:
    spec = fuss()
    data = spec.model_dump()
    data["features"][1]["dimension_ids"] = ["d-unknown"]
    with pytest.raises(ValidationError, match="unknown dimensions"):
        PartSpec.model_validate(data)
    data = spec.model_dump()
    data["features"] = data["features"][1:]
    data["dimensions"] = [d | {"feature_ids": []} for d in data["dimensions"]]
    with pytest.raises(ValidationError, match="first feature must be a base"):
        PartSpec.model_validate(data)
    with pytest.raises(ValidationError, match="duplicate ids"):
        PartSpec(
            spec_version=SPEC_VERSION,
            part_name="x",
            features=[
                ExtrudeBase(id="a", profile=rectangle(1, 1), depth=1),
                Hole(id="a", diameter=0.5, placement=Placement()),
            ],
        )


def test_expected_geometry_expands_patterns() -> None:
    expected = fuss().expected_geometry()
    assert expected.bbox_size == (115.0, 115.0, 76.2)
    assert expected.holes is not None
    assert sorted(h.diameter for h in expected.holes) == [19, 19, 19, 19, 19, 33]
    assert {h.id for h in expected.holes} >= {"f-holes.0", "f-holes.3", "f-bore"}


def test_missing_dimensions_lists_unprinted_values() -> None:
    spec = fuss()
    spec.dimensions.append(
        Dimension(
            id="d-r",
            kind="radius",
            value=1.0,
            method="assumed",
            status="needs_review",
            confidence=0.5,
            reason="note 'nicht bemaßte Radien R=1'",
        )
    )
    assert [d.id for d in spec.missing_dimensions()] == ["d-r"]


def test_feature_union_and_selectors() -> None:
    feature = TypeAdapter(Feature).validate_python(
        {"type": "slot", "id": "s", "length": 20, "width": 6, "placement": {}}
    )
    assert feature.type == "slot"
    with pytest.raises(ValidationError):
        EdgeSelector(by="parallel")
    with pytest.raises(ValidationError, match="perpendicular"):
        Placement(normal=(0, 0, 1), x_dir=(0, 1, 1))
