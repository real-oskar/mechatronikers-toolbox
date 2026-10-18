"""Test bench CLI.

python -m mechatronikers.testbench cases
python -m mechatronikers.testbench compare GENERATED.step REFERENCE.step [--no-align] [--json]
python -m mechatronikers.testbench compare-case CASE_ID GENERATED.step [--json]
python -m mechatronikers.testbench agreement [--verdicts verdicts.json]
python -m mechatronikers.testbench extract-bench [--set all|parts|circuits] [--variants ...]
                                                 [--extractors e0,e1,e1-crops,detect,e3,e4,...]
                                                 [--max-credits N | --dry-run]
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from mechatronikers.testbench.cases import challenge_set, ensure_materialized, get_case
from mechatronikers.testbench.compare import CompareResult, compare_files
from mechatronikers.testbench.pipelines import DEFAULT_PIPELINE, load_pipeline
from mechatronikers.testbench.run import run, usage_text


def _print_result(result: CompareResult) -> None:
    def fmt(v: float | None, unit: str = "") -> str:
        return "n/a" if v is None else f"{v:.4f}{unit}"

    def pm(v: float | None, err: float | None, digits: int, unit: str = "") -> str:
        if v is None:
            return "n/a"
        return f"{v:.{digits}f}" + ("" if err is None else f" ± {err:.{digits}f}") + unit

    rows = [
        ("generated", result.generated),
        ("reference", result.reference),
        ("matches reference", "yes" if result.matches_reference else "no"),
        ("IoU", pm(result.iou, result.iou_stderr, 4)),
        (
            "difference score (1 − IoU)",
            pm(result.difference_score, result.difference_score_stderr, 4),
        ),
        (
            "symmetric difference",
            pm(
                result.symmetric_difference_volume_mm3,
                result.symmetric_difference_volume_stderr,
                1,
                " mm³",
            ),
        ),
        ("alignment RMS", fmt(result.alignment_rms, " mm")),
        ("Hausdorff (sampled)", fmt(result.hausdorff, " mm")),
        ("mean surface distance", fmt(result.mean_surface_distance, " mm")),
        ("volume gen / ref", f"{result.volume_generated:.2f} / {result.volume_reference:.2f}"),
        ("volume rel diff", fmt(result.volume_rel_diff)),
        ("area rel diff", fmt(result.area_rel_diff)),
        ("bbox gen", " x ".join(f"{v:.3f}" for v in result.bbox_generated)),
        ("bbox ref", " x ".join(f"{v:.3f}" for v in result.bbox_reference)),
        ("faces gen / ref", f"{result.face_count_generated} / {result.face_count_reference}"),
        (
            "holes gen / ref / matched",
            f"{len(result.hole_diameters_generated)} / {len(result.hole_diameters_reference)}"
            f" / {result.holes_matched}",
        ),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.testbench")
    parser.add_argument("--data-dir", default="data", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cases", help="list the built-in challenge test cases")

    for name in ("compare", "compare-case"):
        p = sub.add_parser(name)
        if name == "compare":
            p.add_argument("generated", type=Path)
            p.add_argument("reference", type=Path)
        else:
            p.add_argument("case_id")
            p.add_argument("generated", type=Path)
        p.add_argument("--no-align", action="store_true", help="models share a frame already")
        p.add_argument("--samples", type=int, default=4000)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="run a pipeline over a test set and compare with human models")
    p.add_argument("--set", default="challenge-v1", help="test set (only challenge-v1 so far)")
    p.add_argument("--pipeline", default=DEFAULT_PIPELINE, help="module:function")
    p.add_argument(
        "--models", default="default", help="comma-separated; default = MT_MODEL from .env"
    )
    p.add_argument("--variants", default="vector", help="comma-separated")
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--cases", help="comma-separated case IDs (default: all)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, help="output directory (default: var/testbench/<timestamp>)")
    p.add_argument(
        "--no-review-package", action="store_true", help="skip the engineers' review package"
    )
    p.add_argument(
        "--case-timeout",
        type=float,
        default=3600.0,
        help="wall-clock limit per case in seconds; 0 = none (default: 1 h, #145)",
    )

    a = sub.add_parser("agreement", help="check solved against human verdicts on a run's models")
    a.add_argument("--verdicts", type=Path, help="verdict file (default: run-20261017-093112)")
    a.add_argument("--samples", type=int, default=4000)
    a.add_argument("--json", action="store_true")

    m = sub.add_parser("merge", help="combine runs; later runs replace the cases they contain")
    m.add_argument("--base", type=Path, required=True)
    m.add_argument("--update", type=Path, action="append", default=[])
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--no-review-package", action="store_true")

    e = sub.add_parser(
        "extract-bench", help="score drawing extraction paths on vector and raster variants"
    )
    e.add_argument("--set", choices=("all", "parts", "circuits"), default="all")
    e.add_argument("--cases", help="comma-separated case IDs (default: all of the set)")
    e.add_argument("--variants", default="all", help="comma-separated presets, or 'all' (default)")
    e.add_argument(
        "--extractors",
        default="e0",
        help="comma-separated: e0, e1, e1-crops, detect, e3, e4, stub, with options as "
        "name@key=value (e.g. e4@threshold=0.6), or module:factory",
    )
    e.add_argument(
        "--max-credits",
        type=float,
        help="credit cap for all model calls of this run (needed for e3/e4); the ledger's "
        "total budget (MT_BUDGET_TOTAL) applies as well",
    )
    e.add_argument(
        "--dry-run",
        action="store_true",
        help="model backends make no calls; their credits are estimates from token counts",
    )
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--iou", type=float, default=0.5, help="IoU needed for a label match")
    e.add_argument("--variants-dir", type=Path, default=Path("var/extract-bench/variants"))
    e.add_argument("--out", type=Path, help="default: var/extract-bench/<timestamp>")

    args = parser.parse_args(argv)

    if args.command == "agreement":
        from mechatronikers.testbench.verdicts import (
            DEFAULT_VERDICTS,
            agreement,
            agreement_markdown,
        )

        verdicts, agreements = agreement(
            args.verdicts or DEFAULT_VERDICTS, args.data_dir, samples=args.samples
        )
        if args.json:
            print(json.dumps([a.model_dump(mode="json") for a in agreements], indent=2))
        else:
            print(agreement_markdown(verdicts, agreements))
        return 0 if all(a.agrees for a in agreements) else 1

    if args.command == "extract-bench":
        return _extract_bench(parser, args)

    if args.command == "merge":
        from mechatronikers.testbench.merge import merge_runs

        info, rows = merge_runs(args.base, args.update, args.out)
        print(f"{sum(r.solved for r in rows)} / {len(rows)} solved -> {args.out / 'summary.md'}")
        print(f"cost: {usage_text(rows)}")
        if not args.no_review_package:
            from mechatronikers.testbench.review_package import build_review_package

            by_id = {c.case_id: c for c in challenge_set(args.data_dir)}
            package = build_review_package(info, rows, by_id, args.out)
            print(f"review package: {package} (+ .zip)")
        return 0

    if args.command == "run":
        if args.set != "challenge-v1":
            parser.error(f"unknown test set {args.set!r}; uploaded test sets follow with #21")
        cases = challenge_set(args.data_dir)
        if args.cases:
            wanted = {c.strip().lower() for c in args.cases.split(",")}
            cases = [c for c in cases if c.case_id.lower() in wanted]
        try:
            pipeline = load_pipeline(args.pipeline)
        except ValueError as exc:
            parser.error(str(exc))
        out = args.out or Path("var/testbench") / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        info, rows = run(
            cases,
            pipeline,
            pipeline_name=args.pipeline,
            test_set=args.set,
            models=[m.strip() for m in args.models.split(",")],
            variants=[v.strip() for v in args.variants.split(",")],
            out_dir=out,
            repetitions=args.repetitions,
            seed=args.seed,
            progress=True,
            case_timeout_s=args.case_timeout or None,
        )
        print(f"\n{sum(r.solved for r in rows)} / {len(rows)} solved -> {out / 'summary.md'}")
        print(f"cost: {usage_text(rows)}")
        if not args.no_review_package:
            from mechatronikers.testbench.review_package import build_review_package

            package = build_review_package(info, rows, {c.case_id: c for c in cases}, out)
            print(f"review package: {package} (+ .zip)")
        return 0

    if args.command == "cases":
        for case in challenge_set(args.data_dir):
            print(f"{case.case_id:<16} {case.drawing}  {case.reference_step}")
        return 0

    if args.command == "compare":
        reference = args.reference
    else:
        reference = get_case(args.case_id, args.data_dir).reference_step
    ensure_materialized(reference)
    ensure_materialized(args.generated)
    result = compare_files(
        args.generated, reference, do_align=not args.no_align, samples=args.samples
    )
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    return 0 if result.matches_reference else 1


def _extract_bench(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from mechatronikers.testbench.extract_bench import bench_cases, run_bench
    from mechatronikers.testbench.extraction import load_extractor, needs_model
    from mechatronikers.testbench.variants import PRESETS

    def items(text: str, what: str) -> list[str]:
        names = [n.strip() for n in text.split(",") if n.strip()]
        if len(names) != len(set(names)):
            parser.error(f"duplicate {what}: {text}")
        return names

    if not 0 < args.iou <= 1:
        parser.error(f"--iou must be in (0, 1], got {args.iou}")
    names = list(PRESETS) if args.variants == "all" else items(args.variants, "variants")
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        parser.error(f"unknown variants {unknown}; presets: {', '.join(PRESETS)}")
    cases = bench_cases(args.data_dir, args.set)
    if args.cases:
        wanted = {c.lower() for c in items(args.cases, "cases")}
        missing = sorted(wanted - {c.case_id.lower() for c in cases})
        if missing:
            parser.error(f"unknown cases {missing} in set {args.set!r}")
        cases = [c for c in cases if c.case_id.lower() in wanted]
    if not cases:
        parser.error("no cases selected")
    specs = items(args.extractors, "extractors")
    out = args.out or Path("var/extract-bench") / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    reader = None
    try:
        model_needed = any(needs_model(x) for x in specs)
    except ValueError as exc:
        parser.error(str(exc))
    if model_needed:
        if not args.dry_run and args.max_credits is None:
            parser.error("e3/e4 call a model: give --max-credits N (or --dry-run to estimate)")
        from mechatronikers.agent.budget import LedgerUnreadableError
        from mechatronikers.agent.extractors import bench_reader

        try:
            reader = bench_reader(
                max_credits=args.max_credits,
                events=out / "model-events.ndjson",
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if reader.llm is not None:
            guard = reader.llm.budget
            try:
                total = guard.total_spent()
            except LedgerUnreadableError as exc:
                parser.error(str(exc))
            print(
                f"model {reader.config.model}: at most {args.max_credits:.2f} credits for this "
                f"run; {total:.2f} of {guard.total_limit:.2f} spent in total"
            )
    try:
        try:
            extractors = [load_extractor(x, reader=reader) for x in specs]
        except ValueError as exc:
            parser.error(str(exc))
        if len({e.name for e in extractors}) != len(extractors):
            parser.error(f"extractor names must be unique: {[e.name for e in extractors]}")
        run_bench(
            cases,
            [PRESETS[n] for n in names],
            extractors,
            out_dir=out,
            variants_dir=args.variants_dir,
            seed=args.seed,
            iou_threshold=args.iou,
            progress=True,
        )
    finally:
        if reader is not None:
            reader.close()
    if reader is not None:
        if reader.dry_run:
            e = reader.estimated
            print(
                f"\ndry run, no model calls: estimated {e.credits:.3f} credits "
                f"({e.tokens_in} input / {e.tokens_out} output tokens; "
                f"{e.credits_tiles:.3f} if images are billed by tiles)"
            )
        else:
            print(f"\nmodel credits spent in this run: {reader.spent:.4f}")
    print(f"\nresults -> {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
