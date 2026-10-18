"""What the drawing does not state, made explicit for the reviewer (#90, D93).

AGENTS.md rules 4 and 5: the printed dimension is authoritative and values are never invented;
missing information becomes a review item with a reason. Three general cases left no trace in the
plan, and the coverage check (D54) cannot see them, because it matches visible circles and printed
values:

- A feature visible in a view without dimensions. The planner lists it in `undimensioned`; each
  entry becomes a review item of kind `undimensioned_feature`, placed on its view and region,
  whether the feature was left out or modelled from the drawing geometry.
- A symmetric extrusion. Where the mid-plane lies is rarely defined on the drawing and the drawing
  evidence is not machine-checkable, so it is always an assumption (kind `symmetry`), with the
  planner's reason if it gave one.
- A fillet, chamfer or profile arc whose size no printed dimension gives. It is built with a
  `method="assumed"` dimension (needs review) and listed as an assumption.

Deterministic and idempotent: the audit first drops what an earlier audit added, so every later
answer of the planner (replan, fix, revise) is audited the same way.
"""

import math
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from mechatronikers.spec import Dimension, ReviewItem, SourceRef

BBox = tuple[float, float, float, float]
SizeKind = Literal["radius", "chamfer"]

AUTO = "a-auto-"  # assumptions the audit adds
ASSUMED = "d-assumed-"  # dimensions the audit adds
PROFILED = ("extrude_base", "revolve_base", "boss", "pocket")
ASSUMABLE = ("fillet", "chamfer")  # may be built with an assumed size (D93)

# Assumption kinds are free text from the model; these word stems tell which ones cover a size.
_KIND_WORDS = {
    "radius": ("radi", "fillet", "round"),
    "chamfer": ("chamf",),
    "symmetry": ("symm", "direction", "mid"),
}
_REASONS = {
    "radius": "no printed dimension gives this radius, and the plan names no source for it "
    "(dimension or general note)",
    "chamfer": "no printed dimension gives this chamfer, and the plan names no source for it "
    "(dimension or general note)",
    "symmetry": "extruded symmetrically about the sketch plane; the plan does not name the centre "
    "line or dimension on the drawing that defines this mid-plane",
    "undimensioned": "visible on the drawing, but no printed dimension gives its size",
}


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, 0.005 * abs(b))


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"\d+(?:[.,]\d+)?", value)
        return float(m.group(0).replace(",", ".")) if m else None
    return None


def _ids(value: Any) -> list[str]:
    """Ids the model gave as a list (or as a single id)."""
    if isinstance(value, str | int):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str | int) and str(v)]


# --------------------------------------------------------------------------- undimensioned


def _box(value: Any) -> BBox | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    (x0, x1), (y0, y1) = sorted((x0, x1)), sorted((y0, y1))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _overlap(a: BBox, b: BBox) -> BBox | None:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def normalize_undimensioned(items: Any) -> list[dict[str, Any]]:
    """The planner's `undimensioned` answer as {ref, view_id, bbox, feature_id, reason}. `ref` is
    the model's own id: it only recognises an entry the model echoes, it never becomes the id."""
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and item.strip():
            item = {"reason": item}
        if not isinstance(item, dict):
            continue
        box = _box(item.get("bbox"))
        out.append(
            {
                "ref": str(item["id"]) if item.get("id") else None,
                "view_id": str(item["view_id"]) if item.get("view_id") else None,
                "bbox": None if box is None else list(box),
                "feature_id": str(item["feature_id"]) if item.get("feature_id") else None,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return out


def _same_box(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    return all(abs(float(x) - float(y)) <= 1e-6 for x, y in zip(a, b, strict=True))


def _echoes(entry: dict[str, Any], new: dict[str, Any], *, exact: bool) -> bool:
    """`new` says nothing that contradicts `entry` (exact: says the same in every field). A
    feature is linked once: naming another one contradicts the entry."""
    reason, view, box, built = new["reason"], new["view_id"], new["bbox"], new["feature_id"]
    if built and entry.get("feature_id") and built != entry["feature_id"]:
        return False
    if exact:
        return (reason, view) == (entry["reason"], entry["view_id"]) and _same_box(
            box, entry["bbox"]
        )
    return (
        (not reason or not entry["reason"] or reason == entry["reason"])
        and (view is None or entry["view_id"] is None or view == entry["view_id"])
        and (box is None or entry["bbox"] is None or _same_box(box, entry["bbox"]))
    )


def merge_undimensioned(
    previous: list[dict[str, Any]], answer: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Entries are append-only (D93). An answer that echoes an entry (its id with nothing
    contradicting, or the same content) may link the feature built for it or fill in a missing
    view, region or reason; anything else is a new entry with the next id `u-<n>`. So no entry is
    changed or dropped, and ids are never reused, whatever ids the model gives."""
    merged = [dict(e) for e in previous]
    taken = [int(m.group(1)) for e in merged if (m := re.fullmatch(r"u-(\d+)", str(e["id"])))]
    next_id = max(taken, default=0) + 1
    for new in answer:
        target = next(
            (e for e in merged if new["ref"] == e["id"] and _echoes(e, new, exact=False)),
            None,
        ) or next((e for e in merged if _echoes(e, new, exact=True)), None)
        if target is not None:
            target["feature_id"] = new["feature_id"] or target.get("feature_id")
            for key in ("view_id", "bbox", "reason"):
                if not target.get(key):
                    target[key] = new[key]
            continue
        merged.append({"id": f"u-{next_id}", **{k: v for k, v in new.items() if k != "ref"}})
        next_id += 1
    return merged


def locate(view_id: Any, bbox: Any, views: list[dict[str, Any]]) -> tuple[str | None, BBox | None]:
    """Place a reported feature on the sheet: the named view, else the view its region overlaps
    most; the region clipped to that view, or the whole view if no usable region was given."""
    regions = {str(v["id"]): b for v in views if (b := _box(v.get("bbox"))) is not None}
    box = _box(bbox)
    view = str(view_id) if view_id is not None else None
    if view not in regions and box is not None:
        hits = [(vid, _overlap(box, region)) for vid, region in regions.items()]
        scored = [((o[2] - o[0]) * (o[3] - o[1]), vid) for vid, o in hits if o is not None]
        view = max(scored)[1] if scored else None
    if view is None or view not in regions:
        return None, None
    clipped = _overlap(box, regions[view]) if box is not None else None
    return view, clipped or regions[view]


def assumable(features: list[dict[str, Any]]) -> dict[str, bool]:
    """Per planned feature: may it carry a size the drawing does not print? Only a rounded or
    chamfered edge (a fillet, a chamfer, or a feature whose profile arcs the audit built with a
    method="assumed" dimension), D93."""
    return {
        str(f["id"]): f.get("type") in ASSUMABLE
        or any(str(i).startswith(ASSUMED) for i in f.get("dimension_ids", []))
        for f in features
    }


def undimensioned_items(
    entries: list[dict[str, Any]], views: list[dict[str, Any]], features: Mapping[str, bool]
) -> list[ReviewItem]:
    """One review item per reported feature, located on the drawing (never dropped). `features`
    is `assumable()` of the plan: any other feature built for an entry has sizes the drawing
    does not print, which blocks (rule 5)."""
    items = []
    for i, entry in enumerate(entries, 1):
        m = re.fullmatch(r"u-(\d+)", str(entry.get("id")))
        view, region = locate(entry.get("view_id"), entry.get("bbox"), views)
        built = entry.get("feature_id") if entry.get("feature_id") in features else None
        where = f"in view {view}" if view else "(no view given)"
        severity: Literal["warning", "blocking"] = "warning"
        if built is None:
            what = "not modelled"
        elif features[built]:
            what = f"built as {built} with an assumed size"
        else:
            what = f"built as {built} with sizes the drawing does not print; give them or remove it"
            severity = "blocking"
        items.append(
            ReviewItem(
                id=f"undim-{m.group(1) if m else i}",
                kind="undimensioned_feature",
                severity=severity,
                message=f"Feature without dimensions {where}: {what}",
                reason=entry.get("reason") or _REASONS["undimensioned"],
                feature_ids=[built] if built else [],
                sheet_ref=None if region is None else SourceRef(view_id=view, bbox=region),
            )
        )
    return items


# --------------------------------------------------------------------------- assumed sizes


def _arc_radius(a: list[float], m: list[float], b: list[float]) -> float | None:
    (ax, ay), (bx, by), (cx, cy) = a, m, b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    sa, sb, sc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (sa * (by - cy) + sb * (cy - ay) + sc * (ay - by)) / d
    uy = (sa * (cx - bx) + sb * (ax - cx) + sc * (bx - ax)) / d
    return math.hypot(ax - ux, ay - uy)


def _profile_radii(profile: dict[str, Any]) -> Iterator[float]:
    for s in profile.get("segments", []):
        if s.get("kind") == "arc" and (r := _arc_radius(s["start"], s["mid"], s["end"])):
            yield r
    for inner in profile.get("inner", []):
        yield from _profile_radii(inner)


def _sizes(f: dict[str, Any]) -> list[tuple[SizeKind, float]]:
    """Radii and chamfer legs of a feature: (kind, size), each size once."""
    sizes: list[tuple[SizeKind, float]] = []
    if f.get("type") == "fillet":
        sizes.append(("radius", float(f["radius"])))
    elif f.get("type") == "chamfer":
        sizes += [("chamfer", float(f[k])) for k in ("distance", "distance_2") if f.get(k)]
    elif f.get("type") in PROFILED and isinstance(f.get("profile"), dict):
        sizes += [("radius", r) for r in _profile_radii(f["profile"])]
    out: list[tuple[SizeKind, float]] = []
    for kind, size in sizes:
        if not any(k == kind and _same(size, s) for k, s in out):
            out.append((kind, size))
    return out


def _chamfer_legs(d: dict[str, Any]) -> list[float]:
    """Both legs of a printed chamfer such as '2x30°' (which leg the angle is on varies)."""
    value = float(d["value"])
    m = re.search(r"x\s*(\d+(?:[.,]\d+)?)\s*°", str(d.get("stated_text") or ""))
    if not m:
        return [value]
    t = math.tan(math.radians(float(m.group(1).replace(",", "."))))
    return [value, value * t, value / t] if t > 1e-9 else [value]


def _backed(kind: str, size: float, refs: set[str], dimensions: list[dict[str, Any]]) -> bool:
    """A printed (or measured, reviewer-edited) dimension gives this size: a radius, diameter or
    chamfer dimension of that size anywhere on the sheet, or a length the feature references (a
    slot end is half its width). "Anywhere": the agent cannot map an arc to its place on the
    sheet (it has no view projection), so a printed R or Ø of the same size stands in for "the
    same arc", like the value matching of the coverage check; a coincidental match is the milder
    error than a "no printed value" item on a printed one."""
    for d in dimensions:
        value, dk, referenced = float(d["value"]), d.get("kind"), d.get("id") in refs
        if kind == "radius":
            if (dk == "radius" and _same(value, size)) or (
                dk == "diameter" and _same(value, 2 * size)
            ):
                return True
            if referenced and dk == "linear" and (_same(value, size) or _same(value, 2 * size)):
                return True
        else:
            if dk == "chamfer" and any(_same(leg, size) for leg in _chamfer_legs(d)):
                return True
            if referenced and dk == "linear" and _same(value, size):
                return True
    return False


def _kind_is(assumption: dict[str, Any], kind: str) -> bool:
    text = str(assumption.get("kind") or "").lower()
    return any(word in text for word in _KIND_WORDS[kind])


def _link(assumption: dict[str, Any], feature_id: str, dimension_id: str | None = None) -> None:
    if feature_id not in assumption["feature_ids"]:
        assumption["feature_ids"].append(feature_id)
    if dimension_id and dimension_id not in assumption["dimension_ids"]:
        assumption["dimension_ids"].append(dimension_id)


def audit(
    features: list[dict[str, Any]],
    assumptions: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Make symmetric extrusions and unprinted radii and chamfers explicit (D93).

    `dimensions` are the checked dimensions of the drawing. Returns the features (each with the
    ids of its assumed dimensions), the assumptions (the planner's, linked to their features,
    then the audit's own) and the `method="assumed"` dimensions to build with."""
    known = [d for d in dimensions if d.get("status") != "rejected"]
    # A general assumption (the planner named no features, e.g. a note "unspecified radii R1")
    # applies to every matching feature; its links are recomputed in every audit, so a removed
    # feature drops out and an added one is covered.
    own = [
        {
            **a,
            "feature_ids": [] if a.get("general") else _ids(a.get("feature_ids")),
            "dimension_ids": [i for i in _ids(a.get("dimension_ids")) if not i.startswith(ASSUMED)],
        }
        for a in assumptions
        if not str(a.get("id", "")).startswith(AUTO)
    ]
    general = [not a["feature_ids"] for a in own]
    for a, applies_to_all in zip(own, general, strict=True):
        if applies_to_all:
            a["general"] = True
    added: list[dict[str, Any]] = []
    assumed: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    def covering(feature_id: str, kind: str, size: float | None) -> dict[str, Any] | None:
        """The planner's own assumption for this: its kind, its feature (or none named), and
        its value if it gives one."""
        for a, applies_to_all in zip(own, general, strict=True):
            value = _number(a.get("value"))
            if (
                _kind_is(a, kind)
                and (applies_to_all or feature_id in a["feature_ids"])
                and (size is None or value is None or _same(value, size))
            ):
                return a
        return None

    def assume(feature_id: str, kind: str, value: float | str, dimension_id: str | None) -> None:
        added.append(
            {
                "id": f"{AUTO}{len(added) + 1}",
                "kind": kind,
                "value": value,
                "reason": _REASONS[kind],
                "feature_ids": [feature_id],
                "dimension_ids": [dimension_id] if dimension_id else [],
            }
        )

    for feature in features:
        f = {
            **feature,
            "dimension_ids": [
                i for i in _ids(feature.get("dimension_ids")) if not i.startswith(ASSUMED)
            ],
        }
        fid = str(f["id"])
        if f.get("type") == "extrude_base" and f.get("direction") == "symmetric":
            planned = covering(fid, "symmetry", None)
            if planned is not None:
                _link(planned, fid)
            else:
                assume(fid, "symmetry", f"±{float(f['depth']) / 2:g}", None)
        unstated = [
            (k, s) for k, s in _sizes(f) if not _backed(k, s, set(f["dimension_ids"]), known)
        ]
        for n, (kind, size) in enumerate(unstated, 1):
            dim_id = f"{ASSUMED}{fid}" + (f"-{n}" if len(unstated) > 1 else "")
            planned = covering(fid, kind, size)
            reason = str((planned or {}).get("reason") or "").strip() or _REASONS[kind]
            if planned is not None:
                _link(planned, fid, dim_id)
            else:
                assume(fid, kind, size, dim_id)
            assumed.append(
                Dimension(
                    id=dim_id,
                    kind=kind,
                    value=size,
                    method="assumed",
                    confidence=0.0,
                    status="needs_review",
                    reason=reason,
                    feature_ids=[fid],
                ).model_dump(mode="json")
            )
            f["dimension_ids"].append(dim_id)
        out.append(f)
    return out, own + added, assumed


def restore_general(
    assumptions: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A later step may echo a general assumption with its links but without the `general` mark:
    the same id and the same links as before mean it is still general."""
    before = {str(a.get("id")): a for a in previous if a.get("general")}
    out = []
    for a in assumptions:
        old = before.get(str(a.get("id")))
        echoed = old is not None and sorted(_ids(a.get("feature_ids"))) == sorted(
            old["feature_ids"]
        )
        out.append({**a, "general": True} if echoed and "general" not in a else a)
    return out
