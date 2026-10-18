"""What the drawing does not state (#90, D93): located review items and the plan audit.

Hand-written plans for a made-up plate, not tuned to any sample part (AGENTS.md data rules).
"""

import math
from typing import Any

from mechatronikers.agent.pipeline import missing_dimensions
from mechatronikers.agent.unstated import (
    assumable,
    audit,
    locate,
    merge_undimensioned,
    normalize_undimensioned,
    restore_general,
    undimensioned_items,
)
from mechatronikers.spec import Dimension

VIEWS = [
    {"id": "v0", "kind": "orthographic", "bbox": [58, 40, 162, 102]},
    {"id": "v1", "kind": "orthographic", "bbox": [200, 40, 240, 102]},
    {"id": "v2", "kind": "image", "bbox": [300, 60, 360, 120]},
]


def _dim(id: str, kind: str, value: float, text: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "stated": value,
        "stated_text": text or f"{value:g}",
        "value": value,
        "method": "vlm",
        "confidence": 0.9,
        "status": "auto_ok",
        **extra,
    }


def _rect(w: float, h: float, r: float = 0.0) -> dict[str, Any]:
    """A w x h rectangle about the origin, corners rounded with radius r (three-point arcs)."""
    if not r:
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        return {
            "segments": [
                {"kind": "line", "start": list(a), "end": list(b)}
                for a, b in zip(pts, pts[1:] + pts[:1], strict=True)
            ]
        }
    segments: list[dict[str, Any]] = []
    for i, (cx, cy) in enumerate(
        [
            (w / 2 - r, -h / 2 + r),
            (w / 2 - r, h / 2 - r),
            (-w / 2 + r, h / 2 - r),
            (-w / 2 + r, -h / 2 + r),
        ]
    ):
        a0 = math.radians(-90 + 90 * i)
        arc = [
            (cx + r * math.cos(a0 + t), cy + r * math.sin(a0 + t))
            for t in (0, math.pi / 4, math.pi / 2)
        ]
        if segments:
            segments.append({"kind": "line", "start": segments[-1]["end"], "end": list(arc[0])})
        segments.append(
            {"kind": "arc", "start": list(arc[0]), "mid": list(arc[1]), "end": list(arc[2])}
        )
    segments.append({"kind": "line", "start": segments[-1]["end"], "end": segments[0]["start"]})
    return {"segments": segments}


def _base(**extra: Any) -> dict[str, Any]:
    return {"type": "extrude_base", "id": "base", "profile": _rect(100, 60), "depth": 10, **extra}


def _fillet(id: str, radius: float) -> dict[str, Any]:
    return {
        "type": "fillet",
        "id": id,
        "radius": radius,
        "edges": {"by": "parallel", "axis": [0, 0, 1]},
    }


def _chamfer(id: str, distance: float, distance_2: float | None = None) -> dict[str, Any]:
    return {
        "type": "chamfer",
        "id": id,
        "distance": distance,
        "distance_2": distance_2,
        "edges": {"by": "parallel", "axis": [1, 0, 0]},
    }


# --------------------------------------------------------------------------- undimensioned


def _seen(*answers: list[Any]) -> list[dict[str, Any]]:
    """The undimensioned entries after the planning steps that gave these answers."""
    entries: list[dict[str, Any]] = []
    for answer in answers:
        entries = merge_undimensioned(entries, normalize_undimensioned(answer))
    return entries


def test_undimensioned_features_are_placed_on_their_view() -> None:
    entries = _seen(
        [
            {"id": "x", "view_id": "v0", "bbox": [65, 88, 155, 91], "reason": "rib, no sizes"},
            {"view_id": "front", "bbox": [150, 110, 170, 95]},  # unknown view, corners swapped
            {"view_id": "v1", "feature_id": "web"},  # no region: the whole view
            {"view_id": "v0", "bbox": [0, 0, 10, 10]},  # region outside its view
            {"view_id": "v9", "bbox": ["a", 1, 2, 3]},  # nothing usable
            "a step on the back side",  # models sometimes answer with sentences
            42,
        ]
    )
    assert [e["id"] for e in entries] == ["u-1", "u-2", "u-3", "u-4", "u-5", "u-6"]  # own ids
    items = undimensioned_items(entries, VIEWS, {"base": False, "web": False})
    assert {i.kind for i in items} == {"undimensioned_feature"}
    assert [i.id for i in items] == [f"undim-{n}" for n in range(1, 7)]
    refs = [(i.sheet_ref.view_id, i.sheet_ref.bbox) if i.sheet_ref else None for i in items]
    assert refs == [
        ("v0", (65, 88, 155, 91)),
        ("v0", (150, 95, 162, 102)),  # the view it overlaps, clipped to it
        ("v1", (200, 40, 240, 102)),
        ("v0", (58, 40, 162, 102)),
        None,
        None,
    ]
    assert items[0].reason == "rib, no sizes" and items[0].message.endswith("not modelled")
    # a non-edge feature built for it has sizes the drawing does not print (rule 5): blocking
    assert items[2].feature_ids == ["web"] and "built as web with sizes" in items[2].message
    assert items[2].severity == "blocking" and items[0].severity == "warning"
    assert items[4].message.startswith("Feature without dimensions (no view given)")
    assert items[5].reason == "a step on the back side"
    assert items[1].reason  # never without a reason


def test_locate_prefers_the_named_view() -> None:
    region = [230, 50, 310, 70]  # overlaps v1 (10 x 20 mm) more than v2 (10 x 10 mm)
    assert locate(None, region, VIEWS) == ("v1", (230, 50, 240, 70))
    assert locate("v0", region, VIEWS) == ("v0", (58, 40, 162, 102))  # named, region elsewhere
    assert locate("v1", None, VIEWS) == ("v1", (200, 40, 240, 102))
    assert locate(None, None, VIEWS) == (None, None)


def test_a_later_step_never_replaces_an_undimensioned_feature() -> None:
    """The review of #107: id-less or renumbered answers must not overwrite earlier entries."""
    plan = [{"view_id": "v0", "reason": "rib, no sizes"}]
    fix = ["a step on the back side"]  # no id: used to become u-1 and replace the rib
    replan = [{"id": "u-1", "view_id": "v0", "reason": "a hole without a size"}]  # renumbered
    entries = _seen(plan, fix, replan)
    assert [(e["id"], e["reason"]) for e in entries] == [
        ("u-1", "rib, no sizes"),
        ("u-2", "a step on the back side"),
        ("u-3", "a hole without a size"),
    ]
    items = undimensioned_items(entries, VIEWS, {})
    assert [(i.id, i.reason) for i in items] == [
        ("undim-1", "rib, no sizes"),
        ("undim-2", "a step on the back side"),
        ("undim-3", "a hole without a size"),
    ]
    state = {"spec": {"features": [], "assumptions": [], "undimensioned": entries}}
    assert [m.id for m in missing_dimensions(state)] == ["undim-1", "undim-2", "undim-3"]


def test_an_echoed_undimensioned_feature_is_linked_not_duplicated() -> None:
    entries = _seen([{"view_id": "v0", "reason": "rib"}, {"reason": "rounded corners"}])
    echo = [
        {"id": "u-2", "view_id": "v1", "bbox": [210, 50, 220, 60], "feature_id": "round"},
        {"view_id": "v0", "reason": "rib"},  # the same content without an id
    ]
    merged = merge_undimensioned(entries, normalize_undimensioned(echo))
    assert [(e["id"], e["view_id"], e["feature_id"]) for e in merged] == [
        ("u-1", "v0", None),
        ("u-2", "v1", "round"),  # the missing view and region filled in, the feature linked
    ]
    assert merged[1]["bbox"] == [210, 50, 220, 60] and merged[1]["reason"] == "rounded corners"
    assert merge_undimensioned(merged, []) == merged
    # an echo that names another feature does not re-link the entry: it is listed anew
    relinked = merge_undimensioned(
        merged, normalize_undimensioned([{"id": "u-2", "feature_id": "other"}])
    )
    assert [(e["id"], e["feature_id"]) for e in relinked] == [
        ("u-1", None),
        ("u-2", "round"),
        ("u-3", "other"),
    ]
    items = undimensioned_items(merged, VIEWS, assumable([_base(), _fillet("round", 2)]))
    assert items[1].severity == "warning" and "with an assumed size" in items[1].message


def test_undimensioned_features_count_as_missing_dimensions() -> None:
    state = {
        "spec": {
            "features": [_base()],
            "assumptions": [],
            "undimensioned": normalize_undimensioned([{"view_id": "v0", "reason": "rib"}]),
        }
    }
    assert [(m.id, m.reason) for m in missing_dimensions(state)] == [("undim-1", "rib")]


# --------------------------------------------------------------------------- symmetry


def test_symmetric_extrusion_is_an_assumption() -> None:
    features, assumptions, assumed = audit([_base(direction="symmetric")], [], [])
    assert assumed == [] and features[0]["dimension_ids"] == []
    (a,) = assumptions
    assert (a["id"], a["kind"], a["value"], a["feature_ids"]) == (
        "a-auto-1",
        "symmetry",
        "±5",
        ["base"],
    )
    assert "mid-plane" in a["reason"]
    # the planner's own assumption is kept (and linked), not duplicated
    own = {"id": "a-1", "kind": "symmetry", "value": None, "reason": "centre line in view v1"}
    _, assumptions, _ = audit([_base(direction="symmetric")], [own], [])
    assert assumptions == [{**own, "feature_ids": ["base"], "dimension_ids": [], "general": True}]
    # a positive extrusion needs nothing
    assert audit([_base(direction="positive")], [], [])[1] == []


# --------------------------------------------------------------------------- radii and chamfers


def test_unprinted_radius_is_built_as_an_assumed_dimension() -> None:
    features, assumptions, assumed = audit(
        [_base(), _fillet("round", 2)], [], [_dim("d-1", "linear", 100)]
    )
    (dim,) = assumed
    checked = Dimension.model_validate(dim)  # what cad-mcp gets in new_part
    assert (checked.id, checked.kind, checked.value) == ("d-assumed-round", "radius", 2)
    assert (checked.method, checked.status, checked.stated) == ("assumed", "needs_review", None)
    assert checked.feature_ids == ["round"] and checked.reason
    assert features[1]["dimension_ids"] == ["d-assumed-round"]
    assert assumptions == [
        {
            "id": "a-auto-1",
            "kind": "radius",
            "value": 2.0,
            "reason": checked.reason,
            "feature_ids": ["round"],
            "dimension_ids": ["d-assumed-round"],
        }
    ]


def test_printed_radius_or_chamfer_needs_nothing() -> None:
    dims = [
        _dim("d-r", "radius", 2, "R2"),
        _dim("d-c", "chamfer", 2, "2x30°"),
        _dim("d-l", "linear", 3),
    ]
    plan = [
        _base(),
        _fillet("round", 2),  # any printed R2 gives it
        _chamfer("c1", 2, 2 * math.tan(math.radians(30))),  # both legs of '2x30°'
        _chamfer("c2", 3),  # a referenced length
    ]
    plan[3]["dimension_ids"] = ["d-l"]
    features, assumptions, assumed = audit(plan, [], dims)
    assert assumed == [] and assumptions == []
    assert [f["dimension_ids"] for f in features] == [[], [], [], ["d-l"]]


def test_a_rejected_dimension_backs_nothing() -> None:
    _, _, assumed = audit(
        [_base(), _fillet("round", 2)], [], [_dim("d-r", "radius", 2, status="rejected")]
    )
    assert [d["id"] for d in assumed] == ["d-assumed-round"]


def test_general_note_covers_every_matching_radius() -> None:
    note = {
        "id": "a-1",
        "kind": "radius",
        "value": 1,
        "reason": "general note: unspecified radii R1",
    }
    plan = [_base(), _fillet("f1", 1), _fillet("f2", 1), _fillet("f3", 4)]
    features, assumptions, assumed = audit(plan, [note], [])
    assert [(d["id"], d["reason"]) for d in assumed] == [
        ("d-assumed-f1", note["reason"]),
        ("d-assumed-f2", note["reason"]),
        ("d-assumed-f3", assumed[2]["reason"]),
    ]
    assert assumptions[0] == {
        **note,
        "feature_ids": ["f1", "f2"],
        "dimension_ids": ["d-assumed-f1", "d-assumed-f2"],
        "general": True,
    }
    assert [(a["id"], a["value"], a["feature_ids"]) for a in assumptions[1:]] == [
        ("a-auto-1", 4.0, ["f3"])
    ]


def test_general_note_links_are_recomputed_in_every_step() -> None:
    """The review of #107: a feature added later is covered, a removed one drops out."""
    note = {"id": "a-1", "kind": "radius", "value": 1, "reason": "general note: radii R1"}
    _, first, _ = audit([_base(), _fillet("f1", 1), _fillet("f2", 1)], [note], [])
    later = [_base(), _fillet("f2", 1), _fillet("f3", 1)]  # f1 removed, f3 added
    for echoed in (first, [{k: v for k, v in first[0].items() if k != "general"}]):
        planned = restore_general(echoed, first)  # a model may drop the mark when it echoes
        _, assumptions, assumed = audit(later, planned, [])
        assert [(a["id"], a["feature_ids"]) for a in assumptions] == [("a-1", ["f2", "f3"])]
        assert {d["reason"] for d in assumed} == {note["reason"]}  # no "no source" for f3


def test_a_printed_diameter_backs_the_same_radius() -> None:
    """An arc R39 needs no assumption when Ø78 is printed, also if the feature does not
    reference that dimension (the agent cannot map the arc to the sheet)."""
    rounded = _base(profile=_rect(100, 80, 39), dimension_ids=["d-w"])
    dims = [_dim("d-w", "linear", 100), _dim("d-d", "diameter", 78, "Ø78")]
    assert audit([rounded], [], dims)[2] == []
    assert [d["id"] for d in audit([rounded], [], dims[:1])[2]] == ["d-assumed-base"]


def test_profile_arcs_without_a_printed_radius_are_assumed() -> None:
    rounded = _base(profile=_rect(100, 60, 5), dimension_ids=["d-w", "d-h"])
    dims = [_dim("d-w", "linear", 100), _dim("d-h", "linear", 60)]
    features, assumptions, assumed = audit([rounded], [], dims)
    assert [(d["id"], d["kind"], round(d["value"], 6)) for d in assumed] == [
        ("d-assumed-base", "radius", 5)
    ]
    assert features[0]["dimension_ids"] == ["d-w", "d-h", "d-assumed-base"]
    assert assumptions[0]["kind"] == "radius" and assumptions[0]["feature_ids"] == ["base"]
    # a semicircular end is half a referenced width: backed
    slot_end = _base(profile=_rect(100, 20, 10), dimension_ids=["d-h"])
    assert audit([slot_end], [], [_dim("d-h", "linear", 20)])[2] == []


def test_audit_is_idempotent_across_planning_steps() -> None:
    note = {"id": "a-1", "kind": "fillet radius", "value": "R1", "reason": "general note"}
    plan = [_base(direction="symmetric"), _fillet("f1", 1), _chamfer("c1", 0.5)]
    once = audit(plan, [note], [])
    # a later step echoes the audited plan back (assumed ids, the audit's own assumptions)
    twice = audit(once[0], once[1], [])
    assert twice == once
    assert [a["id"] for a in once[1]] == ["a-1", "a-auto-1", "a-auto-2"]
    assert [d["id"] for d in once[2]] == ["d-assumed-f1", "d-assumed-c1"]
    # a changed size gets a fresh entry; the stale one is gone
    changed = [*once[0][:2], {**once[0][2], "distance": 0.8}]
    _, assumptions, assumed = audit(changed, once[1], [])
    assert [(d["id"], d["value"]) for d in assumed][1] == ("d-assumed-c1", 0.8)
    assert [a["value"] for a in assumptions if a["id"].startswith("a-auto-")] == ["±5", 0.8]
