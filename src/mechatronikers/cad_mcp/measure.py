"""Geometric measurements on solids, shared by validation (docs/05) and the test bench (docs/09)."""

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from build123d import Face, Shape, Vector
from numpy.typing import NDArray
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Cylinder

Vec = NDArray[np.float64]

# Faces closer than this (mm) to a common axis with equal radius belong to the same hole.
_GROUP_TOL = 1e-4


@dataclass(eq=False)  # identity: numpy fields make field-wise == ambiguous
class MeasuredHole:
    """A full (360°) concave cylindrical surface, possibly split into several faces."""

    diameter: float
    point: Vec  # a point on the axis
    direction: Vec  # unit axis direction
    face_count: int = 1
    _span: float = field(default=0.0, repr=False)

    def distance_to_axis(self, p: Vec) -> float:
        v = p - self.point
        return float(np.linalg.norm(v - np.dot(v, self.direction) * self.direction))


def to_array(v: Vector) -> Vec:
    return np.array([v.X, v.Y, v.Z], dtype=float)


def bbox_size(shape: Shape[Any]) -> tuple[float, float, float]:
    """Tight axis-aligned bounding box extents."""
    size = shape.bounding_box(optimal=True).size
    return (size.X, size.Y, size.Z)


def _is_concave_cylinder(face: Face, point: Vec, direction: Vec) -> bool:
    """True if the face normal points towards the axis (material outside: a hole, not a boss)."""
    p = face.position_at(0.5, 0.5)
    n = to_array(face.normal_at(p))
    v = to_array(p) - point
    radial = v - np.dot(v, direction) * direction
    return float(np.dot(n, radial)) < 0.0


def find_holes(shape: Shape[Any]) -> list[MeasuredHole]:
    """All holes: concave cylindrical surfaces covering the full circumference.

    Partial concave cylinders (e.g. inner fillets) are not holes and are skipped.
    """
    groups: list[MeasuredHole] = []
    for face in shape.faces():
        # The adaptor also resolves trimmed cylinders, as written by NX and other CAD systems.
        surface = BRepAdaptor_Surface(face.wrapped)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        loc, d = cylinder.Location(), cylinder.Axis().Direction()
        point = np.array([loc.X(), loc.Y(), loc.Z()])
        direction = np.array([d.X(), d.Y(), d.Z()])
        direction /= np.linalg.norm(direction)
        if not _is_concave_cylinder(face, point, direction):
            continue
        umin, umax, _, _ = BRepTools.UVBounds_s(face.wrapped)
        diameter = 2.0 * cylinder.Radius()
        for hole in groups:
            if (
                abs(hole.diameter - diameter) < _GROUP_TOL
                and abs(abs(float(np.dot(hole.direction, direction))) - 1.0) < 1e-9
                and hole.distance_to_axis(point) < _GROUP_TOL
            ):
                hole._span += umax - umin
                hole.face_count += 1
                break
        else:
            groups.append(MeasuredHole(diameter, point, direction, 1, umax - umin))
    return [h for h in groups if h._span >= 2.0 * math.pi - 1e-6]
