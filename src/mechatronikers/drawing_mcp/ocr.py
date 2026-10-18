"""Tesseract OCR for the raster path, run as the external `tesseract` binary (docs/04, T12).

Offline: no Python binding and no new dependency. The binary is found on PATH or through
`MT_TESSERACT`; without it every call raises `TesseractUnavailableError`, so callers can report the
engine as unavailable instead of failing. Output is parsed from Tesseract's TSV (word boxes and
confidences). Image coordinates are pixels, origin top-left, y down.
"""

import os
import shutil
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from mechatronikers.drawing_mcp.raster import (
    TextCandidate,
    crop_image,
    paper_level,
    png_bytes,
    upright_rotations,
)

DEFAULT_LANG = "eng"
SHEET_PSM = 11  # sparse text: drawings are lettering scattered between lines
CROP_PSM_LINE = 7  # one text line
CROP_PSM_BLOCK = 6  # one block (a drawing label of several lines)
CROP_PSM_CHAR = 10  # one character (a lone glyph: zone letters, pin numbers)
SECOND_GUESS_MARGIN = 0.15  # the other reading direction must be this much more confident
CROP_GLYPH_PX = 40.0  # glyph height in the crops Tesseract reads
CROP_PAD = 0.4  # white margin around a crop, in glyph heights
MIN_WORD_HEIGHT_MM = 0.5  # whole-sheet reading: flatter "words" are lines read as letters
MIN_WORD_CONF = 0.3  # whole-sheet reading: weaker words are mostly line art read as letters
TARGET_DPI = 300.0  # whole-sheet reading upsamples low-resolution scans towards this
TIMEOUT_S = 120.0

PxBox = tuple[int, int, int, int]


class TesseractUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrWord:
    text: str
    bbox: PxBox
    conf: float  # 0..1
    block: int
    par: int
    line: int


@dataclass(frozen=True)
class OcrLine:
    text: str
    bbox: PxBox
    conf: float  # mean of the words' confidences, 0..1
    words: int


@dataclass(frozen=True)
class CropRead:
    text: str  # "" when nothing was read
    conf: float  # 0..1; 0 when nothing was read
    rotation: float  # counter-clockwise turn of the crop that gave this reading


def find_tesseract() -> str | None:
    configured = os.environ.get("MT_TESSERACT")
    if configured:
        return configured if shutil.which(configured) else None
    return shutil.which("tesseract")


def _binary(binary: str | None) -> str:
    found = binary or find_tesseract()
    if not found:
        raise TesseractUnavailableError(
            "tesseract is not installed (install tesseract-ocr or set MT_TESSERACT)"
        )
    return found


def tesseract_version(binary: str | None = None) -> str:
    proc = subprocess.run(
        [_binary(binary), "--version"], capture_output=True, text=True, check=False, timeout=30
    )
    first = (proc.stdout or proc.stderr).splitlines()
    return first[0].strip() if first else "tesseract (unknown version)"


def parse_tsv(tsv: str) -> list[OcrWord]:
    """Word rows (level 5) with text; header and structure rows are skipped."""
    words: list[OcrWord] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        conf = float(cols[10])
        if not text or conf < 0:
            continue
        left, top, width, height = (int(v) for v in cols[6:10])
        words.append(
            OcrWord(
                text=text,
                bbox=(left, top, left + width, top + height),
                conf=min(1.0, conf / 100.0),
                block=int(cols[2]),
                par=int(cols[3]),
                line=int(cols[4]),
            )
        )
    return words


def run_tesseract(
    image: Image.Image,
    *,
    psm: int,
    lang: str = DEFAULT_LANG,
    dpi: float | None = None,
    binary: str | None = None,
    timeout: float = TIMEOUT_S,
) -> list[OcrWord]:
    """Words on one image (PNG through stdin, TSV through stdout)."""
    command = [_binary(binary), "stdin", "stdout", "--psm", str(psm), "-l", lang]
    if dpi:
        command += ["--dpi", str(round(dpi))]
    # One thread per process: callers run several processes side by side.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    proc = subprocess.run(
        [*command, "tsv"],
        input=png_bytes(image),
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"tesseract failed ({proc.returncode}): {' '.join(message[-2:])}")
    return parse_tsv(proc.stdout.decode(errors="replace"))


def words_to_lines(words: Sequence[OcrWord], max_gap: float = 1.2) -> list[OcrLine]:
    """Join the words of each Tesseract line; a gap wider than `max_gap` line heights starts a
    new item (separate labels that happen to sit on one row)."""
    rows: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in words:
        rows.setdefault((word.block, word.par, word.line), []).append(word)
    lines: list[OcrLine] = []
    for row in rows.values():
        row.sort(key=lambda w: w.bbox[0])
        height = float(np.median([w.bbox[3] - w.bbox[1] for w in row]))
        current = [row[0]]
        for word in row[1:]:
            if word.bbox[0] - current[-1].bbox[2] > max_gap * height:
                lines.append(_line(current))
                current = []
            current.append(word)
        lines.append(_line(current))
    return lines


def _line(words: list[OcrWord]) -> OcrLine:
    return OcrLine(
        text=" ".join(w.text for w in words),
        bbox=(
            min(w.bbox[0] for w in words),
            min(w.bbox[1] for w in words),
            max(w.bbox[2] for w in words),
            max(w.bbox[3] for w in words),
        ),
        conf=float(np.mean([w.conf for w in words])),
        words=len(words),
    )


def _rotate_back(box: PxBox, turns: int, width: int, height: int) -> PxBox:
    """A box found on the sheet turned clockwise by 90° × turns, in the unturned sheet's frame
    (width, height of the unturned sheet)."""
    x0, y0, x1, y1 = box
    if turns % 4 == 0:
        return box
    if turns % 4 == 1:  # clockwise: (x, y) on the turned image came from (y, height - x)
        return (y0, height - x1, y1, height - x0)
    if turns % 4 == 3:  # counter-clockwise: from (width - y, x)
        return (width - y1, x0, width - y0, x1)
    return (width - x1, height - y1, width - x0, height - y0)


def _overlap_share(a: PxBox, b: PxBox) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h / max(1, smaller)


def read_sheet(
    grey: NDArray[np.uint8],
    dpi: float,
    *,
    turns: Sequence[int] = (0, 1),
    lang: str = DEFAULT_LANG,
    min_conf: float = MIN_WORD_CONF,
    binary: str | None = None,
) -> list[OcrLine]:
    """Tesseract on the whole sheet (sparse text), once per orientation: `turns` are clockwise
    quarter turns of the sheet (1 reads text running bottom to top). Low-resolution sheets are
    upsampled towards 300 dpi. Lines found in several orientations keep the most confident."""
    exe = _binary(binary)
    scale = min(4.0, max(1.0, TARGET_DPI / dpi))
    image = Image.fromarray(grey, mode="L")
    if scale > 1.0:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS
        )

    min_height = MIN_WORD_HEIGHT_MM * dpi * scale / 25.4

    def one(turn: int) -> list[OcrLine]:
        turned = image.rotate(-90 * turn, expand=True) if turn % 4 else image
        words = [
            w
            for w in run_tesseract(turned, psm=SHEET_PSM, lang=lang, dpi=dpi * scale, binary=exe)
            if w.conf >= min_conf
            and any(ch.isalnum() for ch in w.text)
            and min(w.bbox[2] - w.bbox[0], w.bbox[3] - w.bbox[1]) >= min_height
        ]
        lines = []
        for line in words_to_lines(words):
            x0, y0, x1, y1 = _rotate_back(line.bbox, turn, image.width, image.height)
            box = (
                int(x0 / scale),
                int(y0 / scale),
                int(np.ceil(x1 / scale)),
                int(np.ceil(y1 / scale)),
            )
            lines.append(OcrLine(text=line.text, bbox=box, conf=line.conf, words=line.words))
        return lines

    with ThreadPoolExecutor(max_workers=len(turns) or 1) as pool:
        found = [line for lines in pool.map(one, turns) for line in lines]
    found.sort(key=lambda line: -line.conf)
    kept: list[OcrLine] = []
    for line in found:  # a reading in another direction over a kept one is a misreading
        if all(_overlap_share(line.bbox, other.bbox) < 0.25 for other in kept):
            kept.append(line)
    return sorted(kept, key=lambda line: (line.bbox[1], line.bbox[0]))


def _crop_psm(candidate: TextCandidate) -> int:
    if candidate.multiline:
        return CROP_PSM_BLOCK
    x0, y0, x1, y1 = candidate.bbox
    long_side, short_side = max(x1 - x0, y1 - y0), max(1, min(x1 - x0, y1 - y0))
    if candidate.glyphs == 1 and long_side <= 1.3 * short_side:
        return CROP_PSM_CHAR
    return CROP_PSM_LINE


def read_candidates(
    grey: NDArray[np.uint8],
    candidates: Sequence[TextCandidate],
    *,
    lang: str = DEFAULT_LANG,
    binary: str | None = None,
    workers: int | None = None,
) -> list[CropRead]:
    """Tesseract on each candidate's crop (other ink blanked, glyphs scaled to ~40 px). Turned
    candidates are read in both directions; the second guess wins only if clearly more
    confident (Tesseract is often sure of upside-down nonsense on short texts)."""
    exe = _binary(binary)
    paper = paper_level(grey)
    jobs: list[tuple[int, int, float, Image.Image, int]] = []
    for index, candidate in enumerate(candidates):
        for rank, rotation in enumerate(upright_rotations(candidate)):
            image = crop_image(
                grey,
                candidate,
                glyph_target_px=CROP_GLYPH_PX,
                pad_glyphs=CROP_PAD,
                rotate_deg=rotation,
                paper=paper,
            )
            jobs.append((index, rank, rotation, image, _crop_psm(candidate)))

    def one(job: tuple[int, int, float, Image.Image, int]) -> tuple[int, int, CropRead]:
        index, rank, rotation, image, psm = job
        words = run_tesseract(image, psm=psm, lang=lang, binary=exe)
        if not words:
            return index, rank, CropRead(text="", conf=0.0, rotation=rotation)
        text = " ".join(line.text for line in words_to_lines(words, max_gap=10.0))
        conf = float(np.mean([w.conf for w in words]))
        return index, rank, CropRead(text=text, conf=conf, rotation=rotation)

    guesses: list[dict[int, CropRead]] = [{} for _ in candidates]
    with ThreadPoolExecutor(max_workers=workers or min(8, os.cpu_count() or 1)) as pool:
        for index, rank, read in pool.map(one, jobs):
            guesses[index][rank] = read
    best: list[CropRead] = []
    for options in guesses:
        first = options.get(0, CropRead(text="", conf=0.0, rotation=0.0))
        second = options.get(1)
        if (
            second is not None
            and second.text
            and (not first.text or second.conf > first.conf + SECOND_GUESS_MARGIN)
        ):
            first = second
        best.append(first)
    return best
