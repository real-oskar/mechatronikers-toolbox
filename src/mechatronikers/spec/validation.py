"""Validation contract: what cad-mcp measures and reports after every build (docs/03, docs/05)."""

from typing import Literal

from pydantic import BaseModel, Field, computed_field

Vec3 = tuple[float, float, float]
Size3 = tuple[float | None, float | None, float | None]  # None: that axis is not checked

CheckCategory = Literal[
    "V0_execution",
    "V1_geometry",
    "V2_dimensions",
    "V3_step_roundtrip",
    "V4_drawing_consistency",
    "ERC",  # electrical rule checks of a circuit run (E1–E7 scored, A1–A3 advisory, docs/12)
]


class Check(BaseModel):
    """One machine-readable check result: the checks shape of both run kinds (D75)."""

    id: str
    category: CheckCategory
    expected: int | float | str | None = None
    measured: int | float | str | None = None
    tolerance: float | None = None
    passed: bool
    detail: str = ""
    feature_ids: list[str] = Field(default_factory=list)  # circuits: refdes, terminal, net ids


class ValidationReport(BaseModel):
    """Result of validating one build. Scored checks gate the fix loop; advisory ones do not."""

    build_id: str
    checks: list[Check] = Field(default_factory=list)
    advisory: list[Check] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(c.passed for c in self.checks) / len(self.checks)

    def feedback(self) -> str:
        """Failed scored checks as short lines for the model's next attempt."""
        failed = [c for c in self.checks if not c.passed]
        if not failed:
            return "All checks passed."
        lines = []
        for c in failed:
            line = f"- {c.id}: expected {c.expected}, measured {c.measured}"
            if c.tolerance is not None:
                line += f" (tol ±{c.tolerance})"
            if c.detail:
                line += f" -- {c.detail}"
            lines.append(line)
        return "\n".join(lines)


class ExpectedHole(BaseModel):
    """A hole the model must contain, in the part frame (mm)."""

    id: str
    diameter: float
    center: Vec3 | None = None  # any point on the hole axis
    axis: Vec3 | None = None
    feature_ids: list[str] = Field(default_factory=list)


class ExpectedGeometry(BaseModel):
    """V2 targets, derived from the dimension values a part was built from."""

    bbox_size: Size3 | None = None  # axis-aligned extents in the part frame
    holes: list[ExpectedHole] | None = None  # None = do not check holes; [] = expect none
    volume: float | None = None  # advisory only
