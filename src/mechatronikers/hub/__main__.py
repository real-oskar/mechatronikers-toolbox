"""Hub CLI (mode 2, docs/13).

python -m mechatronikers.hub token                     # print a new random token
python -m mechatronikers.hub serve [--host 127.0.0.1] [--port 8780] [--data-dir var/hub]
                                   [--require-tokens]

`serve` reads its credentials from the environment, never from the command line:

  MT_HUB_SHARED_TOKEN   browser credential (token link /?t=… or basic-auth password); generated
                        and printed as a link once if unset (an error with --require-tokens)
  MT_HUB_WORKER_TOKENS  name=token[,name=token…]: one token per worker host (`MT_HUB_TOKEN` there)
  MT_HUB_ORIGIN         public origin for the cross-site checks, if not the Host header
  MT_PG_DSN             the central Postgres store (`hub` extra; blobs below --data-dir);
                        without it: SQLite and a blob folder in --data-dir
"""

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mechatronikers.hub.auth import (
    ORIGIN_ENV,
    SHARED_TOKEN_ENV,
    WORKER_TOKENS_ENV,
    AuthConfigError,
    HubAuth,
    parse_worker_tokens,
)

if TYPE_CHECKING:
    from mechatronikers.hub.records import HubEvents, HubStore

DEFAULT_PORT = 8780
DSN_ENV = "MT_PG_DSN"  # as pgstore.db; that module needs the `hub` extra, so it is not imported


def new_token() -> str:
    return secrets.token_urlsafe(32)


def open_stores(data: Path) -> tuple["HubEvents", "HubStore", str]:
    """Postgres if `MT_PG_DSN` is set (pending migrations are applied; blobs below `data`),
    else SQLite and a blob folder in `data`."""
    from mechatronikers.hub.records import RepositoryEvents, SqliteHubStore
    from mechatronikers.storage.blobs import LocalBlobStore

    if os.environ.get(DSN_ENV, "").strip():
        from mechatronikers.hub.pg import PgHubEvents, PgHubStore
        from mechatronikers.pgstore.store import PgStore

        # readers (SSE, overview) and writers (ingest, uploads) on separate pools, so polling
        # readers never starve ingest (#122: the pool is not first come, first served)
        store = PgStore.open(data / "blobs", max_size=8)
        writer = PgStore.open(data / "blobs", apply_migrations=False, max_size=3)
        return (
            PgHubEvents(store, writer),
            PgHubStore(store, writer),
            f"Postgres, blobs in {data / 'blobs'}",
        )
    from mechatronikers.telemetry.store import EventStore

    blobs = LocalBlobStore(data / "blobs")
    events = RepositoryEvents(EventStore(data / "hub.db"))
    return events, SqliteHubStore(data / "hub.db", blobs), f"SQLite and blobs in {data}"


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from mechatronikers.hub.app import create_hub_app

    shared = os.environ.get(SHARED_TOKEN_ENV, "").strip()
    if args.require_tokens and not (shared and os.environ.get(WORKER_TOKENS_ENV, "").strip()):
        print(f"error: set {SHARED_TOKEN_ENV} and {WORKER_TOKENS_ENV}", file=sys.stderr)
        return 2
    generated = not shared
    shared = shared or new_token()
    try:
        workers = parse_worker_tokens(os.environ.get(WORKER_TOKENS_ENV, ""))
        auth = HubAuth.create(shared, workers, os.environ.get(ORIGIN_ENV) or None)
    except AuthConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    data: Path = args.data_dir
    try:
        events, runs, where = open_stores(data)
    except Exception as exc:  # noqa: BLE001 - reported without the DSN, which holds a password
        message = str(exc)
        if isinstance(exc, ImportError):
            message = f"{DSN_ENV} needs the hub extra (uv sync --extra hub)"
        elif os.environ.get(DSN_ENV, "").strip():
            from mechatronikers.pgstore.db import scrub

            message = scrub(message)
        print(f"error: cannot open the store: {message}", file=sys.stderr)
        return 2
    app = create_hub_app(events=events, runs=runs, auth=auth)
    base = os.environ.get(ORIGIN_ENV) or f"http://{args.host}:{args.port}"
    print(f"hub on {base} ({where}); workers: {', '.join(workers) or 'none'}", flush=True)
    if not workers:
        print(f"no worker can push yet: set {WORKER_TOKENS_ENV}=name=token", file=sys.stderr)
    if generated:
        # like the review link of `session start`: the user needs it, so it is shown once
        print(f"open {base}/?t={shared}  (set {SHARED_TOKEN_ENV} to keep it)", flush=True)
    try:
        # no access log: a token link's `?t=` must not land in a log (as the gateway, #132)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    finally:
        runs.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="print a new random token")
    run = sub.add_parser("serve", help="run the hub")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--data-dir", type=Path, default=Path("var/hub"))
    run.add_argument(
        "--require-tokens", action="store_true", help="refuse to start without both tokens set"
    )
    args = parser.parse_args(argv)
    if args.command == "token":
        print(new_token())
        return 0
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
