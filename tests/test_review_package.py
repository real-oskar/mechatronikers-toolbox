import asyncio
import json
import zipfile
from pathlib import Path

import build123d as bd

from mechatronikers.agent.pipeline import blocking_review_items, build_id_of, missing_dimensions
from mechatronikers.cad_mcp.step_io import export_step
from mechatronikers.drawing_mcp.analysis import analyse
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.pipelines import PipelineOutcome
from mechatronikers.testbench.review_package import build_review_package, describe_feature
from mechatronikers.testbench.run import run
from tests.fixtures.drawings import make_drawing

FEATURES = [
    {
        "type": "extrude_base",
        "id": "base",
        "depth": 10,
        "profile": {
            "segments": [
                {"kind": "line", "start": [-50, -30], "end": [50, -30]},
                {"kind": "line", "start": [50, -30], "end": [50, 30]},
                {"kind": "line", "start": [50, 30], "end": [-50, 30]},
                {"kind": "line", "start": [-50, 30], "end": [-50, -30]},
            ]
        },
    },
    {
        "type": "hole",
        "id": "bore",
        "diameter": 20,
        "hole_type": "through",
        "placement": {"origin": [0, 0, 10], "normal": [0, 0, -1]},
    },
]


def test_pipeline_helpers() -> None:
    refs = [{"uri": "artifact://ses-1/build-abc123/Part.ap242.step"}]
    assert build_id_of(refs) == "build-abc123"
    assert build_id_of([{"uri": "artifact://ses-1/drawing/x.png"}]) is None
    state = {
        "consistency": {
            "dimensions": [
                {"id": "d-gap", "stated": None, "reason": "measured"},
                {"id": "d-l1", "stated": 10},
            ]
        },
        "spec": {"assumptions": [{"id": "a-1", "reason": "note R=1"}]},
    }
    assert [(m.id, m.reason) for m in missing_dimensions(state)] == [
        ("d-gap", "measured"),
        ("a-1", "note R=1"),
    ]


def test_describe_feature_in_german() -> None:
    assert describe_feature(FEATURES[0]) == "Grundkörper extrudiert (Kontur), Dicke 10 mm"
    assert describe_feature(FEATURES[1]).startswith("Bohrung Ø20 (through, durchgehend)")


def test_review_package_from_a_run(tmp_path: Path) -> None:
    (tmp_path / "data" / "Plate").mkdir(parents=True)
    drawing = make_drawing(tmp_path / "data" / "Plate" / "Plate_dwg.pdf")
    part = bd.Box(100, 60, 10) - bd.Cylinder(10, 10)
    reference = export_step(part, tmp_path / "data" / "Plate" / "Plate.stp")
    case = TestCase(case_id="Plate", drawing=drawing, reference_step=reference)
    labels = [lb.model_dump() for lb in analyse(drawing).summary.labels]
    readable = next(lb["id"] for lb in labels if lb["region"] == "drawing")

    def fake_pipeline(
        case: TestCase, *, model: str, variant: str, out_dir: Path
    ) -> PipelineOutcome:
        (out_dir / "artifacts").mkdir()
        step = export_step(
            part.moved(bd.Location((0, 0, 5))), out_dir / "artifacts" / "Plate.ap242.step"
        )
        state = {
            "status": "done",
            "summary": {
                "sheet_size": [420, 297],
                "views": [{"id": "v0", "kind": "orthographic", "bbox": [58, 40, 162, 102]}],
            },
            "labels": labels,
            "readings": {readable: {"id": readable, "text": "100", "readable": True}},
            "consistency": {"dimensions": [], "measurements": [], "review_items": []},
            "spec": {
                "overall_size_dims": [None, None, None],
                "features": FEATURES,
                "assumptions": [
                    {"id": "a-1", "kind": "depth", "value": 10, "reason": "thin plate"}
                ],
                "undimensioned": [
                    {"id": "u-1", "view_id": "v0", "bbox": [65, 88, 155, 91], "reason": "rib"}
                ],
            },
            "coverage": {
                "review_items": [
                    {
                        "id": "cov-1",
                        "kind": "possibly_missing",
                        "message": "View v0: 1 visible circle(s) Ø20 that no feature explains",
                        "reason": "full circle at outline width",
                        "sheet_ref": {"view_id": "v0", "bbox": [97, 57, 123, 83]},
                    }
                ]
            },
        }
        (out_dir / "agent_state.json").write_text(json.dumps(state))
        (out_dir / "agent-events.ndjson").write_text(
            json.dumps(
                {
                    "type": "model.response",
                    "ts": "2026-10-17T08:00:00.000000Z",
                    "payload": {
                        "step": "plan_spec",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cost": 0.01,
                    },
                }
            )
            + "\n"
        )
        return PipelineOutcome(
            step_path=step, missing_dimensions=[], session_id="ses-x", meta={"model": model}
        )

    info, rows = run(
        [case],
        fake_pipeline,
        pipeline_name="fake",
        test_set="unit",
        models=["vendor/model"],
        variants=["vector"],
        out_dir=tmp_path / "run",
    )
    assert rows[0].solved is False  # no validation report from the fake pipeline
    root = build_review_package(info, rows, {"Plate": case}, tmp_path / "run")
    folder = root / "01_Plate"
    overview = (root / "LIESMICH.md").read_text()
    assert "| [Plate](01_Plate/LIESMICH.md) |" in overview and "0.010" in overview
    checklist = (folder / "PRUEFPROTOKOLL.md").read_text()
    assert "Schritt 2: Bohrung Ø20" in checklist
    assert f"bilder/{readable}.png" in (folder / "02_Ablesung" / "Ablesung.md").read_text()
    assert (folder / "02_Ablesung" / "bilder" / f"{readable}.png").is_file()
    assert (folder / "03_Modell_KI" / "Plate_KI.ap242.step").is_file()
    features_md = (folder / "03_Modell_KI" / "Features.md").read_text()
    assert "thin plate" in features_md and "X = nicht geprüft" in features_md
    seen = "Feature without dimensions in view v0: not modelled – rib (Bereich 65, 88, 155, 91 mm)"
    assert f"- **undim-1**: {seen}" in features_md
    pois = (folder / "03_Modell_KI" / "Pruefstellen.md").read_text()
    assert "| cov-1 | v0 | ![](bilder/cov-1.png) | View v0: 1 visible circle(s) Ø20 " in pois
    assert (folder / "03_Modell_KI" / "bilder" / "cov-1.png").is_file()
    assert "Pruefstellen.md`, 1 Stellen" in checklist
    assert (folder / "04_Referenz_BCT" / "Plate_Referenz_NX.stp").is_file()
    assert "IoU" in (folder / "05_Vergleich" / "Vergleich.md").read_text()
    names = zipfile.ZipFile(tmp_path / "run" / f"Review_{info.run_id}.zip").namelist()
    assert f"Review_{info.run_id}/01_Plate/PRUEFPROTOKOLL.md" in names


def test_run_case_reuses_one_event_loop() -> None:
    from mechatronikers.agent import pipeline

    first = pipeline._loop()
    first.run_until_complete(asyncio.sleep(0))
    assert pipeline._loop() is first  # same loop for the next case
    first.close()
    assert pipeline._loop() is not first  # a closed loop is replaced


def test_merge_runs_replaces_rerun_cases(tmp_path: Path) -> None:
    from mechatronikers.testbench.merge import merge_runs

    (tmp_path / "data" / "Plate").mkdir(parents=True)
    drawing = make_drawing(tmp_path / "data" / "Plate" / "Plate_dwg.pdf")
    reference = export_step(bd.Box(10, 10, 10), tmp_path / "data" / "Plate" / "Plate.stp")
    case = TestCase(case_id="Plate", drawing=drawing, reference_step=reference)

    def failing(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
        raise RuntimeError("first attempt")

    def working(case: TestCase, *, model: str, variant: str, out_dir: Path) -> PipelineOutcome:
        step = export_step(bd.Box(10, 10, 10), out_dir / "Plate.ap242.step")
        return PipelineOutcome(step_path=step, missing_dimensions=[])

    kwargs = {"test_set": "unit", "models": ["m"], "variants": ["vector"]}
    run([case], failing, pipeline_name="a", out_dir=tmp_path / "base", **kwargs)  # type: ignore[arg-type]
    up_info, _ = run([case], working, pipeline_name="b", out_dir=tmp_path / "up", **kwargs)  # type: ignore[arg-type]
    info, rows = merge_runs(tmp_path / "base", [tmp_path / "up"], tmp_path / "merged")
    assert [r.status for r in rows] == ["ok"]
    assert info.rerun_cases == {"Plate": up_info.run_id}
    assert (tmp_path / "merged" / "cases" / "Plate").is_dir()
    package = build_review_package(info, rows, {"Plate": case}, tmp_path / "merged")
    assert "Nachgerechnet nach Fehlerkorrekturen" in (package / "LIESMICH.md").read_text()


def test_blocking_review_items_of_an_agent_state() -> None:
    state = {
        "consistency": {
            "review_items": [
                {"id": "r1", "severity": "blocking", "status": "open"},
                {"id": "r2", "severity": "blocking", "status": "approved"},
                {"id": "r3", "severity": "warning", "status": "open"},
            ]
        },
        "coverage": {"review_items": [{"id": "cov-1", "severity": "blocking"}]},
        "failures": [{"feature_id": "base", "message": "V2 failed"}],
    }
    assert blocking_review_items(state) == ["r1", "cov-1", "fail-1"]
    assert blocking_review_items({}) == []
