"""Demo launcher CLI (host side, #135). Runbook: docs/demo.md.

    uv run python -m mechatronikers.demo up [--image TAG] [--rebuild-image] [--no-jobs]
        [--no-hub] [--jobs-port 8000] [--hub-port 8780] [--pg-port 5433]
    uv run python -m mechatronikers.demo status
    uv run python -m mechatronikers.demo run rectifier [--no-browser]
    uv run python -m mechatronikers.demo run fuss --yes [--no-browser]
    uv run python -m mechatronikers.demo down [--yes]

The image is `--image`, else `$MT_IMAGE`, else localhost/mechatronikers:demo. `run rectifier` is
the electrical run (no model); `run fuss` spends credits and needs `--yes`. `down --yes` shelves
without asking. Works from any folder of the checkout (the repository root is found upwards).

`up` is idempotent: what runs keeps running, what stopped is started again. `down` stops only what
`up` and `run` started (var/demo/state.json) and shelves the sandboxes created since `up`.
"""

import argparse
import os
import shlex
import sys
from pathlib import Path

from mechatronikers.demo.launcher import (
    CONTAINERFILE,
    DEFAULT_IMAGE,
    HUB_PORT,
    JOBS_PORT,
    PG_PORT,
    Demo,
    DemoConfig,
    Sessions,
    StoreSessions,
)
from mechatronikers.demo.system import RealSystem, System


def find_root(start: Path) -> Path:
    """The repository root, so `status` and `down` find the demo from any subfolder (e.g.
    frontend/): the nearest folder upwards with the sandbox Containerfile, else the checkout this
    package runs from."""
    for folder in (start, *start.parents):
        if (folder / "pyproject.toml").is_file() and (folder / CONTAINERFILE).is_file():
            return folder
    checkout = Path(__file__).resolve().parents[3]  # src/mechatronikers/demo/__main__.py
    return checkout if (checkout / CONTAINERFILE).is_file() else start


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.demo")
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root (default: found from here)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="check, build or reuse the image, start everything")
    up.add_argument("--image", help=f"sandbox image (default: $MT_IMAGE, else {DEFAULT_IMAGE})")
    up.add_argument("--rebuild-image", action="store_true", help="build it even if it exists")
    up.add_argument("--no-jobs", action="store_true", help="do not start the Fusion jobs adapter")
    up.add_argument("--no-hub", action="store_true", help="do not start the hub and Postgres")
    up.add_argument("--jobs-port", type=int, default=JOBS_PORT)
    up.add_argument("--hub-port", type=int, default=HUB_PORT)
    up.add_argument("--pg-port", type=int, default=PG_PORT)
    sub.add_parser("status", help="what runs, URLs and health")
    down = sub.add_parser("down", help="stop what the launcher started, shelve the demo sandboxes")
    down.add_argument("--yes", action="store_true", help="shelve the demo sandboxes without asking")
    run = sub.add_parser("run", help="start a sandbox and a demo run")
    run.add_argument("demo", choices=["fuss", "rectifier"])
    run.add_argument("--yes", action="store_true", help="confirm spending credits (fuss)")
    run.add_argument("--no-browser", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    system: System | None = None,
    sessions: Sessions | None = None,
) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(raw)
    root = (args.root or find_root(Path.cwd())).resolve()
    # Every child (session, agent, jobs) resolves var/ and .env against the working directory.
    os.chdir(root)
    config = DemoConfig(root=root)
    if args.command == "up":
        config.image = args.image or os.environ.get("MT_IMAGE") or DEFAULT_IMAGE
        config.rebuild_image = args.rebuild_image
        config.jobs = not args.no_jobs
        config.hub = not args.no_hub
        config.jobs_port = args.jobs_port
        config.hub_port = args.hub_port
        config.pg_port = args.pg_port
        # retry hints repeat the user's own flags (`--no-hub`, `--image`, ports)
        config.up_command = shlex.join(["uv", "run", "python", "-m", "mechatronikers.demo", *raw])
    demo = Demo(config, system or RealSystem(), sessions or StoreSessions(root / "var"))
    if args.command == "up":
        return demo.up()
    if args.command == "status":
        return demo.status()
    if args.command == "down":
        return demo.down(yes=args.yes)
    return demo.run(args.demo, yes=args.yes, browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
