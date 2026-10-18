"""Hub ingest: schema validation, own runs only, provenance and secrets (docs/13 §1, D86)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.hub.ingest import MAX_LINE_BYTES, find_secret
from tests.fixtures.hub import (
    BACKENDS,
    BROWSER,
    GATEWAY_TOKEN,
    NDJSON,
    WORKER_A,
    WORKER_B,
    WORKERS,
    Hub,
    event,
    fixture_events,
    hub,
    line,
    ndjson,
)


@pytest.fixture(params=BACKENDS)
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Hub]:
    with hub(tmp_path, request.param) as h:
        yield h


def _post(h: Hub, body: bytes, headers: dict[str, str] = WORKER_A) -> dict[str, Any]:
    response = h.client.post("/api/ingest", content=body, headers={**headers, **NDJSON})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _reasons(result: dict[str, Any]) -> list[str]:
    return [r["reason"] for r in result["rejected"]]


def _history(h: Hub, run_id: str = "run-a1") -> list[dict[str, Any]]:
    response = h.client.get(f"/api/runs/{run_id}/events/history", headers=BROWSER)
    assert response.status_code == 200
    rows: list[dict[str, Any]] = response.json()
    return rows


def test_valid_lines_are_stored_once(h: Hub) -> None:
    body = ndjson(
        line(event("session.created", emitter="session-manager-1")),
        line(event("agent.run_started", seq=1)),
        line(event("tool.called", emitter="cad-1"), "container"),
    )
    assert _post(h, body) == {"stored": 3, "duplicates": 0, "rejected": []}
    # a forwarder that resends after a restart stores nothing twice
    assert _post(h, body) == {"stored": 0, "duplicates": 3, "rejected": []}
    rows = _history(h)
    assert [r["event"]["type"] for r in rows] == [
        "session.created",
        "agent.run_started",
        "tool.called",
    ]
    assert [r["event"]["source"] for r in rows] == ["host", "host", "container"]
    assert h.runs.get_run("run-a1") is not None
    assert h.runs.get_run("run-a1").worker_id == "worker-a"  # type: ignore[union-attr]


def test_the_recorded_run_validates(h: Hub) -> None:
    lines = [
        line(e.model_copy(update={"run_id": "run-fixture0001"}), "host") for e in fixture_events()
    ]
    result = _post(h, ndjson(*lines))
    assert result["stored"] == len(lines) and result["rejected"] == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not json", "invalid_envelope"),
        ("[1, 2]", "invalid_envelope"),
        (json.dumps({"event": {}}), "invalid_envelope"),
        (line(event("a.b")).replace('"source": "host"', '"source": "agent"'), "invalid_envelope"),
        (json.dumps({"source": "host", "event": {}, "extra": 1}), "invalid_envelope"),
        (json.dumps({"source": "host", "event": {"type": "a.b"}}), "invalid_event"),
        (line({**json.loads(event("a.b").to_json()), "ts": "yesterday"}), "invalid_event"),
        (
            line({**json.loads(event("a.b").to_json()), "ts": "2026-10-17T08:00:00"}),
            "invalid_event",
        ),
        (line({**json.loads(event("a.b").to_json()), "seq": -1}), "invalid_event"),
        (line({**json.loads(event("a.b").to_json()), "seq": 2**63}), "invalid_event"),
        (line({**json.loads(event("a.b").to_json()), "recv_ts": "soon"}), "invalid_event"),
        (line({**json.loads(event("a.b").to_json()), "level": "fatal"}), "invalid_event"),
        (line({**json.loads(event("a.b").to_json()), "schema": "other/1"}), "unknown_schema"),
        (line({**json.loads(event("a.b").to_json()), "emitter_id": None}), "missing_emitter"),
        (line({**json.loads(event("a.b").to_json()), "emitter_id": ""}), "missing_emitter"),
        (line({**json.loads(event("a.b").to_json()), "emitter_id": "e" * 101}), "missing_emitter"),
        (line(event("a.b", emitter="hub-0123")), "reserved_emitter"),
        (line({**json.loads(event("a.b").to_json()), "session_id": None}), "missing_run"),
        (line({**json.loads(event("a.b").to_json()), "run_id": None}), "missing_run"),
        (line(event("a.b", run="../run")), "missing_run"),
        (line(event("a.b", session="ses a")), "missing_run"),
        (line(event("a.b", payload={"x": "y" * MAX_LINE_BYTES})), "too_large"),
    ],
)
def test_invalid_lines_are_refused(h: Hub, raw: str, reason: str) -> None:
    good = line(event("tool.called", emitter="cad-1", seq=7))
    result = _post(h, ndjson(raw, good))
    assert result["stored"] == 1 and result["rejected"] == [{"line": 0, "reason": reason}]
    assert len(_history(h)) == 1


def test_only_the_callers_own_runs(h: Hub) -> None:
    _post(h, ndjson(line(event("session.created", emitter="sm-a"))))
    # worker b cannot write into worker a's run, nor take its session for another run
    result = _post(
        h,
        ndjson(
            line(event("agent.run_started", emitter="x-1")),
        ),
        WORKER_B,
    )
    assert _reasons(result) == ["not_your_run"]
    result = _post(h, ndjson(line(event("session.created", emitter="x-2", run="run-b1"))), WORKER_B)
    assert _reasons(result) == ["not_your_run"]
    # nor can worker a file the run under another session
    result = _post(h, ndjson(line(event("tool.called", emitter="c-1", session="ses-zz"))))
    assert _reasons(result) == ["not_your_run"]
    assert [r["event"]["emitter_id"] for r in _history(h)] == ["sm-a"]
    # worker b's own run is fine
    result = _post(h, ndjson(line(event("a.b", session="ses-b", run="run-b"))), WORKER_B)
    assert result["stored"] == 1


def test_container_lines_cannot_take_host_emitters(h: Hub) -> None:
    _post(h, ndjson(line(event("agent.run_started", emitter="agent-1"))))
    forged = event("agent.run_finished", emitter="agent-1", seq=2, payload={"status": "done"})
    assert _reasons(_post(h, ndjson(line(forged, "container")))) == ["emitter_conflict"]
    # a sandbox emitter of one session cannot appear in another
    _post(h, ndjson(line(event("tool.called", emitter="cad-1"), "container")))
    other = event("tool.called", emitter="cad-1", seq=2, session="ses-a2", run="run-a2")
    assert _reasons(_post(h, ndjson(line(other, "container")))) == ["emitter_conflict"]
    # and a host line cannot use a sandbox emitter
    host = event("agent.step_started", emitter="cad-1", seq=3)
    assert _reasons(_post(h, ndjson(line(host)))) == ["emitter_conflict"]
    # the session manager serves many sessions: one host emitter in two sessions is fine
    sm = [
        event("session.created", emitter="sm", session=s, run=r)
        for s, r in (("ses-a1", "run-a1"), ("ses-a3", "run-a3"))
    ]
    sm[1].seq = 2
    assert _post(h, ndjson(*(line(e) for e in sm)))["stored"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"link": f"http://127.0.0.1:40000/?t={GATEWAY_TOKEN}"},
        {"note": f"cookie mt_token={GATEWAY_TOKEN[:20]}"},
        {"raw": GATEWAY_TOKEN},
        {"line": f"header Authorization: Bearer {'k' * 30}"},
        {"token": "abc"},
        {"nested": [{"token": None}]},
        {"note": f"leaked {WORKERS['worker-b']}"},
        {"link": "/?x=1&t=abcdefghijklmnopqrstuvwxyz"},
    ],
)
def test_lines_with_tokens_are_refused(h: Hub, payload: dict[str, Any]) -> None:
    result = _post(h, ndjson(line(event("log.line", emitter="c", payload=payload), "container")))
    assert result["stored"] == 0 and _reasons(result)[0].startswith("secret:")
    assert h.client.get("/api/runs/run-a1/events/history", headers=BROWSER).status_code == 404


def test_secret_scan_lets_ordinary_events_through() -> None:
    for e in fixture_events():
        assert find_secret(e.to_json()) is None, e.type
    sha = "ab" * 32  # a SHA-256 is 64 hex characters, not a gateway token
    assert find_secret(json.dumps({"sha256": sha, "input_tokens": 3, "tokens": 5})) is None
    assert find_secret(json.dumps({"x": "a" + GATEWAY_TOKEN})) is None  # 49 hex: not one token
    assert find_secret("x", ("",)) is None


def test_container_state_does_not_count(h: Hub) -> None:
    """A sandbox line of a state-driving type is kept for the audit trail but moves nothing."""
    host = [
        event("session.created", emitter="sm", seq=1),
        event("session.started", emitter="sm", seq=2),
        event("agent.run_started", emitter="agent-1", payload={"drawing": "a.pdf", "model": "m"}),
        event("agent.step_started", emitter="agent-1", seq=2, payload={"step": "build"}),
        event("model.response", emitter="agent-1", seq=3, payload={"cost": 0.5}),
    ]
    forged = [
        event("agent.run_finished", emitter="box", seq=1, payload={"status": "done"}),
        event("model.response", emitter="box", seq=2, payload={"cost": 99.0}),
        event("run.settled", emitter="box", seq=3, payload={"cost_eur": 0.0}),
        event("session.shelved", emitter="box", seq=4),
        event("agent.run_started", emitter="box", seq=5, payload={"kind": "circuit"}),
        event(
            "review.item_created",
            emitter="box",
            seq=6,
            payload={"revision": 9, "item": {"id": "x", "kind": "mismatch"}},
        ),
    ]
    body = ndjson(*(line(e) for e in host), *(line(e, "container") for e in forged))
    assert _post(h, body)["stored"] == len(host) + len(forged)
    row = h.client.get("/api/runs/run-a1/run", headers=BROWSER).json()
    assert (row["phase"], row["state"], row["cost"]) == ("building", "running", 0.5)
    assert (row["run_kind"], row["revision"], row["open_pois"]) == ("part", 1, 0)
    assert len(_history(h)) == len(host) + len(forged)  # all kept for the timeline
    # a sandbox can only warn about itself: builds without runner isolation show (D95)
    warning = event(
        "runner.isolation_unavailable", emitter="cad-1", payload={"build_id": "build-1"}
    )
    warning.level = "warning"
    _post(h, ndjson(line(warning, "container")))
    row = h.client.get("/api/runs/run-a1/run", headers=BROWSER).json()
    assert row["unisolated_builds"] == 1 and row["phase"] == "building"
    # ... and cannot clear the warning with an odd build id: it counts as another build, and the
    # row keeps its phase and last activity (#137 review)
    odd = [
        event("runner.isolation_unavailable", emitter="cad-1", seq=2, payload={"build_id": []}),
        event("review.approved", emitter="box", seq=7, payload={"item_id": "rev"}),
        event("review.approved", emitter="box", seq=8, payload={"item_id": {"x": []}}),
    ]
    for e in odd:
        e.level = "warning"
    _post(h, ndjson(*(line(e, "container") for e in odd)))
    row = h.client.get("/api/runs/run-a1/run", headers=BROWSER).json()
    assert (row["unisolated_builds"], row["phase"]) == (2, "building")
    assert row["last_activity"] is not None and row["cost"] == 0.5
    # the worker's settled spend replaces the estimates once it comes (#77)
    settled = event("run.settled", emitter="worker-1", payload={"cost_eur": 0.25})
    _post(h, ndjson(line(settled)))
    row = h.client.get("/api/runs/run-a1/run", headers=BROWSER).json()
    assert row["cost"] == 0.25


def test_request_limits(h: Hub) -> None:
    r = h.client.post(
        "/api/ingest", content=b"{}", headers={**WORKER_A, "Content-Type": "text/plain"}
    )
    assert r.status_code == 415
    r = h.client.post("/api/ingest", content=b"{}", headers=WORKER_A)
    assert r.status_code == 415
    many = ndjson(*(["{}"] * 5001))
    r = h.client.post("/api/ingest", content=many, headers={**WORKER_A, **NDJSON})
    assert r.status_code == 413
    huge = b"x" * (16 * 1024 * 1024 + 1)
    r = h.client.post("/api/ingest", content=huge, headers={**WORKER_A, **NDJSON})
    assert r.status_code == 413
