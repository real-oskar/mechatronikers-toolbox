"""Rigid alignment of a generated model onto a reference model (docs/09, open point T19).

Coarse: centres of mass and principal axes of inertia, trying all 24 proper axis
permutations/flips (mirroring is never allowed). Fine: point-to-plane ICP.
"""

import itertools
from dataclasses import dataclass
from typing import Any

import numpy as np
from build123d import Location, Shape
from numpy.typing import NDArray
from OCP.BRepGProp import BRepGProp
from OCP.gp import gp_Trsf
from OCP.GProp import GProp_GProps
from scipy.spatial import cKDTree

from mechatronikers.testbench.surface import SurfaceMesh

Points = NDArray[np.float64]


def _proper_rotations() -> list[Points]:
    rotations = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1.0, -1.0), repeat=3):
            m = np.zeros((3, 3))
            for row, (col, sign) in enumerate(zip(perm, signs, strict=True)):
                m[row, col] = sign
            if np.linalg.det(m) > 0:
                rotations.append(m)
    return rotations


_ROTATIONS = _proper_rotations()


@dataclass
class Alignment:
    """Maps the moving shape onto the fixed one: x_fixed = rotation @ x_moving + translation."""

    rotation: Points
    translation: Points
    rms: float  # symmetric RMS point-to-surface distance after alignment (mm), exact
    candidates: int

    def matrix(self) -> list[list[float]]:
        m = np.eye(4)
        m[:3, :3] = self.rotation
        m[:3, 3] = self.translation
        return [[float(v) for v in row] for row in m]


def principal_frame(shape: Shape[Any]) -> tuple[Points, Points]:
    """Centre of mass and principal axes (columns, ascending moment, right-handed)."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, props)
    com = props.CentreOfMass()
    mat = props.MatrixOfInertia()
    inertia = np.array([[mat.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)])
    _, axes = np.linalg.eigh(inertia)
    if np.linalg.det(axes) < 0:
        axes[:, 2] *= -1
    return np.array([com.X(), com.Y(), com.Z()]), axes


def _rodrigues(w: Points) -> Points:
    theta = float(np.linalg.norm(w))
    if theta < 1e-15:
        return np.eye(3)
    k = w / theta
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    result: Points = np.eye(3) + np.sin(theta) * kx + (1 - np.cos(theta)) * kx @ kx
    return result


def _icp_point_to_plane(
    src: Points,
    dst: Points,
    dst_normals: Points,
    tree: cKDTree,
    rotation: Points,
    translation: Points,
    iters: int,
) -> tuple[Points, Points]:
    for _ in range(iters):
        moved = src @ rotation.T + translation
        _, idx = tree.query(moved)
        q, n = dst[idx], dst_normals[idx]
        a = np.hstack([np.cross(moved, n), n])
        b = -np.sum((moved - q) * n, axis=1)
        x, *_ = np.linalg.lstsq(a, b, rcond=None)
        step = _rodrigues(x[:3])
        rotation = step @ rotation
        translation = step @ translation + x[3:]
        if np.linalg.norm(x) < 1e-10:
            break
    return rotation, translation


def align(
    moving: Shape[Any], fixed: Shape[Any], *, samples: int = 2000, seed: int = 0, iters: int = 60
) -> Alignment:
    """Best rigid transform of `moving` onto `fixed` over all coarse candidates."""
    c_m, axes_m = principal_frame(moving)
    c_f, axes_f = principal_frame(fixed)
    mesh_m, mesh_f = SurfaceMesh.from_shape(moving), SurfaceMesh.from_shape(fixed)
    src, _ = mesh_m.sample(samples, seed)
    dst, dst_normals = mesh_f.sample(samples * 10, seed + 1)
    back, _ = mesh_f.sample(samples, seed + 2)
    tree = cKDTree(dst)
    best: Alignment | None = None
    for s in _ROTATIONS:
        r0 = axes_f @ s @ axes_m.T
        rotation, translation = _icp_point_to_plane(
            src, dst, dst_normals, tree, r0, c_f - r0 @ c_m, iters
        )
        forward = mesh_f.distance(src @ rotation.T + translation, exact=False)
        backward = mesh_m.distance((back - translation) @ rotation, exact=False)  # inverse
        rms = float(np.sqrt((np.mean(forward**2) + np.mean(backward**2)) / 2))
        if best is None or rms < best.rms:
            best = Alignment(rotation, translation, rms, len(_ROTATIONS))
    assert best is not None
    return _refine(best, mesh_m, mesh_f, samples * 5, seed + 3, iters)


def _refine(
    coarse: Alignment, mesh_m: SurfaceMesh, mesh_f: SurfaceMesh, samples: int, seed: int, iters: int
) -> Alignment:
    """Final point-to-plane ICP pass with denser samples; reports the exact RMS."""
    src, _ = mesh_m.sample(samples, seed)
    dst, dst_normals = mesh_f.sample(samples * 10, seed + 1)
    back, _ = mesh_f.sample(samples, seed + 2)
    rotation, translation = _icp_point_to_plane(
        src, dst, dst_normals, cKDTree(dst), coarse.rotation, coarse.translation, iters
    )
    forward = mesh_f.distance(src @ rotation.T + translation)
    backward = mesh_m.distance((back - translation) @ rotation)
    rms = float(np.sqrt((np.mean(forward**2) + np.mean(backward**2)) / 2))
    return Alignment(rotation, translation, rms, coarse.candidates)


def transform_shape(shape: Shape[Any], alignment: Alignment) -> Shape[Any]:
    """Rigidly move `shape` by the alignment (rotation re-orthonormalised for OpenCascade)."""
    u, _, vt = np.linalg.svd(alignment.rotation)
    r = u @ vt
    t = alignment.translation
    trsf = gp_Trsf()
    trsf.SetValues(
        *(float(v) for v in (r[0, 0], r[0, 1], r[0, 2], t[0])),
        *(float(v) for v in (r[1, 0], r[1, 1], r[1, 2], t[1])),
        *(float(v) for v in (r[2, 0], r[2, 1], r[2, 2], t[2])),
    )
    return shape.moved(Location(trsf))
