"""Pipeline interface for test bench runs, plus a harness self-check pipeline.

The real pipeline (agent + MCP servers, #17) plugs in as `module:function` with this signature:

    def run_case(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome
"""

import importlib
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from mechatronikers.cad_mcp.step_io import export_step, import_step
from mechatronikers.cad_mcp.validation import validate
from mechatronikers.spec import ValidationReport
from mechatronikers.testbench.cases import TestCase, ensure_materialized

DEFAULT_PIPELINE = "mechatronikers.agent.pipeline:run_case"

# A pipeline that calls models writes its event stream here, in its case folder (as
# mechatronikers.agent.pipeline does); the test bench reads the recorded spend from it.
MODEL_EVENTS = "agent-events.ndjson"


class MissingDimension(BaseModel):
    """A dimension the drawing did not state; the metric requires a reason for each (M3)."""

    id: str
    reason: str | None


class PipelineOutcome(BaseModel):
    step_path: Path | None  # exported STEP of the generated model
    report: ValidationReport | None = None  # final MCP validation report
    missing_dimensions: list[MissingDimension] | None = None  # None = pipeline does not report
    session_id: str | None = None  # session whose events document the run
    # Ids of review items still open with severity "blocking" (D91: a solved case has none);
    # None = the pipeline does not report them, so the case cannot count as solved.
    blocking_review_items: list[str] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. prompt version hashes, model params


class ModelUsage(BaseModel):
    """Model spend of one case: credits, calls and tokens."""

    cost: float = 0.0  # provider credits (USD on OpenRouter), as recorded by the budget guard
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            cost=round(self.cost + other.cost, 6),
            model_calls=self.model_calls + other.model_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


def model_usage(out_dir: Path) -> ModelUsage | None:
    """Sum of the `model.response` events in `out_dir/agent-events.ndjson`, or None if the
    pipeline wrote no model events (e.g. the self-check).

    The agent's ModelClient emits one `model.response` per call, right after the budget guard
    recorded its cost, so this is the run's recorded spend. It is read from the file rather than
    from the pipeline's return value, so a case whose pipeline crashed still reports what it cost.
    """
    path = out_dir / MODEL_EVENTS
    if not path.is_file():
        return None
    usage = ModelUsage()
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:  # blank or cut off by a crash mid-write
            continue
        if not isinstance(event, dict) or event.get("type") != "model.response":
            continue
        payload = event.get("payload") or {}
        usage += ModelUsage(
            cost=float(payload.get("cost", 0.0)),
            model_calls=1,
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
        )
    return usage


class Pipeline(Protocol):
    """A pipeline may also take `timeout_s`: the case's wall-clock limit (`--case-timeout`). It
    then ends the case itself, shelves its session and raises `CaseTimeoutError` (#145)."""

    def __call__(
        self, case: TestCase, *, model: str, variant: str, out_dir: Path
    ) -> PipelineOutcome: ...


class CaseTimeoutError(TimeoutError):
    """The case ran longer than its limit; recorded as `pipeline_error` with `timeout: ...`."""


def load_pipeline(spec: str) -> Pipeline:
    """Resolve 'package.module:function'."""
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"pipeline must look like 'module:function', got {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        hint = " (the agent pipeline lands with #17)" if spec == DEFAULT_PIPELINE else ""
        raise ValueError(f"cannot import pipeline module {module_name!r}{hint}") from exc
    pipeline: Pipeline = getattr(module, attr)
    return pipeline


def reference_roundtrip(
    case: TestCase, *, model: str, variant: str, out_dir: Path
) -> PipelineOutcome:
    """HARNESS SELF-CHECK ONLY: re-exports the human model through our STEP writer.

    It reads the reference, so its results say nothing about the pipeline. Use it to verify the
    test bench itself (alignment, metrics, report) on real data: every case should match.
    """
    shape = import_step(ensure_materialized(case.reference_step))
    step = export_step(shape, out_dir / "generated.step")
    return PipelineOutcome(
        step_path=step,
        report=validate(shape, build_id=f"roundtrip-{case.case_id}", step_path=step),
        missing_dimensions=[],
        blocking_review_items=[],
        meta={"self_check": True},
    )
