"""Contract of `EventRepository` (storage.repositories), run against every implementation."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.storage import EventRepository
from mechatronikers.telemetry import Event
from tests.fixtures.stores import EVENT_REPOSITORIES, Factory


@pytest.fixture(params=EVENT_REPOSITORIES)
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[EventRepository]:
    factory: Factory = request.param
    with factory(tmp_path) as store:
        yield store


def _event(seq: int, **extra: Any) -> Event:
    fields: dict[str, Any] = {
        "ts": f"2026-10-17T08:00:{seq:02d}.000000Z",
        "mono_ns": seq,
        "seq": seq,
        "emitter_id": "agent-1",
        "session_id": "ses-a",
        "component": "agent",
        "type": "agent.step_started",
    }
    return Event.model_validate(fields | extra)


def test_add_returns_increasing_ids_and_events_round_trip(repo: EventRepository) -> None:
    full = _event(
        1,
        run_id="run-1",
        recv_ts="2026-10-17T08:00:01.500000Z",
        level="warning",
        duration_ms=12.5,
        payload={"step": "plan", "nested": {"list": [1, 2.5, None, "x"]}},
        artifacts=[{"path": "artifacts/build-1/Fuss.step", "sha256": "ab" * 32}],
        trace_id="t",
        span_id="s",
    )
    first, second = repo.add(full), repo.add(_event(2))
    assert first is not None and second is not None and second > first
    assert repo.events("ses-a") == [(first, full), (second, _event(2))]


def test_duplicates_are_ignored_per_emitter_and_seq(repo: EventRepository) -> None:
    assert repo.add(_event(1)) is not None
    assert repo.add(_event(1, type="other")) is None
    assert repo.add(_event(1, emitter_id="agent-2")) is not None
    # without an emitter id nothing identifies a duplicate, so both are kept
    assert repo.add(_event(5, emitter_id=None)) is not None
    assert repo.add(_event(5, emitter_id=None)) is not None
    assert repo.count("ses-a") == 4


def test_events_are_per_session_paged_by_ingest_id(repo: EventRepository) -> None:
    ids = []
    for seq in range(1, 8):
        ingest_id = repo.add(_event(seq, session_id="ses-a" if seq % 2 else "ses-b"))
        assert ingest_id is not None
        ids.append(ingest_id)
    repo.add(_event(9, session_id=None))  # unscoped: in no session's history
    a = repo.events("ses-a")
    assert [i for i, _ in a] == ids[0::2]
    assert [e.seq for _, e in a] == [1, 3, 5, 7]
    assert [e.seq for _, e in repo.events("ses-a", after=ids[2])] == [5, 7]
    assert [e.seq for _, e in repo.events("ses-a", after=ids[0], limit=2)] == [3, 5]
    assert repo.events("ses-unknown") == []
    assert (repo.count("ses-a"), repo.count("ses-b"), repo.count("ses-unknown")) == (4, 3, 0)


def test_last_seq_per_emitter(repo: EventRepository) -> None:
    assert repo.last_seq("agent-1") is None
    for seq in (1, 2, 7, 3):
        repo.add(_event(seq))
    repo.add(_event(40, emitter_id="agent-2"))
    assert (repo.last_seq("agent-1"), repo.last_seq("agent-2")) == (7, 40)


def test_last_activity_is_the_newest_ts_as_sent(repo: EventRepository) -> None:
    assert repo.last_activity("ses-a") is None
    repo.add(_event(1, ts="2026-10-17T08:00:01.123456Z"))
    repo.add(_event(2, ts="2026-10-17T09:30:00.000001Z", type="session.reminder_sent"))
    repo.add(_event(3, ts="2026-10-17T08:15:00.000000Z", session_id="ses-b"))
    assert repo.last_activity("ses-a") == "2026-10-17T09:30:00.000001Z"
    assert repo.last_activity("ses-a", ("session.reminder_sent",)) == "2026-10-17T08:00:01.123456Z"
    assert repo.last_activity("ses-a", ("session.reminder_sent", "agent.step_started")) is None


def test_cursors_are_stored_per_source(repo: EventRepository) -> None:
    assert repo.get_cursor("container:mt-ses-a-session") is None
    repo.set_cursor("container:mt-ses-a-session", "1760688000000000000")
    repo.set_cursor("container:mt-ses-a-gateway", "5")
    repo.set_cursor("container:mt-ses-a-session", "1760688000000000001")
    assert repo.get_cursor("container:mt-ses-a-session") == "1760688000000000001"
    assert repo.get_cursor("container:mt-ses-a-gateway") == "5"
