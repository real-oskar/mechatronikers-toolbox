"""Starts the build runner in isolation and collects its result (docs/05).

Isolation modes:
- "container": separate Podman container, `--network none`, read-only root, CPU/memory/PID limits.
- "subprocess": child process with time and resource limits, in its own user, network and mount
  namespaces (D95, `runner.namespaces`): no network at all, and of the session folder it sees only
  its build directory. Use this mode inside the sandbox. Where the kernel does not allow the
  namespaces (macOS, some container setups), `MT_RUNNER_NAMESPACES` decides: `required` refuses
  the build, `auto` runs it with a warning event.
"""

import contextlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from pydantic import ValidationError

from mechatronikers.runner import namespaces
from mechatronikers.runner.worker import failure_report
from mechatronikers.spec import BuildJob, BuildResult
from mechatronikers.telemetry import Event, EventEmitter

Isolation = Literal["container", "subprocess"]

DEFAULT_IMAGE = os.environ.get("MT_IMAGE", "localhost/mechatronikers:dev")
MEMORY_LIMIT_BYTES = 2 * 1024**3
CPU_LIMIT = 2
MAX_LINE_CHARS = 4000  # output lines are cut to this length in runner.stdout/stderr events
MAX_EVENT_CHARS = 64 * 1024  # longer lines are never passed through, and not held in memory


def _limit_resources(timeout_s: float) -> Callable[[], None]:
    def apply() -> None:
        os.setsid()  # own process group, so a timeout kills every child
        for limit, value in (
            (resource.RLIMIT_CPU, int(timeout_s) + 5),
            (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES),
        ):
            # Not enforceable on every platform (e.g. RLIMIT_AS on macOS).
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(limit, (value, value))

    return apply


@dataclass(frozen=True)
class RunnerOrigin:
    """What the launcher started the runner with; its own events must carry exactly this."""

    emitter_id: str
    build_id: str
    session_id: str | None
    run_id: str | None


def is_runner_event(line: str, origin: RunnerOrigin) -> bool:
    """True for the runner's own events of this build (docs/08, Provenance).

    The build script runs in the runner process and prints to the same stdout, so a line that
    parses as an event proves nothing: it must come from the emitter id the launcher assigned, for
    this build, session and run, and be a `build.*` event.
    """
    try:
        event = Event.model_validate(json.loads(line))
    except (ValueError, ValidationError, RecursionError):
        return False
    return (
        event.emitter_id == origin.emitter_id
        and event.payload.get("build_id") == origin.build_id
        and (event.session_id or None) == origin.session_id
        and (event.run_id or None) == origin.run_id
        and event.type.startswith("build.")
    )


def _lines(pipe: IO[str]) -> Iterator[tuple[str, bool]]:
    """Output lines without the newline, and whether the line was cut: the rest of a line longer
    than MAX_EVENT_CHARS is skipped, so a build script cannot fill the sandbox app's memory."""
    while chunk := pipe.readline(MAX_EVENT_CHARS + 1):
        cut = not chunk.endswith("\n") and len(chunk) > MAX_EVENT_CHARS
        if cut:
            while (rest := pipe.readline(MAX_EVENT_CHARS)) and not rest.endswith("\n"):
                pass
        yield chunk.rstrip("\n"), cut


def _forward(
    pipe: IO[str],
    emitter: EventEmitter,
    origin: RunnerOrigin,
    stream_name: str,
    tail: list[str] | None = None,
) -> None:
    """Pass the runner's own events through unchanged; wrap any other output as events.
    `tail` keeps the last line, for the error of a runner that wrote no result."""
    out = emitter.stream
    for line, cut in _lines(pipe):
        if not line:
            continue
        if tail is not None:
            tail[:] = [line[:MAX_LINE_CHARS]]
        if stream_name == "stdout" and not cut and is_runner_event(line, origin):
            out.write(line + "\n")
            out.flush()
            continue
        payload: dict[str, object] = {"build_id": origin.build_id, "line": line[:MAX_LINE_CHARS]}
        if cut or len(line) > MAX_LINE_CHARS:
            payload["truncated"] = True
        emitter.emit(
            f"runner.{stream_name}",
            payload,
            level="warning" if stream_name == "stderr" else "debug",
        )


def container_command(
    job_dir: Path,
    out_dir: Path,
    name: str,
    image: str,
    env: dict[str, str],
    *,
    emitter_id: str | None = None,
) -> list[str]:
    cmd = [
        shutil.which("podman") or "podman",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
        "--memory",
        str(MEMORY_LIMIT_BYTES),
        "--cpus",
        str(CPU_LIMIT),
        "--pids-limit",
        "256",
        "--cap-drop",
        "all",
        "--security-opt",
        "no-new-privileges",
        "--label",
        "mt.role=runner",
        "-v",
        f"{job_dir}:/job:ro,Z",
        "-v",
        f"{out_dir}:/out:rw,Z",
    ]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    return [
        *cmd,
        image,
        "python",
        "-m",
        "mechatronikers.runner",
        "--job",
        "/job/job.json",
        "--out",
        "/out",
        *(["--emitter-id", emitter_id] if emitter_id else []),
    ]


def run_build(
    job: BuildJob,
    work_dir: Path,
    *,
    isolation: Isolation = "subprocess",
    emitter: EventEmitter | None = None,
    image: str = DEFAULT_IMAGE,
) -> BuildResult:
    """Run `job` isolated; outputs land in `work_dir/<build_id>/out`."""
    emitter = emitter or EventEmitter("build-launcher")
    build_dir = (work_dir / job.build_id).resolve()
    job_dir, out_dir = build_dir / "job", build_dir / "out"
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(job.model_dump_json())
    env = {k: v for k in ("MT_SESSION_ID", "MT_RUN_ID") if (v := os.environ.get(k) or "")}
    if emitter.session_id:
        env["MT_SESSION_ID"] = emitter.session_id
    origin = RunnerOrigin(
        emitter_id=uuid.uuid4().hex[:12],
        build_id=job.build_id,
        session_id=env.get("MT_SESSION_ID"),
        run_id=env.get("MT_RUN_ID"),
    )

    name = f"mt-runner-{job.build_id}"
    jail: namespaces.Jail | None = None
    if isolation == "container":
        cmd = container_command(job_dir, out_dir, name, image, env, emitter_id=origin.emitter_id)
        preexec = None
    else:
        cmd = [sys.executable, "-m", "mechatronikers.runner", "--job", str(job_dir / "job.json")]
        cmd += ["--out", str(out_dir), "--emitter-id", origin.emitter_id]
        preexec = _limit_resources(job.timeout_s)
        cmd, jail, refused = _isolate(cmd, job, work_dir, emitter)
        if refused is not None:
            return refused

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",  # a build script may write any bytes
            env={**os.environ, **env},
            preexec_fn=preexec,  # noqa: PLW1509 - limits must apply before exec
            pass_fds=jail.pass_fds if jail else (),
        )
    except BaseException:
        if jail is not None:
            jail.close()
        raise
    jail_failed = _confirm(jail, job, emitter)
    assert proc.stdout is not None and proc.stderr is not None
    stderr_tail: list[str] = []
    readers = [
        threading.Thread(target=_forward, args=(proc.stdout, emitter, origin, "stdout")),
        threading.Thread(
            target=_forward, args=(proc.stderr, emitter, origin, "stderr", stderr_tail)
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=job.timeout_s)
    except subprocess.TimeoutExpired:
        if isolation == "container":
            subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        for reader in readers:
            reader.join()
        duration = (time.monotonic() - start) * 1000
        detail = f"killed after {job.timeout_s:g} s time limit"
        emitter.emit(
            "build.timeout", {"build_id": job.build_id}, level="error", duration_ms=duration
        )
        return BuildResult(
            build_id=job.build_id,
            status="timeout",
            error=detail,
            duration_ms=duration,
            report=failure_report(job.build_id, "V0.timeout", detail),
        )
    for reader in readers:
        reader.join()
    if jail_failed is not None:
        return jail_failed

    result_file = out_dir / "result.json"
    if result_file.is_file():
        return BuildResult.model_validate_json(result_file.read_text())
    detail = f"runner exited with code {proc.returncode} without a result"
    if stderr_tail:
        detail += f": {stderr_tail[-1][:300]}"
        if "--emitter-id" in stderr_tail[-1]:
            detail += " (the runner image predates --emitter-id; rebuild it)"
    return BuildResult(
        build_id=job.build_id,
        status="failed",
        error=detail,
        duration_ms=(time.monotonic() - start) * 1000,
        report=failure_report(job.build_id, "V0.error", detail),
    )


# Builds that ran without namespaces, per builds folder as the caller configured it: the review
# API marks every gate state of such a session (D95), and the frontend and the agent warn about
# it. Kept in this process and keyed by the unresolved path, never derived from the filesystem:
# such a build can write the session volume, e.g. swap `builds/` for a symlink.
_UNISOLATED: dict[Path, set[str]] = {}


def unisolated_builds(work_dir: Path) -> int:
    """How many builds in `work_dir` ran without runner namespaces since this process started."""
    return len(_UNISOLATED.get(work_dir, ()))


def _refused(job: BuildJob, detail: str) -> BuildResult:
    return BuildResult(
        build_id=job.build_id,
        status="failed",
        error=detail,
        duration_ms=0,
        report=failure_report(job.build_id, "V0.error", detail),
    )


def _isolate(
    cmd: list[str], job: BuildJob, work_dir: Path, emitter: EventEmitter
) -> tuple[list[str], namespaces.Jail | None, BuildResult | None]:
    """Subprocess mode: the runner command in its own namespaces (D95), else (policy `auto`) the
    plain command with a warning, or the refusal of the build (policy `required`)."""
    # Lexical, not resolved: an earlier build may have swapped builds/ for a symlink, and the
    # session folder hidden from this one must stay the configured one.
    builds = Path(os.path.abspath(work_dir))
    hidden = builds.parent  # the session folder: builds/, input/, review/, artifacts/
    mode = namespaces.policy()
    reason = "turned off" if mode == "off" else namespaces.unavailable(hidden)
    if reason is None:
        jail = namespaces.Jail(builds / job.build_id, hidden)
        return jail.command(cmd), jail, None
    payload = {"build_id": job.build_id, "reason": reason, "policy": mode}
    if mode != "required":
        emitter.emit("runner.isolation_unavailable", payload, level="warning")
        _UNISOLATED.setdefault(work_dir, set()).add(job.build_id)
        return cmd, None, None
    emitter.emit("runner.isolation_unavailable", payload, level="error")
    detail = f"build refused: runner namespaces are required but unavailable ({reason})"
    return cmd, None, _refused(job, detail)


def _confirm(
    jail: namespaces.Jail | None, job: BuildJob, emitter: EventEmitter
) -> BuildResult | None:
    """The trampoline's report, as an event: `runner.isolated`, or the failure of a build whose
    namespaces passed the probe but could not be entered (no script ran)."""
    if jail is None:
        return None
    reason = jail.outcome()
    if reason is None:
        namespaces_ = ["user", "network", "mount"]
        emitter.emit("runner.isolated", {"build_id": job.build_id, "namespaces": namespaces_})
        return None
    payload = {"build_id": job.build_id, "reason": reason, "policy": namespaces.policy()}
    emitter.emit("runner.isolation_unavailable", payload, level="error")
    return _refused(job, f"build failed: the runner namespaces could not be entered ({reason})")
