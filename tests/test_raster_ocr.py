"""Raster path: text detector, crops, Tesseract TSV handling and the E1 bench adapters."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mechatronikers.drawing_mcp import ocr
from mechatronikers.drawing_mcp.ocr import (
    OcrWord,
    TesseractUnavailableError,
    find_tesseract,
    parse_tsv,
    read_candidates,
    read_sheet,
    run_tesseract,
    words_to_lines,
)
from mechatronikers.drawing_mcp.raster import (
    TextCandidate,
    crop_image,
    ink_mask,
    load_grey,
    text_candidates,
    upright_rotations,
)
from mechatronikers.testbench.extract_bench import BenchCase, run_bench
from mechatronikers.testbench.extraction import (
    BackendUnavailableError,
    CandidateExtractor,
    ExtractionInput,
    TesseractCropExtractor,
    TesseractSheetExtractor,
    load_extractor,
    needs_model,
    parse_spec,
)
from mechatronikers.testbench.variants import VariantSpec
from tests.fixtures.drawings import make_drawing
from tests.fixtures.raster import DPI, draw_sheet, save_sheet

TEXTS = [
    ("R12", (100, 100), 0),
    ("100nF", (400, 100), 0),
    ("GND", (800, 150), 90),
    ("Note one", (100, 400), 0),
    ("Note two", (100, 440), 0),
]
needs_tesseract = pytest.mark.skipif(find_tesseract() is None, reason="tesseract not installed")


def _inside(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def test_line_candidates_split_runs_and_find_turned_text() -> None:
    grey = draw_sheet(TEXTS)
    found = text_candidates(grey, DPI, "line")
    assert len(found) == 5  # the solid and the dashed line are not lettering
    by_pos = {name: next(c for c in found if _inside(c.bbox, x + 20, y + 20))
              for name, (x, y), _ in TEXTS}  # fmt: skip
    assert len({c.id for c in by_pos.values()}) == 5  # every text its own candidate
    assert by_pos["GND"].angle == 90.0 and by_pos["R12"].angle == 0.0
    gnd = by_pos["GND"].bbox
    assert gnd[3] - gnd[1] > 2 * (gnd[2] - gnd[0])  # turned: taller than wide
    assert by_pos["100nF"].glyphs == 5 and not any(c.multiline for c in found)


def test_cluster_candidates_merge_a_label_of_two_lines_and_slanted_text() -> None:
    grey = draw_sheet([*TEXTS, ("R9,5", (1000, 300), 35)])
    found = text_candidates(grey, DPI, "cluster")
    note = next(c for c in found if _inside(c.bbox, 120, 420))
    assert _inside(note.bbox, 120, 460) and note.multiline  # both note lines, one label
    slanted = next(c for c in found if _inside(c.bbox, 1040, 320))
    assert -45 < slanted.angle < -20  # rising baseline (y points down)
    assert upright_rotations(slanted) == (slanted.angle, slanted.angle + 180)


def test_detector_copes_with_low_contrast_and_noise() -> None:
    grey = draw_sheet(TEXTS).astype(np.float32) * 0.6 + 50
    grey += np.random.default_rng(1).normal(0, 8, grey.shape)
    noisy = np.clip(grey, 0, 255).astype(np.uint8)
    assert ink_mask(noisy).mean() < 0.05
    assert len(text_candidates(noisy, DPI, "line")) == 5
    assert text_candidates(np.full((50, 50), 255, np.uint8), DPI) == []


def test_upright_rotations_prefer_text_read_from_the_bottom() -> None:
    def cand(angle: float) -> TextCandidate:
        return TextCandidate(id="c", bbox=(0, 0, 10, 10), angle=angle, glyph_px=10, glyphs=2)

    assert upright_rotations(cand(0.0)) == (0.0,)
    assert upright_rotations(cand(90.0)) == (-90.0, 90.0)  # bottom to top first
    assert upright_rotations(cand(30.0)) == (30.0, 210.0)


def test_crop_is_scaled_padded_turned_and_isolated() -> None:
    grey = np.full((200, 300), 255, np.uint8)
    grey[50:70, 50:60] = 0  # the glyph
    grey[50:70, 66:70] = 0  # ink in the margin that belongs to something else
    cand = TextCandidate(
        id="c0", bbox=(50, 50, 60, 70), angle=0.0, glyph_px=20, glyphs=1, parts=((50, 50, 60, 70),)
    )
    crop = np.asarray(crop_image(grey, cand, glyph_target_px=40, pad_glyphs=0.4))
    assert crop.shape == (2 * (20 + 16), 2 * (10 + 16))  # pad 8 px, then scaled ×2
    assert crop.min() < 50 and crop[:, -12:].min() > 200  # foreign ink blanked
    loose = np.asarray(crop_image(grey, cand, glyph_target_px=20, isolate=False))
    assert loose[:, -6:].min() < 50  # kept without isolation
    turned = crop_image(grey, cand, glyph_target_px=20, rotate_deg=90)
    assert turned.size == (36, 26)  # canvas 26 × 36 px, turned


def test_load_grey_reads_the_resolution(tmp_path: Path) -> None:
    path = save_sheet(draw_sheet(TEXTS[:1], lines=False), tmp_path / "s.png", dpi=150)
    grey, dpi = load_grey(path)
    assert grey.dtype == np.uint8 and dpi == pytest.approx(150, abs=0.1)
    Image.new("L", (10, 10), 255).save(tmp_path / "bare.png")
    assert load_grey(tmp_path / "bare.png")[1] is None


HEADER = "level page_num block_num par_num line_num word_num left top width height conf text"
TSV = (
    HEADER.replace(" ", "\t")
    + """
1\t1\t0\t0\t0\t0\t0\t0\t500\t200\t-1\t
5\t1\t1\t1\t1\t1\t10\t20\t30\t15\t95.5\tR12
5\t1\t1\t1\t1\t2\t50\t20\t40\t15\t80\t100nF
5\t1\t1\t1\t1\t3\t300\t20\t30\t15\t90\tfar
5\t1\t2\t1\t1\t1\t10\t80\t30\t15\t-1\t
5\t1\t2\t1\t1\t2\t10\t80\t30\t15\t70\t\x20
"""
)


def test_parse_tsv_and_join_words_into_lines() -> None:
    words = parse_tsv(TSV)
    assert [(w.text, w.bbox, w.conf) for w in words] == [
        ("R12", (10, 20, 40, 35), 0.955),
        ("100nF", (50, 20, 90, 35), 0.8),
        ("far", (300, 20, 330, 35), 0.9),
    ]
    lines = words_to_lines(words)
    assert [(line.text, line.bbox, line.words) for line in lines] == [
        ("R12 100nF", (10, 20, 90, 35), 2),
        ("far", (300, 20, 330, 35), 1),
    ]
    assert lines[0].conf == pytest.approx(0.8775)


def test_boxes_from_turned_sheets_map_back() -> None:
    # Sheet 100 × 50; point (10, 5). Turned clockwise it sits at (45, 10) of a 50 × 100 image.
    assert ocr._rotate_back((44, 9, 46, 11), 1, 100, 50) == (9, 4, 11, 6)
    # Counter-clockwise it sits at (5, 90).
    assert ocr._rotate_back((4, 89, 6, 91), 3, 100, 50) == (9, 4, 11, 6)
    assert ocr._rotate_back((89, 44, 91, 46), 2, 100, 50) == (9, 4, 11, 6)
    assert ocr._rotate_back((1, 2, 3, 4), 0, 100, 50) == (1, 2, 3, 4)


def test_missing_tesseract_is_reported_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MT_TESSERACT", str(tmp_path / "no-tesseract"))
    assert find_tesseract() is None
    with pytest.raises(TesseractUnavailableError):
        run_tesseract(Image.new("L", (10, 10), 255), psm=7)
    path = save_sheet(draw_sheet(TEXTS), tmp_path / "sheet.png")
    inp = ExtractionInput(
        case_id="s", variant="raster-test", kind="raster", domain="circuit", path=path
    )
    for extractor in (TesseractSheetExtractor(), TesseractCropExtractor()):
        with pytest.raises(BackendUnavailableError, match="not installed"):
            extractor.extract(inp)

    # In a bench run such pairs are n/a, not failures.
    case = BenchCase(case_id="Plate", domain="part", pdf=make_drawing(tmp_path / "plate.pdf"))
    spec = VariantSpec(name="raster-40", kind="raster", dpi=40)
    out = tmp_path / "run"
    _, rows = run_bench([case], [spec], [TesseractSheetExtractor()], out_dir=out,
                        variants_dir=tmp_path / "v")  # fmt: skip
    assert rows[0].status == "unavailable" and rows[0].score is None
    assert "| e1 | raster-40 | part | 0 / 1 | n/a (unavailable: tesseract is not installed" in (
        (out / "summary.md").read_text()
    )


def test_detector_extractor_locates_without_reading(tmp_path: Path) -> None:
    path = save_sheet(draw_sheet(TEXTS), tmp_path / "sheet.png")
    inp = ExtractionInput(case_id="s", variant="r", kind="raster", domain="circuit", path=path)
    result = CandidateExtractor().extract(inp)
    assert len(result.labels) == 5 and all(label.text is None for label in result.labels)
    with pytest.raises(ValueError, match="raster"):
        CandidateExtractor().extract(inp.model_copy(update={"kind": "vector"}))
    Image.new("L", (10, 10), 255).save(tmp_path / "bare.png")
    with pytest.raises(ValueError, match="resolution unknown"):
        CandidateExtractor().extract(inp.model_copy(update={"path": tmp_path / "bare.png"}))


def test_extractor_specs_with_options() -> None:
    assert parse_spec("e4@threshold=0.6@lang=deu") == ("e4", {"threshold": 0.6, "lang": "deu"})
    assert needs_model("e3") and needs_model("e4@threshold=0.5") and not needs_model("e1")
    e1 = load_extractor("e1@min_conf=0.5")
    assert e1.name == "e1@min_conf=0.5" and isinstance(e1, TesseractSheetExtractor)
    assert e1.min_conf == 0.5 and load_extractor("e1-crops").name == "e1-crops"
    for wrong, message in (
        ("e1@nope=1", "bad options"),
        ("e1@x", "bad option"),
        ("e3", "calls a model"),
        ("e4@threshold=0.5", "calls a model"),
    ):
        with pytest.raises(ValueError, match=message):
            load_extractor(wrong)


@needs_tesseract
def test_tesseract_reads_the_sheet_and_the_crops() -> None:
    grey = draw_sheet(TEXTS)
    texts = {line.text for line in read_sheet(grey, DPI)}
    assert {"R12", "GND", "Note one", "Note two"} <= texts  # GND from the turned pass
    found = text_candidates(grey, DPI, "line")
    reads = read_candidates(grey, found)
    by_text = {r.text: r for r in reads}
    assert {"R12", "GND", "Note one", "Note two"} <= set(by_text)
    assert by_text["GND"].rotation == -90.0 and by_text["R12"].conf > 0.8


@needs_tesseract
def test_e1_adapters_on_a_raster_file(tmp_path: Path) -> None:
    path = save_sheet(draw_sheet(TEXTS), tmp_path / "sheet.png")
    inp = ExtractionInput(case_id="s", variant="r", kind="raster", domain="circuit", path=path)
    sheet = TesseractSheetExtractor().extract(inp)
    crops = TesseractCropExtractor().extract(inp)
    for result in (sheet, crops):
        assert "R12" in {label.text for label in result.labels}
        assert result.credits == 0 and str(result.meta["engine"]).startswith("tesseract")
    assert crops.meta["candidates"] == 5


def test_words_from_several_orientations_keep_the_most_confident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turned pass misreads upright text as short vertical words; those are dropped where
    they overlap a more confident upright reading."""
    grey = np.full((100, 200), 255, np.uint8)

    def fake(image: Image.Image, **kwargs: object) -> list[OcrWord]:
        if image.size == (200, 100):  # upright pass
            return [OcrWord("Note", (10, 10, 90, 30), 0.95, 1, 1, 1)]
        # turned clockwise: (x, y) of the sheet at (100 - y, x); a misreading over "Note"
        # and a real vertical word elsewhere
        return [
            OcrWord("22", (75, 10, 85, 40), 0.7, 1, 1, 1),
            OcrWord("GND", (20, 150, 40, 190), 0.9, 2, 1, 1),
            OcrWord("-", (20, 100, 40, 101), 0.9, 3, 1, 1),  # flat: a line, not a word
        ]

    monkeypatch.setattr(ocr, "run_tesseract", fake)
    monkeypatch.setattr(ocr, "_binary", lambda binary: "tesseract")
    lines = read_sheet(grey, 300.0)
    assert [(line.text, line.bbox) for line in lines] == [
        ("Note", (10, 10, 90, 30)),
        ("GND", (150, 60, 190, 80)),
    ]
