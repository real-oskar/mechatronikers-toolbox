"""Build runner contract: what cad-mcp sends to the runner and what comes back (docs/05)."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from mechatronikers.spec.validation import ExpectedGeometry, ValidationReport


class BuildJob(BaseModel):
    """One isolated build. Exactly one of `features` (kind "features") or `script` is used."""

    build_id: str
    kind: Literal["features", "script"]
    features: list[dict[str, Any]] = Field(default_factory=list)  # PartSpec feature list (#8)
    script: str | None = None  # fallback: build123d code that assigns the solid to `result`
    expected: ExpectedGeometry | None = None
    step_schema: Literal["AP214", "AP242"] = "AP242"
    timeout_s: float = 60.0


def features_sha256(features: list[dict[str, Any]]) -> str:
    """SHA-256 of a feature list (JSON, keys sorted), as `BuildJob.features` carries it. Builds
    with the same hash built the same geometry, even when their build ids differ (#101)."""
    canonical = json.dumps(features, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class BuildArtifact(BaseModel):
    name: str  # file name inside the build output directory
    format: Literal["step-ap242", "step-ap214", "brep"]
    bytes: int
    sha256: str


class BuildResult(BaseModel):
    build_id: str
    status: Literal["ok", "failed", "timeout"]
    error: str | None = None
    duration_ms: float
    report: ValidationReport
    artifacts: list[BuildArtifact] = Field(default_factory=list)
    features_sha256: str | None = None  # `features_sha256(job.features)`; None for scripts
