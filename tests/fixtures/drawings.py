"""Synthetic vector drawings (reportlab) with known content, for drawing-mcp tests."""

import io
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

WIDTH, HEIGHT = landscape(A3)


def _y(top_mm: float) -> float:
    """reportlab uses a bottom-left origin; tests think in sheet mm from the top-left."""
    return HEIGHT - top_mm * mm


def make_drawing(path: Path, *, rib: bool = False, corner_radius: float = 0.0) -> Path:
    """A3 sheet: frame (20,10)-(410,287), title block from (230,251), one view with a 100x60
    rectangle and a Ø20 circle, a 3-glyph and a 2-glyph label, an arrowhead, an embedded image.

    Optional features drawn without any dimension (#90): `rib`, a 90x3 strip (65,88)-(155,91)
    inside the view; `corner_radius`, rounded corners of the rectangle (sheet mm)."""
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    c.setLineWidth(0.37)
    c.rect(20 * mm, _y(287), 390 * mm, 277 * mm)  # inner frame
    c.line(230 * mm, _y(251), 410 * mm, _y(251))  # title block top
    c.line(230 * mm, _y(251), 230 * mm, _y(287))  # title block left
    c.setLineWidth(0.99)
    if corner_radius:
        c.roundRect(60 * mm, _y(100), 100 * mm, 60 * mm, corner_radius * mm)
    else:
        c.rect(60 * mm, _y(100), 100 * mm, 60 * mm)  # view outline (60,40)-(160,100)
    if rib:
        c.rect(65 * mm, _y(91), 90 * mm, 3 * mm)
    c.circle(110 * mm, _y(70), 10 * mm)  # Ø20 at (110, 70)
    c.setFillColorRGB(0, 0, 0)
    for i in range(3):  # label of three glyph-like filled curved shapes at (60..68, 110)
        c.ellipse((60 + 3 * i) * mm, _y(112), (62 + 3 * i) * mm, _y(108), stroke=0, fill=1)
    for i in range(2):  # second label at (140, 30)
        c.ellipse((140 + 3 * i) * mm, _y(32), (142 + 3 * i) * mm, _y(28), stroke=0, fill=1)
    p = c.beginPath()  # arrowhead: filled triangle, must not become a label
    p.moveTo(170 * mm, _y(70))
    p.lineTo(173 * mm, _y(69))
    p.lineTo(173 * mm, _y(71))
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.setLineWidth(0.5)
    _linear_dimension(c, 60, 160, 125)  # 100 mm dimension line at y=125, label above it
    for i in range(3):  # its label: three glyphs at (104..112, 119..122)
        c.ellipse((104 + 3 * i) * mm, _y(122), (106 + 3 * i) * mm, _y(119), stroke=0, fill=1)
    _arrow(c, (117.071, 62.929), (-0.7071, 0.7071))  # Ø20 leader: radial arrow tip on the circle
    c.line(117.071 * mm, _y(62.929), 125 * mm, _y(55))
    for i in range(3):  # its label at (126..134, 51..54)
        c.ellipse((126 + 3 * i) * mm, _y(54), (128 + 3 * i) * mm, _y(51), stroke=0, fill=1)
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "gray").save(buf, format="PNG")
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 300 * mm, _y(120), 60 * mm, 60 * mm)
    c.showPage()
    c.save()
    return path


def _arrow(c: canvas.Canvas, tip: tuple[float, float], direction: tuple[float, float]) -> None:
    """NX-style arrowhead: two 3.5 mm strokes meeting at the tip (sheet mm, y down)."""
    dx, dy = direction
    for side in (1, -1):
        bx = tip[0] - 3.5 * dx - side * 0.46 * dy
        by = tip[1] - 3.5 * dy + side * 0.46 * dx
        c.line(tip[0] * mm, _y(tip[1]), bx * mm, _y(by))


def _linear_dimension(c: canvas.Canvas, x0: float, x1: float, y: float) -> None:
    c.line(x0 * mm, _y(y), x1 * mm, _y(y))
    _arrow(c, (x0, y), (-1, 0))
    _arrow(c, (x1, y), (1, 0))


def _glyphs(c: canvas.Canvas, x: float, y: float, n: int = 3) -> None:
    """A label of `n` glyph-like filled shapes, top-left at (x, y), 3 mm high."""
    for i in range(n):
        c.ellipse((x + 3 * i) * mm, _y(y + 3), (x + 2 + 3 * i) * mm, _y(y), stroke=0, fill=1)


def _radius_leader(
    c: canvas.Canvas,
    center: tuple[float, float],
    radius: float,
    at: tuple[float, float],
    toward: tuple[float, float],
    length: float,
) -> None:
    """Radius dimension: arrow tip on the arc at unit direction `at` from the centre, leader of
    `length` mm running from the tip along unit direction `toward` (to the label)."""
    tip = (center[0] + radius * at[0], center[1] + radius * at[1])
    _arrow(c, tip, (-toward[0], -toward[1]))
    end = (tip[0] + length * toward[0], tip[1] + length * toward[1])
    c.line(tip[0] * mm, _y(tip[1]), end[0] * mm, _y(end[1]))


def make_two_radii_drawing(path: Path) -> Path:
    """Scale 1:1. A plate outline with two radii close together: an R10 outer corner (centre
    (140, 70)) and an R5 inner fillet (centre (155, 80)), each with a radial leader to its own
    label: R10 label at (158..166, 49..52), R5 label at (169..177, 62.5..65.5). The R5 label sits
    nearer the R10 arrow than its own, so proximity alone binds it to the wrong arc. Overall
    length 110 dimensioned below the part, label at (110..118, 119..122)."""
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    c.setLineWidth(0.37)
    c.rect(20 * mm, _y(287), 390 * mm, 277 * mm)
    c.line(230 * mm, _y(251), 410 * mm, _y(251))
    c.line(230 * mm, _y(251), 230 * mm, _y(287))
    c.setLineWidth(0.99)
    for a, b in (
        ((60, 60), (140, 60)),
        ((150, 70), (150, 80)),
        ((155, 85), (170, 85)),
        ((170, 85), (170, 110)),
        ((170, 110), (60, 110)),
        ((60, 110), (60, 60)),
    ):
        c.line(a[0] * mm, _y(a[1]), b[0] * mm, _y(b[1]))
    # reportlab angles count counter-clockwise with y up; sheet y points down.
    c.arc(130 * mm, _y(80), 150 * mm, _y(60), startAng=0, extent=90)  # R10: (140,60)-(150,70)
    c.arc(150 * mm, _y(85), 160 * mm, _y(75), startAng=180, extent=90)  # R5: (150,80)-(155,85)
    c.setLineWidth(0.5)
    s = 0.7071
    _radius_leader(c, (140, 70), 10, (s, -s), (s, -s), 14)  # from outside the corner
    _glyphs(c, 158, 49)
    _radius_leader(c, (155, 80), 5, (-s, s), (s, -s), 26)  # through the fillet's centre
    _glyphs(c, 169, 62.5)
    _linear_dimension(c, 60, 170, 125)
    _glyphs(c, 110, 119)
    c.showPage()
    c.save()
    return path


def make_foreign_arrow_drawing(path: Path, layout: str) -> tuple[float, float]:
    """Scale 1:1. A radius leader next to another dimension's radial arrow whose shaft line,
    extended, runs through the radius label (from the review of #106). Returns a point inside
    the radius label.

    - "far": R5 fillet (centre (100, 100)) with a 10 mm leader, a shoulder and its label above
      the shoulder; an R8 arc 45 mm to the right whose own leader points left along the label row.
    - "collinear": R20 outer arc (centre (100, 100)) with a 12 mm leader; a concentric Ø20 hole
      whose dimension line lies on the same line as the R20 leader.
    """
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    c.setLineWidth(0.37)
    c.rect(20 * mm, _y(287), 390 * mm, 277 * mm)
    c.line(230 * mm, _y(251), 410 * mm, _y(251))
    c.line(230 * mm, _y(251), 230 * mm, _y(287))
    s = 0.7071
    if layout == "far":
        c.setLineWidth(0.99)
        c.arc(95 * mm, _y(105), 105 * mm, _y(95), startAng=0, extent=90)
        c.setLineWidth(0.5)
        tip = (100 + 5 * s, 100 - 5 * s)
        _arrow(c, tip, (-s, s))
        end = (tip[0] + 10 * s, tip[1] - 10 * s)
        c.line(tip[0] * mm, _y(tip[1]), end[0] * mm, _y(end[1]))
        c.line(end[0] * mm, _y(end[1]), (end[0] + 6) * mm, _y(end[1]))  # shoulder
        label = (end[0] + 1.0, end[1] - 4.5)
        _glyphs(c, *label)
        ox, oy = label[0] + 45, label[1] + 1.5
        c.setLineWidth(0.99)
        c.arc((ox - 8) * mm, _y(oy + 8), (ox + 8) * mm, _y(oy - 8), startAng=90, extent=180)
        c.setLineWidth(0.5)
        _arrow(c, (ox - 8, oy), (1, 0))  # its shaft runs left, towards the R5 label
        c.line((ox - 8) * mm, _y(oy), (ox - 16) * mm, _y(oy))
        _glyphs(c, ox - 24, oy - 4.5)
    else:
        cx, cy = 100.0, 100.0
        c.setLineWidth(0.99)
        c.arc((cx - 20) * mm, _y(cy + 20), (cx + 20) * mm, _y(cy - 20), startAng=-90, extent=180)
        for a, b in (((cx, cy - 20), (cx - 40, cy - 20)), ((cx, cy + 20), (cx - 40, cy + 20))):
            c.line(a[0] * mm, _y(a[1]), b[0] * mm, _y(b[1]))
        c.line((cx - 40) * mm, _y(cy - 20), (cx - 40) * mm, _y(cy + 20))
        c.circle(cx * mm, _y(cy), 10 * mm)
        c.setLineWidth(0.5)
        tip = (cx + 20 * s, cy - 20 * s)
        _arrow(c, tip, (-s, s))
        end = (tip[0] + 12 * s, tip[1] - 12 * s)
        c.line(tip[0] * mm, _y(tip[1]), end[0] * mm, _y(end[1]))
        label = (end[0] + 0.5, end[1] - 3.5)
        _glyphs(c, *label)
        a, b = (cx - 10 * s, cy + 10 * s), (cx + 10 * s, cy - 10 * s)  # Ø20 dimension line
        _arrow(c, a, (-s, s))
        _arrow(c, b, (s, -s))
        c.line(a[0] * mm, _y(a[1]), b[0] * mm, _y(b[1]))
    c.showPage()
    c.save()
    return (label[0] + 1, label[1] + 1.5)


def make_notched_block(path: Path) -> Path:
    """A3 sheet, scale 1: a made-up 100 x 60 x 20 block with a notch from the top (x 10..30 of
    ±50, 8 deep), in three views. Front (sheet x 50..150, y 150..170) and top (x 50..150,
    y 60..120) show the notch; in the side view (x 200..260, y 150..170) it is hidden."""
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    c.setLineWidth(0.37)
    c.rect(20 * mm, _y(287), 390 * mm, 277 * mm)  # inner frame
    c.line(230 * mm, _y(251), 410 * mm, _y(251))  # title block top
    c.line(230 * mm, _y(251), 230 * mm, _y(287))
    c.setLineWidth(0.7)
    front = [(50, 170), (150, 170), (150, 150), (130, 150), (130, 158), (110, 158), (110, 150)]
    front += [(50, 150), (50, 170)]
    for (x0, y0), (x1, y1) in zip(front, front[1:], strict=False):
        c.line(x0 * mm, _y(y0), x1 * mm, _y(y1))
    c.rect(50 * mm, _y(120), 100 * mm, 60 * mm)  # top view
    for x in (110, 130):  # notch floor edges seen from above
        c.line(x * mm, _y(60), x * mm, _y(120))
    c.rect(200 * mm, _y(170), 60 * mm, 20 * mm)  # side view
    c.showPage()
    c.save()
    return path
