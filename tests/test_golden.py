"""Golden test (#18): Fuss built from its hand-written spec, without any model."""

from pathlib import Path

from mechatronikers.cad_mcp.build import build_features
from mechatronikers.cad_mcp.step_io import import_step
from mechatronikers.cad_mcp.validation import validate
from mechatronikers.testbench.compare import compare
from tests.fixtures.lfs import require_lfs
from tests.fixtures.specs import fuss

REFERENCE = Path("data/Fuss/Fuss.stp")


def _part():  # type: ignore[no-untyped-def]
    return build_features([f.model_dump(mode="json") for f in fuss().features])


def test_fuss_passes_v1_to_v3() -> None:
    report = validate(_part(), build_id="golden-fuss", expected=fuss().expected_geometry())
    assert report.passed, report.feedback()


def test_fuss_matches_the_human_model() -> None:
    result = compare(_part(), import_step(require_lfs(REFERENCE)), samples=2000)
    assert result.matches_reference
    assert result.iou is not None and result.iou > 0.999
    assert result.mean_surface_distance < 0.01
