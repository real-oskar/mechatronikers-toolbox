"""Linux namespaces for the build runner in subprocess mode (D95, #99).

Inside a session the runner is a child process of the sandbox app (D41). It runs model-written
code, and without more isolation it would share the app's network (the review API on port 8000)
and the session volume (the review state in `/session/review`), so a build script could approve
its own review gates. The runner is therefore started through this module
(`python -m mechatronikers.runner.namespaces BUILD_DIR SESSION_DIR -- RUNNER...`), which enters

- a new user namespace that maps only its own uid and gid, so after exec it holds no capabilities;
- a new network namespace: only a loopback interface that is down, no route anywhere;
- a new mount namespace in which the session folder (the parent of the builds folder) is an empty
  tmpfs that holds only this build's directory,

and then execs the runner. A separate process rather than a `preexec_fn`: the sandbox app is not
dumpable (it holds the gateway secret), a forked child inherits that, and a non-dumpable process
cannot write its own uid map; after an exec it is dumpable again, with none of the app's memory.
The build script cannot undo the mounts: it has no capabilities in this user namespace, and in a
nested one the kernel locks mounts it inherited.
"""

import contextlib
import ctypes
import functools
import os
import select
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast, get_args

import mechatronikers

# `required`: no build without namespaces; `auto` (the default, also for sessions since the
# decision of 2026-10-17): use them where the kernel allows, else run with a warning event;
# `off`: never.
Policy = Literal["required", "auto", "off"]
POLICY_ENV = "MT_RUNNER_NAMESPACES"

_CLONE_NEWNS = 0x00020000
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000
_MS_BIND = 0x1000
_MS_REC = 0x4000
_MS_PRIVATE = 0x40000


def policy() -> Policy:
    value = (os.environ.get(POLICY_ENV) or "auto").strip().lower()
    if value not in get_args(Policy):
        allowed = ", ".join(get_args(Policy))
        raise ValueError(f"{POLICY_ENV} must be one of {allowed}, not {value!r}")
    return cast(Policy, value)


def _write(path: str, data: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def enter(build_dir: Path, hidden: Path) -> None:
    """Move the calling process into its own namespaces (see the module docstring); `hidden`
    is replaced by a tmpfs that holds only `build_dir`. Raises OSError naming the failed step."""
    if sys.platform != "linux":
        raise OSError(f"Linux namespaces are not available on {sys.platform}")
    libc = ctypes.CDLL(None, use_errno=True)

    def check(result: int, step: str) -> None:
        if result != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"{step}: {os.strerror(errno)}")

    uid, gid = os.getuid(), os.getgid()
    check(libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS | _CLONE_NEWNET), "unshare")
    _write("/proc/self/setgroups", "deny")
    _write("/proc/self/uid_map", f"{uid} {uid} 1")
    _write("/proc/self/gid_map", f"{gid} {gid} 1")
    check(libc.mount(b"none", b"/", None, _MS_REC | _MS_PRIVATE, None), "private mounts")
    # Opened in the new mount namespace: a bind mount needs a source from the caller's own.
    fd = os.open(build_dir, os.O_PATH | os.O_DIRECTORY)
    try:
        options = b"size=1m,mode=0755"
        check(libc.mount(b"tmpfs", os.fsencode(hidden), b"tmpfs", 0, options), "hide session")
        os.makedirs(build_dir, exist_ok=True)
        source = f"/proc/self/fd/{fd}".encode()
        check(libc.mount(source, os.fsencode(build_dir), None, _MS_BIND | _MS_REC, None), "bind")
    finally:
        os.close(fd)


def command(runner: list[str], build_dir: Path, hidden: Path, status_fd: int = -1) -> list[str]:
    """`runner`, started in its own namespaces; `status_fd` gets "ok" or why entering failed."""
    module = "mechatronikers.runner.namespaces"
    status = ["--status-fd", str(status_fd)] if status_fd >= 0 else []
    return [sys.executable, "-m", module, *status, str(build_dir), str(hidden), "--", *runner]


@dataclass
class Jail:
    """One runner start in namespaces, with a pipe on which the trampoline confirms that it
    entered them (or says why not) before it execs the runner, which never sees the pipe."""

    build_dir: Path
    hidden: Path
    _fds: tuple[int, int] = field(default_factory=os.pipe)

    @property
    def pass_fds(self) -> tuple[int]:
        return (self._fds[1],)

    def command(self, runner: list[str]) -> list[str]:
        return command(runner, self.build_dir, self.hidden, self._fds[1])

    def close(self) -> None:
        """Release the pipe, e.g. when the start failed."""
        for fd in self._fds:
            with contextlib.suppress(OSError):
                os.close(fd)

    def outcome(self, timeout_s: float = 30.0) -> str | None:
        """After the start: None once the trampoline confirmed, else the reason it failed."""
        read, write = self._fds
        os.close(write)
        try:
            data = b""
            while select.select([read], [], [], timeout_s)[0]:
                chunk = os.read(read, 4096)
                if not chunk:
                    break
                data += chunk
            else:
                return f"no confirmation from the trampoline within {timeout_s:g} s"
        finally:
            os.close(read)
        text = data.decode(errors="replace")
        if text == "ok":
            return None
        return text or "the trampoline exited without confirming"


@functools.cache
def kernel_support() -> str | None:
    """None if `enter` works here, else why not. Tried once per process, the same way a runner
    is started."""
    if sys.platform != "linux":
        return f"Linux namespaces are not available on {sys.platform}"
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / "builds" / "probe"
        build.mkdir(parents=True)
        proc = subprocess.run(
            command([sys.executable, "-c", "pass"], build, Path(tmp)),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if proc.returncode == 0:
        return None
    lines = proc.stderr.strip().splitlines()
    return lines[-1] if lines else f"probe exited with {proc.returncode}"


def unavailable(hidden: Path) -> str | None:
    """None if a runner can be isolated with `hidden` as its session folder, else why not."""
    reason = kernel_support()
    if reason is not None:
        return reason
    needed = [Path(sys.prefix), Path(sys.base_prefix), Path(mechatronikers.__file__).parent]
    for path in needed:
        if path.resolve().is_relative_to(hidden.resolve()):
            return f"{hidden} holds the Python environment ({path}), it cannot be hidden"
    return None


def main(argv: list[str]) -> int:
    status = -1
    if argv[:1] == ["--status-fd"] and len(argv) > 1:
        status, argv = int(argv[1]), argv[2:]
    if len(argv) < 4 or argv[2] != "--":
        print(
            "usage: python -m mechatronikers.runner.namespaces [--status-fd N] BUILD SESSION -- "
            "CMD...",
            file=sys.stderr,
        )
        return 2
    build_dir, hidden, _, *runner = argv
    try:
        enter(Path(build_dir), Path(hidden))
    except OSError as exc:
        print(f"runner namespaces: {exc}", file=sys.stderr)
        if status >= 0:
            os.write(status, str(exc).encode())
        return 125
    if status >= 0:
        os.write(status, b"ok")
        os.close(status)  # the runner must not be able to confirm anything itself
    os.execv(runner[0], runner)  # no capabilities after this exec (D95)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
