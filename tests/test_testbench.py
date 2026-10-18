from pathlib import Path

import build123d as bd
import pytest

from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.testbench import compare
from mechatronikers.testbench.__main__ import main
from mechatronikers.testbench.cases import LfsPointerError, challenge_set, ensure_materialized


@pytest.fixture
def bracket() -> bd.Part:
    """Asymmetric part, so the alignment has a unique answer."""
    part = bd.Box(40, 20, 8) + bd.Pos(-15, 0, 10) * bd.Box(10, 20, 12)
    return part - bd.Pos(10, 0, 0) * bd.Cylinder(3, 8) - bd.Pos(0, 5, 0) * bd.Cylinder(2, 8)


def test_identical_part_matches_after_rigid_motion(bracket: bd.Part) -> None:
    moved = bracket.moved(bd.Location((17, -4, 9), (1, 1, 0.3), 63))
    result = compare(moved, bracket, samples=1500)
    assert result.matches_reference
    assert result.iou is not None and result.iou > 0.995
    assert result.alignment_rms is not None and result.alignment_rms < 0.01
    assert result.mean_surface_distance < 0.01
    assert result.holes_matched == 2


def test_wrong_hole_diameter_is_detected(bracket: bd.Part) -> None:
    other = bd.Box(40, 20, 8) + bd.Pos(-15, 0, 10) * bd.Box(10, 20, 12)
    other = other - bd.Pos(10, 0, 0) * bd.Cylinder(4, 8) - bd.Pos(0, 5, 0) * bd.Cylinder(2, 8)
    result = compare(other, bracket, samples=1500)
    assert result.holes_matched == 1
    assert not result.matches_reference


def test_mirror_image_is_not_aligned_onto_original(bracket: bd.Part) -> None:
    mirrored = bracket.mirror(bd.Plane.YZ)
    result = compare(mirrored, bracket, samples=1500)
    assert result.iou is not None and result.iou < 0.99


def test_no_align_keeps_frames(bracket: bd.Part) -> None:
    result = compare(bracket, bracket, do_align=False, samples=1000)
    assert result.transform is None
    assert result.iou is not None and result.iou > 0.999


def _make_case(root: Path, name: str, step_content: bytes) -> None:
    folder = root / name
    folder.mkdir()
    (folder / f"{name}_dwg.pdf").write_bytes(b"%PDF-1.4")
    (folder / f"{name}.stp").write_bytes(step_content)


def test_challenge_set_and_lfs_pointer_detection(tmp_path: Path) -> None:
    _make_case(tmp_path, "Alpha", b"ISO-10303-21;")
    _make_case(tmp_path, "Beta", b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n")
    (tmp_path / "notes").mkdir()

    cases = challenge_set(tmp_path)
    assert [c.case_id for c in cases] == ["Alpha", "Beta"]
    ensure_materialized(cases[0].reference_step)
    with pytest.raises(LfsPointerError):
        ensure_materialized(cases[1].reference_step)


def test_cli_compare_exit_code(bracket: bd.Part, tmp_path: Path) -> None:
    a = export_step(bracket, tmp_path / "a.step")
    b = export_step(bracket.moved(bd.Location((5, 5, 5))), tmp_path / "b.step")
    assert main(["compare", str(a), str(b), "--samples", "1000", "--json"]) == 0


def test_cli_compare_prints_difference_score(
    bracket: bd.Part, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = export_step(bracket, tmp_path / "a.step")
    assert main(["compare", str(a), str(a), "--no-align", "--samples", "500"]) == 0
    out = capsys.readouterr().out
    assert "difference score (1 − IoU)  0.0000 ± 0.0" in out
    assert "difference score (1 − IoU)  0.0000 ± 0.0000\n" not in out  # never exactly known
    assert "symmetric difference" in out and " mm³" in out


def test_contains_handles_vertical_walls_and_holes() -> None:
    import numpy as np

    from mechatronikers.testbench.surface import contains

    part = bd.Box(10, 10, 10) - bd.Cylinder(2, 10)  # vertical walls + a through hole
    points = np.array([[4.0, 4.0, 0.0], [0.0, 0.0, 0.0], [6.0, 0.0, 0.0], [0.0, 0.0, 6.0]])
    assert contains(part, points).tolist() == [True, False, False, False]


def test_sampled_iou_matches_analytic_overlap() -> None:
    from mechatronikers.testbench.compare import estimate_iou

    a = bd.Box(10, 10, 10)
    b = bd.Pos(5, 0, 0) * bd.Box(10, 10, 10)  # overlap 500 of union 1500 -> IoU 1/3
    iou, stderr = estimate_iou(a, b)
    assert iou is not None and stderr is not None
    assert abs(iou - 1 / 3) < 4 * stderr + 1e-3


def test_difference_score_and_symmetric_difference_volume() -> None:
    a = bd.Box(10, 10, 10)
    b = bd.Pos(5, 0, 0) * bd.Box(10, 10, 10)  # IoU 1/3, symmetric difference 1000 mm³
    result = compare(a, b, do_align=False, samples=500)
    assert result.iou is not None and result.iou_stderr is not None
    assert result.difference_score == pytest.approx(1 - result.iou)
    assert result.difference_score_stderr == result.iou_stderr
    volume, stderr = (
        result.symmetric_difference_volume_mm3,
        result.symmetric_difference_volume_stderr,
    )
    assert volume is not None and stderr is not None
    assert abs(volume - 1000) < 4 * stderr + 1
    dumped = result.model_dump()
    assert dumped["difference_score"] == result.difference_score
    assert dumped["symmetric_difference_volume_mm3"] == volume


def test_no_disagreeing_sample_is_not_reported_as_exact() -> None:
    from mechatronikers.testbench.compare import estimate_iou

    box = bd.Box(10, 10, 10)
    iou, stderr = estimate_iou(box, box)
    assert iou == 1.0 and stderr is not None and 0 < stderr < 1e-3  # one-sample resolution
    result = compare(box, box, do_align=False, samples=200)
    thin = result.model_copy(update={"volume_generated": 1010.0, "volume_reference": 1000.0})
    assert thin.iou == 1.0 and thin.symmetric_difference_volume_mm3 == 10.0  # exact |ΔV|
    assert thin.difference_score_stderr == stderr


def test_hausdorff_above_one_mm_does_not_match(bracket: bd.Part) -> None:
    taller = bd.Box(40, 20, 8) + bd.Pos(-15, 0, 10.75) * bd.Box(10, 20, 13.5)  # boss +1.5 mm
    taller = taller - bd.Pos(10, 0, 0) * bd.Cylinder(3, 8) - bd.Pos(0, 5, 0) * bd.Cylinder(2, 8)
    result = compare(taller, bracket, do_align=False, samples=1500)
    assert result.iou is not None and result.iou >= 0.95  # IoU and holes alone would pass
    assert result.holes_matched == 2 and result.hausdorff == pytest.approx(1.5, abs=0.01)
    assert not result.matches_reference  # D91: Hausdorff ≤ 1 mm
