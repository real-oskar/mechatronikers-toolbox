"""Test bench runs: pipeline over a test set, compared with the human models (docs/09)."""

import inspect
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from mechatronikers.cad_mcp.step_io import import_step
from mechatronikers.spec import SPEC_VERSION
from mechatronikers.testbench.cases import TestCase, ensure_materialized
from mechatronikers.testbench.compare import CompareResult, compare
from mechatronikers.testbench.pipelines import (
    CaseTimeoutError,
    ModelUsage,
    Pipeline,
    PipelineOutcome,
    model_usage,
)

SUPPORTED_VARIANTS = ("vector",)  # raster variants follow with #22


class ResultRow(BaseModel):
    case_id: str
    model: str
    variant: str
    repetition: int
    status: str  # "ok" | "pipeline_error" | "compare_error"
    error: str | None = None
    duration_s: float
    m1_step_produced: bool
    m2_validation_passed: bool | None  # None: pipeline gave no report
    m3_missing_with_reason: bool | None  # None: pipeline does not report missing dimensions
    missing_dimensions: int | None = None
    session_id: str | None = None
    blocking_review_items: list[str] | None = None  # open, severity "blocking"; None: not reported
    # Model spend of the case (#58), from the recorded model events; None = nothing recorded
    cost: float | None = None  # provider credits
    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    comparison: CompareResult | None = None
    meta: dict[str, object] = Field(default_factory=dict)

    @property
    def solved(self) -> bool:
        """Docs/09 definition (D91): M2 passes, the geometry matches the human model (IoU, holes,
        Hausdorff) and no blocking review item remains. A pipeline that reports no blocking
        items (None, also rows written before #93) cannot count as solved, like a missing M2."""
        return bool(
            self.m2_validation_passed
            and self.comparison is not None
            and self.comparison.matches_reference
            and self.blocking_review_items == []
        )

    @property
    def usage(self) -> ModelUsage | None:
        if self.cost is None:
            return None
        return ModelUsage(
            cost=self.cost,
            model_calls=self.model_calls or 0,
            input_tokens=self.input_tokens or 0,
            output_tokens=self.output_tokens or 0,
        )

    def record_usage(self, case_dir: Path) -> None:
        """Take the case's model spend from the events its pipeline wrote to `case_dir`."""
        usage = model_usage(case_dir)
        if usage is not None:
            self.cost, self.model_calls = usage.cost, usage.model_calls
            self.input_tokens, self.output_tokens = usage.input_tokens, usage.output_tokens


class RunInfo(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None = None
    pipeline: str
    test_set: str
    cases: list[str]
    models: list[str]
    variants: list[str]
    repetitions: int
    seed: int
    git_commit: str | None
    spec_version: str
    python: str
    host: str
    merged_from: list[str] = Field(default_factory=list)  # run ids, if merged (testbench merge)
    rerun_cases: dict[str, str] = Field(default_factory=dict)  # case -> run id it was taken from


def model_dir(model: str) -> str:
    """File-system safe folder name for a model id (OpenRouter ids contain '/')."""
    return model.replace(":", "_").replace("/", "_")


def case_dir(run_dir: Path, case_id: str, model: str, variant: str, repetition: int) -> Path:
    """Working folder of one case × model × variant × repetition inside a run folder."""
    return run_dir / "cases" / case_id / model_dir(model) / variant / f"rep{repetition}"


def _git_commit() -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return proc.stdout.strip() or None


def _row(
    case: TestCase,
    model: str,
    variant: str,
    repetition: int,
    pipeline: Pipeline,
    out_dir: Path,
    seed: int,
    timeout_s: float | None = None,
) -> ResultRow:
    start = time.monotonic()
    base = {"case_id": case.case_id, "model": model, "variant": variant, "repetition": repetition}
    limit = {"timeout_s": timeout_s} if timeout_s and takes_timeout(pipeline) else {}
    try:
        outcome: PipelineOutcome = pipeline(
            case, model=model, variant=variant, out_dir=out_dir, **limit
        )
    except Exception as exc:  # a crashing pipeline is a result, not a crashed run
        # Only the case limit is a timeout here; any other TimeoutError is an ordinary failure.
        message = str(exc) or "no message"
        failed = ResultRow(
            **base,
            status="pipeline_error",
            error=f"timeout: {message}"
            if isinstance(exc, CaseTimeoutError)
            else f"{type(exc).__name__}: {message}",
            duration_s=time.monotonic() - start,
            m1_step_produced=False,
            m2_validation_passed=None,
            m3_missing_with_reason=None,
        )
        failed.record_usage(out_dir)  # what it spent before it crashed
        return failed
    produced = outcome.step_path is not None and outcome.step_path.is_file()
    missing = outcome.missing_dimensions
    row = ResultRow(
        **base,
        status="ok",
        duration_s=0.0,
        m1_step_produced=produced,
        m2_validation_passed=None if outcome.report is None else outcome.report.passed,
        m3_missing_with_reason=None if missing is None else all(m.reason for m in missing),
        missing_dimensions=None if missing is None else len(missing),
        session_id=outcome.session_id,
        blocking_review_items=outcome.blocking_review_items,
        meta=outcome.meta,
    )
    row.record_usage(out_dir)
    if produced:
        assert outcome.step_path is not None
        try:
            row.comparison = compare(
                import_step(outcome.step_path),
                import_step(ensure_materialized(case.reference_step)),
                generated_name=str(outcome.step_path),
                reference_name=str(case.reference_step),
                seed=seed,
            )
        except Exception as exc:
            row.status, row.error = "compare_error", f"{type(exc).__name__}: {exc}"
    row.duration_s = time.monotonic() - start
    return row


def takes_timeout(pipeline: Pipeline) -> bool:
    """Whether the pipeline enforces `--case-timeout` itself (it has a `timeout_s` parameter)."""
    try:
        return "timeout_s" in inspect.signature(pipeline).parameters
    except (TypeError, ValueError):
        return False


def run(
    cases: list[TestCase],
    pipeline: Pipeline,
    *,
    pipeline_name: str,
    test_set: str,
    models: list[str],
    variants: list[str],
    out_dir: Path,
    repetitions: int = 1,
    seed: int = 0,
    progress: bool = False,
    case_timeout_s: float | None = None,
) -> tuple[RunInfo, list[ResultRow]]:
    """Run every case × model × variant × repetition; writes run.json, results.jsonl, summary.md.
    `case_timeout_s`: wall-clock limit per case for a pipeline that takes `timeout_s` (#145); a
    case over it is a `pipeline_error` with `timeout: ...`, and the run goes on."""
    unsupported = [v for v in variants if v not in SUPPORTED_VARIANTS]
    if unsupported:
        raise ValueError(f"unsupported variants {unsupported}; supported: {SUPPORTED_VARIANTS}")
    started = datetime.now(UTC)
    info = RunInfo(
        run_id=f"run-{started:%Y%m%d-%H%M%S}",
        started_at=started.isoformat(),
        pipeline=pipeline_name,
        test_set=test_set,
        cases=[c.case_id for c in cases],
        models=models,
        variants=variants,
        repetitions=repetitions,
        seed=seed,
        git_commit=_git_commit(),
        spec_version=SPEC_VERSION,
        python=platform.python_version(),
        host=platform.platform(),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[ResultRow] = []
    with (out_dir / "results.jsonl").open("w") as results:
        for case in cases:
            for model in models:
                for variant in variants:
                    for rep in range(1, repetitions + 1):
                        work = case_dir(out_dir, case.case_id, model, variant, rep)
                        work.mkdir(parents=True, exist_ok=True)
                        row = _row(case, model, variant, rep, pipeline, work, seed, case_timeout_s)
                        rows.append(row)
                        results.write(row.model_dump_json() + "\n")
                        results.flush()
                        if progress:
                            cost = "" if row.cost is None else f"  ({row.cost:.4f} credits)"
                            print(
                                f"{case.case_id:<16} {model:<24} {variant:<10} {_verdict(row)}"
                                + cost
                            )
    info.finished_at = datetime.now(UTC).isoformat()
    (out_dir / "run.json").write_text(info.model_dump_json(indent=2))
    (out_dir / "summary.md").write_text(summary_markdown(info, rows))
    return info, rows


def _verdict(row: ResultRow) -> str:
    if row.status != "ok":
        return f"{row.status}: {row.error}"
    return "solved" if row.solved else "not solved"


def total_usage(rows: list[ResultRow]) -> tuple[ModelUsage, int]:
    """Summed model spend of all rows, and the number of rows without recorded spend."""
    total, missing = ModelUsage(), 0
    for row in rows:
        usage = row.usage
        if usage is None:
            missing += 1
        else:
            total += usage
    return total, missing


def usage_text(rows: list[ResultRow]) -> str:
    """One line for summaries and the CLI: total credits, calls and tokens."""
    total, missing = total_usage(rows)
    text = (
        f"{total.cost:.4f} credits · {total.model_calls} model calls · "
        f"{total.input_tokens:,} input / {total.output_tokens:,} output tokens"
    )
    if missing:
        text += f" · no recorded spend for {missing} of {len(rows)} rows"
    return text


def _cost_table(rows: list[ResultRow]) -> list[str]:
    total, _ = total_usage(rows)
    lines = [
        "## Cost",
        "",
        "| Case | Model | Variant | Rep | Model calls | Input tokens | Output tokens "
        "| Cost (credits) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        u = r.usage
        counts = (
            "| – | – | – "
            if u is None
            else f"| {u.model_calls} | {u.input_tokens:,} | {u.output_tokens:,} "
        )
        lines.append(
            f"| {r.case_id} | {r.model} | {r.variant} | {r.repetition} {counts}"
            f"| {_fmt(u.cost if u else None, 4)} |"
        )
    lines.append(
        f"| **Total** | | | | **{total.model_calls}** | **{total.input_tokens:,}** "
        f"| **{total.output_tokens:,}** | **{total.cost:.4f}** |"
    )
    return lines


# Why a case ended without a model (#92), in the words of the run view.
STOP_REASONS = {
    "cap": "rework limit reached",
    "plateau": "no improvement in two rework rounds",
    "error": "a step failed",
    "budget": "model budget used up",
}


def stop_text(row: ResultRow) -> str | None:
    """The give-up reason of a case, or None if it did not stop without a model."""
    reason = row.meta.get("stop_reason")
    if not isinstance(reason, str):
        return None
    text = STOP_REASONS.get(reason, reason)
    rounds, error = row.meta.get("fix_iterations"), row.meta.get("error")
    if reason == "cap" and rounds is not None:
        text += f" ({rounds} rounds)"
    if reason == "error" and error:
        text += f": {str(error)[:80]}"
    return text


def _listed(values: object) -> str:
    text = ", ".join(map(str, values)) if isinstance(values, list) and values else "–"
    return text.replace("|", "/")


def _stop_table(rows: list[ResultRow]) -> list[str]:
    lines = [
        "## Stopped without a model",
        "",
        "| Case | Model | Variant | Rep | Reason | Last failing checks | Features not built |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        reason = stop_text(r)
        if reason is None:
            continue
        checks, features = (_listed(r.meta.get(k)) for k in ("failing_checks", "failed_features"))
        lines.append(
            f"| {r.case_id} | {r.model} | {r.variant} | {r.repetition} "
            f"| {reason.replace('|', '/')} | {checks} | {features} |"
        )
    return [*lines, ""] if len(lines) > 4 else []


def _fmt(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _flag(value: bool | None) -> str:
    return "–" if value is None else ("✓" if value else "✗")


def summary_markdown(info: RunInfo, rows: list[ResultRow]) -> str:
    solved = sum(r.solved for r in rows)
    lines = [
        f"# Test bench run {info.run_id}",
        "",
        f"- Pipeline: `{info.pipeline}` · Test set: `{info.test_set}` · Models: "
        + ", ".join(f"`{m}`" for m in info.models),
        f"- Variants: {', '.join(info.variants)} · Repetitions: {info.repetitions}"
        f" · Seed: {info.seed}",
        f"- Commit: `{info.git_commit}` · Spec {info.spec_version} · Started {info.started_at}",
        f"- **Solved: {solved} / {len(rows)}** (D91: M2 passes, IoU ≥ 0.95, holes match, "
        "Hausdorff ≤ 1 mm, no blocking review item)",
        f"- **Cost: {usage_text(rows)}**",
        "",
        "| Case | Model | Variant | Rep | M1 STEP | M2 valid | M3 reasons | Blocking | IoU "
        "| 1 − IoU | Sym. diff (mm³) | Mean dev (mm) | Hausdorff (mm) | Holes gen/ref | Solved "
        "| Time (s) | Note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r.comparison
        holes = (
            "–"
            if c is None
            else (f"{len(c.hole_diameters_generated)}/{len(c.hole_diameters_reference)}")
        )
        note = (r.error or "")[:60].replace("|", "/")
        blocking = "–" if r.blocking_review_items is None else len(r.blocking_review_items)
        lines.append(
            f"| {r.case_id} | {r.model} | {r.variant} | {r.repetition} "
            f"| {_flag(r.m1_step_produced)} "
            f"| {_flag(r.m2_validation_passed)} | {_flag(r.m3_missing_with_reason)} "
            f"| {blocking} "
            f"| {_fmt(c.iou if c else None)} | {_fmt(c.difference_score if c else None, 4)} "
            f"| {_fmt(c.symmetric_difference_volume_mm3 if c else None, 1)} "
            f"| {_fmt(c.mean_surface_distance if c else None)} "
            f"| {_fmt(c.hausdorff if c else None)} "
            f"| {holes} | {_flag(r.solved)} | {r.duration_s:.1f} | {note} |"
        )
    lines += ["", *_stop_table(rows), *_cost_table(rows), ""]
    return "\n".join(lines)
