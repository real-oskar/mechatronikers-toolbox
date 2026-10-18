"""Human verdicts as evaluation data and the bench's agreement with them (#93)."""

import json
from pathlib import Path

import build123d as bd
import pytest

from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.testbench.__main__ import main
from mechatronikers.testbench.cases import challenge_set
from mechatronikers.testbench.verdicts import DEFAULT_VERDICTS, agreement, load_verdicts
from tests.fixtures.lfs import require_lfs


def test_verdict_file_credits_the_reviewer_and_lists_every_part() -> None:
    verdicts = load_verdicts(DEFAULT_VERDICTS)
    assert verdicts.run_id == "run-20261017-093112"
    assert "@Backverbot" in verdicts.reviewer
    by_part = {v.part: v for v in verdicts.verdicts}
    assert sorted(by_part) == [c.case_id for c in challenge_set("data")]
    assert {p for p, v in by_part.items() if v.verdict == "good"} == {
        "Flansch",
        "Fuss",
        "Traegerplatte",
        "Welle",
    }
    assert by_part["Maschinenteil"].model is None  # the run produced no model for it
    assert "Kritischer Fehler" in " ".join(by_part["Rotation"].reasons)  # verbatim, German
    for v in verdicts.verdicts:
        assert v.reasons
        if v.model is not None:
            assert (DEFAULT_VERDICTS.parent / v.model).is_file()


def test_solved_agrees_with_every_human_verdict() -> None:
    """Regression test for D91: on these labels the rule agrees 9/9."""
    for v in load_verdicts(DEFAULT_VERDICTS).verdicts:
        require_lfs(Path("data") / v.part / f"{v.part}.stp")
        if v.model is not None:
            require_lfs(DEFAULT_VERDICTS.parent / v.model)
    _, rows = agreement(DEFAULT_VERDICTS, samples=1000)
    assert [(r.part, r.agrees) for r in rows if not r.agrees] == []
    hausdorff = {r.part: r.comparison.hausdorff for r in rows if r.comparison is not None}
    assert hausdorff["Rotation"] > 2.0  # IoU 0.987 passes; the missing R6 fillet does not
    assert max(hausdorff[p] for p in ("Flansch", "Fuss", "Traegerplatte", "Welle")) < 1.0


def test_cli_agreement_reports_and_sets_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data" / "Plate"
    data.mkdir(parents=True)
    plate = bd.Box(30, 20, 8) - bd.Cylinder(3, 8)
    export_step(plate, data / "Plate.stp")
    (data / "Plate_dwg.pdf").write_bytes(b"%PDF-1.4")
    export_step(plate.moved(bd.Location((5, 5, 5))), tmp_path / "Plate_KI.step")
    entry = {"part": "Plate", "verdict": "good", "reasons": ["passt"], "model": "Plate_KI.step"}
    file = tmp_path / "verdicts.json"
    meta = {"run_id": "run-x", "reviewer": "r", "reviewed_at": "2026-10-17", "source": "s"}
    file.write_text(json.dumps({**meta, "labels": "l", "verdicts": [entry]}))
    argv = ["--data-dir", str(tmp_path / "data"), "agreement", "--verdicts", str(file)]
    assert main([*argv, "--samples", "500"]) == 0
    assert "Agreement: 1 / 1" in capsys.readouterr().out
    file.write_text(json.dumps({**meta, "labels": "l", "verdicts": [{**entry, "verdict": "poor"}]}))
    assert main([*argv, "--samples", "500"]) == 1  # the bench calls it solved, the human not
