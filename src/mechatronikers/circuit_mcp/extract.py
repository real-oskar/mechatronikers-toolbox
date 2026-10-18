"""Raw extraction of a schematic PDF: stroked geometry (shared with drawing-mcp) plus the text
layer, which EDA exports keep as real text (unlike the NX drawings, docs/04).

Coordinates: sheet millimetres from the top-left corner.
"""

import ctypes
import math
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from mechatronikers.drawing_mcp.vector import PT_TO_MM, RawDrawing, extract

# Normalise signs that matter for parsing: minus and en dash, micro and mu.
_TEXT_FIXES = str.maketrans({chr(0x2212): "-", chr(0x2013): "-", chr(0xB5): "u", chr(0x3BC): "u"})
# Glyphs without a Unicode mapping come back as control characters. In schematics this is almost
# always the minus sign of an EDA font; the analysis flags such texts for review.
UNMAPPED = "-"


@dataclass
class RawText:
    text: str
    bbox: tuple[float, float, float, float]  # sheet mm
    angle: float  # degrees, counter-clockwise
    size: float  # font size, sheet mm
    unmapped: bool = False  # contained a glyph without Unicode mapping (read as UNMAPPED)


@dataclass
class RawSchematic:
    drawing: RawDrawing
    texts: list[RawText]


def _text_of(obj: pdfium.PdfObject, textpage: pdfium.PdfTextPage) -> str:
    n_bytes = raw.FPDFTextObj_GetText(obj.raw, textpage.raw, None, 0)
    if n_bytes <= 2:
        return ""
    buf = ctypes.create_string_buffer(n_bytes)
    raw.FPDFTextObj_GetText(
        obj.raw, textpage.raw, ctypes.cast(buf, ctypes.POINTER(raw.FPDF_WCHAR)), n_bytes
    )
    return buf.raw[: n_bytes - 2].decode("utf-16-le", errors="replace")


def extract_texts(pdf_path: Path, page_index: int = 0) -> list[RawText]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf[page_index]
    _, height_pt = page.get_size()
    textpage = page.get_textpage()
    texts: list[RawText] = []
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_TEXT], max_depth=5):
        text = _text_of(obj, textpage).translate(_TEXT_FIXES).strip()
        unmapped = any(ord(c) < 32 for c in text)
        text = "".join(UNMAPPED if ord(c) < 32 else c for c in text)
        if not text:
            continue
        left, bottom, right, top = obj.get_bounds()
        m = obj.get_matrix()
        size = ctypes.c_float()
        raw.FPDFTextObj_GetFontSize(obj.raw, ctypes.byref(size))
        texts.append(
            RawText(
                text=text,
                bbox=(
                    left * PT_TO_MM,
                    (height_pt - top) * PT_TO_MM,
                    right * PT_TO_MM,
                    (height_pt - bottom) * PT_TO_MM,
                ),
                angle=round(math.degrees(math.atan2(m.b, m.a)), 1) % 360,
                size=size.value * math.hypot(m.a, m.b) * PT_TO_MM,
                unmapped=unmapped,
            )
        )
    return _dedupe(texts)


def _dedupe(texts: list[RawText]) -> list[RawText]:
    """Drop repeated copies of the same text at the same place (KiCad writes texts twice)."""
    seen: set[tuple[str, int, int, int, int]] = set()
    out = []
    for t in texts:
        x0, y0, x1, y1 = (round(v * 10) for v in t.bbox)
        if (t.text, x0, y0, x1, y1) not in seen:
            seen.add((t.text, x0, y0, x1, y1))
            out.append(t)
    return out


def extract_schematic(pdf_path: Path, page_index: int = 0) -> RawSchematic:
    return RawSchematic(extract(pdf_path, page_index), extract_texts(pdf_path, page_index))
