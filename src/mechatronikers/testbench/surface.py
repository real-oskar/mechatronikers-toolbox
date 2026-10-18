"""Surface sampling and exact point-to-surface distances on a tessellated shape."""

from dataclasses import dataclass
from typing import Any

import numpy as np
from build123d import Shape
from numpy.typing import NDArray
from scipy.spatial import cKDTree

Points = NDArray[np.float64]

# Triangles are split until no edge is longer than this share of the bounding box diagonal,
# so the nearest-centroid candidates always contain the truly closest triangle.
_MAX_EDGE_SHARE = 0.02
_CANDIDATES = 16


def _subdivide(
    v: Points, t: NDArray[np.int64], max_edge: float
) -> tuple[Points, NDArray[np.int64]]:
    count = len(v)
    while True:
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        longest = np.max(
            [
                np.linalg.norm(b - a, axis=1),
                np.linalg.norm(c - b, axis=1),
                np.linalg.norm(a - c, axis=1),
            ],
            axis=0,
        )
        big = longest > max_edge
        if not big.any():
            return v, t
        tb = t[big]
        ab, bc, ca = (
            (v[tb[:, 0]] + v[tb[:, 1]]) / 2,
            (v[tb[:, 1]] + v[tb[:, 2]]) / 2,
            (v[tb[:, 2]] + v[tb[:, 0]]) / 2,
        )
        n = len(tb)
        i_ab = np.arange(count, count + n)
        i_bc = i_ab + n
        i_ca = i_bc + n
        count += 3 * n
        v = np.concatenate([v, ab, bc, ca])
        split = np.concatenate(
            [
                np.stack([tb[:, 0], i_ab, i_ca], axis=1),
                np.stack([i_ab, tb[:, 1], i_bc], axis=1),
                np.stack([i_ca, i_bc, tb[:, 2]], axis=1),
                np.stack([i_ab, i_bc, i_ca], axis=1),
            ]
        )
        t = np.concatenate([t[~big], split])


@dataclass
class SurfaceMesh:
    """Fine triangle mesh of a shape's surface with a spatial index."""

    a: Points
    b: Points
    c: Points
    normals: Points  # unit triangle normals
    areas: NDArray[np.float64]
    tree: cKDTree
    reach: float  # largest centroid-to-vertex distance of any triangle

    @classmethod
    def from_shape(cls, shape: Shape[Any]) -> "SurfaceMesh":
        diag = shape.bounding_box().diagonal
        vertices, triangles = shape.tessellate(max(diag * 2e-4, 1e-4))
        v = np.array([[p.X, p.Y, p.Z] for p in vertices], dtype=float)
        t = np.array(triangles, dtype=np.int64)
        v, t = _subdivide(v, t, diag * _MAX_EDGE_SHARE)
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        cross = np.cross(b - a, c - a)
        norm = np.linalg.norm(cross, axis=1)
        keep = norm > 1e-12
        a, b, c, cross, norm = a[keep], b[keep], c[keep], cross[keep], norm[keep]
        centroids = (a + b + c) / 3
        reach = float(max(np.max(np.linalg.norm(x - centroids, axis=1)) for x in (a, b, c)))
        return cls(a, b, c, cross / norm[:, None], norm / 2, cKDTree(centroids), reach)

    def sample(self, count: int, seed: int = 0) -> tuple[Points, Points]:
        """Area-weighted random points and their surface normals."""
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(self.a), size=count, p=self.areas / self.areas.sum())
        r1 = np.sqrt(rng.random(count))[:, None]
        r2 = rng.random(count)[:, None]
        points = (1 - r1) * self.a[idx] + r1 * (1 - r2) * self.b[idx] + r1 * r2 * self.c[idx]
        return points, self.normals[idx]

    def distance(self, points: Points, *, exact: bool = True) -> NDArray[np.float64]:
        """Distance from each point to the mesh surface.

        `exact=False` only checks the nearest candidate triangles: an upper bound that is exact
        for well-aligned shapes and fast enough to rank alignment candidates.
        """
        k = min(_CANDIDATES, len(self.a))
        centroid_dist, idx = self.tree.query(points, k=k)
        idx = idx.reshape(len(points), k)
        centroid_dist = centroid_dist.reshape(len(points), k)
        p = points[:, None, :]
        dist = _point_triangle_distance(p, self.a[idx], self.b[idx], self.c[idx], self.normals[idx])
        result: NDArray[np.float64] = dist.min(axis=1)
        if not exact:
            return result
        # The closest triangle's centroid lies within result + reach. Where that radius exceeds
        # the k-th candidate's centroid distance, a triangle may have been missed: search a ball.
        unsure = np.flatnonzero(result + self.reach > centroid_dist[:, -1])
        for i in unsure:
            ball = np.array(self.tree.query_ball_point(points[i], result[i] + self.reach))
            if len(ball):
                d = _point_triangle_distance(
                    points[i], self.a[ball], self.b[ball], self.c[ball], self.normals[ball]
                )
                result[i] = min(result[i], float(np.min(d)))
        return result


def _segment_distance(p: Points, a: Points, b: Points) -> NDArray[np.float64]:
    ab = b - a
    t = np.clip(np.sum((p - a) * ab, axis=-1) / np.maximum(np.sum(ab * ab, axis=-1), 1e-30), 0, 1)
    result: NDArray[np.float64] = np.linalg.norm(p - (a + t[..., None] * ab), axis=-1)
    return result


def _point_triangle_distance(
    p: Points, a: Points, b: Points, c: Points, n: Points
) -> NDArray[np.float64]:
    plane = np.sum((p - a) * n, axis=-1)
    proj = p - plane[..., None] * n
    inside = (
        (np.sum(np.cross(b - a, proj - a) * n, axis=-1) >= 0)
        & (np.sum(np.cross(c - b, proj - b) * n, axis=-1) >= 0)
        & (np.sum(np.cross(a - c, proj - c) * n, axis=-1) >= 0)
    )
    edge = np.minimum(
        np.minimum(_segment_distance(p, a, b), _segment_distance(p, b, c)),
        _segment_distance(p, c, a),
    )
    result: NDArray[np.float64] = np.where(inside, np.abs(plane), edge)
    return result


_GRID = 96  # cells per axis of the 2D acceleration grid for point-in-mesh tests


def contains(shape: Shape[Any], points: Points, seed: int = 0) -> NDArray[np.bool_]:
    """Point-in-solid test by ray parity against the tessellated surface (rays along +Z).

    A 2D grid over XY limits each point to the triangles whose projection covers its cell.
    Points are jittered by 1e-9 of the model size so rays do not hit edges exactly.
    """
    diag = shape.bounding_box().diagonal
    vertices, triangles = shape.tessellate(max(diag * 5e-4, 1e-4))
    v = np.array([[p.X, p.Y, p.Z] for p in vertices], dtype=float)
    t = np.array(triangles, dtype=np.int64)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    rng = np.random.default_rng(seed)
    pts = points + rng.uniform(-1e-9, 1e-9, points.shape) * max(diag, 1.0)

    lo = np.minimum(v[:, :2].min(axis=0), pts[:, :2].min(axis=0))
    hi = np.maximum(v[:, :2].max(axis=0), pts[:, :2].max(axis=0))
    cell = (hi - lo) / _GRID + 1e-12

    def cell_of(xy: Points) -> NDArray[np.int64]:
        idx: NDArray[np.int64] = np.clip(((xy - lo) / cell).astype(np.int64), 0, _GRID - 1)
        return idx

    area2 = (b - a)[:, 0] * (c - a)[:, 1] - (c - a)[:, 0] * (b - a)[:, 1]
    keep = np.abs(area2) > 1e-12 * max(diag, 1.0) ** 2
    a, b, c = a[keep], b[keep], c[keep]

    tri_lo = cell_of(np.minimum(np.minimum(a, b), c)[:, :2])
    tri_hi = cell_of(np.maximum(np.maximum(a, b), c)[:, :2])
    buckets: dict[int, list[int]] = {}
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(tri_lo, tri_hi, strict=True)):
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                buckets.setdefault(gx * _GRID + gy, []).append(i)

    pt_cells = cell_of(pts[:, :2])
    keys = pt_cells[:, 0] * _GRID + pt_cells[:, 1]
    result = np.zeros(len(pts), dtype=bool)
    order = np.argsort(keys, kind="stable")
    boundaries = np.flatnonzero(np.diff(keys[order])) + 1
    for group in np.split(order, boundaries):
        tris = buckets.get(int(keys[group[0]]))
        if not tris:
            continue
        ta, tb, tc = a[tris], b[tris], c[tris]
        p = pts[group]
        # 2D barycentric coordinates of each point in each triangle's XY projection.
        v0, v1 = (tb - ta)[None, :, :2], (tc - ta)[None, :, :2]
        v2 = p[:, None, :2] - ta[None, :, :2]
        den = v0[..., 0] * v1[..., 1] - v1[..., 0] * v0[..., 1]  # nonzero: filtered above
        u = (v2[..., 0] * v1[..., 1] - v1[..., 0] * v2[..., 1]) / den
        w = (v0[..., 0] * v2[..., 1] - v2[..., 0] * v0[..., 1]) / den
        hit = (u >= 0) & (w >= 0) & (u + w <= 1)
        z = ta[None, :, 2] + u * (tb - ta)[None, :, 2] + w * (tc - ta)[None, :, 2]
        crossings = np.count_nonzero(hit & (z > p[:, None, 2]), axis=1)
        result[group] = crossings % 2 == 1
    return result
