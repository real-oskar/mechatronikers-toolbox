"""Executes one BuildJob inside the isolated runner process and writes its outputs."""

import hashlib
import time
import traceback
from pathlib import Path
from typing import Any

import build123d as bd

from mechatronikers.cad_mcp.build import build_features
from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.cad_mcp.validation import validate
from mechatronikers.spec import BuildArtifact, BuildJob, BuildResult, Check, ValidationReport
from mechatronikers.spec.build import features_sha256
from mechatronikers.telemetry import EventEmitter


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def failure_report(build_id: str, check_id: str, detail: str) -> ValidationReport:
    return ValidationReport(
        build_id=build_id,
        checks=[
            Check(
                id=check_id,
                category="V0_execution",
                expected="completes",
                measured=check_id.removeprefix("V0."),
                passed=False,
                detail=detail,
            )
        ],
    )


def _run_script(script: str) -> bd.Shape[Any]:
    namespace: dict[str, Any] = {}
    exec("from build123d import *", namespace)  # noqa: S102 - runner is the sandbox (docs/05)
    exec(compile(script, "<build-script>", "exec"), namespace)  # noqa: S102
    result = namespace.get("result")
    if not isinstance(result, bd.Shape):
        raise TypeError("script must assign a build123d shape to `result`")
    return result


def execute(job: BuildJob, out_dir: Path, emitter: EventEmitter) -> BuildResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    # The feature-list hash ties builds of the same geometry together (#101).
    digest = features_sha256(job.features) if job.kind == "features" else None
    hashed = {"features_sha256": digest} if digest else {}
    emitter.emit("build.started", {"build_id": job.build_id, "kind": job.kind, **hashed})
    try:
        if job.kind == "script":
            if not job.script:
                raise ValueError("script job without script")
            shape = _run_script(job.script)
        else:
            shape = build_features(job.features)
        step = export_step(shape, out_dir / "part.step", schema=job.step_schema)
        brep = out_dir / "part.brep"
        bd.export_brep(shape, brep)
        report = validate(shape, build_id=job.build_id, expected=job.expected, step_path=step)
        step_format = "step-ap242" if job.step_schema == "AP242" else "step-ap214"
        artifacts = [
            BuildArtifact(name=p.name, format=f, bytes=p.stat().st_size, sha256=_sha256(p))
            for p, f in ((step, step_format), (brep, "brep"))
        ]
        result = BuildResult(
            build_id=job.build_id,
            status="ok",
            duration_ms=(time.monotonic() - start) * 1000,
            report=report,
            artifacts=artifacts,
            features_sha256=digest,
        )
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(exc)).strip()
        result = BuildResult(
            build_id=job.build_id,
            status="failed",
            error=detail,
            duration_ms=(time.monotonic() - start) * 1000,
            report=failure_report(job.build_id, "V0.error", detail),
            features_sha256=digest,
        )
    (out_dir / "result.json").write_text(result.model_dump_json(indent=2))
    emitter.emit(
        "build.completed" if result.status == "ok" else "build.failed",
        {
            "build_id": job.build_id,
            "passed": result.report.passed,
            "error": result.error,
            **hashed,
        },
        level="info" if result.status == "ok" else "error",
        duration_ms=result.duration_ms,
    )
    return result
