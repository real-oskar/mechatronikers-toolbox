"""Concurrent runs that share one spend ledger, each in its own process (#134).

A separate light module: spawned processes import only this and the budget guard."""

import multiprocessing
from pathlib import Path
from typing import Any

from mechatronikers.agent.budget import BudgetGuard


def cost(worker: int) -> float:
    return (worker + 1) / 1024  # binary fractions: the sum is exact in any order


def _record_many(ledger: str, worker: int, records: int, start: Any) -> None:
    """One run: a budget check and a recorded call, `records` times."""
    guard = BudgetGuard(1e9, 1e9, Path(ledger))
    start.wait()  # all processes write at the same time
    for _ in range(records):
        guard.check()
        guard.record(cost(worker))
    assert not guard.take_issues()


class Writers:
    """`processes` runs recording `records` calls each into `ledger`, started together."""

    def __init__(self, ledger: Path, processes: int, records: int) -> None:
        ctx = multiprocessing.get_context("spawn")  # no fork of a threaded pytest process
        # kept on self: the semaphore must outlive the children's start (spawn unpickles it)
        self.start = ctx.Barrier(processes)
        self.procs = [
            ctx.Process(target=_record_many, args=(str(ledger), w, records, self.start))
            for w in range(processes)
        ]
        for p in self.procs:
            p.start()

    def alive(self) -> bool:
        return any(p.is_alive() for p in self.procs)

    def join(self) -> None:
        for p in self.procs:
            p.join(timeout=120)
        assert [p.exitcode for p in self.procs] == [0] * len(self.procs)
