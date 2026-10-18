"""Validation after every build: V1 geometry, V2 dimensions vs spec, V3 STEP round trip (docs/05).

V2 proves "model matches spec", not "spec matches drawing"; the test bench covers the latter.
"""

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Shape
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape

from mechatronikers.cad_mcp.measure import MeasuredHole, bbox_size, find_holes
from mechatronikers.cad_mcp.step_io import export_step, import_step
from mechatronikers.spec import Check, ExpectedGeometry, ExpectedHole, ValidationReport

DEFAULT_LINEAR_TOL = 0.01  # mm; open point T6
DEFAULT_ANGLE_TOL = 0.1  # degrees
VOLUME_ADVISORY_REL_TOL = 0.05  # fillets and overlapping cuts make analytic volumes fragile
ROUNDTRIP_VOLUME_REL_TOL = 1e-4


def _count_edges(shape: TopoDS_Shape) -> int:
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def free_edge_count(shape: Shape[Any]) -> int:
    """Edges bounding only one face. A closed solid has none."""
    bounds = ShapeAnalysis_FreeBounds(shape.wrapped, False, False, False)
    return _count_edges(bounds.GetClosedWires()) + _count_edges(bounds.GetOpenWires())


def check_geometry(shape: Shape[Any]) -> list[Check]:
    """V1: exactly one solid, closed, geometrically valid, positive volume."""
    solids = len(shape.solids())
    free_edges = free_edge_count(shape)
    volume = shape.volume
    return [
        Check(
            id="V1.solid_count",
            category="V1_geometry",
            expected=1,
            measured=solids,
            passed=solids == 1,
        ),
        Check(
            id="V1.brep_valid",
            category="V1_geometry",
            expected="valid",
            measured="valid" if BRepCheck_Analyzer(shape.wrapped).IsValid() else "invalid",
            passed=BRepCheck_Analyzer(shape.wrapped).IsValid(),
        ),
        Check(
            id="V1.closed_shell",
            category="V1_geometry",
            expected=0,
            measured=free_edges,
            passed=free_edges == 0,
            detail="free edges (bounding only one face)",
        ),
        Check(
            id="V1.positive_volume",
            category="V1_geometry",
            expected="> 0",
            measured=round(volume, 6),
            passed=volume > 0,
        ),
    ]


def _match_hole(expected: ExpectedHole, candidates: list[MeasuredHole]) -> MeasuredHole | None:
    def cost(h: MeasuredHole) -> float:
        c = abs(h.diameter - expected.diameter)
        if expected.center is not None:
            c += h.distance_to_axis(np.array(expected.center, dtype=float))
        return c

    return min(candidates, key=cost, default=None)


def _hole_checks(
    expected: list[ExpectedHole], measured: list[MeasuredHole], tol: float
) -> list[Check]:
    checks: list[Check] = []
    remaining = list(measured)
    for hole in expected:
        match = _match_hole(hole, remaining)
        if match is None:
            checks.append(
                Check(
                    id=f"V2.hole_found.{hole.id}",
                    category="V2_dimensions",
                    expected=f"Ø{hole.diameter}",
                    measured="missing",
                    passed=False,
                    feature_ids=hole.feature_ids,
                )
            )
            continue
        remaining.remove(match)
        checks.append(
            Check(
                id=f"V2.hole_diameter.{hole.id}",
                category="V2_dimensions",
                expected=hole.diameter,
                measured=round(match.diameter, 6),
                tolerance=tol,
                passed=abs(match.diameter - hole.diameter) <= tol,
                feature_ids=hole.feature_ids,
            )
        )
        if hole.center is not None:
            offset = match.distance_to_axis(np.array(hole.center, dtype=float))
            checks.append(
                Check(
                    id=f"V2.hole_position.{hole.id}",
                    category="V2_dimensions",
                    expected=0.0,
                    measured=round(offset, 6),
                    tolerance=tol,
                    passed=offset <= tol,
                    detail="distance from expected center to measured hole axis",
                    feature_ids=hole.feature_ids,
                )
            )
        if hole.axis is not None:
            axis = np.array(hole.axis, dtype=float)
            axis /= np.linalg.norm(axis)
            cos = min(1.0, abs(float(np.dot(axis, match.direction))))
            angle = math.degrees(math.acos(cos))
            checks.append(
                Check(
                    id=f"V2.hole_axis.{hole.id}",
                    category="V2_dimensions",
                    expected=0.0,
                    measured=round(angle, 6),
                    tolerance=DEFAULT_ANGLE_TOL,
                    passed=angle <= DEFAULT_ANGLE_TOL,
                    detail="angle between expected and measured axis (degrees)",
                    feature_ids=hole.feature_ids,
                )
            )
    return checks


def check_dimensions(
    shape: Shape[Any], expected: ExpectedGeometry, tol: float = DEFAULT_LINEAR_TOL
) -> tuple[list[Check], list[Check]]:
    """V2: built solid vs spec. Returns (scored, advisory) checks."""
    scored: list[Check] = []
    advisory: list[Check] = []
    if expected.bbox_size is not None:
        for axis, want, got in zip("xyz", expected.bbox_size, bbox_size(shape), strict=True):
            if want is None:  # no printed overall dimension along this axis
                continue
            scored.append(
                Check(
                    id=f"V2.bbox_{axis}",
                    category="V2_dimensions",
                    expected=want,
                    measured=round(got, 6),
                    tolerance=tol,
                    passed=abs(got - want) <= tol,
                )
            )
    if expected.holes is not None:
        measured_holes = find_holes(shape)
        scored.extend(_hole_checks(expected.holes, measured_holes, tol))
        # Advisory: circular pockets and turned bores are full cylinders too, so extra cylinders
        # are legitimate. Every expected hole is still checked strictly above (D51).
        advisory.append(
            Check(
                id="V2.hole_count",
                category="V2_dimensions",
                expected=len(expected.holes),
                measured=len(measured_holes),
                passed=len(expected.holes) == len(measured_holes),
                detail="cylindrical holes incl. circular pockets and bores; advisory",
            )
        )
    if expected.volume is not None:
        volume = shape.volume
        rel = abs(volume - expected.volume) / expected.volume if expected.volume else math.inf
        advisory.append(
            Check(
                id="V2.volume",
                category="V2_dimensions",
                expected=expected.volume,
                measured=round(volume, 6),
                tolerance=VOLUME_ADVISORY_REL_TOL,
                passed=rel <= VOLUME_ADVISORY_REL_TOL,
                detail="relative deviation; advisory",
            )
        )
    return scored, advisory


def check_step_roundtrip(
    shape: Shape[Any], step_path: str | Path, tol: float = DEFAULT_LINEAR_TOL
) -> list[Check]:
    """V3: the exported STEP re-imports and matches the built solid."""
    try:
        loaded = import_step(step_path)
    except Exception as exc:  # any reader failure is a failed check, not a crash
        return [
            Check(
                id="V3.reimport",
                category="V3_step_roundtrip",
                expected="loads",
                measured="error",
                passed=False,
                detail=str(exc),
            )
        ]
    volume, loaded_volume = shape.volume, loaded.volume
    rel = abs(loaded_volume - volume) / volume if volume else math.inf
    bbox_diff = max(abs(a - b) for a, b in zip(bbox_size(shape), bbox_size(loaded), strict=True))
    solids, loaded_solids = len(shape.solids()), len(loaded.solids())
    faces, loaded_faces = len(shape.faces()), len(loaded.faces())
    return [
        Check(
            id="V3.reimport",
            category="V3_step_roundtrip",
            expected="loads",
            measured="loads",
            passed=True,
        ),
        Check(
            id="V3.solid_count",
            category="V3_step_roundtrip",
            expected=solids,
            measured=loaded_solids,
            passed=solids == loaded_solids,
        ),
        Check(
            id="V3.volume",
            category="V3_step_roundtrip",
            expected=round(volume, 6),
            measured=round(loaded_volume, 6),
            tolerance=ROUNDTRIP_VOLUME_REL_TOL,
            passed=rel <= ROUNDTRIP_VOLUME_REL_TOL,
            detail="relative deviation",
        ),
        Check(
            id="V3.bbox_size",
            category="V3_step_roundtrip",
            expected=0.0,
            measured=round(bbox_diff, 6),
            tolerance=tol,
            passed=bbox_diff <= tol,
            detail="largest bounding box extent difference",
        ),
        Check(
            id="V3.face_count",
            category="V3_step_roundtrip",
            expected=faces,
            measured=loaded_faces,
            passed=faces == loaded_faces,
        ),
    ]


def validate(
    shape: Shape[Any],
    *,
    build_id: str,
    expected: ExpectedGeometry | None = None,
    step_path: str | Path | None = None,
    linear_tol: float = DEFAULT_LINEAR_TOL,
) -> ValidationReport:
    """Run V1–V3. Without `step_path`, an AP242 export to a temporary file is checked."""
    checks = check_geometry(shape)
    advisory: list[Check] = []
    if expected is not None:
        scored, advisory = check_dimensions(shape, expected, linear_tol)
        checks.extend(scored)
    if step_path is not None:
        checks.extend(check_step_roundtrip(shape, step_path, linear_tol))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = export_step(shape, Path(tmp) / "part.step")
            checks.extend(check_step_roundtrip(shape, path, linear_tol))
    return ValidationReport(build_id=build_id, checks=checks, advisory=advisory)
