"""Merge test bench runs: later runs replace the results of the cases they contain."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from mechatronikers.testbench.run import ResultRow, RunInfo, case_dir, summary_markdown


def _load(run_dir: Path) -> tuple[RunInfo, list[ResultRow]]:
    info = RunInfo.model_validate_json((run_dir / "run.json").read_text())
    rows = [
        ResultRow.model_validate_json(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for row in rows:
        if row.cost is None:  # runs from before #58: take the spend from the case's events
            row.record_usage(case_dir(run_dir, row.case_id, row.model, row.variant, row.repetition))
    return info, rows


def merge_runs(base: Path, updates: list[Path], out: Path) -> tuple[RunInfo, list[ResultRow]]:
    """Copy `base`, then let every run in `updates` (in order) replace its cases."""
    if out.exists():
        shutil.rmtree(out)
    info, rows = _load(base)
    shutil.copytree(base / "cases", out / "cases")
    by_key = {(r.case_id, r.model, r.variant, r.repetition): r for r in rows}
    merged_from = [info.run_id]
    rerun: dict[str, str] = {}
    for update in updates:
        up_info, up_rows = _load(update)
        merged_from.append(up_info.run_id)
        for row in up_rows:
            by_key[(row.case_id, row.model, row.variant, row.repetition)] = row
            rerun[row.case_id] = up_info.run_id
            target = out / "cases" / row.case_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(update / "cases" / row.case_id, target)
    order = {c: i for i, c in enumerate(info.cases)}
    merged = sorted(by_key.values(), key=lambda r: (order.get(r.case_id, 999), r.case_id))
    merged_info = info.model_copy(
        update={
            "run_id": f"{info.run_id}-merged",
            "finished_at": datetime.now(UTC).isoformat(),
            "merged_from": merged_from,
            "rerun_cases": rerun,
        }
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(merged_info.model_dump_json(indent=2))
    (out / "results.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in merged))
    (out / "summary.md").write_text(summary_markdown(merged_info, merged))
    return merged_info, merged
