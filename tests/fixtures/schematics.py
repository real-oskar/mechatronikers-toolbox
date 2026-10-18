"""Synthetic vector schematic (reportlab) with known connectivity, for circuit-mcp tests.

Black and white, real text layer, drawn the way EDA tools export: one path per wire and pin
line, closed outlines for bodies, filled dots at junctions.
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

WIDTH, HEIGHT = landscape(A4)

# Expected result: nets as sets of terminal descriptions.
EXPECTED_NETS = {
    "V1+": {"V1:+", "R1"},
    "OUT": {"R1", "R2"},
    "GND": {"R2", "V1:-", "R3"},
    "VREF": {"R3"},
}


def _y(top_mm: float) -> float:
    return HEIGHT - top_mm * mm


def _line(c: canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    c.line(x0 * mm, _y(y0), x1 * mm, _y(y1))


def _text(c: canvas.Canvas, x: float, baseline: float, text: str, size: float = 5) -> None:
    c.setFont("Helvetica", size)
    c.drawString(x * mm, _y(baseline), text)


def make_schematic(path: Path, *, ground_name: bool = True) -> Path:
    """V1 (5 V) feeds the divider R1 (10k) / R2 (4k7); the divider tap is labelled OUT. R3 hangs
    between a VREF label and GND, and its wire crosses OUT without a junction dot."""
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    c.setLineWidth(0.5)
    c.rect(10 * mm, _y(200), 277 * mm, 190 * mm)  # frame

    # V1: circle with pin lines, "+" mark at the top
    c.setLineWidth(0.7)
    c.circle(60 * mm, _y(100), 5 * mm)
    c.setLineWidth(0.5)
    _line(c, 60, 95, 60, 90)
    _line(c, 60, 105, 60, 110)
    _text(c, 59, 98, "+")
    _text(c, 67, 99, "V1")
    _text(c, 67, 103, "5")

    # R1 horizontal
    c.setLineWidth(0.7)
    c.rect(105 * mm, _y(82), 10 * mm, 4 * mm)
    c.setLineWidth(0.5)
    _line(c, 100, 80, 105, 80)
    _line(c, 115, 80, 120, 80)
    _text(c, 107, 76, "R1")
    _text(c, 107, 85.5, "10k")

    # R2 vertical
    c.setLineWidth(0.7)
    c.rect(128 * mm, _y(105), 4 * mm, 10 * mm)
    c.setLineWidth(0.5)
    _line(c, 130, 90, 130, 95)
    _line(c, 130, 105, 130, 110)
    _text(c, 134, 99, "R2")
    _text(c, 134, 103, "4k7")

    # R3 vertical, top wire crosses OUT without a dot
    c.setLineWidth(0.7)
    c.rect(168 * mm, _y(100), 4 * mm, 10 * mm)
    c.setLineWidth(0.5)
    _line(c, 170, 85, 170, 90)
    _line(c, 170, 100, 170, 105)
    _text(c, 174, 94, "R3")
    _text(c, 174, 98, "1k")

    # wires
    _line(c, 60, 90, 60, 80)
    _line(c, 60, 80, 100, 80)
    _line(c, 120, 80, 140, 80)
    _line(c, 130, 80, 130, 90)
    _line(c, 140, 80, 185, 80)  # OUT
    _line(c, 170, 85, 170, 70)  # crosses OUT at (170, 80)
    _line(c, 170, 70, 190, 70)  # VREF
    _line(c, 60, 110, 60, 120)
    _line(c, 130, 110, 130, 120)
    _line(c, 170, 105, 170, 120)
    _line(c, 60, 120, 170, 120)  # ground bus
    _line(c, 95, 120, 95, 123)
    c.circle(130 * mm, _y(80), 0.5 * mm, stroke=0, fill=1)
    c.circle(130 * mm, _y(120), 0.5 * mm, stroke=0, fill=1)
    c.circle(95 * mm, _y(120), 0.5 * mm, stroke=0, fill=1)
    _text(c, 150, 79.2, "OUT")
    _text(c, 175, 69.2, "VREF")

    # ground symbol below the bus: stem end, base line and tip
    p = c.beginPath()
    p.moveTo(95 * mm, _y(123))
    p.lineTo(97 * mm, _y(123))
    p.lineTo(95 * mm, _y(125))
    p.lineTo(93 * mm, _y(123))
    p.lineTo(95 * mm, _y(123))
    c.drawPath(p, stroke=1, fill=0)
    if ground_name:
        _text(c, 93, 128, "GND")

    _text(c, 40, 180, ".op")
    c.showPage()
    c.save()
    return path
