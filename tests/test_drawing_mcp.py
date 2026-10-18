import asyncio
import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from langchain_mcp_adapters.client import MultiServerMCPClient
from PIL import Image

from mechatronikers.drawing_mcp.analysis import analyse
from mechatronikers.drawing_mcp.session import DrawingSession
from mechatronikers.sandbox.app import create_app
from mechatronikers.session.connections import mcp_connections
from mechatronikers.telemetry import EventEmitter
from mechatronikers.testbench.cases import LfsPointerError, ensure_materialized
from tests.fixtures.drawings import make_drawing


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    (tmp_path / "input").mkdir()
    return make_drawing(tmp_path / "input" / "sheet.pdf")


def test_layout_and_sheet_format(drawing: Path) -> None:
    s = analyse(drawing).summary
    assert s.sheet_format == "A3"
    assert s.drawing_area == pytest.approx((20, 10, 410, 287), abs=0.2)
    assert s.title_block == pytest.approx((230, 251, 410, 287), abs=0.2)


def test_circles_views_and_images(drawing: Path) -> None:
    a = analyse(drawing)
    circles = [p for p in a.primitives if p.kind == "circle"]
    assert len(circles) == 1
    assert circles[0].radius == pytest.approx(10, abs=0.01)
    assert circles[0].center == pytest.approx((110, 70), abs=0.01)
    ortho = [v for v in a.summary.views if v.kind == "orthographic"]
    assert len(ortho) == 1 and ortho[0].bbox == pytest.approx((60, 40, 160, 100), abs=3)
    assert ortho[0].circle_count == 1
    (image,) = [v for v in a.summary.views if v.kind == "image"]
    assert image.bbox == pytest.approx((300, 60, 360, 120), abs=0.5)


def test_labels_cluster_glyphs_and_skip_arrowheads(drawing: Path) -> None:
    labels = [label for label in analyse(drawing).summary.labels if label.region == "drawing"]
    assert sorted(label.glyph_count for label in labels) == [2, 3, 3, 3]
    three = next(label for label in labels if label.bbox[0] < 61 and label.bbox[1] > 107)
    assert three.orientation == "horizontal"
    assert three.bbox == pytest.approx((60, 108, 68, 112), abs=0.3)
    assert three.view_id is not None


def test_session_render_geometry_and_readings(drawing: Path, tmp_path: Path) -> None:
    session = DrawingSession(
        drawing.parent, tmp_path / "artifacts", EventEmitter("drawing-mcp", stream=io.StringIO())
    )
    summary = session.load("sheet.pdf")
    drawing_id = session.drawing_id_of(summary)
    with pytest.raises(ValueError, match="inside"):
        session.load("../outside.pdf")
    image = session.render(drawing_id, (60, 40, 160, 100), dpi=127)
    assert Image.open(io.BytesIO(image.png)).size == pytest.approx((500, 300), abs=2)
    assert Path(image.path).is_file() and image.uri.startswith("artifact://")
    hits = session.find_geometry(drawing_id, (95, 55, 125, 85), kinds=["circle"])
    assert [p.kind for p in hits.primitives] == ["circle"]
    label = session.labels(drawing_id, region="drawing")[0]
    assert session.set_label_text(drawing_id, label.id, "Ø20", "vlm").text == "Ø20"


def test_load_renders_the_sheet_as_artifacts(drawing: Path, tmp_path: Path) -> None:
    events = io.StringIO()
    session = DrawingSession(
        drawing.parent, tmp_path / "artifacts", EventEmitter("drawing-mcp", stream=events)
    )
    drawing_id = session.drawing_id_of(session.load("sheet.pdf"))
    created = [e["payload"] for e in map(json.loads, events.getvalue().splitlines())
               if e["type"] == "artifact.created"]  # fmt: skip
    assert [(a["role"], a["dpi"], a["drawing_id"]) for a in created] == [
        ("sheet", 150, drawing_id),
        ("sheet", 200, drawing_id),
    ]
    for artifact in created:
        png = Path(artifact["path"])
        assert png.parent == tmp_path / "artifacts" / "drawing" / drawing_id
        assert png.read_bytes().startswith(b"\x89PNG") and artifact["bytes"] == png.stat().st_size
        assert artifact["uri"] == f"artifact://local/drawing/{drawing_id}/{png.name}"
    small, large = (Image.open(a["path"]).size for a in created)
    assert large[0] > small[0]


def test_huge_pages_render_within_the_pixel_budget(tmp_path: Path) -> None:
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    from mechatronikers.drawing_mcp.vector import MAX_SHEET_PIXELS, render_sheet, sheet_dpi

    pdf = tmp_path / "huge.pdf"
    page = canvas.Canvas(str(pdf), pagesize=(5000 * mm, 3000 * mm))  # PDF pages reach ~5 m
    page.line(0, 0, 5000 * mm, 3000 * mm)
    page.save()
    renders = render_sheet(pdf, tmp_path / "out")
    assert [dpi for dpi, _ in renders] == [sheet_dpi((5000, 3000), 200)]  # both capped: one file
    dpi, png = renders[0]
    width, height = Image.open(png).size
    assert dpi < 150 and width * height <= MAX_SHEET_PIXELS
    assert sheet_dpi((420, 297), 200) == 200  # A3 keeps full quality


def test_a_failed_sheet_render_does_not_fail_the_load(
    drawing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise MemoryError("page too large")

    monkeypatch.setattr("mechatronikers.drawing_mcp.session.render_sheet", broken)
    events = io.StringIO()
    session = DrawingSession(
        drawing.parent, tmp_path / "artifacts", EventEmitter("drawing-mcp", stream=events)
    )
    assert session.load("sheet.pdf").labels  # the analysis is there
    lines = [json.loads(line) for line in events.getvalue().splitlines()]
    failed = [e for e in lines if e["type"] == "artifact.render_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "warning"
    assert "page too large" in failed[0]["payload"]["error"]
    assert not [e for e in lines if e["type"] == "artifact.created"]


REAL = Path("data/Flansch/Flansch_dwg.pdf")


def test_real_flansch_drawing() -> None:
    try:
        ensure_materialized(REAL)
    except (LfsPointerError, FileNotFoundError):
        pytest.skip("sample drawing not available (Git LFS not pulled)")
    a = analyse(REAL)
    diameters = {round(2 * p.radius, 1) for p in a.primitives if p.kind == "circle" and p.radius}
    assert {5.0, 7.0, 40.0, 60.0} <= diameters  # sheet mm at scale 2:1: Ø2.5, Ø3.5, Ø20, Ø30
    assert len([label for label in a.summary.labels if label.region == "drawing"]) >= 20
    assert sum(v.kind == "image" for v in a.summary.views) == 1
    assert a.summary.title_block == pytest.approx((230, 251, 410, 287), abs=1)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "input").mkdir()
    make_drawing(tmp_path / "input" / "sheet.pdf")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = create_app("ses-test", tmp_path / "builds", "subprocess")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_agent_reads_labels_through_mcp(server: str) -> None:
    async def scenario() -> tuple[dict, list]:  # type: ignore[type-arg]
        client = MultiServerMCPClient(mcp_connections(server, "unused"))  # type: ignore[arg-type]
        tools = {t.name: t for t in await client.get_tools()}
        loaded = json.loads((await tools["load_drawing"].ainvoke({"path": "sheet.pdf"}))[0]["text"])
        blocks = await tools["list_labels"].ainvoke(
            {"drawing_id": loaded["drawing_id"], "region": "drawing"}
        )
        ids = [json.loads(block["text"])["id"] for block in blocks]
        images = await tools["render_labels"].ainvoke(
            {"drawing_id": loaded["drawing_id"], "label_ids": ids}
        )
        return loaded, images

    loaded, images = asyncio.run(scenario())
    assert loaded["sheet_format"] == "A3" and loaded["circle_count"] == 1
    assert sum(1 for part in images if part.get("type") == "image") == 4
