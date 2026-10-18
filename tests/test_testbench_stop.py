"""The test bench summary says why a case ended without a model (#92)."""

from typing import Any

from mechatronikers.testbench.run import ResultRow, RunInfo, summary_markdown

INFO = RunInfo(
    run_id="run-unit",
    started_at="2026-10-17T08:00:00+00:00",
    pipeline="unit",
    test_set="unit",
    cases=[],
    models=["m"],
    variants=["vector"],
    repetitions=1,
    seed=0,
    git_commit=None,
    spec_version="0",
    python="3",
    host="unit",
)


def _row(case_id: str, **meta: Any) -> ResultRow:
    return ResultRow(
        case_id=case_id,
        model="m",
        variant="vector",
        repetition=1,
        status="ok",
        duration_s=1.0,
        m1_step_produced=False,
        m2_validation_passed=False,
        m3_missing_with_reason=True,
        meta=meta,
    )


def test_summary_lists_the_give_up_reason_per_case() -> None:
    rows = [
        _row("Fuss", stop_reason="cap", fix_iterations=3, failing_checks=["V2.bbox_x"]),
        _row("Halter", stop_reason="plateau", failing_checks=[], failed_features=["bore"]),
        _row("Platte", stop_reason="error", error="fix: the model answered without JSON"),
        _row("Welle", stop_reason="budget"),
        _row("Solved", status="done", stop_reason=None),
    ]
    stopped = summary_markdown(INFO, rows).split("## Stopped without a model")[1]
    stopped = stopped.split("## Cost")[0]
    assert "| Fuss | m | vector | 1 | rework limit reached (3 rounds) | V2.bbox_x | – |" in stopped
    assert "| no improvement in two rework rounds | – | bore |" in stopped
    assert "| a step failed: fix: the model answered without JSON | – | – |" in stopped
    assert "| Welle | m | vector | 1 | model budget used up |" in stopped
    assert "Solved" not in stopped


def test_summary_without_give_ups_has_no_stop_section() -> None:
    text = summary_markdown(INFO, [_row("Solved", status="done")])
    assert "Stopped without a model" not in text
