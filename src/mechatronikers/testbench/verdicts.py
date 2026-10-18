"""Human verdicts on generated models (#93): does the bench's "solved" agree with people?

A verdict file lists, for one test bench run, each part's verdict with the reviewer's reasons
(verbatim) and the generated STEP model it refers to, stored next to the file.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mechatronikers.testbench.cases import ensure_materialized, get_case
from mechatronikers.testbench.compare import CompareResult, compare_files

DEFAULT_VERDICTS = Path("tests/fixtures/human_verdicts/run-20261017-093112/verdicts.json")


class HumanVerdict(BaseModel):
    part: str  # test case id
    verdict: Literal["good", "poor", "failed"]  # good = the reviewer accepts the model
    note: str | None = None
    reasons: list[str]  # verbatim, in the reviewer's language
    model: str | None  # generated STEP next to the verdict file; None = the run produced none


class VerdictSet(BaseModel):
    run_id: str
    reviewer: str
    reviewed_at: str  # date
    source: str  # where the reasons and models come from
    labels: str  # who mapped the reasons to good / poor / failed
    verdicts: list[HumanVerdict]


class Agreement(BaseModel):
    part: str
    verdict: str
    solved: bool  # the bench's geometry verdict (D91); False without a model (M1 fails)
    agrees: bool
    comparison: CompareResult | None = None


def load_verdicts(path: Path) -> VerdictSet:
    return VerdictSet.model_validate_json(path.read_text())


def agreement(
    path: Path = DEFAULT_VERDICTS, data_dir: Path = Path("data"), samples: int = 4000
) -> tuple[VerdictSet, list[Agreement]]:
    """Compare every verdict's model with its human reference and check that the bench calls it
    solved exactly when the reviewer called it good.

    Only the geometry part of "solved" can be checked here: the run's validation reports and
    review items are not stored with the verdicts.
    """
    verdicts = load_verdicts(path)
    rows = []
    for v in verdicts.verdicts:
        comparison = None
        if v.model is not None:
            comparison = compare_files(
                ensure_materialized(path.parent / v.model),
                ensure_materialized(get_case(v.part, data_dir).reference_step),
                samples=samples,
            )
        solved = comparison is not None and comparison.matches_reference
        rows.append(
            Agreement(
                part=v.part,
                verdict=v.verdict,
                solved=solved,
                agrees=solved == (v.verdict == "good"),
                comparison=comparison,
            )
        )
    return verdicts, rows


def agreement_markdown(verdicts: VerdictSet, rows: list[Agreement]) -> str:
    def num(value: float | None, digits: int) -> str:
        return "–" if value is None else f"{value:.{digits}f}"

    lines = [
        f"Human verdicts on {verdicts.run_id} by {verdicts.reviewer} ({verdicts.reviewed_at})",
        "Bench verdict: the geometry part of solved (D91: IoU ≥ 0.95, holes match, "
        "Hausdorff ≤ 1 mm)",
        "",
        "| Part | Human | IoU | Hausdorff (mm) | Holes gen/ref | Bench | Agrees |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r.comparison
        holes = (
            "–"
            if c is None
            else (f"{len(c.hole_diameters_generated)}/{len(c.hole_diameters_reference)}")
        )
        lines.append(
            f"| {r.part} | {r.verdict} | {num(c.iou if c else None, 3)} "
            f"| {num(c.hausdorff if c else None, 2)} | {holes} "
            f"| {'solved' if r.solved else 'not solved'} | {'✓' if r.agrees else '✗'} |"
        )
    lines += ["", f"Agreement: {sum(r.agrees for r in rows)} / {len(rows)}"]
    return "\n".join(lines)
