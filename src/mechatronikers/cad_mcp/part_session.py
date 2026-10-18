"""State behind the cad-mcp tools: one part as a feature list, rebuilt after every change (docs/05).

The feature list is the state; the solid is never edited directly. Every change is built and
validated in the build runner. A change whose build fails or does not pass validation is rolled
back, so the part always stays at its last good state and the caller gets the report that names
the failing feature.
"""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Literal

import build123d as bd
from pydantic import BaseModel, TypeAdapter

from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.runner.launcher import Isolation, run_build
from mechatronikers.spec import (
    SPEC_VERSION,
    BuildJob,
    BuildResult,
    Dimension,
    Feature,
    PartSpec,
    ReviewItem,
    ScriptFeature,
    ValidationReport,
)
from mechatronikers.spec.build import features_sha256
from mechatronikers.spec.validation import Size3
from mechatronikers.telemetry import EventEmitter

ExportFormat = Literal["step-ap242", "step-ap214", "stl", "obj", "features-json", "review-json"]
ViewName = Literal["front", "top", "right", "iso"]

DimIds3 = tuple[str | None, str | None, str | None]

_FEATURE: TypeAdapter[Feature] = TypeAdapter(Feature)

# Camera direction (towards the viewer) and up vector per view; part frame is Z up.
_VIEWS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}


class ArtifactRef(BaseModel):
    uri: str  # artifact://<session>/<build>/<file>
    path: str
    format: str
    bytes: int
    sha256: str
    # The build the file was made from, and its feature-list hash (#101; absent before)
    build_id: str | None = None
    features_sha256: str | None = None


class ChangeResult(BaseModel):
    applied: bool  # False: the change was rolled back
    feature_count: int
    build: BuildResult
    message: str


class PartState(BaseModel):
    part_name: str | None
    overall_size: Size3 | None
    overall_size_dims: DimIds3 | None
    notes: list[str]  # e.g. overall size axes that are not checked, and why
    features: list[dict[str, Any]]
    last_build_id: str | None
    last_report_passed: bool | None
    bbox: tuple[float, float, float] | None
    volume: float | None


def gating_failures(result: BuildResult) -> list[str]:
    """Checks that block a change. The overall size (V2.bbox_*) cannot match while the part is
    still being built up, so it is reported as progress but does not block."""
    if result.status != "ok":
        return [result.error or result.status]
    return [c.id for c in result.report.checks if not c.passed and not c.id.startswith("V2.bbox")]


def printed_overall_size(
    dims_by_axis: DimIds3, dimensions: list[Dimension]
) -> tuple[Size3, list[str]]:
    """Overall size from the printed dimensions named per axis (D53). The caller only chooses
    which dimension spans the part; the value is the printed one, or the reviewer's edit.
    Anything else leaves the axis unchecked, with a note for the reviewer."""
    by_id = {d.id: d for d in dimensions}
    size: list[float | None] = []
    notes: list[str] = []
    for axis, dim_id in zip("xyz", dims_by_axis, strict=True):
        d = by_id.get(dim_id) if dim_id else None
        if dim_id is None:
            problem = "no single printed dimension spans the part"
        elif d is None:
            problem = f"unknown dimension {dim_id}"
        elif d.kind not in ("linear", "diameter"):
            problem = f"{dim_id} '{d.stated_text}' is a {d.kind} dimension"
        elif d.stated is None and d.method != "human":
            problem = f"{dim_id} is not printed on the drawing"
        elif d.status == "rejected":
            problem = f"{dim_id} was rejected"
        else:
            # A reviewer's edit (method "human", recorded with old and new value) wins.
            size.append(d.value if d.method == "human" else d.stated)
            continue
        size.append(None)
        notes.append(f"Overall size {axis} not checked: {problem}")
    return (size[0], size[1], size[2]), notes


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PartSession:
    def __init__(
        self,
        builds_dir: Path,
        artifacts_dir: Path,
        isolation: Isolation,
        emitter: EventEmitter,
    ) -> None:
        self.builds_dir = builds_dir
        self.artifacts_dir = artifacts_dir
        self.isolation = isolation
        self.emitter = emitter
        self.part_name: str | None = None
        self.overall_size: Size3 | None = None
        self.overall_size_dims: DimIds3 | None = None
        self.notes: list[str] = []
        self.features: list[Feature] = []
        self.dimensions: list[Dimension] = []
        self.review_items: list[ReviewItem] = []
        self.last_good: BuildResult | None = None

    # -- state -------------------------------------------------------------------------

    def spec(self, features: list[Feature] | None = None) -> PartSpec:
        return PartSpec(
            spec_version=SPEC_VERSION,
            part_name=self.part_name or "part",
            overall_size=self.overall_size,
            overall_size_dims=self.overall_size_dims,
            dimensions=self.dimensions,
            features=self.features if features is None else features,
            review_items=self.review_items,
        )

    def new_part(
        self,
        part_name: str,
        overall_size: Size3 | None,
        dimensions: list[Dimension] | None = None,
        review_items: list[ReviewItem] | None = None,
        overall_size_dims: DimIds3 | None = None,
    ) -> None:
        """Start a part. Dimensions (from the drawing) may be referenced by features. With
        `overall_size_dims` the overall size comes from those printed dimensions (preferred)."""
        if overall_size is not None and overall_size_dims is not None:
            raise ValueError("give overall_size or overall_size_dims, not both")
        self.dimensions = list(dimensions or [])
        self.notes = []
        if overall_size_dims is not None:
            overall_size, self.notes = printed_overall_size(overall_size_dims, self.dimensions)
            # provenance only for the axes that resolved to a printed value
            resolved = zip(overall_size_dims, overall_size, strict=True)
            x, y, z = (i if v is not None else None for i, v in resolved)
            overall_size_dims = (x, y, z)
        self.part_name, self.overall_size = part_name, overall_size
        self.overall_size_dims = overall_size_dims
        self.review_items = list(review_items or [])
        self.features, self.last_good = [], None
        self.emitter.emit(
            "part.created",
            {
                "part_name": part_name,
                "overall_size": overall_size,
                "overall_size_dims": overall_size_dims,
                "notes": self.notes,
            },
        )

    def state(self) -> PartState:
        bbox = volume = None
        if self.last_good is not None:
            shape = self._shape()
            size = shape.bounding_box(optimal=True).size
            bbox, volume = (size.X, size.Y, size.Z), shape.volume
        return PartState(
            part_name=self.part_name,
            overall_size=self.overall_size,
            overall_size_dims=self.overall_size_dims,
            notes=self.notes,
            features=[f.model_dump(mode="json") for f in self.features],
            last_build_id=None if self.last_good is None else self.last_good.build_id,
            last_report_passed=None if self.last_good is None else self.last_good.report.passed,
            bbox=bbox,
            volume=volume,
        )

    # -- changes -------------------------------------------------------------------------

    def _build(self, features: list[Feature]) -> BuildResult:
        expected = self.spec(features).expected_geometry()
        job = BuildJob(
            build_id=f"build-{uuid.uuid4().hex[:12]}",
            kind="features",
            features=[f.model_dump(mode="json") for f in features],
            expected=expected,
        )
        result = run_build(job, self.builds_dir, isolation=self.isolation, emitter=self.emitter)
        # Hashed here, not taken from the runner's result file, which a build script could write.
        result.features_sha256 = features_sha256(job.features)
        self._validated(result, features)
        return result

    def _validated(self, result: BuildResult, features: list[Feature]) -> None:
        # The full report, so the review app's validation gate never has to guess (#20).
        self.emitter.emit(
            "validation.completed",
            {
                "build_id": result.build_id,
                "features_sha256": result.features_sha256,
                "status": result.status,
                "passed": result.report.passed,
                "feature_ids": [f.id for f in features],
                "report": result.report.model_dump(mode="json"),
            },
            level="info" if result.report.passed else "warning",
        )

    def _apply(self, candidate: list[Feature], action: str, feature_id: str) -> ChangeResult:
        try:
            self.spec(candidate)  # structural rules (base first, unique ids, …)
        except ValueError as exc:
            raise ValueError(f"{action} {feature_id} rejected: {exc}") from exc
        result = self._build(candidate) if candidate else None
        if result is not None and gating_failures(result):
            self.emitter.emit(
                "spec.change_rejected",
                {"action": action, "feature_id": feature_id, "build_id": result.build_id},
                level="warning",
            )
            return ChangeResult(
                applied=False,
                feature_count=len(self.features),
                build=result,
                message=f"{action} {feature_id} rolled back: "
                + (result.error or result.report.feedback()),
            )
        self.features = candidate
        self.last_good = result
        self.emitter.emit(f"spec.feature_{action}", {"feature_id": feature_id})
        open_checks = [] if result is None else [c for c in result.report.checks if not c.passed]
        progress = "; open: " + ", ".join(c.id for c in open_checks) if open_checks else ""
        return ChangeResult(
            applied=True,
            feature_count=len(candidate),
            build=result if result is not None else self._empty_result(),
            message=f"{action} {feature_id}: applied{progress or ', all checks passed'}",
        )

    def _empty_result(self) -> BuildResult:
        return BuildResult(
            build_id="none", status="ok", duration_ms=0, report=ValidationReport(build_id="none")
        )

    def add_feature(self, feature: Feature | dict[str, Any]) -> ChangeResult:
        parsed = _FEATURE.validate_python(feature) if isinstance(feature, dict) else feature
        result = self._apply([*self.features, parsed], "added", parsed.id)
        if result.applied and isinstance(parsed, ScriptFeature):
            self.review_items.append(
                ReviewItem(
                    id=f"review-{parsed.id}",
                    kind="unsupported",
                    severity="warning",
                    message=f"Feature {parsed.id} was built with free code (fallback)",
                    reason=parsed.reason,
                    feature_ids=[parsed.id],
                )
            )
        return result

    def update_feature(self, feature_id: str, changes: dict[str, Any]) -> ChangeResult:
        index = self._index(feature_id)
        if "id" in changes and changes["id"] != feature_id:
            raise ValueError("a feature id cannot be changed; delete and add instead")
        merged = {**self.features[index].model_dump(), **changes}
        candidate = list(self.features)
        candidate[index] = _FEATURE.validate_python(merged)
        return self._apply(candidate, "updated", feature_id)

    def delete_feature(self, feature_id: str) -> ChangeResult:
        index = self._index(feature_id)
        candidate = [f for i, f in enumerate(self.features) if i != index]
        return self._apply(candidate, "deleted", feature_id)

    def validate(self) -> BuildResult:
        """The full report of the current build: the one `render_views` and `export` write.

        Not a new build (#101): every change is already built and validated, and the last good
        build is the current feature list, so a rebuild would only add a second build id for the
        same geometry. The report is emitted again, as the last word on this feature list."""
        if not self.features or self.last_good is None:
            raise ValueError("no features yet")
        self._validated(self.last_good, self.features)
        return self.last_good

    def _index(self, feature_id: str) -> int:
        for i, f in enumerate(self.features):
            if f.id == feature_id:
                return i
        raise KeyError(f"unknown feature {feature_id!r}; existing: {[f.id for f in self.features]}")

    # -- outputs -------------------------------------------------------------------------

    def _out_dir(self) -> Path:
        if self.last_good is None:
            raise ValueError("nothing built yet")
        return self.builds_dir / self.last_good.build_id / "out"

    def _shape(self) -> bd.Shape[Any]:
        return bd.import_brep(self._out_dir() / "part.brep")

    def _ref(self, path: Path, fmt: str) -> ArtifactRef:
        assert self.last_good is not None
        session = self.emitter.session_id or "local"
        ref = ArtifactRef(
            uri=f"artifact://{session}/{self.last_good.build_id}/{path.name}",
            path=str(path),
            format=fmt,
            bytes=path.stat().st_size,
            sha256=_sha256(path),
            build_id=self.last_good.build_id,
            features_sha256=self.last_good.features_sha256,
        )
        self.emitter.emit("artifact.created", ref.model_dump())
        return ref

    def export(self, formats: list[ExportFormat]) -> list[ArtifactRef]:
        """Write the last good build in the requested formats; returns artifact references."""
        assert self.last_good is not None or not formats
        shape = self._shape()
        assert self.last_good is not None
        target = self.artifacts_dir / self.last_good.build_id
        target.mkdir(parents=True, exist_ok=True)
        name = (self.part_name or "part").replace("/", "_")
        refs = []
        for fmt in formats:
            if fmt == "step-ap242":
                path = export_step(shape, target / f"{name}.ap242.step", "AP242", name)
            elif fmt == "step-ap214":
                path = export_step(shape, target / f"{name}.ap214.step", "AP214", name)
            elif fmt == "stl":
                path = target / f"{name}.stl"
                bd.export_stl(shape, path, tolerance=0.01)
            elif fmt == "obj":
                path = target / f"{name}.obj"
                _write_obj(shape, path)
            elif fmt == "features-json":
                path = target / f"{name}.features.json"
                path.write_text(self.spec().model_dump_json(indent=2))
            else:
                path = target / f"{name}.review.json"
                path.write_text(
                    json.dumps([r.model_dump(mode="json") for r in self.review_items], indent=2)
                )
            refs.append(self._ref(path, fmt))
        return refs

    def render_views(self, views: list[ViewName]) -> list[ArtifactRef]:
        """Hidden-line line drawings (SVG) of the last good build, for checking against the
        drawing. Visible edges solid, hidden edges dashed grey."""
        shape = self._shape()
        assert self.last_good is not None
        target = self.artifacts_dir / self.last_good.build_id
        target.mkdir(parents=True, exist_ok=True)
        size = max(shape.bounding_box().diagonal, 1.0)
        center = shape.bounding_box().center()
        refs = []
        for view in views:
            direction, up = _VIEWS[view]
            origin = center + bd.Vector(*direction).normalized() * size * 3
            visible, hidden = shape.project_to_viewport(origin, viewport_up=up, look_at=center)
            svg = bd.ExportSVG(margin=5)
            svg.add_layer("visible", line_weight=0.3)
            svg.add_layer("hidden", line_color=(0.47, 0.47, 0.47), line_type=bd.LineType.ISO_DASH)
            svg.add_shape(visible, layer="visible")
            svg.add_shape(hidden, layer="hidden")
            path = target / f"view-{view}.svg"
            svg.write(str(path))
            refs.append(self._ref(path, f"svg-{view}"))
        return refs


def _write_obj(shape: bd.Shape[Any], path: Path) -> None:
    vertices, triangles = shape.tessellate(0.01)
    with path.open("w") as f:
        f.write("# mechatronikers OBJ export (mm)\n")
        for v in vertices:
            f.write(f"v {v.X:.6f} {v.Y:.6f} {v.Z:.6f}\n")
        for a, b, c in triangles:
            f.write(f"f {a + 1} {b + 1} {c + 1}\n")
