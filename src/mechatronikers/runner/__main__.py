"""Runner entry point, executed inside the isolated process or container.

python -m mechatronikers.runner --job /job/job.json --out /out
"""

import argparse
import os
import sys
from pathlib import Path

from mechatronikers.runner.worker import execute
from mechatronikers.spec import BuildJob
from mechatronikers.telemetry import EventEmitter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.runner")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    # Assigned by the launcher: it passes through only events from this emitter (docs/08).
    parser.add_argument("--emitter-id")
    args = parser.parse_args(argv)
    job = BuildJob.model_validate_json(args.job.read_text())
    emitter = EventEmitter(
        "build-runner",
        session_id=os.environ.get("MT_SESSION_ID"),
        run_id=os.environ.get("MT_RUN_ID"),
    )
    if args.emitter_id:
        emitter.emitter_id = args.emitter_id
    result = execute(job, args.out, emitter)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
