"""Spend ledger shared by concurrent runs (#134): locked, atomic, recoverable. No network."""

import asyncio
import io
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from mechatronikers.agent.budget import (
    BudgetExceededError,
    BudgetGuard,
    LedgerUnreadableError,
    openrouter_usage,
)
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.llm import ModelClient, text_block
from mechatronikers.telemetry import EventEmitter
from tests.fixtures.ledger import Writers, cost

PROCESSES, RECORDS = 8, 200


def test_concurrent_processes_keep_every_record_and_the_exact_total(tmp_path: Path) -> None:
    ledger = tmp_path / "var" / "agent-spend.json"
    Writers(ledger, PROCESSES, RECORDS).join()
    data = json.loads(ledger.read_text())
    assert data["calls"] == PROCESSES * RECORDS
    assert data["total"] == RECORDS * sum(cost(w) for w in range(PROCESSES))
    assert json.loads((ledger.parent / "agent-spend.json.bak").read_text()) == data
    files = {f.name for f in ledger.parent.iterdir()}  # no temp file left behind
    assert files == {"agent-spend.json", "agent-spend.json.bak", "agent-spend.json.lock"}


def test_check_never_raises_during_concurrent_writes(tmp_path: Path) -> None:
    ledger = tmp_path / "agent-spend.json"
    writers = Writers(ledger, 4, 100)
    guard = BudgetGuard(1.0, 1e9, ledger)
    seen = [0.0]
    deadline = time.monotonic() + 120
    while writers.alive() and time.monotonic() < deadline:
        guard.check()  # never a JSON error, never a partial file
        seen.append(guard.total_spent())
    writers.join()
    assert seen == sorted(seen)  # the total only grows: no lost or half-read records
    assert guard.total_spent() == 100 * sum(cost(w) for w in range(4))
    assert not guard.take_issues()


@pytest.mark.parametrize("content", ["", '{"total": 0.5, "ca', "[]", '{"total": "x"}'])
def test_a_corrupt_ledger_is_restored_from_its_backup(tmp_path: Path, content: str) -> None:
    ledger = tmp_path / "agent-spend.json"
    guard = BudgetGuard(5.0, 2.0, ledger)
    guard.record(0.25)
    guard.record(0.5)
    ledger.write_text(content)  # e.g. a partial write by an older version without the lock
    guard.check()  # the budget check survives
    assert guard.total_spent() == 0.75
    issues = guard.take_issues()
    assert {i.type for i in issues} == {"budget.ledger_recovered"}
    assert issues[0].level == "warning" and issues[0].payload["total"] == 0.75
    guard.record(0.25)  # repairs the ledger
    assert json.loads(ledger.read_text()) == {"total": 1.0, "calls": 3}
    assert {i.type for i in guard.take_issues()} == {"budget.ledger_recovered"}
    guard.check()
    assert not guard.take_issues()  # repaired: no more warnings


def test_an_unreadable_ledger_without_backup_refuses_paid_calls(tmp_path: Path) -> None:
    ledger = tmp_path / "agent-spend.json"
    ledger.write_text('{"total": 3.')
    guard = BudgetGuard(1.0, 5.0, ledger)
    with pytest.raises(LedgerUnreadableError, match="no paid call starts"):
        guard.check()
    assert issubclass(LedgerUnreadableError, BudgetExceededError)  # a run ends cleanly
    guard.record(0.1)  # a call already paid is counted for the run, never added to a guess
    assert guard.run_spent == 0.1 and ledger.read_text() == '{"total": 3.'
    issues = guard.take_issues()
    assert [i.type for i in issues] == ["budget.ledger_unreadable"] * 2
    assert issues[0].level == "error" and issues[1].payload["unrecorded_cost"] == 0.1
    assert not (tmp_path / "agent-spend.json.bak").exists()


def test_old_ledgers_stay_readable_and_a_deleted_one_starts_from_zero(tmp_path: Path) -> None:
    ledger = tmp_path / "var" / "agent-spend.json"
    guard = BudgetGuard(1.0, 5.0, ledger)
    assert guard.total_spent() == 0.0 and not ledger.parent.exists()  # a read creates nothing
    ledger.parent.mkdir()
    ledger.write_text(json.dumps({"total": 1.25, "calls": 3, "note": "kept"}))  # before #134
    assert guard.total_spent() == 1.25
    guard.record(0.25)
    assert json.loads(ledger.read_text()) == {"total": 1.5, "calls": 4, "note": "kept"}
    ledger.unlink()  # deleting the ledger resets it, the stale backup is not used
    assert guard.total_spent() == 0.0
    assert not guard.take_issues()


def _client(ledger: Path, events: io.StringIO) -> ModelClient:
    config = AgentConfig(model="fake", ledger=ledger)
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    answer = AIMessage("ok", response_metadata={"token_usage": {"cost": 0.125}})
    model = GenericFakeChatModel(messages=iter([answer]))
    emitter = EventEmitter("agent", session_id="ses-ledger", run_id="run-l1", stream=events)
    return ModelClient(model, config, guard, emitter)


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_model_client_emits_ledger_events(tmp_path: Path) -> None:
    ledger = tmp_path / "agent-spend.json"
    BudgetGuard(1.0, 5.0, ledger).record(0.5)
    ledger.write_text("{")
    events = io.StringIO()
    assert asyncio.run(_client(ledger, events).ask("read_labels", "sys", [text_block("x")])) == "ok"
    recovered = [e for e in _events(events) if e["type"] == "budget.ledger_recovered"]
    assert recovered and all(e["level"] == "warning" for e in recovered)
    assert recovered[0]["payload"]["total"] == 0.5
    assert json.loads(ledger.read_text()) == {"total": 0.625, "calls": 2}

    (tmp_path / "agent-spend.json.bak").unlink()
    ledger.write_text("{")
    events = io.StringIO()
    with pytest.raises(LedgerUnreadableError):
        asyncio.run(_client(ledger, events).ask("read_labels", "sys", [text_block("x")]))
    types = [(e["type"], e["level"]) for e in _events(events)]
    assert types == [("budget.ledger_unreadable", "error")]  # no model call was made


def _credits(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_credits": 30.0, "total_usage": 2.5}})

    return httpx.MockTransport(handler)


def test_openrouter_usage_reads_the_credits_endpoint(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    config = AgentConfig(model="fake", api_key="sk-or-test", ledger=tmp_path / "s.json")
    assert openrouter_usage(config, _credits(seen)) == (30.0, 2.5)
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/credits"
    assert seen[0].headers["authorization"] == "Bearer sk-or-test"
    other = config.model_copy(update={"model_provider": "anthropic", "base_url": None})
    with pytest.raises(ValueError, match="not it"):
        openrouter_usage(other, _credits(seen))  # the key goes to OpenRouter only
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        openrouter_usage(config.model_copy(update={"api_key": ""}), _credits(seen))
    assert len(seen) == 1


def test_spend_command_shows_ledger_and_remote_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mechatronikers.agent import __main__ as cli

    ledger = tmp_path / "agent-spend.json"
    BudgetGuard(1.0, 5.0, ledger).record(0.75)
    config = AgentConfig(model="fake", api_key="sk-or-secret", ledger=ledger)
    monkeypatch.setattr(cli.AgentConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli, "openrouter_usage", lambda c: openrouter_usage(c, _credits([])))
    assert cli.main(["spend", "--remote"]) == 0
    out = capsys.readouterr()
    assert "0.7500 of 5.00 credits spent" in out.out
    assert "OpenRouter: 2.5000 of 30.00 credits used" in out.out
    assert "sk-or-secret" not in out.out + out.err
    ledger.write_text("")
    (tmp_path / "agent-spend.json.bak").unlink()
    assert cli.main(["spend"]) == 1
    assert "no paid call starts" in capsys.readouterr().err
