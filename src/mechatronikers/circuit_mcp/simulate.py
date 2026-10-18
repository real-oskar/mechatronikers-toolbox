"""ngspice operating point of an exported netlist (docs/12, D71).

ngspice runs as its own process with a time and memory limit, inside the offline session
container, on a netlist circuit-mcp wrote and validated itself: no `.control`, `.include` or
`.lib` reaches it. A circuit with parts left out of the netlist is not simulated at all, because
its voltages would look plausible and be wrong.
"""

import contextlib
import os
import re
import resource
import shutil
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

TIMEOUT_S = 20.0
MEMORY_LIMIT_BYTES = 1024**3
_ANALYSIS = re.compile(r"^\.(ac|dc|tran|op|noise|tf)\b", re.IGNORECASE)
_ERROR = re.compile(r"error|unknown (model|subckt)|could not find|unable to find", re.IGNORECASE)


class SimulationResult(BaseModel):
    ok: bool
    reason: str = ""
    node_voltages: dict[str, float] = Field(default_factory=dict)
    source_currents: dict[str, float] = Field(default_factory=dict)
    log_tail: str = ""


def op_netlist(cir: str) -> str:
    """The netlist with its analyses replaced by one operating point."""
    keep = [
        line
        for line in cir.splitlines()
        if not _ANALYSIS.match(line) and line.strip().lower() != ".end"
    ]
    return "\n".join([*keep, ".op", ".end"]) + "\n"


def parse_op(output: str) -> tuple[dict[str, float], dict[str, float]]:
    """Node voltages and source currents from ngspice's batch operating point tables."""
    voltages: dict[str, float] = {}
    currents: dict[str, float] = {}
    table: dict[str, float] | None = None
    for line in output.splitlines():
        words = line.split()
        lower = [w.lower() for w in words]
        if lower[:2] == ["node", "voltage"]:
            table = voltages
        elif lower[:2] == ["source", "current"]:
            table = currents
        elif table is not None and len(words) == 2 and not words[0].startswith("-"):
            try:
                table[words[0]] = float(words[1])
            except ValueError:
                table = None
        elif words and not words[0].startswith("-"):
            table = None
    return voltages, currents


def error_lines(output: str, limit: int = 3) -> list[str]:
    """ngspice's error lines with what they refer to: "Error on line 5 or its substitute:" is
    followed by the netlist line and then the cause, so a line ending in ":" takes the next one
    along (#152)."""
    lines = [line.strip() for line in output.splitlines()]
    found: list[str] = []
    i = 0
    while i < len(lines) and len(found) < limit:
        if _ERROR.search(lines[i]):
            text = lines[i]
            while text.endswith(":") and i + 1 < len(lines) and lines[i + 1]:
                i += 1
                text = f"{text} {lines[i]}"
            found.append(text)
        i += 1
    return found


def _limits(timeout_s: float) -> Callable[[], None]:
    def apply() -> None:
        os.setsid()  # own process group, so a timeout kills every child
        for limit, value in (
            (resource.RLIMIT_CPU, int(timeout_s) + 5),
            (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES),
        ):
            with contextlib.suppress(ValueError, OSError):  # RLIMIT_AS is not enforced on macOS
                resource.setrlimit(limit, (value, value))

    return apply


def run_op(cir: str, work_dir: Path, timeout_s: float = TIMEOUT_S) -> SimulationResult:
    binary = shutil.which("ngspice")
    if binary is None:
        reason = "ngspice is not installed (the sandbox image has it)"
        return SimulationResult(ok=False, reason=reason)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "op.cir").write_text(op_netlist(cir))
    proc = subprocess.Popen(
        [binary, "-b", "op.cir"],
        cwd=work_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(work_dir)},
        preexec_fn=_limits(timeout_s),  # noqa: PLW1509 - limits must apply before exec
    )
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return SimulationResult(ok=False, reason=f"ngspice killed after {timeout_s:g} s")
    tail = "\n".join(output.splitlines()[-30:])
    (work_dir / "op.log").write_text(output)
    voltages, currents = parse_op(output)
    errors = error_lines(output)
    if errors or not voltages:
        reason = " ".join(errors) if errors else f"no operating point (exit code {proc.returncode})"
        return SimulationResult(ok=False, reason=reason[:300], log_tail=tail)
    return SimulationResult(
        ok=True, node_voltages=voltages, source_currents=currents, log_tail=tail
    )
