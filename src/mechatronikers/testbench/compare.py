"""Compare a generated model with a reference (human-made) model (docs/09 metrics)."""

import math
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Shape
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from pydantic import BaseModel, Field, computed_field
from scipy.stats import qmc

from mechatronikers.cad_mcp.measure import bbox_size, find_holes
from mechatronikers.cad_mcp.step_io import import_step
from mechatronikers.testbench.align import align, transform_shape
from mechatronikers.testbench.surface import SurfaceMesh, contains

# The decided "solved" geometry (D91): re-check the Hausdorff limit when more labelled parts exist.
IOU_THRESHOLD = 0.95
HAUSDORFF_LIMIT = 1.0  # mm
IOU_SAMPLES = 2**15


class CompareResult(BaseModel):
    generated: str
    reference: str
    aligned: bool
    alignment_rms: float | None = None
    transform: list[list[float]] | None = None
    volume_generated: float
    volume_reference: float
    volume_rel_diff: float
    area_generated: float
    area_reference: float
    area_rel_diff: float
    bbox_generated: tuple[float, float, float]  # after alignment, reference frame
    bbox_reference: tuple[float, float, float]
    face_count_generated: int
    face_count_reference: int
    hole_diameters_generated: list[float]
    hole_diameters_reference: list[float]
    holes_matched: int
    iou: float | None = None  # sampled estimate, see iou_stderr
    iou_stderr: float | None = None
    iou_samples: int = 0
    hausdorff: float  # max point-to-surface distance over samples, both ways (mm)
    mean_surface_distance: float  # mean point-to-surface distance, both ways (mm)
    errors: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def difference_score(self) -> float | None:
        """1 − IoU: 0 = identical, 1 = no overlap (#5); standard error = `iou_stderr`."""
        return None if self.iou is None else 1.0 - self.iou

    @computed_field  # type: ignore[prop-decorator]
    @property
    def difference_score_stderr(self) -> float | None:
        return self.iou_stderr

    @computed_field  # type: ignore[prop-decorator]
    @property
    def symmetric_difference_volume_mm3(self) -> float | None:
        """Volume in exactly one of the two models: |A ∪ B| − |A ∩ B| (mm³).

        From the exact volumes and the sampled IoU, not from a boolean: with
        |A ∩ B| = IoU·|A ∪ B| and |A ∪ B| = V_gen + V_ref − |A ∩ B| it is
        (1 − IoU)·(V_gen + V_ref)/(1 + IoU). Never below |V_gen − V_ref|, which is exact: a
        difference too thin for the samples to hit must not read as 0.
        """
        if self.iou is None:
            return None
        total = self.volume_generated + self.volume_reference
        sampled = (1.0 - self.iou) * total / (1.0 + self.iou)
        return max(sampled, abs(self.volume_generated - self.volume_reference))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def symmetric_difference_volume_stderr(self) -> float | None:
        """Propagated from `iou_stderr`: |d/dIoU| = 2·(V_gen + V_ref)/(1 + IoU)²."""
        if self.iou is None or self.iou_stderr is None:
            return None
        total = self.volume_generated + self.volume_reference
        return 2.0 * total / (1.0 + self.iou) ** 2 * self.iou_stderr

    @computed_field  # type: ignore[prop-decorator]
    @property
    def matches_reference(self) -> bool:
        """The geometry part of "solved" (D91): IoU ≥ 0.95, the hole count matches with every
        hole matched, and the Hausdorff distance is ≤ 1 mm."""
        return (
            self.iou is not None
            and self.iou >= IOU_THRESHOLD
            and len(self.hole_diameters_generated) == len(self.hole_diameters_reference)
            and self.holes_matched == len(self.hole_diameters_reference)
            and self.hausdorff <= HAUSDORFF_LIMIT
        )


def _rel(a: float, b: float) -> float:
    return abs(a - b) / abs(b) if b else math.inf


def _volume(shape: Shape[Any]) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, props)
    return float(props.Mass())


def estimate_iou(
    a: Shape[Any], b: Shape[Any], samples: int = IOU_SAMPLES, seed: int = 0
) -> tuple[float | None, float | None]:
    """Volumetric IoU from scrambled Sobol points in the joint bounding box: (iou, stderr).

    Not a boolean intersection: OpenCascade booleans of near-coincident solids (exactly the
    "correct model" case) return empty or wrong results, or hang.
    """
    boxes = [a.bounding_box(), b.bounding_box()]
    lo = np.min([[bb.min.X, bb.min.Y, bb.min.Z] for bb in boxes], axis=0)
    hi = np.max([[bb.max.X, bb.max.Y, bb.max.Z] for bb in boxes], axis=0)
    points = lo + qmc.Sobol(d=3, scramble=True, seed=seed).random(samples) * (hi - lo)
    in_a, in_b = contains(a, points, seed), contains(b, points, seed)
    union = int(np.count_nonzero(in_a | in_b))
    if union == 0:
        return None, None
    iou = int(np.count_nonzero(in_a & in_b)) / union
    # Binomial standard error, with the variance floored at one sample: if no sample disagrees,
    # the IoU is still only known to the resolution of one sample, not exactly.
    return iou, float(np.sqrt(max(iou * (1 - iou), 1 / union) / union))


def match_diameters(generated: list[float], reference: list[float], tol: float) -> int:
    """Greedy one-to-one matching of hole diameters within `tol`."""
    remaining = sorted(generated)
    matched = 0
    for d in sorted(reference):
        best = min(remaining, key=lambda g: abs(g - d), default=None)
        if best is not None and abs(best - d) <= tol:
            remaining.remove(best)
            matched += 1
    return matched


def compare(
    generated: Shape[Any],
    reference: Shape[Any],
    *,
    generated_name: str = "generated",
    reference_name: str = "reference",
    do_align: bool = True,
    samples: int = 4000,
    seed: int = 0,
    hole_tol: float = 0.01,
) -> CompareResult:
    """Align `generated` onto `reference` (optional) and compute all metrics."""
    errors: list[str] = []
    rms: float | None = None
    matrix: list[list[float]] | None = None
    if do_align:
        alignment = align(generated, reference, samples=min(samples, 2000), seed=seed)
        generated = transform_shape(generated, alignment)
        rms, matrix = alignment.rms, alignment.matrix()

    v_gen, v_ref = _volume(generated), _volume(reference)
    iou, iou_stderr = estimate_iou(generated, reference, IOU_SAMPLES, seed)
    if iou is None:
        errors.append("iou: neither model contains a sample point")

    mesh_gen, mesh_ref = SurfaceMesh.from_shape(generated), SurfaceMesh.from_shape(reference)
    p_gen, _ = mesh_gen.sample(samples, seed)
    p_ref, _ = mesh_ref.sample(samples, seed + 1)
    d_gr = mesh_ref.distance(p_gen)
    d_rg = mesh_gen.distance(p_ref)

    holes_gen = sorted(round(h.diameter, 6) for h in find_holes(generated))
    holes_ref = sorted(round(h.diameter, 6) for h in find_holes(reference))

    return CompareResult(
        generated=generated_name,
        reference=reference_name,
        aligned=do_align,
        alignment_rms=rms,
        transform=matrix,
        volume_generated=v_gen,
        volume_reference=v_ref,
        volume_rel_diff=_rel(v_gen, v_ref),
        area_generated=generated.area,
        area_reference=reference.area,
        area_rel_diff=_rel(generated.area, reference.area),
        bbox_generated=bbox_size(generated),
        bbox_reference=bbox_size(reference),
        face_count_generated=len(generated.faces()),
        face_count_reference=len(reference.faces()),
        hole_diameters_generated=holes_gen,
        hole_diameters_reference=holes_ref,
        holes_matched=match_diameters(holes_gen, holes_ref, hole_tol),
        iou=iou,
        iou_stderr=iou_stderr,
        iou_samples=IOU_SAMPLES,
        hausdorff=float(max(np.max(d_gr), np.max(d_rg))),
        mean_surface_distance=float((np.mean(d_gr) + np.mean(d_rg)) / 2),
        errors=errors,
    )


def compare_files(generated: str | Path, reference: str | Path, **kwargs: object) -> CompareResult:
    return compare(
        import_step(generated),
        import_step(reference),
        generated_name=str(generated),
        reference_name=str(reference),
        **kwargs,  # type: ignore[arg-type]
    )
