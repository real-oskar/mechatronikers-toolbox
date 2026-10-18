"""Contract of `RunRepository` (storage.repositories), run against every implementation, and the
run table built from the repository interfaces."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.session.manager import gateway_token
from mechatronikers.session.runs import run_table
from mechatronikers.session.store import SessionRecord, SessionStore
from mechatronikers.spec.review import RunSummary
from mechatronikers.storage import EventRepository, RunRepository
from mechatronikers.telemetry import Event
from mechatronikers.telemetry.store import EventStore
from tests.fixtures.stores import (
    EVENT_REPOSITORIES,
    RUN_REPOSITORIES,
    TOKENLESS_RUN_REPOSITORIES,
    Factory,
)

FIXTURE = Path(__file__).parent / "fixtures" / "events" / "fuss-run.ndjson"
Record = Callable[..., SessionRecord]


@pytest.fixture(params=RUN_REPOSITORIES)
def kind(request: pytest.FixtureRequest) -> Factory:
    factory: Factory = request.param
    return factory


@pytest.fixture
def repo(kind: Factory, tmp_path: Path) -> Iterator[RunRepository]:
    with kind(tmp_path / "runs") as store:
        yield store


@pytest.fixture
def record(kind: Factory) -> Record:
    """Records as this implementation holds them: with the gateway token in mode 1, without it
    in the hub (docs/13 §5)."""
    tokenless = kind in TOKENLESS_RUN_REPOSITORIES

    def make(id: str, created_at: str, **extra: Any) -> SessionRecord:
        stored = _record(id, created_at, **extra)
        return stored.model_copy(update={"token": None}) if tokenless else stored

    return make


@pytest.fixture(params=EVENT_REPOSITORIES)
def events(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[EventRepository]:
    factory: Factory = request.param
    with factory(tmp_path / "events") as store:
        yield store


def _record(id: str, created_at: str, **extra: Any) -> SessionRecord:
    fields: dict[str, Any] = {
        "id": id,
        "state": "running",
        "token": f"tok-{id}",
        "created_at": created_at,
        "updated_at": created_at,
        "image": "localhost/mechatronikers:dev",
    }
    return SessionRecord.model_validate(fields | extra)


def test_save_and_get_round_trip(repo: RunRepository, record: Record) -> None:
    full = record(
        "ses-a",
        "2026-10-17T08:00:00.000000Z",
        state="shelved",
        port=40000,
        retention_class="audit",
        consent="eval_only",
        last_reminder_at="2026-10-17T08:15:00.000000Z",
        snapshot_id="snap-1",
        expires_at="2027-10-17T08:00:00Z",
    )
    repo.save(full)
    repo.save(record("ses-b", "2026-10-17T09:00:00.000000Z"))
    assert repo.get("ses-a") == full
    assert repo.get("ses-b") == record("ses-b", "2026-10-17T09:00:00.000000Z")


def test_unknown_run_is_a_key_error(repo: RunRepository) -> None:
    with pytest.raises(KeyError):
        repo.get("ses-unknown")


def test_save_replaces_the_record(repo: RunRepository, record: Record) -> None:
    stored = record("ses-a", "2026-10-17T08:00:00.000000Z")
    repo.save(stored)
    stored.state, stored.port = "awaiting_review", 40001
    repo.save(stored)
    assert repo.get("ses-a").state == "awaiting_review" and repo.get("ses-a").port == 40001
    assert [r.id for r in repo.all()] == ["ses-a"]


def test_all_is_oldest_first_and_filters_by_state(repo: RunRepository, record: Record) -> None:
    assert repo.all() == []
    repo.save(record("ses-c", "2026-10-17T10:00:00.000000Z", state="shelved"))
    repo.save(record("ses-a", "2026-10-17T08:00:00.000000Z"))
    repo.save(record("ses-b", "2026-10-17T09:00:00.000000Z", state="awaiting_review"))
    assert [r.id for r in repo.all()] == ["ses-a", "ses-b", "ses-c"]
    active = repo.all(("running", "awaiting_review"))
    assert [r.id for r in active] == ["ses-a", "ses-b"]
    assert repo.all(("failed",)) == []


def test_run_table_reads_only_the_repositories(
    repo: RunRepository, events: EventRepository, record: Record
) -> None:
    for line in FIXTURE.read_text().splitlines():
        events.add(Event.model_validate(json.loads(line)))
    repo.save(record("ses-fixture0001", "2026-10-17T08:00:00.000000Z", port=40000))
    repo.save(record("ses-new", "2026-10-17T09:00:00.000000Z", state="created"))
    rows = run_table(repo, events, lambda sid: f"http://127.0.0.1:40000/?t={sid}")
    assert [r.session_id for r in rows] == ["ses-new", "ses-fixture0001"]  # newest first
    new, fuss = rows
    assert (new.phase, new.link) == ("provisioning", None)  # no link before it runs
    assert (fuss.phase, fuss.checks_passed, fuss.checks_total) == ("valid", 3, 3)
    assert fuss.link == "http://127.0.0.1:40000/?t=ses-fixture0001"
    assert run_table(repo, events)[1].link is None


# -- mode 1 specifics (not part of the shared contract) -------------------------------------


def test_records_may_carry_no_token_but_the_session_manager_needs_one(tmp_path: Path) -> None:
    """Hub records carry no gateway token (it never leaves the worker host); the session
    manager's records always have one, and the SQLite column stays NOT NULL."""
    hub_record = SessionRecord.model_validate(
        {"id": "ses-hub", "state": "running", "created_at": "2026-10-17T08:00:00.000000Z",
         "updated_at": "2026-10-17T08:00:00.000000Z", "image": "img", "port": 40000}
    )  # fmt: skip
    assert hub_record.token is None
    assert "token" not in RunSummary.model_fields  # never sent to clients
    with pytest.raises(ValueError, match="no gateway token"):
        gateway_token(hub_record)
    assert gateway_token(_record("ses-a", "2026-10-17T08:00:00.000000Z")) == "tok-ses-a"
    store = EventStore(tmp_path / "state.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            SessionStore(store.connection()).save(hub_record)
    finally:
        store.close()
