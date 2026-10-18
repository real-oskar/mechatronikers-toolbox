import json
from pathlib import Path

import build123d as bd
import pytest

from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.pipelines import (
    DEFAULT_PIPELINE,
    MissingDimension,
    PipelineOutcome,
    load_pipeline,
    reference_roundtrip,
)
from mechatronikers.testbench.run import ResultRow, run


@pytest.fixture
def cases(tmp_path: Path) -> list[TestCase]:
    part = bd.Box(30, 20, 8) - bd.Pos(8, 0, 0) * bd.Cylinder(3, 8)
    folder = tmp_path / "data" / "Plate"
    folder.mkdir(parents=True)
    (folder / "Plate_dwg.pdf").write_bytes(b"%PDF-1.4")
    step = export_step(part, folder / "Plate.stp")
    return [TestCase(case_id="Plate", drawing=folder / "Plate_dwg.pdf", reference_step=step)]


def test_self_check_pipeline_solves_and_writes_reports(
    cases: list[TestCase], tmp_path: Path
) -> None:
    out = tmp_path / "run"
    info, rows = run(
        cases,
        reference_roundtrip,
        pipeline_name="self-check",
        test_set="unit",
        models=["none"],
        variants=["vector"],
        out_dir=out,
    )
    (row,) = rows
    assert row.status == "ok" and row.solved
    assert row.m1_step_produced and row.m2_validation_passed and row.m3_missing_with_reason
    assert json.loads((out / "results.jsonl").read_text())["case_id"] == "Plate"
    assert json.loads((out / "run.json").read_text())["run_id"] == info.run_id
    summary = (out / "summary.md").read_text()
    assert "**Solved: 1 / 1**" in summary and "| Plate |" in summary
    assert "| 1 − IoU | Sym. diff (mm³) |" in summary
    stored = json.loads((out / "results.jsonl").read_text())["comparison"]
    assert stored["difference_score"] == pytest.approx(1 - stored["iou"])
    assert stored["symmetric_difference_volume_mm3"] is not None

    # rows written before the difference score (#5) still load and get it computed
    raw = json.loads((out / "results.jsonl").read_text())
    for key in (
        "difference_score",
        "difference_score_stderr",
        "symmetric_difference_volume_mm3",
        "symmetric_difference_volume_stderr",
    ):
        del raw["comparison"][key]
    old = ResultRow.model_validate_json(json.dumps(raw))
    assert old.comparison is not None and old.comparison.iou is not None
    assert old.comparison.difference_score == pytest.approx(1 - old.comparison.iou)
    assert old.solved
    # rows written before #93 report no blocking items: they load, but cannot count as solved
    del raw["blocking_review_items"]
    older = ResultRow.model_validate_json(json.dumps(raw))
    assert older.blocking_review_items is None and not older.solved


def test_open_blocking_review_item_means_not_solved(cases: list[TestCase], tmp_path: Path) -> None:
    def blocked(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
        outcome = reference_roundtrip(case, model=model, variant=variant, out_dir=out_dir)
        outcome.blocking_review_items = ["fail-1"]
        return outcome

    out = tmp_path / "run"
    _, (row,) = run(
        cases,
        blocked,
        pipeline_name="x",
        test_set="unit",
        models=["m"],
        variants=["vector"],
        out_dir=out,
    )
    assert row.comparison is not None and row.comparison.matches_reference
    assert row.m2_validation_passed and not row.solved
    assert "| ✓ | ✓ | ✓ | 1 |" in (out / "summary.md").read_text()  # M1, M2, M3, Blocking


def test_crashing_pipeline_is_recorded_not_raised(cases: list[TestCase], tmp_path: Path) -> None:
    def broken(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
        raise RuntimeError("model unavailable")

    _, (row,) = run(
        cases,
        broken,
        pipeline_name="broken",
        test_set="unit",
        models=["m"],
        variants=["vector"],
        out_dir=tmp_path / "run",
    )
    assert row.status == "pipeline_error" and "model unavailable" in (row.error or "")
    assert not row.solved and not row.m1_step_produced


def test_missing_dimension_without_reason_fails_m3(cases: list[TestCase], tmp_path: Path) -> None:
    def no_reason(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
        outcome = reference_roundtrip(case, model=model, variant=variant, out_dir=out_dir)
        outcome.missing_dimensions = [MissingDimension(id="d1", reason=None)]
        return outcome

    _, (row,) = run(
        cases,
        no_reason,
        pipeline_name="x",
        test_set="unit",
        models=["m"],
        variants=["vector"],
        out_dir=tmp_path / "run",
    )
    assert row.m3_missing_with_reason is False and row.missing_dimensions == 1


def test_unsupported_variant_and_missing_agent_pipeline(
    cases: list[TestCase], tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="unsupported variants"):
        run(
            cases,
            reference_roundtrip,
            pipeline_name="x",
            test_set="unit",
            models=["m"],
            variants=["raster-300"],
            out_dir=tmp_path / "run",
        )
    from mechatronikers.agent.pipeline import run_case

    assert load_pipeline(DEFAULT_PIPELINE) is run_case  # the agent pipeline (#17, #19)
    with pytest.raises(ValueError, match="cannot import"):
        load_pipeline("mechatronikers.nonexistent:run")
    assert (
        load_pipeline("mechatronikers.testbench.pipelines:reference_roundtrip")
        is reference_roundtrip
    )
