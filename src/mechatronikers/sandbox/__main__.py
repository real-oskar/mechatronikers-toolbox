"""Session container entry point: serves both MCP servers on port 8000."""

import ctypes
import os
import sys
from pathlib import Path
from typing import cast

import uvicorn
from starlette.applications import Starlette

from mechatronikers.runner.launcher import Isolation
from mechatronikers.sandbox.app import create_app
from mechatronikers.session.gateway import GATEWAY_SECRET_ENV

_PR_SET_DUMPABLE = 4


def protect_process() -> None:
    """Keep the gateway secret from the build runner, which runs as the same user (D95): a
    non-dumpable process's memory and /proc/<pid>/environ are closed to other processes of that
    user (with ptrace_scope 0, as in the Podman VM, they are open otherwise)."""
    if sys.platform == "linux":
        if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")


def app_from_env() -> Starlette:
    """The app as the session manager configured it. The gateway secret leaves the environment
    here, before any build runs, so the build runner does not inherit it."""
    return create_app(
        session_id=os.environ.get("MT_SESSION_ID"),
        builds_dir=Path(os.environ.get("MT_BUILDS_DIR", "/session/builds")),
        isolation=cast(Isolation, os.environ.get("MT_RUNNER_ISOLATION", "subprocess")),
        gateway_secret=os.environ.pop(GATEWAY_SECRET_ENV, None) or None,
    )


def main() -> None:
    protect_process()
    app = app_from_env()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MT_PORT", "8000")), access_log=False)  # noqa: S104 - reachable only via the gateway network


if __name__ == "__main__":
    main()
