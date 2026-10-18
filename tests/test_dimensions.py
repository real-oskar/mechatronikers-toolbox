import io
from pathlib import Path

import numpy as np
import pytest

from mechatronikers.drawing_mcp.analysis import analyse
from mechatronikers.drawing_mcp.dimensions import (
    Arrow,
    ChainClaim,
    DimensionClaim,
    GapRequest,
    check_consistency,
    find_arrows,
    leader_gap,
    measure_label,
    parse_dimension_text,
)
from mechatronikers.drawing_mcp.session import DrawingSession
from mechatronikers.spec.drawing import Primitive
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.cases import LfsPointerError, ensure_materialized
from tests.fixtures.drawings import (
    make_drawing,
    make_foreign_arrow_drawing,
    make_two_radii_drawing,
)


@pytest.mark.parametrize(
    ("text", "kind", "value", "count"),
    [
        ("60", "linear", 60, 1),
        ("76,2", "linear", 76.2, 1),
        ("Ø20", "diameter", 20, 1),
        ("⌀ 2,5", "diameter", 2.5, 1),
        ("3x Ø12", "diameter", 12, 3),
        ("4×Ø19", "diameter", 19, 4),
        ("R9,5", "radius", 9.5, 1),
        ("M10", "thread", 10, 1),
        ("25,5°", "angle", 25.5, 1),
        ("hello", "other", None, 1),
    ],
)
def test_parse_dimension_text(text: str, kind: str, value: float | None, count: int) -> None:
    parsed = parse_dimension_text(text)
    assert (parsed.kind, parsed.value, parsed.count) == (kind, value, count)


def test_parse_chamfer_and_tolerances() -> None:
    chamfer = parse_dimension_text("5x45°")
    assert (chamfer.kind, chamfer.value, chamfer.angle) == ("chamfer", 5, 45)
    fit = parse_dimension_text("Ø20 H7")
    assert fit.value == 20 and fit.tolerance is not None and fit.tolerance.fit == "H7"
    sym = parse_dimension_text("10 ±0,1")
    assert sym.tolerance is not None and sym.tolerance.plus == pytest.approx(0.1)


@pytest.fixture
def sheet(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "input").mkdir()
    path = make_drawing(tmp_path / "input" / "sheet.pdf")
    analysis = analyse(path)
    labels = {label.id: label for label in analysis.summary.labels}

    def label_at(x: float, y: float) -> str:
        return next(
            i
            for i, lb in labels.items()
            if lb.bbox[0] <= x <= lb.bbox[2] and lb.bbox[1] <= y <= lb.bbox[3]
        )

    return analysis, labels, label_at


def test_arrows_and_measurements(sheet) -> None:  # type: ignore[no-untyped-def]
    analysis, labels, label_at = sheet
    arrows = find_arrows(analysis.raw)
    assert len(arrows) == 4  # two on the dimension line, the Ø leader, the filled triangle
    claims = [
        DimensionClaim(label_id=label_at(108, 120.5), text="100"),
        DimensionClaim(label_id=label_at(130, 52.5), text="Ø20"),
    ]
    report = check_consistency(labels, arrows, analysis.primitives, claims, sheet_scale=1.0)
    by_text = {d.stated_text: d for d in report.dimensions}
    assert by_text["100"].measured == pytest.approx(100, abs=0.01)
    assert by_text["Ø20"].measured == pytest.approx(20, abs=0.01)
    assert all(d.status == "auto_ok" for d in report.dimensions)
    assert report.scales[0].scale == 1.0 and report.scales[0].agrees_with_sheet


def test_mismatch_underline_chain_pattern_and_gap(sheet) -> None:  # type: ignore[no-untyped-def]
    analysis, labels, label_at = sheet
    linear, round_ = label_at(108, 120.5), label_at(130, 52.5)
    report = check_consistency(
        labels,
        find_arrows(analysis.raw),
        analysis.primitives,
        [
            DimensionClaim(label_id=linear, text="90"),  # geometry says 100
            DimensionClaim(label_id=round_, text="2x Ø20"),  # only one circle exists
        ],
        sheet_scale=1.0,
        chains=[ChainClaim(part_label_ids=[linear, round_], total_label_id=linear)],
        gaps=[
            GapRequest(
                id="d-depth", kind="linear", sheet_value=12.5, reason="pocket depth from side view"
            )
        ],
    )
    dims = {d.id: d for d in report.dimensions}
    assert dims[f"d-{linear}"].status == "needs_review" and dims[f"d-{linear}"].value == 90
    assert dims["d-depth"].method == "measured" and dims["d-depth"].value == 12.5
    assert dims["d-depth"].reason and "pocket depth" in dims["d-depth"].reason
    messages = " | ".join(r.message for r in report.review_items)
    assert "disagrees" in messages and "1 circles of Ø20" in messages and "Chain sums" in messages
    assert "d-depth = 12.5 mm measured" in messages

    underlined = check_consistency(
        labels, find_arrows(analysis.raw), analysis.primitives,
        [DimensionClaim(label_id=linear, text="90", underlined=True)], sheet_scale=1.0,
    )  # fmt: skip
    assert underlined.dimensions[0].status == "auto_ok"  # not to scale: no mismatch
    assert any(r.kind == "assumption" for r in underlined.review_items)


def test_session_tools(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    make_drawing(tmp_path / "input" / "sheet.pdf")
    session = DrawingSession(
        tmp_path / "input", tmp_path / "artifacts", EventEmitter("d", stream=io.StringIO())
    )
    drawing_id = session.drawing_id_of(session.load("sheet.pdf"))
    label = next(
        lb
        for lb in session.labels(drawing_id, region="drawing")
        if lb.bbox[0] <= 108 <= lb.bbox[2] and lb.bbox[1] <= 120.5 <= lb.bbox[3]
    )
    with pytest.raises(ValueError, match="no text yet"):
        session.measure_label(drawing_id, label.id)
    session.set_label_text(drawing_id, label.id, "100", "vlm")
    assert session.measure_label(drawing_id, label.id).sheet_value == pytest.approx(100, abs=0.01)
    report = session.check(drawing_id, sheet_scale=1.0)
    assert [d.stated_text for d in report.dimensions] == ["100"]
    lines = [
        p
        for p in session.find_geometry(drawing_id, (59, 39, 161, 101), kinds=["line"]).primitives
        if p.width == 0.99
    ]
    top = next(
        p
        for p in lines
        if p.start and p.end and abs(p.start[1] - 40) < 0.1 and abs(p.end[1] - 40) < 0.1
    )
    bottom = next(
        p
        for p in lines
        if p.start and p.end and abs(p.start[1] - 100) < 0.1 and abs(p.end[1] - 100) < 0.1
    )
    between = session.measure_between(drawing_id, top.id, bottom.id, scale=2.0)
    assert between.kind == "parallel distance" and between.sheet_value == pytest.approx(
        60, abs=0.01
    )
    assert between.part_value == pytest.approx(30, abs=0.01)


# Regression on the real sample drawings: label texts as printed (read by hand once).
REAL = {
    "Flansch": (2.0, {"l12": "10", "l13": "25", "l14": "6", "l15": "20", "l18": "38", "l19": "10",
                      "l25": "5", "l26": "Ø30", "l27": "25", "l30": "Ø2,5", "l31": "Ø3,5",
                      "l32": "Ø20", "l37": "20", "l39": "60"}),
    "Fuss": (1.0, {"l10": "R9,5", "l13": "76,2", "l14": "19", "l17": "19", "l20": "Ø57",
                   "l21": "115", "l22": "71", "l23": "Ø33", "l26": "R22", "l27": "Ø19"}),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_drawings_confirm_every_dimension(name: str) -> None:
    path = Path(f"data/{name}/{name}_dwg.pdf")
    try:
        ensure_materialized(path)
    except (LfsPointerError, FileNotFoundError):
        pytest.skip("sample drawing not available (Git LFS not pulled)")
    scale, texts = REAL[name]
    analysis = analyse(path)
    labels = {label.id: label for label in analysis.summary.labels}
    report = check_consistency(
        labels, find_arrows(analysis.raw), analysis.primitives,
        [DimensionClaim(label_id=k, text=v) for k, v in texts.items()], sheet_scale=scale,
    )  # fmt: skip
    assert [d.stated_text for d in report.dimensions if d.status != "auto_ok"] == []
    assert all(s.scale == scale for s in report.scales)
    # only the labels this test does not read are left, each as a review item
    unread = {f"d-{lb.id}" for lb in labels.values() if lb.region == "drawing"} - {
        f"d-{k}" for k in texts
    }
    assert {d for i in report.review_items for d in i.dimension_ids} == unread
    assert all(i.message.endswith("was not read") for i in report.review_items)


def test_lettering_is_not_a_dimension(sheet) -> None:  # type: ignore[no-untyped-def]
    analysis, labels, label_at = sheet
    arrows = find_arrows(analysis.raw)
    letters = label_at(61, 110)  # the 3-glyph label left of the view: here read as a view name
    claims = [
        DimensionClaim(label_id=letters, text="A-A"),
        DimensionClaim(label_id=label_at(141, 30), text="4x?"),  # has a digit: unreadable dimension
    ]
    report = check_consistency(labels, arrows, analysis.primitives, claims, sheet_scale=1.0)
    read = [i.message for i in report.review_items if not i.message.endswith("was not read")]
    assert read == ["Could not interpret '4x?'"]
    assert report.dimensions == []


# -- radii close together (#91): a radius label binds to the arc its leader points to ---------


@pytest.fixture
def two_radii(tmp_path: Path):  # type: ignore[no-untyped-def]
    analysis = analyse(make_two_radii_drawing(tmp_path / "radii.pdf"))
    labels = {label.id: label for label in analysis.summary.labels}

    def label_at(x: float, y: float) -> str:
        return next(
            i
            for i, lb in labels.items()
            if lb.bbox[0] <= x <= lb.bbox[2] and lb.bbox[1] <= y <= lb.bbox[3]
        )

    arcs = {round(p.radius): p.id for p in analysis.primitives if p.kind == "arc" and p.radius}
    return analysis, labels, label_at, arcs


def test_radius_labels_bind_to_the_arc_their_leader_points_to(two_radii) -> None:  # type: ignore[no-untyped-def]
    analysis, labels, label_at, arcs = two_radii
    arrows = find_arrows(analysis.raw)
    r10, r5 = label_at(162, 50.5), label_at(173, 64)
    # The R5 label is nearer the R10 arrow than its own: proximity picked the R10 arc.
    first = measure_label(labels[r5], "radius", arrows, analysis.primitives)
    assert first.primitive_ids == [arcs[5]] and first.leader
    claims = [
        DimensionClaim(label_id=r10, text="R10"),
        DimensionClaim(label_id=r5, text="R5"),
        DimensionClaim(label_id=label_at(114, 120.5), text="110"),
    ]
    report = check_consistency(labels, arrows, analysis.primitives, claims, sheet_scale=1.0)
    bound = {m.label_id: m for m in report.measurements}
    assert bound[r10].primitive_ids == [arcs[10]] and bound[r5].primitive_ids == [arcs[5]]
    assert not any("matched to print" in m.method for m in report.measurements)
    assert [d.status for d in report.dimensions] == ["auto_ok"] * 3
    assert report.review_items == []


def test_radius_that_does_not_match_its_arc_is_a_review_item(two_radii) -> None:  # type: ignore[no-untyped-def]
    """Labels read the other way round: matching each to the arc of its printed size would
    silently build both radii on the wrong corners (an R10 where the drawing shows R5)."""
    analysis, labels, label_at, arcs = two_radii
    claims = [
        DimensionClaim(label_id=label_at(162, 50.5), text="R5"),
        DimensionClaim(label_id=label_at(173, 64), text="R10"),
        DimensionClaim(label_id=label_at(114, 120.5), text="110"),
    ]
    report = check_consistency(
        labels, find_arrows(analysis.raw), analysis.primitives, claims, sheet_scale=1.0
    )
    dims = {d.stated_text: d for d in report.dimensions}
    assert (dims["R5"].status, dims["R5"].value) == ("needs_review", 5)  # print stays (D12)
    assert (dims["R10"].status, dims["R10"].value) == ("needs_review", 10)
    assert dims["R5"].measured == pytest.approx(10, abs=0.01)
    assert dims["R10"].measured == pytest.approx(5, abs=0.01)
    assert dims["R5"].reason and "arc its leader points to measures R10" in dims["R5"].reason
    assert [(i.kind, i.message) for i in report.review_items] == [
        ("mismatch", "Radius 'R5' does not match the arc it points to"),
        ("mismatch", "Radius 'R10' does not match the arc it points to"),
    ]
    assert dims["110"].status == "auto_ok"


def test_leader_gap_follows_the_drawn_leader_only() -> None:
    box = (10.0, 0.0, 14.0, 3.0)
    arrow = Arrow(tip=np.array([0.0, 1.5]), direction=np.array([-1.0, 0.0]))  # shaft runs right

    def line(x0: float, x1: float) -> Primitive:
        return Primitive(id=f"p{x0}", kind="line", width=0.5, start=(x0, 1.5), end=(x1, 1.5))

    assert leader_gap(arrow, box, [line(0, 12)]) == 0.0  # the leader runs onto the label
    assert leader_gap(arrow, box, [line(0, 9)]) == pytest.approx(1.0)  # ends right beside it
    assert leader_gap(arrow, box, []) is None  # an arrow without a drawn leader leads nowhere
    assert leader_gap(arrow, box, [line(20, 30)]) is None  # a stroke elsewhere on its line


@pytest.mark.parametrize(("layout", "text", "radius"), [("far", "R5", 5), ("collinear", "R20", 20)])
def test_a_foreign_arrow_aimed_at_the_label_does_not_win(
    tmp_path: Path, layout: str, text: str, radius: float
) -> None:
    """Another dimension's radial arrow whose shaft line, extended, runs through the label must
    not take the label from its own leader (review of #106)."""
    point = make_foreign_arrow_drawing(tmp_path / "sheet.pdf", layout)
    analysis = analyse(tmp_path / "sheet.pdf")
    labels = {label.id: label for label in analysis.summary.labels}
    label = next(
        lb for lb in labels.values()
        if lb.bbox[0] <= point[0] <= lb.bbox[2] and lb.bbox[1] <= point[1] <= lb.bbox[3]
    )  # fmt: skip
    arrows = find_arrows(analysis.raw)
    m = measure_label(label, "radius", arrows, analysis.primitives)
    assert m.leader and m.sheet_value == pytest.approx(radius, abs=0.01)
    report = check_consistency(
        labels, arrows, analysis.primitives,
        [DimensionClaim(label_id=label.id, text=text)], sheet_scale=1.0,
    )  # fmt: skip
    # The other dimension's label is not read here: only an `unreadable` item for it (D96).
    assert {i.kind for i in report.review_items} <= {"unreadable"}
    assert report.dimensions[0].status == "auto_ok"


def test_every_drawing_label_is_read_or_a_review_item(sheet) -> None:  # type: ignore[no-untyped-def]
    """A label nobody read (unreadable crop, skipped by the model) must not pass silently: if
    it is a dimension, the feature it sizes may be missing (#89)."""
    analysis, labels, label_at = sheet
    linear = label_at(108, 120.5)
    claims = [DimensionClaim(label_id=linear, text="100")]
    report = check_consistency(
        labels, find_arrows(analysis.raw), analysis.primitives, claims, sheet_scale=1.0
    )
    drawing = [lb.id for lb in labels.values() if lb.region == "drawing"]
    unread = [i for i in report.review_items if i.kind == "unreadable"]
    assert [i.dimension_ids for i in unread] == [[f"d-{i}"] for i in drawing if i != linear]
    assert all(i.sheet_ref is not None and i.reason for i in unread)
    title = [lb.id for lb in labels.values() if lb.region != "drawing"]
    assert not {f"d-{i}" for i in title} & {d for i in unread for d in i.dimension_ids}
