"""Jobs adapter CLI (host side, #119). Needs `session watch` running and the agent's `.env`.

MT_JOBS_API_KEY=<key> python -m mechatronikers.jobs serve [--port 8000] [--host 127.0.0.1]

Each uploaded PDF starts a sandbox and a real agent run (credits; the agent's budget guard
MT_BUDGET_RUN / MT_BUDGET_TOTAL applies). Sandboxes keep running after a job ends, until they are
shelved (`python -m mechatronikers.session shelve <session-id> --yes`, D22).
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from mechatronikers.agent.config import LANGUAGES
from mechatronikers.jobs.app import (
    API_KEY_ENV,
    JOB_TIMEOUT_S,
    KEEP_DAYS,
    MAX_UPLOAD,
    JobsConfig,
    SubprocessLauncher,
    collector_emit,
    create_jobs_app,
)
from mechatronikers.jobs.store import JobStore
from mechatronikers.session.__main__ import build_manager
from mechatronikers.session.manager import DEFAULT_IMAGE

LOOPBACK = ("127.0.0.1", "::1", "localhost")
MIN_KEY = 16


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.jobs")
    parser.add_argument("--var-dir", type=Path, default=Path("var"))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the add-in's jobs API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback --host; needs --review-host and TLS in front of the adapter "
        "and of the forwarded gateway ports (the key, PDFs and review links cross the network)",
    )
    serve.add_argument(
        "--review-host",
        help="https://host for review links handed to remote clients (the gateway port is kept)",
    )
    serve.add_argument("--image", default=os.environ.get("MT_IMAGE", DEFAULT_IMAGE))
    serve.add_argument("--lang", choices=LANGUAGES, help="run language (default: MT_LANG, else en)")
    serve.add_argument("--max-upload-mb", type=float, default=MAX_UPLOAD / 1024 / 1024)
    serve.add_argument(
        "--max-active",
        type=int,
        default=2,
        help="jobs running at the same time (not a cap on sandboxes: a finished job's sandbox "
        "keeps running until it is shelved)",
    )
    serve.add_argument(
        "--job-timeout-min",
        type=float,
        default=JOB_TIMEOUT_S / 60,
        help="a job without a final decision is stopped after this many minutes",
    )
    serve.add_argument(
        "--keep-days", type=float, default=KEEP_DAYS, help="delete finished jobs after N days"
    )
    serve.add_argument("--collector-url", help="collector API for the agent (default: its own)")
    return parser


def _env_lang() -> str | None:
    """MT_LANG if valid; the agent reads it too, this only picks the language of the messages."""
    lang = (os.environ.get("MT_LANG") or "").strip().lower()
    return lang if lang in LANGUAGES else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    key = os.environ.get(API_KEY_ENV, "").strip()
    if len(key) < MIN_KEY:
        print(
            f"set {API_KEY_ENV} to a key of at least {MIN_KEY} characters, e.g. from\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(24))"',
            file=sys.stderr,
        )
        return 2
    if args.host not in LOOPBACK and not args.allow_remote:
        print(
            f"refusing to listen on {args.host}: only 127.0.0.1 unless --allow-remote",
            file=sys.stderr,
        )
        return 2
    if args.host not in LOOPBACK and not (args.review_host or "").startswith("https://"):
        print(
            "--allow-remote needs --review-host https://<host>: the review link carries the "
            "session token and must be reachable by the remote client",
            file=sys.stderr,
        )
        return 2
    manager = build_manager(args.var_dir, args.image)
    app = create_jobs_app(
        JobsConfig(
            api_key=key,
            max_upload=int(args.max_upload_mb * 1024 * 1024),
            max_active=args.max_active,
            job_timeout_s=args.job_timeout_min * 60,
            keep_days=args.keep_days,
            lang=args.lang or _env_lang(),
            review_host=args.review_host if args.host not in LOOPBACK else None,
        ),
        JobStore(args.var_dir / "jobs"),
        manager,
        manager.collector.store,
        SubprocessLauncher(args.var_dir, args.collector_url),
        emit=collector_emit(manager.collector),
    )
    print(
        f"jobs API on http://{args.host}:{args.port} (image {args.image}); "
        f"every job starts a sandbox and a paid agent run. Ctrl+C to stop.",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
