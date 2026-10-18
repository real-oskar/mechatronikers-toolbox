from pathlib import Path

import build123d as bd
import pytest

from mechatronikers.cad_mcp import validate
from mechatronikers.cad_mcp.measure import find_holes
from mechatronikers.cad_mcp.step_io import export_step, read_schema
from mechatronikers.spec import ExpectedGeometry, ExpectedHole


@pytest.fixture
def plate() -> bd.Part:
    """40 x 30 x 10 plate with a Ø8 through hole at (5, 0) and a Ø6 boss on top."""
    part = bd.Box(40, 30, 10) - bd.Pos(5, 0, 0) * bd.Cylinder(4, 10)
    return part + bd.Pos(-12, 0, 10) * bd.Cylinder(3, 10)


def _expected(diameter: float = 8.0) -> ExpectedGeometry:
    return ExpectedGeometry(
        bbox_size=(40, 30, 20),
        holes=[ExpectedHole(id="h1", diameter=diameter, center=(5, 0, 0), axis=(0, 0, 1))],
    )


def _by_id(report_checks: list, check_id: str):  # type: ignore[no-untyped-def]
    return next(c for c in report_checks if c.id == check_id)


def test_valid_part_passes_all_checks(plate: bd.Part) -> None:
    report = validate(plate, build_id="b1", expected=_expected())
    assert report.passed, report.feedback()
    categories = {c.category for c in report.checks}
    assert categories == {"V1_geometry", "V2_dimensions", "V3_step_roundtrip"}


def test_find_holes_ignores_bosses(plate: bd.Part) -> None:
    holes = find_holes(plate)
    assert [round(h.diameter, 6) for h in holes] == [8.0]


def test_partial_concave_cylinder_is_not_a_hole() -> None:
    box = bd.Box(20, 20, 10)
    part = bd.fillet(box.edges().filter_by(bd.Axis.Z), radius=2)  # convex corners
    notch = bd.Box(20, 20, 10) - bd.Pos(10, 10, 0) * bd.Cylinder(5, 10)  # quarter-circle cut
    assert find_holes(part) == []
    assert find_holes(notch) == []


def test_wrong_hole_diameter_fails_with_feedback(plate: bd.Part) -> None:
    report = validate(plate, build_id="b1", expected=_expected(diameter=10.0))
    assert not report.passed
    check = _by_id(report.checks, "V2.hole_diameter.h1")
    assert not check.passed
    assert "V2.hole_diameter.h1: expected 10.0, measured 8.0" in report.feedback()


def test_missing_hole_is_reported(plate: bd.Part) -> None:
    expected = _expected()
    assert expected.holes is not None
    expected.holes.append(ExpectedHole(id="h2", diameter=5, center=(-15, 10, 0)))
    report = validate(plate, build_id="b1", expected=expected)
    assert not _by_id(report.advisory, "V2.hole_count").passed  # advisory, D51
    assert not _by_id(report.checks, "V2.hole_found.h2").passed


def test_hole_position_offset_fails(plate: bd.Part) -> None:
    expected = ExpectedGeometry(holes=[ExpectedHole(id="h1", diameter=8, center=(6, 0, 0))])
    check = _by_id(validate(plate, build_id="b1", expected=expected).checks, "V2.hole_position.h1")
    assert not check.passed
    assert check.measured == pytest.approx(1.0)


def test_open_shell_fails_v1() -> None:
    face = bd.Box(1, 1, 1).faces()[0]
    report = validate(face, build_id="b1")
    assert not _by_id(report.checks, "V1.solid_count").passed
    assert not _by_id(report.checks, "V1.closed_shell").passed


def test_two_disjoint_solids_fail_v1() -> None:
    part = bd.Box(1, 1, 1) + bd.Pos(5, 0, 0) * bd.Box(1, 1, 1)
    assert not _by_id(validate(part, build_id="b1").checks, "V1.solid_count").passed


def test_volume_is_advisory(plate: bd.Part) -> None:
    expected = ExpectedGeometry(volume=1.0)
    report = validate(plate, build_id="b1", expected=expected)
    assert report.passed
    assert not _by_id(report.advisory, "V2.volume").passed


@pytest.mark.parametrize(("schema", "header"), [("AP242", "AP242"), ("AP214", "AUTOMOTIVE_DESIGN")])
def test_export_step_schema(plate: bd.Part, tmp_path: Path, schema: str, header: str) -> None:
    path = export_step(plate, tmp_path / "part.step", schema=schema)  # type: ignore[arg-type]
    assert header in read_schema(path)


def test_unreadable_step_fails_v3(plate: bd.Part, tmp_path: Path) -> None:
    broken = tmp_path / "broken.step"
    broken.write_text("not a step file")
    report = validate(plate, build_id="b1", step_path=broken)
    assert not _by_id(report.checks, "V3.reimport").passed


def test_circular_pocket_is_not_an_unexpected_hole() -> None:
    plate = bd.Box(40, 30, 10) - bd.Pos(0, 0, 3) * bd.Cylinder(5, 4)  # blind circular pocket
    report = validate(plate, build_id="b1", expected=ExpectedGeometry(holes=[]))
    assert report.passed, report.feedback()
    assert not _by_id(report.advisory, "V2.hole_count").passed
