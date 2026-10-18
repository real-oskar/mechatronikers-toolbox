"""Synthetic raster sheets for the raster path tests: text drawn with Pillow's bundled font."""

from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

Text = tuple[str, tuple[int, int], int]  # text, top-left position, counter-clockwise degrees

DPI = 300.0


def draw_sheet(
    texts: list[Text],
    *,
    size: tuple[int, int] = (1400, 900),
    font_px: int = 36,
    lines: bool = True,
) -> NDArray[np.uint8]:
    """White sheet with black text; with `lines` a long solid line and a dashed line, both well
    away from the text."""
    image = Image.new("L", size, 255)
    font = ImageFont.load_default(size=font_px)
    for text, (x, y), angle in texts:
        if angle == 0:
            ImageDraw.Draw(image).text((x, y), text, fill=0, font=font)
            continue
        left, top, right, bottom = font.getbbox(text)
        tile = Image.new("L", (right + 8, bottom + 8), 0)
        ImageDraw.Draw(tile).text((4, 4), text, fill=255, font=font)
        tile = tile.rotate(angle, expand=True)
        image.paste(Image.new("L", tile.size, 0), (x, y), tile)
    if lines:
        draw = ImageDraw.Draw(image)
        draw.line([(40, size[1] - 120), (size[0] - 40, size[1] - 120)], fill=0, width=4)
        for x0 in range(40, size[0] - 80, 70):  # dashes 45 px, gaps 25 px
            draw.line([(x0, size[1] - 60), (x0 + 45, size[1] - 60)], fill=0, width=4)
    return np.asarray(image, dtype=np.uint8)


def save_sheet(grey: NDArray[np.uint8], path: Path, dpi: float = DPI) -> Path:
    Image.fromarray(grey, mode="L").save(path, format="PNG", dpi=(dpi, dpi))
    return path
