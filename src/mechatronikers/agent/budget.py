"""Budget guard: no model call starts once the run or total limit is reached (docs/08 events).

The spend ledger (`var/agent-spend.json`: `{"total": credits, "calls": n}`) is shared by all runs
on the host, and runs are concurrent (jobs adapter, parallel bench runs, hub workers). Every read
and every read-modify-write holds an `flock` on a sibling `.lock` file, and a write replaces the
ledger atomically (temp file in the same directory, fsync, `os.replace`), so no process loses a
record or reads a partial file. Each write also keeps the same content as `.bak`. A ledger that
cannot be read (e.g. written by an older version without the lock) is restored from that backup
with a warning; without a usable backup no paid call starts, since guessing 0 could pass
`MT_BUDGET_TOTAL` (D101).
"""

import contextlib
import fcntl
import json
import math
import os
import tempfile
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import httpx

    from mechatronikers.agent.config import AgentConfig


class BudgetExceededError(RuntimeError):
    pass


class LedgerUnreadableError(BudgetExceededError):
    """Neither the ledger nor its backup can be read: no paid call starts until a person checks
    the file. A budget stop, so a run ends cleanly (`budget_exceeded`) instead of crashing."""


@dataclass(frozen=True)
class LedgerState:
    total: float  # credits recorded by all runs
    calls: int


@dataclass(frozen=True)
class LedgerIssue:
    """A problem with the ledger file; `ModelClient` emits it as an event (docs/08)."""

    type: Literal["budget.ledger_recovered", "budget.ledger_unreadable"]
    level: Literal["warning", "error"]
    payload: dict[str, Any]


def _parse(path: Path) -> dict[str, Any]:
    """The ledger content. Raises OSError or ValueError (incl. a JSON error) if it is unusable."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    total, calls = data.get("total", 0.0), data.get("calls", 0)
    if isinstance(total, bool) or not isinstance(total, int | float):
        raise ValueError(f"total is not a number: {total!r}")
    if not math.isfinite(total) or total < 0:
        raise ValueError(f"total out of range: {total!r}")
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        raise ValueError(f"calls is not a count: {calls!r}")
    return data


def _state(data: dict[str, Any]) -> LedgerState:
    return LedgerState(total=float(data.get("total", 0.0)), calls=int(data.get("calls", 0)))


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _replace(target: Path, text: str) -> None:
    """Write `text` to `target` atomically: readers see the old or the new content, never a part."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _fsync_dir(path: Path) -> None:
    """Make the renames durable; not every platform can fsync a directory."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class SpendLedger:
    """The ledger file of all runs on this host, safe for concurrent processes and threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.backup = path.with_name(path.name + ".bak")
        self.lock_path = path.with_name(path.name + ".lock")  # never deleted: flock needs it

    @contextlib.contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)  # releases the lock

    def _load(self) -> tuple[dict[str, Any] | None, LedgerIssue | None]:
        """Content under the lock: {} for a new (or deliberately deleted) ledger, the backup with
        a warning for an unreadable one, None with an error if the backup is unusable too."""
        try:
            return _parse(self.path), None
        except FileNotFoundError:  # writes are atomic, so a missing ledger was never written
            return {}, None
        except (OSError, ValueError) as exc:
            error = _error(exc)
        payload: dict[str, Any] = {"ledger": str(self.path), "error": error}
        try:
            data = _parse(self.backup)
        except (OSError, ValueError) as exc:
            payload["backup_error"] = _error(exc)
            return None, LedgerIssue("budget.ledger_unreadable", "error", payload)
        state = _state(data)
        payload |= {"backup": str(self.backup), "total": state.total, "calls": state.calls}
        return data, LedgerIssue("budget.ledger_recovered", "warning", payload)

    def read(self) -> tuple[LedgerState | None, LedgerIssue | None]:
        """The recorded spend, or None if neither the ledger nor its backup can be read."""
        if not self.path.parent.is_dir():  # nothing recorded yet; a read creates no folder
            return LedgerState(0.0, 0), None
        with self._locked(exclusive=False):
            data, issue = self._load()
        return (None if data is None else _state(data)), issue

    def add(self, cost: float) -> tuple[LedgerState | None, LedgerIssue | None]:
        """Record one call. An unreadable ledger without a usable backup is left as it is for a
        person to check (None): the cost is not added to a guessed total."""
        with self._locked(exclusive=True):
            data, issue = self._load()
            if data is None:
                assert issue is not None
                issue.payload["unrecorded_cost"] = cost
                return None, issue
            data["total"] = float(data.get("total", 0.0)) + cost
            data["calls"] = int(data.get("calls", 0)) + 1
            text = json.dumps(data, indent=2)
            _replace(self.path, text)
            _replace(self.backup, text)
            _fsync_dir(self.path.parent)
            return _state(data), issue


class BudgetGuard:
    """Tracks spend per run and across runs (ledger file). Costs are provider credits."""

    def __init__(self, run_limit: float, total_limit: float, ledger: Path) -> None:
        self.run_limit = run_limit
        self.total_limit = total_limit
        self.ledger = ledger
        self.store = SpendLedger(ledger)
        self.run_spent = 0.0
        self.calls = 0
        self._lock = threading.Lock()
        self._issues: deque[LedgerIssue] = deque()

    def _unreadable(self) -> LedgerUnreadableError:
        return LedgerUnreadableError(
            f"spend ledger {self.ledger} cannot be read and has no usable backup "
            f"({self.store.backup.name}): no paid call starts. Check the file (compare "
            "`agent spend --remote`), then correct its total or delete it to start from 0."
        )

    def total_spent(self) -> float:
        """Credits recorded by all runs. Raises LedgerUnreadableError instead of guessing."""
        state, issue = self.store.read()
        if issue is not None:
            self._issues.append(issue)
        if state is None:
            raise self._unreadable()
        return state.total

    def check(self) -> None:
        """Raise before a call if a limit is already reached or the ledger cannot be read."""
        if self.run_spent >= self.run_limit:
            raise BudgetExceededError(
                f"run budget reached: {self.run_spent:.4f} of {self.run_limit:.4f} credits"
            )
        total = self.total_spent()
        if total >= self.total_limit:
            raise BudgetExceededError(
                f"total budget reached: {total:.4f} of {self.total_limit:.4f} credits"
            )

    def record(self, cost: float) -> None:
        """Count a finished call. Never raises for an unreadable ledger: the call is paid already,
        so the issue is reported and the next `check()` refuses."""
        with self._lock:
            self.run_spent += cost
            self.calls += 1
            _, issue = self.store.add(cost)
        if issue is not None:
            self._issues.append(issue)

    def take_issues(self) -> list[LedgerIssue]:
        """Ledger issues since the last call, each reported once."""
        issues: list[LedgerIssue] = []
        while self._issues:
            try:
                issues.append(self._issues.popleft())
            except IndexError:  # taken by another thread meanwhile
                break
        return issues


def openrouter_usage(
    config: "AgentConfig", transport: "httpx.BaseTransport | None" = None
) -> tuple[float, float]:
    """(credits bought, credits used) of the OpenRouter account behind the configured key, from
    `GET /credits`, to reconcile the ledger with what the provider charged. The key is sent only
    to OpenRouter and never printed."""
    import httpx

    if not config.openrouter:
        raise ValueError("--remote reads OpenRouter's /credits; the configured provider is not it")
    if not config.api_key:
        raise ValueError("OPENROUTER_API_KEY is not set (.env)")
    with httpx.Client(transport=transport, timeout=15) as client:
        response = client.get(
            f"{config.base_url}/credits", headers={"Authorization": f"Bearer {config.api_key}"}
        )
    response.raise_for_status()
    data = response.json()["data"]
    return float(data["total_credits"]), float(data["total_usage"])
