"""Central Postgres store: migration runner and schema (docs/13 "Central store", D72, #73).

The file and CLI tests run everywhere. The tests on a real Postgres need `MT_PG_DSN` and skip
without it (CI runs them in the `postgres` job). Each test gets its own fresh schema, which it
drops afterwards, so they never touch other data in that database.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

from mechatronikers.pgstore import (
    DEFAULT_ORG,
    DSN_ENV,
    Connection,
    Migration,
    MigrationError,
    StoreConfigError,
    connect,
    dsn_from_env,
    load,
    migrate,
    scrub,
    status,
)
from mechatronikers.pgstore.__main__ import main

TABLES = {
    "orgs",
    "workers",
    "testcases",
    "testsets",
    "testset_cases",
    "plans",
    "batches",
    "runs",
    "sessions",
    "events",
    "event_cursors",
    "gates",
    "gate_corrections",
    "findings",
    "finding_actions",
    "artifacts",
    "commands",
    "spend",
    "bench_results",
    "dataset_records",
    "api_tokens",
}
SHA = "ab" * 32


# -- without Postgres -----------------------------------------------------------------------------


def test_packaged_migrations_load_in_order() -> None:
    migrations = load()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "initial"
    assert all(len(m.checksum) == 64 for m in migrations)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (["0001_a.sql", "0001_b.sql"], "two migration files"),
        (["0001_a.sql", "0003_c.sql"], "without gaps"),
        (["1_a.sql"], "NNNN_name.sql"),
        (["0001_Upper.sql"], "NNNN_name.sql"),
    ],
)
def test_bad_migration_folders_are_rejected(tmp_path: Path, files: list[str], message: str) -> None:
    for name in files:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(MigrationError, match=message):
        load(tmp_path)


def test_other_files_beside_the_migrations_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("notes")
    assert [m.name for m in load(tmp_path)] == ["first"]


def test_dsn_comes_from_the_environment() -> None:
    assert dsn_from_env({DSN_ENV: "  "}) is None
    assert dsn_from_env({}) is None
    assert dsn_from_env({DSN_ENV: "postgresql://x@h/db"}) == "postgresql://x@h/db"


def test_connect_without_dsn_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(StoreConfigError, match=DSN_ENV):
        connect()


def test_cli_without_dsn_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    assert main(["status"]) == 1
    assert DSN_ENV in capsys.readouterr().err


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://mt:Sup3r%zzSecret@127.0.0.1:1/mt",  # an unencoded % in the password
        "postgresql://mt:Sup3r%25zzSecret@127.0.0.1:1/mt",  # valid, but nothing listens
        "host=127.0.0.1 port=1 user=mt password='Sup3r%zzSecret' dbname=mt",
    ],
)
def test_cli_errors_never_show_the_password(
    dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DSN_ENV, dsn)
    assert main(["status"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Sup3r" not in err and "zzSecret" not in err


def test_scrub_removes_the_dsn_and_its_password() -> None:
    dsn = "postgresql://mt:p%40ss@db/mt"
    assert scrub(f"bad {dsn} and p@ss", dsn) == "bad *** and ***"
    assert scrub("nothing secret", None) == "nothing secret"


# -- on Postgres ----------------------------------------------------------------------------------


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """DSN of a fresh, empty schema in the `MT_PG_DSN` database."""
    dsn = dsn_from_env()
    if dsn is None:
        pytest.skip(f"set {DSN_ENV} to run the Postgres store tests")
    schema = f"mt_test_{uuid.uuid4().hex[:12]}"
    with connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-c search_path={schema}")
    finally:
        with connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def conn(pg_dsn: str) -> Iterator[Connection]:
    with connect(pg_dsn) as c:
        yield c


@pytest.fixture
def db(conn: Connection) -> Connection:
    migrate(conn)
    return conn


def _tables(conn: Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {r["table_name"] for r in rows}


def test_migrations_apply_on_an_empty_database_and_are_idempotent(conn: Connection) -> None:
    assert _tables(conn) == set()
    versions = [m.version for m in load()]
    assert migrate(conn) == versions
    assert _tables(conn) == TABLES | {"schema_migrations"}
    assert conn.execute("SELECT id FROM orgs").fetchall() == [{"id": DEFAULT_ORG}]
    before = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(conn) == []
    assert conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall() == before
    assert all(s.applied_at is not None for s in status(conn))


def test_status_lists_pending_migrations(conn: Connection) -> None:
    assert [(s.version, s.applied_at) for s in status(conn)] == [(m.version, None) for m in load()]


def test_a_failing_migration_leaves_nothing_behind(conn: Connection) -> None:
    good = Migration(1, "good", "CREATE TABLE a (id int);")
    bad = Migration(2, "bad", "CREATE TABLE b (id int); SELECT no_such_function();")
    with pytest.raises(errors.UndefinedFunction):
        migrate(conn, [good, bad])
    assert _tables(conn) == {"a", "schema_migrations"}
    assert [s.version for s in status(conn, [good, bad]) if s.applied_at] == [1]
    fixed = Migration(2, "bad", "CREATE TABLE b (id int);")
    assert migrate(conn, [good, fixed]) == [2]


def test_an_edited_migration_is_an_error(conn: Connection) -> None:
    migrate(conn, [Migration(1, "a", "CREATE TABLE a (id int);")])
    with pytest.raises(MigrationError, match="edited"):
        migrate(conn, [Migration(1, "a", "CREATE TABLE a (id bigint);")])


def test_a_newer_database_is_an_error(conn: Connection) -> None:
    migrate(conn, [Migration(1, "a", "SELECT 1;"), Migration(2, "b", "SELECT 1;")])
    with pytest.raises(MigrationError, match="newer"):
        migrate(conn, [Migration(1, "a", "SELECT 1;")])


def test_migrate_refuses_an_open_transaction(pg_dsn: str) -> None:
    with connect(pg_dsn, autocommit=False) as c:
        c.execute("SELECT 1")
        with pytest.raises(MigrationError, match="transaction"):
            migrate(c)


def test_concurrent_runners_apply_each_migration_once(pg_dsn: str) -> None:
    results: list[list[int]] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            with connect(pg_dsn) as c:
                results.append(migrate(c))
        except BaseException as exc:  # reported below
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    assert sorted(v for r in results for v in r) == [m.version for m in load()]


def test_cli_migrates_and_reports(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DSN_ENV, pg_dsn)
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    applied = ", ".join(f"{m.version:04d}" for m in load())
    assert out[:2] == [f"applied {applied}", "up to date"]
    assert out[2].startswith("0001_initial  2")


# -- schema rules ---------------------------------------------------------------------------------


def _run(db: Connection, run_id: str = "run-1", **cols: Any) -> str:
    values = {"id": run_id, "org_id": DEFAULT_ORG} | cols
    db.execute(
        sql.SQL("INSERT INTO runs ({}) VALUES ({})").format(
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() * len(values)),
        ),
        list(values.values()),
    )
    return run_id


def test_workers_claim_different_queued_runs(db: Connection, pg_dsn: str) -> None:
    for i in range(3):
        _run(db, f"run-{i}", created_at=f"2026-10-17T10:00:0{i}Z")
    claim = (
        "SELECT id FROM runs WHERE org_id = %s AND state = 'queued' "
        "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    with connect(pg_dsn) as a, connect(pg_dsn) as b, a.transaction(), b.transaction():
        first = a.execute(claim, (DEFAULT_ORG,)).fetchone()
        second = b.execute(claim, (DEFAULT_ORG,)).fetchone()
    assert first == {"id": "run-0"} and second == {"id": "run-1"}
    with db.transaction():  # a three-row table is scanned anyway; ask whether the index fits
        db.execute("SET LOCAL enable_seqscan = off")
        plan = db.execute("EXPLAIN " + claim, (DEFAULT_ORG,)).fetchall()
    assert any("runs_queue" in row["QUERY PLAN"] for row in plan)


def test_rows_stay_in_the_org_of_their_run(db: Connection) -> None:
    db.execute("INSERT INTO orgs (id, name) VALUES ('org-b', 'B')")
    _run(db)
    with pytest.raises(errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO gates (run_id, id, org_id, kind, request) "
            "VALUES ('run-1', 'g1', 'org-b', 'spec_review', '{}')"
        )


def test_a_gate_is_decided_once(db: Connection) -> None:
    _run(db)
    db.execute(
        "INSERT INTO gates (run_id, id, org_id, kind, request) "
        "VALUES ('run-1', 'g1', %s, 'spec_review', '{\"id\": \"g1\"}') ON CONFLICT DO NOTHING",
        (DEFAULT_ORG,),
    )
    decide = (
        "UPDATE gates SET decision = %s, action = %s, decided_by = 'Ann', decided_at = now() "
        "WHERE run_id = 'run-1' AND id = 'g1'"
    )
    with pytest.raises(errors.CheckViolation):  # gate 1 knows approve and reject only
        db.execute(decide, ('{"action": "finish"}', "finish"))
    db.execute(decide, ('{"action": "approve"}', "approve"))
    with pytest.raises(errors.IntegrityConstraintViolation, match="already decided"):
        db.execute(decide, ('{"action": "reject"}', "reject"))
    with pytest.raises(errors.IntegrityConstraintViolation, match="request"):
        db.execute("UPDATE gates SET request = '{}' WHERE id = 'g1'")


def test_events_are_unique_per_emitter_and_untrusted_by_default(db: Connection) -> None:
    insert = (
        "INSERT INTO events (org_id, run_id, emitter_id, seq, component, type, level, ts, body)"
        " VALUES (%s, 'run-1', %s, %s, 'agent', 'agent.step_started', 'info',"
        " '2026-10-17T08:00:00.000000Z', '{}') ON CONFLICT (emitter_id, seq) DO NOTHING"
        " RETURNING id, source"
    )
    first = db.execute(insert, (DEFAULT_ORG, "em1", 1)).fetchone()
    assert first is not None and first["source"] == "container"  # host lines say so
    assert db.execute(insert, (DEFAULT_ORG, "em1", 1)).fetchone() is None
    # without an emitter id nothing identifies a duplicate (EventRepository contract, #72)
    assert db.execute(insert, (DEFAULT_ORG, None, 5)).fetchone() is not None
    assert db.execute(insert, (DEFAULT_ORG, None, 5)).fetchone() is not None
    with pytest.raises(errors.CheckViolation):
        db.execute(
            "INSERT INTO events (org_id, emitter_id, seq, source, component, type, level, ts,"
            " body) VALUES (%s, 'em2', 1, 'sandbox', 'x', 'x', 'info', 'x', '{}')",
            (DEFAULT_ORG,),
        )


def test_the_gateway_token_stays_on_the_worker_host(db: Connection) -> None:
    insert = "INSERT INTO sessions (id, org_id, state, record) VALUES ('ses-1', %s, 'running', %s)"
    with pytest.raises(errors.CheckViolation):
        db.execute(insert, (DEFAULT_ORG, '{"id": "ses-1", "token": "secret"}'))
    db.execute(insert, (DEFAULT_ORG, '{"id": "ses-1", "image": "localhost/mt:dev"}'))


@pytest.mark.parametrize(
    ("role", "key"),
    [
        ("export", "inputs/run-1/a.pdf"),  # inputs/ holds inputs only
        ("input", "artifacts/ses-1/build-1/a.pdf"),  # inputs live below inputs/
        ("report", "testcases/case-1/ref.stp"),  # references never become run artifacts
        ("export", "artifacts/ses-1/../x.step"),  # no "." or ".." segments
        ("export", "artifacts//x.step"),
    ],
)
def test_artifact_keys_follow_the_blob_rules(db: Connection, role: str, key: str) -> None:
    _run(db)
    with pytest.raises(errors.CheckViolation):
        db.execute(
            "INSERT INTO artifacts (id, org_id, run_id, role, name, blob_key, content_type,"
            " sha256, bytes, expires_at) VALUES ('art-1', %s, 'run-1', %s, 'a', %s, 'x', %s, 1,"
            " now() + interval '30 days')",
            (DEFAULT_ORG, role, key, SHA),
        )


@pytest.mark.parametrize("retention", ["ephemeral", "standard", "audit"])
def test_every_artifact_expires(db: Connection, retention: str) -> None:
    """D81: all three classes have a duration, so the retention job must find every row."""
    _run(db)
    with pytest.raises(errors.NotNullViolation):
        db.execute(
            "INSERT INTO artifacts (id, org_id, run_id, role, name, blob_key, content_type,"
            " sha256, bytes, retention_class) VALUES ('art-1', %s, 'run-1', 'export', 'a',"
            " 'artifacts/ses-1/build-1/a.step', 'model/step', %s, 1, %s)",
            (DEFAULT_ORG, SHA, retention),
        )


def test_a_plan_its_run_and_its_input_are_created_together(db: Connection) -> None:
    with db.transaction():
        db.execute(
            "INSERT INTO plans (id, org_id, kind, file_name, input_artifact_id, source, consent)"
            " VALUES ('plan-1', %s, 'part', 'a.pdf', 'art-1', 'upload', 'not_allowed')",
            (DEFAULT_ORG,),
        )
        _run(db, plan_id="plan-1")
        db.execute(
            "INSERT INTO artifacts (id, org_id, run_id, role, name, blob_key, content_type,"
            " sha256, bytes, expires_at) VALUES ('art-1', %s, 'run-1', 'input', 'a.pdf',"
            " 'inputs/run-1/a.pdf', 'application/pdf', %s, 10, now() + interval '30 days')",
            (DEFAULT_ORG, SHA),
        )
    assert db.execute("SELECT state FROM runs").fetchone() == {"state": "queued"}


def test_costs_are_never_negative(db: Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        _run(db, cost_eur=-0.01)


def test_finding_actions_are_append_only(db: Connection) -> None:
    _run(db)
    db.execute(
        "INSERT INTO findings (run_id, id, org_id, revision, origin, kind, severity, record,"
        " created_by) VALUES ('run-1', 'poi-1', %s, 1, 'reviewer', 'user_note', 'warning', '{}',"
        " 'Ann')",
        (DEFAULT_ORG,),
    )
    db.execute(
        "INSERT INTO finding_actions (run_id, finding_id, org_id, action, reviewer)"
        " VALUES ('run-1', 'poi-1', %s, 'approved', 'Ann')",
        (DEFAULT_ORG,),
    )
    with pytest.raises(errors.IntegrityConstraintViolation, match="append-only"):
        db.execute("UPDATE finding_actions SET reviewer = 'Bob'")
    with pytest.raises(errors.IntegrityConstraintViolation, match="append-only"):
        db.execute("DELETE FROM finding_actions")


def _record(
    db: Connection,
    split: str,
    consent: str,
    source_run: str | None = "run-1",
    source_session: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO dataset_records (id, org_id, task, target, source_run, source_session,"
        " split, consent) VALUES (%s, %s, 'label_reading', '{}', %s, %s, %s, %s)",
        (f"rec-{uuid.uuid4().hex[:8]}", DEFAULT_ORG, source_run, source_session, split, consent),
    )


def test_training_splits_need_consent(db: Connection) -> None:
    _run(db)
    _record(db, "train", "training_allowed")
    _record(db, "eval", "not_allowed")
    with pytest.raises(errors.CheckViolation):
        _record(db, "val", "eval_only")


def test_records_of_test_cases_stay_eval(db: Connection) -> None:
    db.execute(
        "INSERT INTO testcases (id, org_id, kind, name, drawing_key)"
        " VALUES ('case-1', %s, 'part', 'Fuss', 'testcases/case-1/Fuss_dwg.pdf')",
        (DEFAULT_ORG,),
    )
    db.execute(
        "INSERT INTO plans (id, org_id, kind, file_name, source, testcase_id, consent)"
        " VALUES ('plan-1', %s, 'part', 'Fuss_dwg.pdf', 'testcase', 'case-1', 'training_allowed')",
        (DEFAULT_ORG,),
    )
    _run(db, plan_id="plan-1", session_id="ses-tc")
    _run(db, "run-2", plan_id="plan-1")  # its session is known from the sessions table only
    db.execute(
        "INSERT INTO sessions (id, org_id, run_id, state, record)"
        " VALUES ('ses-tc2', %s, 'run-2', 'running', '{}')",
        (DEFAULT_ORG,),
    )
    _record(db, "eval", "training_allowed")
    with pytest.raises(errors.CheckViolation, match="D32"):
        _record(db, "train", "training_allowed")
    # naming only the session of a test-case run does not get around it
    for session in ("ses-tc", "ses-tc2"):
        with pytest.raises(errors.CheckViolation, match="D32"):
            _record(db, "train", "training_allowed", source_run=None, source_session=session)
    _record(db, "train", "training_allowed", source_run=None, source_session="ses-other")


def test_reference_keys_stay_below_their_test_case(db: Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        db.execute(
            "INSERT INTO testcases (id, org_id, kind, name, drawing_key, reference_key)"
            " VALUES ('case-1', %s, 'part', 'Fuss', 'testcases/case-1/a.pdf',"
            " 'testcases/case-2/a.stp')",
            (DEFAULT_ORG,),
        )


def test_tokens_are_bound_to_their_run_or_worker(db: Connection) -> None:
    insert = (
        "INSERT INTO api_tokens (id, org_id, role, token_sha256, run_id)"
        " VALUES (%s, %s, %s, %s, %s)"
    )
    _run(db)
    db.execute(insert, ("tok-1", DEFAULT_ORG, "run", SHA, "run-1"))
    with pytest.raises(errors.CheckViolation):
        db.execute(insert, ("tok-2", DEFAULT_ORG, "run", "cd" * 32, None))
    with pytest.raises(errors.UniqueViolation):
        db.execute(insert, ("tok-3", DEFAULT_ORG, "shared", SHA, None))


def test_ids_keep_the_review_store_charset(db: Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        _run(db, "run/../x")
