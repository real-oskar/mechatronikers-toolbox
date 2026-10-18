import io
import json
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.cad_mcp.part_session import PartSession, printed_overall_size
from mechatronikers.spec import (
    Dimension,
    ExtrudeBase,
    Hole,
    Placement,
    ScriptFeature,
    rectangle,
)
from mechatronikers.spec.build import features_sha256
from mechatronikers.telemetry import EventEmitter


@pytest.fixture
def session(tmp_path: Path) -> PartSession:
    s = PartSession(
        tmp_path / "builds",
        tmp_path / "artifacts",
        "subprocess",
        EventEmitter("cad-mcp", stream=io.StringIO()),
    )
    s.new_part("Plate", (60, 40, 10))
    assert s.add_feature(ExtrudeBase(id="base", profile=rectangle(60, 40), depth=10)).applied
    return s


def test_failing_change_is_rolled_back(session: PartSession) -> None:
    result = session.add_feature(
        Hole(id="air", diameter=5, placement=Placement(origin=(100, 0, 10), normal=(0, 0, -1)))
    )
    assert not result.applied and "air" in result.message
    assert [f["id"] for f in session.state().features] == ["base"]


def test_update_delete_and_state(session: PartSession) -> None:
    hole = {
        "type": "hole",
        "id": "h",
        "diameter": 5,
        "placement": {"origin": [0, 0, 10], "normal": [0, 0, -1]},
    }
    assert session.add_feature(hole).applied
    assert session.update_feature("h", {"diameter": 8}).applied
    assert session.state().features[1]["diameter"] == 8
    with pytest.raises(ValueError, match="cannot be changed"):
        session.update_feature("h", {"id": "other"})
    with pytest.raises(KeyError):
        session.delete_feature("missing")
    assert session.delete_feature("h").applied
    assert session.state().bbox == pytest.approx((60, 40, 10))


def test_exports_and_views(session: PartSession) -> None:
    refs = session.export(
        ["step-ap242", "step-ap214", "stl", "obj", "features-json", "review-json"]
    )
    assert [r.format for r in refs] == [
        "step-ap242",
        "step-ap214",
        "stl",
        "obj",
        "features-json",
        "review-json",
    ]
    assert all(
        Path(r.path).stat().st_size == r.bytes and r.uri.startswith("artifact://") for r in refs
    )
    assert "AP242" in Path(refs[0].path).read_text()[:2000]
    assert json.loads(Path(refs[4].path).read_text())["part_name"] == "Plate"
    views = session.render_views(["front", "iso"])
    assert all(Path(v.path).read_text().lstrip().startswith("<?xml") for v in views)


def _events(session: PartSession, type: str) -> list[dict[str, Any]]:
    stream = session.emitter.stream
    assert isinstance(stream, io.StringIO)
    return [
        e["payload"] for e in map(json.loads, stream.getvalue().splitlines()) if e["type"] == type
    ]


def test_validation_views_and_exports_share_one_build(session: PartSession) -> None:
    """#101: validate_part reports the current build instead of building again, so the validated
    build is the one whose files are exported, and its feature-list hash is on every event."""
    hole = Hole(id="h", diameter=5, placement=Placement(origin=(0, 0, 10), normal=(0, 0, -1)))
    assert session.add_feature(hole).applied
    builds = sorted(p.name for p in session.builds_dir.iterdir())
    validation = session.validate()
    assert sorted(p.name for p in session.builds_dir.iterdir()) == builds  # no new build
    assert validation.build_id == session.state().last_build_id
    digest = features_sha256([f.model_dump(mode="json") for f in session.features])
    assert validation.features_sha256 == digest
    validated = _events(session, "validation.completed")
    assert (validated[-1]["build_id"], validated[-1]["features_sha256"]) == (
        validation.build_id,
        digest,
    )
    refs = session.export(["step-ap242", "stl", "features-json"]) + session.render_views(["iso"])
    created = _events(session, "artifact.created")
    assert created == [r.model_dump() for r in refs]
    for ref in created:
        assert (ref["build_id"], ref["features_sha256"]) == (validation.build_id, digest)
        assert ref["uri"].split("/")[3] == validation.build_id

    # The same feature list built again: a new build id, the same hash.
    assert session.update_feature("h", {"diameter": 8}).applied
    assert session.update_feature("h", {"diameter": 5}).applied
    rebuilt = session.validate()
    assert rebuilt.build_id != validation.build_id and rebuilt.features_sha256 == digest


def test_validate_needs_a_build(tmp_path: Path) -> None:
    s = PartSession(
        tmp_path / "builds", tmp_path / "artifacts", "subprocess",
        EventEmitter("cad-mcp", stream=io.StringIO()),
    )  # fmt: skip
    s.new_part("Plate", None)
    with pytest.raises(ValueError, match="no features yet"):
        s.validate()


def test_script_feature_creates_review_item(session: PartSession) -> None:
    feature = ScriptFeature(
        id="sc", code="part = part - Pos(0, 0, 10) * Box(4, 4, 4)", reason="odd cut"
    )
    assert session.add_feature(feature).applied
    (item,) = session.review_items
    assert item.kind == "unsupported" and item.reason == "odd cut" and item.feature_ids == ["sc"]


def _printed(did: str, kind: str, value: float | None) -> Dimension:
    if value is None:  # a gap: measured from the drawing, not printed
        return Dimension(id=did, kind=kind, measured=10, value=10, method="measured",  # type: ignore[arg-type]
                         confidence=0.5, status="needs_review", reason="not printed")  # fmt: skip
    return Dimension(id=did, kind=kind, stated=value, stated_text=f"{value:g}", value=value,  # type: ignore[arg-type]
                     method="vlm", confidence=0.9)  # fmt: skip


def test_overall_size_comes_from_printed_dimensions(tmp_path: Path) -> None:
    dims = [_printed("d-60", "linear", 60), _printed("d-r5", "radius", 5)]
    dims.append(_printed("d-gap", "linear", None))
    size, notes = printed_overall_size(("d-60", None, "d-r5"), dims)
    assert size == (60, None, None)
    assert "y not checked: no single printed" in notes[0] and "radius dimension" in notes[1]
    _, notes = printed_overall_size(("d-gap", "d-nope", None), dims)
    assert "not printed" in notes[0] and "unknown dimension d-nope" in notes[1]

    events = io.StringIO()
    s = PartSession(
        tmp_path / "builds", tmp_path / "artifacts", "subprocess",
        EventEmitter("cad-mcp", stream=events),
    )  # fmt: skip
    s.new_part("Plate", None, dims, overall_size_dims=("d-60", "d-nope", None))
    assert s.add_feature(ExtrudeBase(id="base", profile=rectangle(60, 40), depth=10)).applied
    checks = {c.id: c for c in s.validate().report.checks}
    assert checks["V2.bbox_x"].passed and checks["V2.bbox_x"].expected == 60
    assert "V2.bbox_y" not in checks and "V2.bbox_z" not in checks
    validated = [e for e in map(json.loads, events.getvalue().splitlines())
                 if e["type"] == "validation.completed"]  # fmt: skip
    assert len(validated) == 2 and validated[-1]["payload"]["passed"] is True
    assert validated[-1]["payload"]["report"]["checks"] and validated[-1]["payload"]["build_id"]
    state = s.state()
    assert state.overall_size_dims == ("d-60", None, None) and len(state.notes) == 2
    with pytest.raises(ValueError, match="not both"):
        s.new_part("Plate", (60, 40, 10), dims, overall_size_dims=("d-60", None, None))
