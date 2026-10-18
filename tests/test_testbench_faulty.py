"""The test bench flags every deliberately faulty Befestigung model (#5).

SF01–SF11 in tests/fixtures/step/faulty are altered copies of the human Befestigung model
(README there). Each must fail at least one check against the reference, and an unaltered,
moved copy must pass all of them, so the checks are not trivially strict.
"""

from pathlib import Path
from typing import Any

import build123d as bd
import pytest

from mechatronikers.cad_mcp.step_io import import_step
from mechatronikers.testbench.compare import (
    HAUSDORFF_LIMIT,
    IOU_THRESHOLD,
    CompareResult,
    compare,
)
from tests.fixtures.lfs import require_lfs

FAULTY_DIR = Path("tests/fixtures/step/faulty")
REFERENCE = Path("data/Befestigung/Befestigung.stp")
SAMPLES = 1000  # surface samples per side; alignment dominates the runtime (~6 s per file)

# Shows that the bench detects these changes: moved unaltered copies stay ≤ 0.013 mm Hausdorff at
# these settings, the faulty files are ≥ 0.2 mm. "Solved" uses 1 mm (D91, HAUSDORFF_LIMIT).
DEVIATION_LIMIT = 0.1

# Which check catches which file (seed 0). "iou" and "holes" are part of `matches_reference`;
# only a Hausdorff distance above DEVIATION_LIMIT catches all eleven.
CAUGHT_BY = {
    "SF01": {"deviation"},
    "SF02": {"deviation"},
    "SF03": {"deviation"},
    "SF04": {"deviation"},
    "SF05": {"deviation", "holes"},
    "SF06": {"deviation", "holes"},
    "SF07": {"deviation", "holes"},
    "SF08": {"deviation", "iou"},
    "SF09": {"deviation"},
    "SF10": {"deviation"},
    "SF11": {"deviation", "holes"},
}
# Known limit of D91: these defects (0.2–0.6 mm) stay below its 1 mm limit, so the files count
# as solved. SF04 (a 1.0 mm defect, 0.995 mm after alignment) sits at the limit: not asserted.
SOLVED_UNDER_D91 = {"SF01", "SF02", "SF03", "SF09", "SF10"}


@pytest.fixture(scope="module")
def reference() -> Any:
    return import_step(require_lfs(REFERENCE))


def failed_checks(result: CompareResult) -> set[str]:
    failed = set()
    if result.iou is None or result.iou < IOU_THRESHOLD:
        failed.add("iou")
    n_ref = len(result.hole_diameters_reference)
    if len(result.hole_diameters_generated) != n_ref or result.holes_matched != n_ref:
        failed.add("holes")
    if result.hausdorff > DEVIATION_LIMIT:
        failed.add("deviation")
    within_d91 = result.hausdorff <= HAUSDORFF_LIMIT
    assert result.matches_reference == (not failed & {"iou", "holes"} and within_d91)
    return failed


def test_fixture_set_is_complete() -> None:
    assert sorted(p.stem.removeprefix("Befestigung_") for p in FAULTY_DIR.glob("*.step")) == (
        sorted(CAUGHT_BY)
    )


def test_unaltered_moved_copy_passes_every_check(reference: Any) -> None:
    moved = reference.moved(bd.Location((12, -7, 30), (0.3, 1, 0.5), 41))
    result = compare(moved, reference, samples=SAMPLES)
    assert failed_checks(result) == set()
    assert result.difference_score is not None and result.difference_score < 0.01


@pytest.mark.parametrize("name", sorted(CAUGHT_BY))
def test_faulty_model_is_flagged(name: str, reference: Any) -> None:
    faulty = import_step(require_lfs(FAULTY_DIR / f"Befestigung_{name}.step"))
    result = compare(faulty, reference, samples=SAMPLES)
    assert failed_checks(result) == CAUGHT_BY[name], result.model_dump_json(indent=2)
    if name in SOLVED_UNDER_D91:
        assert result.matches_reference
