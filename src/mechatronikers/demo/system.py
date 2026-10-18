"""What the demo launcher does to the host, behind one interface so the tests can use a fake.

Child processes run in their own session (process group), so they outlive the launcher and a stop
signal reaches the whole group. A PID is only signalled after its command line was checked
against a marker: PIDs get reused, and `down` must never stop someone else's process.
"""

import contextlib
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class System(Protocol):
    platform: str

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
        capture: bool = True,
    ) -> Result:
        """Run to completion; `capture=False` lets the output through to the terminal."""
        ...

    def spawn(self, cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log: Path) -> int:
        """Start in the background (own session, stdout and stderr to `log`); returns the PID."""
        ...

    def alive(self, pid: int, marker: str) -> bool:
        """The process runs and its command line contains `marker`."""
        ...

    def stop(self, pid: int) -> bool:
        """SIGINT to its group, then SIGTERM, then SIGKILL; True once it is gone."""
        ...

    def port_in_use(self, port: int) -> bool: ...
    def http_status(self, url: str) -> int | None: ...
    def module_available(self, name: str) -> bool: ...
    def which(self, name: str) -> str | None: ...
    def processes(self) -> list[tuple[int, str]]: ...
    def sleep(self, seconds: float) -> None: ...
    def open_browser(self, url: str) -> None: ...


class RealSystem:
    def __init__(self, proc: Path = Path("/proc"), ps: str = "ps") -> None:
        self.platform = sys.platform
        # Command lines come from /proc where it exists (Linux; slim images have no `ps`), else
        # from `ps` (macOS). Neither: unknown, so no process is ever signalled.
        self.proc = proc
        self.ps = ps

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
        capture: bool = True,
    ) -> Result:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed commands built by the launcher
                list(cmd),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                capture_output=capture,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            return Result(127, "", f"{cmd[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{cmd[0]}: no answer after {timeout:.0f} s")
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")

    def spawn(self, cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Logs can hold review links (with their token): only this user may read them.
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as out:
            proc = subprocess.Popen(  # noqa: S603 - fixed commands built by the launcher
                list(cmd),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return proc.pid

    @staticmethod
    def _exists(pid: int) -> bool:
        # A child of this very process stays a zombie until reaped; kill(pid, 0) would see it.
        with contextlib.suppress(ChildProcessError):
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, but belongs to another user
        return True

    def _ps(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                [self.ps, *args], capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def _command_line(self, pid: int) -> str:
        """The command line of `pid`, "" if it is gone or cannot be read."""
        if self.proc.is_dir():
            try:
                raw = (self.proc / str(pid) / "cmdline").read_bytes()
            except OSError:
                return ""
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
        return self._ps("-o", "command=", "-p", str(pid)).strip()

    def alive(self, pid: int, marker: str) -> bool:
        return pid > 1 and self._exists(pid) and marker in self._command_line(pid)

    def stop(self, pid: int) -> bool:
        for sig, wait_s in ((signal.SIGINT, 10.0), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                if not self._exists(pid):
                    return True
                time.sleep(0.2)
        return not self._exists(pid)

    def port_in_use(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def http_status(self, url: str) -> int | None:
        try:
            return httpx.get(url, timeout=2).status_code
        except httpx.HTTPError:
            return None

    def module_available(self, name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def processes(self) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        if self.proc.is_dir():
            for entry in self.proc.iterdir():
                if entry.name.isdigit() and (command := self._command_line(int(entry.name))):
                    rows.append((int(entry.name), command))
            return rows
        for line in self._ps("-axo", "pid=,command=").splitlines():
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit():
                rows.append((int(pid), command.strip()))
        return rows

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def open_browser(self, url: str) -> None:
        webbrowser.open(url)
