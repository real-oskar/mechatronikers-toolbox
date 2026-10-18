"""Session manager CLI (host side).

python -m mechatronikers.session build-image
python -m mechatronikers.session start [--drawing data/Fuss/Fuss_dwg.pdf]
python -m mechatronikers.session status [SESSION_ID]
python -m mechatronikers.session connections SESSION_ID     # MultiServerMCPClient config
python -m mechatronikers.session events SESSION_ID [--after N]
python -m mechatronikers.session collect SESSION_ID         # ingest container output once
python -m mechatronikers.session remind                     # one idle check
python -m mechatronikers.session watch [--port 8765]        # follow logs, reminders, HTTP API
                                                           # (+ gateway socket on Linux)
python -m mechatronikers.session watch --hub URL            # … and mirror to a hub (MT_HUB_TOKEN)
python -m mechatronikers.session shelve SESSION_ID --yes    # snapshot, verify, stop
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from mechatronikers.session.gateway import COLLECTOR_PORT, collector_route
from mechatronikers.session.manager import SessionManager, ShelveNotConfirmedError, configured_image
from mechatronikers.session.runtime import PodmanRuntime
from mechatronikers.session.store import SessionStore
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.store import EventStore

if TYPE_CHECKING:
    from mechatronikers.hub.forwarder import HubForwarder

VAR = Path("var")


def build_manager(var_dir: Path, image: str) -> SessionManager:
    store = EventStore(var_dir / "state.db")
    collector = Collector(store, var_dir / "storage")
    return SessionManager(
        PodmanRuntime(),
        collector,
        SessionStore(store.connection()),
        var_dir / "storage",
        image=image,
        collector_route=collector_route(var_dir),
    )


def watch_once(
    manager: SessionManager,
    stop: threading.Event,
    followed: set[str],
    reminder_after: timedelta,
) -> None:
    """One round of `session watch`. A failure for one session is reported and retried next
    round; it never ends the watcher, which serves every session (#78)."""
    for record in manager.sessions.all(("running", "awaiting_review")):
        if record.id in followed:
            continue
        try:
            manager.follow(record.id, stop)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot follow {record.id}: {exc!r}"[:300], file=sys.stderr, flush=True)
            continue
        followed.add(record.id)
        print(f"following {record.id}", flush=True)
    try:
        for session_id in manager.remind(reminder_after):
            print(f"reminder: sandbox {session_id} is still running", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"reminders failed: {exc!r}"[:300], file=sys.stderr, flush=True)


def _forwarder(manager: SessionManager, hub_url: str) -> "HubForwarder | None":
    """The opt-in forwarder of `watch --hub` (docs/13, D79), or None if it cannot start."""
    from mechatronikers.hub.forwarder import HUB_TOKEN_ENV, ForwardError, HubForwarder

    token = os.environ.get(HUB_TOKEN_ENV, "").strip()
    if not token:
        print(f"--hub needs the worker token in {HUB_TOKEN_ENV}", file=sys.stderr)
        return None
    forwarder = HubForwarder(
        manager.collector.store,
        manager.sessions,
        hub_url,
        token,
        copy_artifact=manager.copy_artifact,
    )
    try:
        print(f"mirroring to {forwarder.hub_url} as worker {forwarder.check()}", flush=True)
    except ForwardError as exc:  # keep trying: the hub may come up later
        print(f"hub forwarder: {exc}; retrying", file=sys.stderr, flush=True)
    return forwarder


def _watch(
    manager: SessionManager,
    port: int,
    reminder_after: timedelta,
    var_dir: Path,
    hub_url: str | None = None,
) -> int:
    from mechatronikers.telemetry.collector_app import collector_server, create_collector_app
    from mechatronikers.telemetry.local_api import ingest_key

    forwarder = _forwarder(manager, hub_url) if hub_url else None
    if hub_url and forwarder is None:
        return 2
    stop = threading.Event()
    followed: set[str] = set()
    # 127.0.0.1 for host callers and the run table; the Unix socket for the gateway where host
    # loopback is out of the containers' reach (rootless Linux, #53).
    unix_socket = manager.collector_route.socket
    api, sockets = collector_server(
        create_collector_app(
            manager.collector,
            manager.sessions,
            lambda sid: manager.info(sid).link,
            ingest_key=ingest_key(var_dir),  # the run drivers read the same key (#146)
            # the run table's "finish and shelve", after the user's OK in its dialog (D22, #52)
            shelve=lambda sid, confirmed: manager.shelve(sid, confirmed=confirmed),
        ),
        port,
        unix_socket,
    )
    threading.Thread(target=api.run, kwargs={"sockets": sockets}, daemon=True).start()
    if forwarder is not None:
        threading.Thread(target=forwarder.run, args=(stop,), daemon=True, name="hub").start()
    where = f"http://127.0.0.1:{port}" + (f" and {unix_socket}" if unix_socket else "")
    print(f"collector API on {where} (Ctrl+C to stop)", flush=True)
    try:
        while True:
            watch_once(manager, stop, followed, reminder_after)
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
        api.should_exit = True
    finally:
        if unix_socket is not None:
            unix_socket.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.session")
    parser.add_argument("--var-dir", type=Path, default=VAR)
    parser.add_argument("--image", default=None, help="default: $MT_IMAGE, else :dev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build-image")
    start = sub.add_parser("start")
    start.add_argument("--drawing", type=Path)
    start.add_argument(
        "--retention", choices=["ephemeral", "standard", "audit"], default="standard"
    )
    status = sub.add_parser("status")
    status.add_argument("session_id", nargs="?")
    for name in ("connections", "collect"):
        sub.add_parser(name).add_argument("session_id")
    events = sub.add_parser("events")
    events.add_argument("session_id")
    events.add_argument("--after", type=int, default=0)
    remind = sub.add_parser("remind")
    remind.add_argument("--after-minutes", type=float, default=15)
    watch = sub.add_parser("watch")
    watch.add_argument("--port", type=int, default=COLLECTOR_PORT)
    watch.add_argument("--reminder-minutes", type=float, default=15)
    watch.add_argument(
        "--hub", help="mirror events and artifacts to this hub (token: MT_HUB_TOKEN)"
    )
    shelve = sub.add_parser("shelve")
    shelve.add_argument("session_id")
    shelve.add_argument("--yes", action="store_true", help="confirm: snapshot and stop the sandbox")
    args = parser.parse_args(argv)

    if args.command == "build-image":
        cmd = [
            "podman",
            "build",
            "-t",
            configured_image(args.image),
            "-f",
            "deploy/containers/Containerfile",
            ".",
        ]
        return subprocess.run(cmd, check=False).returncode

    manager = build_manager(args.var_dir, configured_image(args.image))
    if args.command == "start":
        info = manager.start(args.drawing, retention_class=args.retention)
        print(info.model_dump_json(indent=2))
        print(f"\nReview link: {info.link}\nThe sandbox keeps running until you shelve it.")
    elif args.command == "status":
        records = (
            [manager.sessions.get(args.session_id)] if args.session_id else manager.sessions.all()
        )
        for r in records:
            print(f"{r.id}  {r.state:<16} port={r.port}  created={r.created_at}", end="")
            print(f"  snapshot={r.snapshot_id}" if r.snapshot_id else "")
    elif args.command == "connections":
        print(json.dumps(manager.info(args.session_id).mcp, indent=2))
    elif args.command == "collect":
        print(f"{manager.collect(args.session_id)} new events")
    elif args.command == "events":
        for ingest_id, event in manager.collector.store.events(args.session_id, args.after):
            print(f"{ingest_id:>6} {event.ts} {event.component:<16} {event.type}")
    elif args.command == "remind":
        for session_id in manager.remind(timedelta(minutes=args.after_minutes)):
            print(f"reminder: sandbox {session_id} is still running")
    elif args.command == "watch":
        return _watch(
            manager, args.port, timedelta(minutes=args.reminder_minutes), args.var_dir, args.hub
        )
    elif args.command == "shelve":
        try:
            manifest = manager.shelve(args.session_id, confirmed=args.yes)
        except ShelveNotConfirmedError as exc:
            print(f"{exc}. Re-run with --yes to confirm.", file=sys.stderr)
            return 2
        print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
