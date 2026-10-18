"""Postgres repositories beyond the shared contract (#73 phase B): ingest order under concurrency,
provenance, text Postgres cannot hold, hub-only rules, the artifact index and the pool.

The shared contract runs in `tests/test_contract_*.py`. These tests need `MT_PG_DSN` and skip
without it, except the ones that need no database.
"""

import hashlib
import io
import json
import logging
import math
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from mechatronikers.pgstore import (
    DEFAULT_ORG,
    PgArtifactStore,
    PgEventRepository,
    PgReviewRepository,
    PgRunRepository,
    PgStore,
    Pool,
    PoolTimeoutError,
    StoreConfigError,
    check_artifact_key,
    connect,
    events_id_cache,
)
from mechatronikers.pgstore import repositories as pg_repositories
from mechatronikers.pgstore.values import REPLACEMENT
from mechatronikers.review_app.store import ReviewConflictError, ReviewStore
from mechatronikers.session.store import SessionRecord
from mechatronikers.spec.circuit import CircuitGatePayload, Component, Correction, NetSummary
from mechatronikers.spec.poi import ReviewAction, ReviewItem, SourceRef
from mechatronikers.spec.review import (
    GateDecision,
    GateRequest,
    GateState,
    ItemActionRequest,
    NewItem,
    StoredItem,
)
from mechatronikers.storage import BlobConflictError, BlobNotFoundError, LocalBlobStore
from mechatronikers.telemetry import Event, EventEmitter
from tests.fixtures.stores import NEEDS_PG, add_run, pg_pool, pg_schema

T0 = "2026-10-17T08:00:00.000000Z"


@pytest.fixture
def pool() -> Iterator[Pool]:
    with pg_pool() as p:
        yield p


@pytest.fixture
def events(pool: Pool) -> PgEventRepository:
    return PgEventRepository(pool)


def _event(seq: int, **extra: Any) -> Event:
    fields: dict[str, Any] = {
        "ts": f"2026-10-17T08:00:{seq % 60:02d}.000000Z",
        "mono_ns": seq,
        "seq": seq,
        "emitter_id": "agent-1",
        "session_id": "ses-a",
        "run_id": "run-a",
        "component": "agent",
        "type": "agent.step_started",
    }
    return Event.model_validate(fields | extra)


def _columns(pool: Pool, ingest_id: int) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT org_id, session_id, run_id, emitter_id, seq, source, component, type, ts "
            "FROM events WHERE id = %s",
            (ingest_id,),
        ).fetchone()
    assert row is not None
    return row


# -- without Postgres -----------------------------------------------------------------------


def test_a_pool_needs_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MT_PG_DSN", raising=False)
    with pytest.raises(StoreConfigError, match="MT_PG_DSN"):
        Pool()


@pytest.mark.parametrize(
    ("key", "role"),
    [
        ("inputs/run-1/a.pdf", "export"),  # inputs/ holds inputs only
        ("artifacts/ses-1/build-1/a.pdf", "input"),  # inputs live below inputs/
        ("testcases/case-1/ref.stp", "report"),  # references never become run artifacts
        ("artifacts/ses-1/../x.step", "export"),
        ("artifacts/ses-1/build-1/a.step", "model"),
    ],
)
def test_artifact_keys_follow_the_table_rules(key: str, role: Any) -> None:
    with pytest.raises(ValueError):
        check_artifact_key(key, role)


def test_unstorable_events_are_named_before_any_write() -> None:
    assert PgEventRepository.problem(_event(1)) is None
    assert PgEventRepository.problem(_event(2**63)) == "seq out of range"
    assert PgEventRepository.problem(_event(1, session_id="ses a")) == "invalid session_id"
    assert PgEventRepository.problem(_event(1, run_id="r" * 101)) == "invalid run_id"
    assert "emitter_id" in (PgEventRepository.problem(_event(1, emitter_id="e" * 101)) or "")


# -- events ---------------------------------------------------------------------------------


@NEEDS_PG
def test_ingest_fills_the_provenance_columns(events: PgEventRepository, pool: Pool) -> None:
    container = events.add(_event(1))
    host = events.add(_event(1, emitter_id="driver-1", component="driver"), source="host")
    assert container is not None and host is not None
    assert _columns(pool, container) | {"seq": 1} == {
        "org_id": DEFAULT_ORG,
        "session_id": "ses-a",
        "run_id": "run-a",
        "emitter_id": "agent-1",
        "seq": 1,
        "source": "container",  # untrusted unless the caller says otherwise
        "component": "agent",
        "type": "agent.step_started",
        "ts": "2026-10-17T08:00:01.000000Z",
    }
    assert _columns(pool, host)["source"] == "host"
    with pytest.raises(ValueError, match="source"):
        events.add(_event(2), source="sandbox")  # type: ignore[arg-type]


@NEEDS_PG
def test_events_page_by_run_too(events: PgEventRepository) -> None:
    ids = [events.add(_event(i, run_id="run-a" if i % 2 else "run-b")) for i in range(1, 6)]
    assert [i for i, _ in events.run_events("run-a")] == ids[0::2]
    assert [e.seq for _, e in events.run_events("run-a", after=ids[0] or 0, limit=1)] == [3]
    assert events.run_events("run-unknown") == [] and events.run_events("../x") == []


@NEEDS_PG
def test_add_many_keeps_the_order_and_skips_duplicates(events: PgEventRepository) -> None:
    assert events.add(_event(2)) is not None
    ids = events.add_many(
        [(_event(1), "host"), (_event(2), "host"), (_event(3), "container"), (_event(3), "host")]
    )
    assert ids[1] is None and ids[3] is None  # stored before, and twice in this batch
    assert ids[0] is not None and ids[2] is not None and ids[2] > ids[0]
    assert [e.seq for _, e in events.events("ses-a")] == [2, 1, 3]  # ingest order, not seq
    assert events.add_many([]) == []


@NEEDS_PG
def test_a_batch_with_an_unstorable_event_stores_nothing(events: PgEventRepository) -> None:
    with pytest.raises(ValueError, match="invalid session_id"):
        events.add_many([(_event(1), "host"), (_event(2, session_id="ses a/b"), "host")])
    with pytest.raises(ValueError, match="seq out of range"):
        events.add(_event(-(2**63) - 1))
    assert events.count("ses-a") == 0


@NEEDS_PG
def test_nul_and_lone_surrogates_are_replaced_not_fatal(
    events: PgEventRepository, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    payload: dict[str, Any] = {
        "line": "a\x00b",
        "k\x00ey": ["\ud800", {"deep": "\x00"}],
        "ok": "plain ä",
        "dup\x00": 1,
    }
    payload["dup" + REPLACEMENT] = 2  # equal to the first key once cleaned: neither is dropped
    hostile = _event(
        1,
        component="agent\x00",
        type="agent.step\x00started",
        ts="2026-10-17T08:00:01Z\x00",
        emitter_id="agent\x00-1",
        payload=payload,
    )
    with caplog.at_level(logging.WARNING, logger="mechatronikers.pgstore.repositories"):
        ingest_id = events.add(hostile)
    assert ingest_id is not None and "unstorable values replaced" in caplog.text
    assert "collides; kept as dup\ufffd#2" in caplog.text
    [(_, stored)] = events.events("ses-a")
    assert stored.payload == {
        "line": "a\ufffdb",
        "k\ufffdey": ["\ufffd", {"deep": "\ufffd"}],
        "ok": "plain ä",
        "dup\ufffd": 1,
        "dup\ufffd#2": 2,
    }
    assert (stored.component, stored.type) == ("agent�", "agent.step�started")
    assert _columns(pool, ingest_id)["emitter_id"] == "agent�-1"
    assert events.last_seq("agent\x00-1") == 1  # looked up as stored
    assert events.add(hostile) is None  # still de-duplicated
    assert events.last_activity("ses-a", ("x\x00",)) == "2026-10-17T08:00:01Z�"
    events.set_cursor("container:\x00", "5\x00")
    assert events.get_cursor("container:\x00") == "5�"


def _hold_first_insert(
    monkeypatch: pytest.MonkeyPatch, emitter_id: str
) -> tuple[threading.Event, threading.Event]:
    """Make the ingest of `emitter_id` stop after its insert, before its commit."""
    inserted, release = threading.Event(), threading.Event()
    original = PgEventRepository._insert

    def held(self: PgEventRepository, conn: Any, row: Any) -> int | None:
        result = original(self, conn, row)
        if row.params["emitter_id"] == emitter_id:
            inserted.set()
            release.wait(10)
        return result

    monkeypatch.setattr(PgEventRepository, "_insert", held)
    return inserted, release


@NEEDS_PG
@pytest.mark.parametrize("serialised", [True, False], ids=["locked", "unlocked"])
def test_a_reader_never_skips_an_event_that_commits_late(
    events: PgEventRepository, monkeypatch: pytest.MonkeyPatch, serialised: bool
) -> None:
    """Without the ingest locks, an id taken first and committed last is skipped by a reader
    paging with `after`; with them, the second writer of the session waits."""
    if not serialised:
        monkeypatch.setattr(pg_repositories, "_lock", lambda conn, namespace, keys: None)
    inserted, release = _hold_first_insert(monkeypatch, "slow")
    slow = threading.Thread(target=events.add, args=(_event(1, emitter_id="slow"),))
    slow.start()
    try:
        assert inserted.wait(10)  # "slow" holds the lower id, not yet committed
        fast = threading.Thread(target=events.add, args=(_event(1, emitter_id="fast"),))
        fast.start()
        # another session and run ingest meanwhile in both cases
        assert events.add(_event(1, emitter_id="other", session_id="ses-b", run_id="run-b"))
        fast.join(1)
        assert fast.is_alive() == serialised
        seen = events.events("ses-a")  # a reader pages now
        cursor = seen[-1][0] if seen else 0
    finally:
        release.set()
        slow.join(10)
    fast.join(10)
    seen += events.events("ses-a", after=cursor)
    stored = [i for i, _ in events.events("ses-a")]
    if serialised:
        assert [i for i, _ in seen] == stored
        assert [e.emitter_id for _, e in seen] == ["slow", "fast"]
    else:  # the failure the locks prevent
        assert [e.emitter_id for _, e in seen] == ["fast"] and len(stored) == 2


@NEEDS_PG
def test_paging_during_concurrent_ingest_sees_every_event_once(pool: Pool) -> None:
    writers, per_writer = 6, 30
    done = threading.Event()
    seen: list[int] = []
    failures: list[BaseException] = []

    def write(n: int) -> None:
        repo = PgEventRepository(pool)
        try:
            for seq in range(1, per_writer + 1):
                if seq % 3:
                    repo.add(_event(seq, emitter_id=f"w{n}"), source="host")
                else:
                    repo.add_many([(_event(seq, emitter_id=f"w{n}", run_id=f"run-{n}"), "host")])
        except BaseException as exc:  # reported below
            failures.append(exc)

    def read() -> None:
        repo = PgEventRepository(pool)
        while True:
            finished = done.is_set()
            page = repo.events("ses-a", after=seen[-1] if seen else 0, limit=7)
            seen.extend(i for i, _ in page)
            if finished and not page:
                return

    reader = threading.Thread(target=read)
    reader.start()
    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    done.set()
    reader.join(60)
    assert failures == []
    stored = [i for i, _ in PgEventRepository(pool).events("ses-a", limit=10_000)]
    assert len(stored) == writers * per_writer
    assert seen == stored


# -- runs -----------------------------------------------------------------------------------


def _session(id: str = "ses-a", **extra: Any) -> SessionRecord:
    fields: dict[str, Any] = {"id": id, "state": "running", "created_at": T0, "updated_at": T0,
                              "image": "localhost/mechatronikers:dev"}  # fmt: skip
    return SessionRecord.model_validate(fields | extra)


@NEEDS_PG
def test_hub_run_records_never_hold_the_gateway_token(pool: Pool) -> None:
    runs = PgRunRepository(pool)
    with pytest.raises(ValueError, match="gateway token"):
        runs.save(_session(token="tok-secret"))
    with pytest.raises(KeyError):
        runs.get("ses-a")
    runs.save(_session())
    with pool.connection() as conn:
        row = conn.execute("SELECT record FROM sessions WHERE id = 'ses-a'").fetchone()
    assert row is not None and "token" not in row["record"]


@NEEDS_PG
def test_run_records_link_their_run_and_copy_filter_columns(pool: Pool) -> None:
    runs = PgRunRepository(pool)
    add_run(pool, "run-a")
    runs.save(_session(expires_at="2027-10-17T08:00:00Z", snapshot_id="snap-1"), run_id="run-a")
    runs.save(_session(state="shelved", snapshot_id="snap-1"))  # the link is kept
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT run_id, state, snapshot_id, expires_at, created_at FROM sessions"
        ).fetchone()
    assert row is not None
    assert (row["run_id"], row["state"], row["snapshot_id"]) == ("run-a", "shelved", "snap-1")
    assert row["expires_at"] is None and row["created_at"].year == 2026
    with pytest.raises(ValueError, match="session id"):
        runs.save(_session("ses a"))


@NEEDS_PG
def test_records_stay_in_their_organisation(pool: Pool) -> None:
    with pool.connection() as conn:
        conn.execute("INSERT INTO orgs (id, name) VALUES ('org-b', 'B')")
    a, b = PgRunRepository(pool), PgRunRepository(pool, org_id="org-b")
    a.save(_session())
    assert b.all() == []
    with pytest.raises(KeyError):
        b.get("ses-a")
    with pytest.raises(ValueError, match="another organisation"):
        b.save(_session(state="failed"))
    assert a.get("ses-a").state == "running"
    ea, eb = PgEventRepository(pool), PgEventRepository(pool, org_id="org-b")
    ea.add(_event(1))
    ea.set_cursor("src", "1")
    assert eb.events("ses-a") == [] and eb.count("ses-a") == 0 and eb.get_cursor("src") is None


# -- reviews --------------------------------------------------------------------------------


def _gate(id: str, run_id: str = "run-1", **extra: Any) -> GateRequest:
    fields: dict[str, Any] = {"id": id, "kind": "spec_review", "run_id": run_id, "created_at": T0}
    return GateRequest.model_validate(fields | extra)


def _decision(gate_id: str, **extra: Any) -> GateDecision:
    fields: dict[str, Any] = {"gate_id": gate_id, "action": "approve", "reviewer": "Anna",
                              "decided_at": T0}  # fmt: skip
    return GateDecision.model_validate(fields | extra)


@pytest.fixture
def reviews(pool: Pool) -> PgReviewRepository:
    return PgReviewRepository(pool, add_run(pool, "run-1"))


@NEEDS_PG
def test_review_transactions_are_per_run_and_not_nested(
    pool: Pool, reviews: PgReviewRepository
) -> None:
    other = PgReviewRepository(pool, add_run(pool, "run-2"))
    with reviews.transaction():
        with pytest.raises(RuntimeError, match="not nested"), reviews.transaction():
            pass
        finished = threading.Event()

        def write_other_run() -> None:
            with other.transaction():
                other.save_gate(GateState(request=_gate("gate-b", "run-2")))
            finished.set()

        thread = threading.Thread(target=write_other_run)
        thread.start()
        assert finished.wait(5)  # another run does not wait for this one
        thread.join(5)
    assert other.get_gate("gate-b") is not None and reviews.gates() == []


@NEEDS_PG
def test_a_rolled_back_transaction_leaves_nothing(reviews: PgReviewRepository) -> None:
    with pytest.raises(KeyError), reviews.transaction():
        reviews.save_gate(GateState(request=_gate("gate-a")))
        raise KeyError("a review rule failed")
    assert reviews.gates() == []


@NEEDS_PG
def test_gates_keep_their_corrections_and_their_decision_once(
    pool: Pool, reviews: PgReviewRepository
) -> None:
    payload = CircuitGatePayload(
        schematic_id="sch-1",
        sheet_size=(297.0, 210.0),
        components=[Component(refdes="R1", kind="resistor", value="10k", bbox=(1, 2, 3, 4))],
        nets=[NetSummary(id="n1", name="VIN", terminals=["R1.1"], wires=2)],
    )
    state = GateState(request=_gate("gate-c", run_kind="circuit", circuit=payload))
    reviews.save_gate(state)
    fixes = [Correction(target="component", id="R1", field="value", old="10k", new="47k"),
             Correction(target="net", id="n1", field="name", new="VOUT")]  # fmt: skip
    state.decision = _decision("gate-c", corrections=fixes)
    reviews.save_gate(state)
    reviews.save_gate(state)  # the same state again: nothing changes
    with pool.connection() as conn:
        gate = conn.execute("SELECT kind, run_kind, action, decided_by, decided_at FROM gates")
        row = gate.fetchone()
        corrections = conn.execute(
            "SELECT position, target, target_id, field, old_value, new_value "
            "FROM gate_corrections ORDER BY position"
        ).fetchall()
    assert row is not None and row["decided_at"] is not None
    assert (row["kind"], row["run_kind"], row["action"], row["decided_by"]) == (
        "spec_review", "circuit", "approve", "Anna",
    )  # fmt: skip
    assert [tuple(c.values()) for c in corrections] == [
        (0, "component", "R1", "value", "10k", "47k"),
        (1, "net", "n1", "name", None, "VOUT"),
    ]
    state.decision = _decision("gate-c", action="reject", reason="wrong part")
    with pytest.raises(errors.IntegrityConstraintViolation, match="already decided"):
        reviews.save_gate(state)
    with pytest.raises(ValueError, match="not a gate of run run-1"):
        reviews.save_gate(GateState(request=_gate("gate-x", "run-2")))
    with pytest.raises(ValueError, match="gate id"):
        reviews.get_gate("../escape")


@NEEDS_PG
def test_poi_history_is_appended_to_the_audit_trail_once(
    pool: Pool, reviews: PgReviewRepository
) -> None:
    item = ReviewItem(id="poi-1", kind="user_note", message="burr\x00", reason="seen")
    stored = StoredItem(item=item, revision=1, origin="reviewer", created_at=T0, created_by="Anna")
    reviews.save_item(stored)
    stored.item.history.append(ReviewAction(at=T0, by="Ben", action="edited", new_value=4.2))
    stored.item.status = "edited"
    reviews.save_item(stored)
    stored.item.history.append(ReviewAction(at=T0, by="Ben", action="note", comment="ok"))
    reviews.save_item(stored)
    reviews.save_item(stored)
    with pool.connection() as conn:
        actions = conn.execute(
            "SELECT action, reviewer, new_value, comment FROM finding_actions ORDER BY id"
        ).fetchall()
        finding = conn.execute("SELECT status, origin FROM findings").fetchone()
    assert [tuple(a.values()) for a in actions] == [
        ("edited", "Ben", 4.2, None),
        ("note", "Ben", None, "ok"),
    ]
    assert finding == {"status": "edited", "origin": "reviewer"}
    got = reviews.get_item("poi-1")
    assert got is not None and got.item.message == "burr�"


# -- artifacts and the bundle ---------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def artifacts(pool: Pool, tmp_path: Path) -> PgArtifactStore:
    return PgArtifactStore(pool, LocalBlobStore(tmp_path / "storage"))


@NEEDS_PG
def test_artifacts_are_indexed_rows_over_blobs(pool: Pool, artifacts: PgArtifactStore) -> None:
    add_run(pool, "run-1")
    key = "artifacts/ses-a/build-1/Fuss.step"
    put: dict[str, Any] = dict(key=key, role="export", name="Fuss.step", sha256=_sha(b"solid"),
               content_type="model/step", run_id="run-1", session_id="ses-a",
               build_id="build-1", revision=1, format="step")  # fmt: skip
    record = artifacts.put(io.BytesIO(b"solid"), **put)
    assert (record.key, record.bytes, record.retention_class) == (key, 5, "standard")
    assert record.id.startswith("art-") and record.created_at.endswith("Z")
    with artifacts.open(record.id) as f:
        assert f.read() == b"solid"
    assert artifacts.put(io.BytesIO(b"solid"), **put) == record  # a retried upload
    with pytest.raises(BlobConflictError):
        artifacts.put(io.BytesIO(b"other"), **put | {"sha256": _sha(b"other")})
    audit = artifacts.put(
        io.BytesIO(b"{}"), key="artifacts/ses-a/build-1/spec.json", role="spec", name="spec.json",
        sha256=_sha(b"{}"), content_type="application/json", retention_class="audit",
        session_id="ses-a",
    )  # fmt: skip
    with pool.connection() as conn:
        days = conn.execute(
            "SELECT id, extract(day FROM expires_at - created_at) AS d FROM artifacts ORDER BY d"
        ).fetchall()
    assert [(r["id"], int(r["d"])) for r in days] == [(record.id, 30), (audit.id, 365)]  # D81
    assert [a.id for a in artifacts.list(run_id="run-1")] == [record.id]
    assert {a.id for a in artifacts.list(session_id="ses-a")} == {record.id, audit.id}
    assert artifacts.get("art-unknown") is None
    with pytest.raises(BlobNotFoundError):
        artifacts.open("art-unknown")
    with pool.connection() as conn:  # the retention job removed the bytes (D81)
        conn.execute("UPDATE artifacts SET deleted_at = now() WHERE id = %s", (audit.id,))
    with pytest.raises(BlobNotFoundError):
        artifacts.open(audit.id)


@NEEDS_PG
def test_a_refused_artifact_writes_no_bytes(pool: Pool, artifacts: PgArtifactStore) -> None:
    with pytest.raises(ValueError, match="inputs/"):
        artifacts.put(io.BytesIO(b"x"), key="inputs/run-1/a.pdf", role="export", name="a.pdf",
                      sha256=_sha(b"x"), content_type="application/pdf")  # fmt: skip
    assert list(artifacts.blobs.list("")) == []
    # a run without a row: the bytes stay unreferenced, and a retry after the run exists works
    put: dict[str, Any] = dict(key="inputs/run-9/a.pdf", role="input", name="a.pdf",
                               sha256=_sha(b"pdf"), content_type="application/pdf",
                               run_id="run-9")  # fmt: skip
    with pytest.raises(errors.ForeignKeyViolation):
        artifacts.put(io.BytesIO(b"pdf"), **put)
    assert artifacts.list() == []
    add_run(pool, "run-9")
    assert artifacts.put(io.BytesIO(b"pdf"), **put).run_id == "run-9"


@NEEDS_PG
def test_the_store_opens_migrated_and_hands_out_reviews_of_known_runs(tmp_path: Path) -> None:
    with pg_schema() as dsn:
        store = PgStore.open(tmp_path / "storage", dsn)  # migrating again is a no-op
        try:
            with pytest.raises(KeyError):
                store.reviews("run-1")
            add_run(store.pool, "run-1")
            store.reviews("run-1").save_gate(GateState(request=_gate("gate-a")))
            assert store.reviews("run-1").get_gate("gate-a") is not None
            store.runs.save(_session())
            assert store.events.add(_event(1)) is not None
        finally:
            store.close()
        with pytest.raises(StoreConfigError, match="closed"):
            store.runs.all()


@NEEDS_PG
def test_the_pool_drops_connections_left_in_a_transaction(pool: Pool) -> None:
    with pool.connection() as conn:
        first = conn
        conn.execute("BEGIN")
    assert first.closed
    with pool.connection() as conn:
        assert conn is not first
        second = conn
    with pool.connection() as conn:
        assert conn is second  # a clean connection is reused


# -- review hardening ---------------------------------------------------------------------


@NEEDS_PG
def test_non_finite_numbers_become_null_and_never_fail_a_batch(
    events: PgEventRepository, reviews: PgReviewRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """json.loads and pydantic accept NaN and Infinity, JSONB does not: like mode 1 (JSON null),
    they are stored as null, so one such line never fails the forwarder's batch."""
    good = '{"ts": "2026-10-17T08:00:01.000000Z", "mono_ns": 1, "seq": %d, "emitter_id": "c1",'
    good += ' "session_id": "ses-a", "component": "build", "type": "build.step"%s}'
    lines = [
        good % (1, ""),
        good % (2, ', "duration_ms": NaN, "payload": {"x": Infinity, "y": [-Infinity, 1.5]}'),
        good % (3, ', "duration_ms": Infinity'),
        good % (4, ""),
    ]
    batch = [(Event.model_validate(json.loads(line)), "container") for line in lines]
    assert math.isnan(batch[1][0].duration_ms or 0.0)  # what a container line can carry
    with caplog.at_level(logging.WARNING, logger="mechatronikers.pgstore.repositories"):
        ids = events.add_many(batch)  # type: ignore[arg-type]
    assert None not in ids and "nan -> null" in caplog.text
    stored = [e for _, e in events.events("ses-a")]
    assert [e.seq for e in stored] == [1, 2, 3, 4]
    assert (stored[1].duration_ms, stored[1].payload) == (None, {"x": None, "y": [None, 1.5]})
    assert stored[2].duration_ms is None
    # review records the same way, as the JSON repository writes them
    item = ReviewItem(id="poi-1", kind="user_note", message="m", reason="r")
    poi = StoredItem(item=item, revision=1, origin="reviewer", created_at=T0, created_by="Anna",
                     proposed_value=float("nan"))  # fmt: skip
    poi.item.history.append(ReviewAction(at=T0, by="Ben", action="edited", new_value=math.inf))
    reviews.save_item(poi)
    got = reviews.get_item("poi-1")
    assert got is not None and got.proposed_value is None
    assert got.item.history[0].new_value is None


def _dsn_with(dsn: str, option: str) -> str:
    options = conninfo_to_dict(dsn).get("options", "")
    return make_conninfo(dsn, options=f"{options} {option}".strip())


@NEEDS_PG
def test_connections_run_read_committed_whatever_the_server_default(tmp_path: Path) -> None:
    """With a default of serializable (or repeatable read), a statement after a lock wait would
    still see the snapshot from before the wait: a POI action waiting while the result review
    is finished would land on the finished review. The pool pins READ COMMITTED."""
    with pg_schema() as dsn:
        pool = Pool(_dsn_with(dsn, "-c default_transaction_isolation=serializable"))
        try:
            with pool.connection() as conn:
                assert conn.execute("SHOW transaction_isolation").fetchone() == {
                    "transaction_isolation": "read committed"
                }
                with conn.transaction():
                    row = conn.execute("SHOW transaction_isolation").fetchone()
                assert row == {"transaction_isolation": "read committed"}
            add_run(pool, "run-1")
            _poi_action_after_the_review_finished(pool, tmp_path)
        finally:
            pool.close()


def _poi_action_after_the_review_finished(pool: Pool, tmp_path: Path) -> None:
    emitter = EventEmitter("review-app", stream=io.StringIO())
    first = ReviewStore(tmp_path / "a", emitter, lambda: T0,
                        repository=PgReviewRepository(pool, "run-1"))  # fmt: skip
    gate = _gate("gate-2", kind="result_review", revision=2)
    first.open_gate(gate)
    sheet = SourceRef(bbox=(0, 0, 1, 1))
    note = NewItem(message="burr", reviewer="Anna", revision=2, sheet_ref=sheet)
    added = first.add_item(note)
    holding, finish = threading.Event(), threading.Event()
    outcome: list[BaseException | None] = []

    def finish_review() -> None:  # holds the run's lock while it finishes the review
        repo = PgReviewRepository(pool, "run-1")
        with repo.transaction():
            state = repo.get_gate("gate-2")
            assert state is not None
            state.decision = _decision("gate-2", action="finish")
            repo.save_gate(state)
            holding.set()
            finish.wait(10)

    def act() -> None:  # a second request, on its own repository object
        second = ReviewStore(tmp_path / "b", emitter, lambda: T0,
                             repository=PgReviewRepository(pool, "run-1"))  # fmt: skip
        try:
            second.act(added.item.id, ItemActionRequest(action="approved", reviewer="Ben"))
            outcome.append(None)
        except BaseException as exc:  # checked below
            outcome.append(exc)

    finisher = threading.Thread(target=finish_review)
    finisher.start()
    assert holding.wait(10)
    actor = threading.Thread(target=act)
    actor.start()
    with pool.connection() as conn:  # wait until the action waits on the run's lock
        for _ in range(200):
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ).fetchone()
            if row is not None and row["n"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the action never waited for the run's lock")
    finish.set()
    finisher.join(10)
    actor.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], ReviewConflictError)
    got = first.repository.get_item(added.item.id)
    assert got is not None and got.item.status == "open" and got.item.history == []


@NEEDS_PG
def test_review_objects_of_one_run_share_the_open_transaction(tmp_path: Path) -> None:
    with pg_schema() as dsn:
        store = PgStore.open(tmp_path / "storage", dsn)
        try:
            add_run(store.pool, "run-1")
            with store.reviews("run-1").transaction():
                # another object of the run (a request handler's) joins instead of waiting
                store.reviews("run-1").save_gate(GateState(request=_gate("gate-a")))
                with (
                    pytest.raises(RuntimeError, match="not nested"),
                    store.reviews("run-1").transaction(),
                ):
                    pass
            assert store.reviews("run-1").get_gate("gate-a") is not None
            with pytest.raises(KeyError), store.reviews("run-1").transaction():
                store.reviews("run-1").save_gate(GateState(request=_gate("gate-b")))
                raise KeyError("a rule failed")
            assert [g.request.id for g in store.reviews("run-1").gates()] == ["gate-a"]
        finally:
            store.close()


@NEEDS_PG
def test_a_lock_wait_times_out_instead_of_hanging(pool: Pool) -> None:
    add_run(pool, "run-1")
    other = Pool(pool._dsn, lock_timeout=0.3)  # the same schema: e.g. a second hub process
    try:
        with PgReviewRepository(pool, "run-1").transaction():
            started = time.monotonic()
            with (
                pytest.raises(errors.LockNotAvailable),
                PgReviewRepository(other, "run-1").transaction(),
            ):
                pass
            assert time.monotonic() - started < 5
        with other.connection() as conn:
            settings = conn.execute(
                "SELECT current_setting('lock_timeout') AS lock, "
                "current_setting('statement_timeout') AS statement"
            ).fetchone()
        assert settings == {"lock": "300ms", "statement": "1min"}
    finally:
        other.close()


@NEEDS_PG
def test_the_pool_is_capped_and_waits_for_a_free_connection(pool: Pool) -> None:
    small = Pool(pool._dsn, max_size=2, timeout=0.2)
    try:
        held = [small.connection(), small.connection()]
        for borrowed in held:
            borrowed.__enter__()
        with pytest.raises(PoolTimeoutError):
            small._take()
        for borrowed in held:
            borrowed.__exit__(None, None, None)
        backends: set[int] = set()
        failures: list[BaseException] = []
        patient = Pool(pool._dsn, max_size=3, timeout=30)

        def borrow() -> None:
            try:
                for _ in range(5):
                    with patient.connection() as conn:
                        backends.add(conn.info.backend_pid)
                        conn.execute("SELECT pg_sleep(0.01)")
            except BaseException as exc:  # reported below
                failures.append(exc)

        threads = [threading.Thread(target=borrow) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        patient.close()
        assert failures == [] and 1 <= len(backends) <= 3
    finally:
        small.close()


@NEEDS_PG
def test_the_pool_replaces_connections_the_server_ended(pool: Pool) -> None:
    with pool.connection() as conn:
        pid = conn.info.backend_pid
    with connect(pool._dsn) as admin:  # a restart, failover or idle timeout ends the session
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    events = PgEventRepository(pool)
    assert events.add(_event(1)) is not None  # the next call works
    with pool.connection() as conn:
        assert conn.info.backend_pid != pid


@NEEDS_PG
def test_event_ids_keep_sequence_cache_1(tmp_path: Path) -> None:
    """Paging needs ids in lock order; a sequence cache hands each backend its own block."""
    with pg_schema() as dsn:
        with connect(dsn) as conn:
            assert events_id_cache(conn) == 1
            conn.execute("ALTER TABLE events ALTER COLUMN id SET CACHE 20")
        with pytest.raises(StoreConfigError, match="CACHE 1"):
            PgStore.open(tmp_path / "storage", dsn)
