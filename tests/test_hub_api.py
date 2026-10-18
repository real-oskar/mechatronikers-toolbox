"""The hub's read API: the overview contract of #75, run views, and artifact uploads."""

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.spec.review import HubRun, RunSummary
from tests.fixtures.hub import (
    BACKENDS,
    BROWSER,
    NDJSON,
    WORKER_A,
    WORKER_B,
    Hub,
    event,
    fixture_events,
    hub,
    line,
    ndjson,
)

# One row of `frontend/src/test/hubRuns.ts` on #75 (feat/75-hub-overview), the overview's
# fixture for `GET /api/runs`: the hub's rows must have exactly these keys (`HubRun` there).
HUB_RUN_75 = {
    "session_id": "ses-fuss0001",
    "run_kind": "part",
    "drawing": "Fuss_dwg.pdf",
    "part_name": "Fuss",
    "state": "running",
    "checks_passed": 12,
    "checks_total": 12,
    "open_pois": 2,
    "revision": 2,
    "fix_iterations": 1,
    "max_fix_iterations": 3,
    "model": "vendor/vision-model",
    "cost": 0.084,
    "started_at": "2026-10-17T07:12:00.000000Z",
    "last_activity": "2026-10-17T07:31:40.000000Z",
    "link": None,
    "plan": "Fuss",
    "reviewer": "Anna",
    "batch": None,
    "run_id": "run-fuss0001",
    "phase": "valid",
}
# optional in RunSummary (types.ts); `unisolated_builds` came with #104 (D95)
OPTIONAL_75 = {"stop_reason", "failing_checks", "failed_features", "unisolated_builds"}
OPTIONAL_75 |= {"retention_class", "retention_days", "expires_at"}  # what shelving keeps (#52)


TYPES_TS = Path(__file__).parents[1] / "frontend" / "src" / "api" / "types.ts"


def _ts_fields(source: str, interface: str) -> set[str]:
    """Field names of one TypeScript interface in `types.ts`."""
    body = re.search(rf"export interface {interface}\b[^{{]*\{{(.*?)\n\}}", source, re.S)
    assert body is not None, interface
    return set(re.findall(r"^\s+(\w+)\??:", body.group(1), re.M))


@pytest.mark.skipif(not TYPES_TS.is_file(), reason="frontend/ is not in this tree")
def test_hub_rows_mirror_the_frontend_types() -> None:
    """`HubRun` (Python) and `HubRun` in frontend/src/api/types.ts (#75) name the same fields."""
    source = TYPES_TS.read_text()
    summary, hub_run = _ts_fields(source, "RunSummary"), _ts_fields(source, "HubRun")
    assert summary == set(RunSummary.model_fields)
    assert summary | hub_run == set(HubRun.model_fields)
    assert _ts_fields(source, "RunPage") == {"runs", "next_cursor"}


@pytest.fixture(params=BACKENDS)
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Hub]:
    with hub(tmp_path, request.param) as h:
        yield h


def _post(h: Hub, *lines: str, headers: dict[str, str] = WORKER_A) -> None:
    r = h.client.post("/api/ingest", content=ndjson(*lines), headers={**headers, **NDJSON})
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text


def _fixture_run(h: Hub, run_id: str = "run-fixture0001") -> None:
    events = fixture_events()
    sources = {"session-manager": "host", "agent": "host"}
    _post(
        h,
        *(
            line(e.model_copy(update={"run_id": run_id}), sources.get(e.component, "container"))  # type: ignore[arg-type]
            for e in events
        ),
    )


def _start(h: Hub, n: int, ts: str, kind: str = "part") -> None:
    session, run = f"ses-{n:04d}", f"run-{n:04d}"
    _post(
        h,
        line(event("session.created", emitter=f"sm-{n}", session=session, run=run, ts=ts)),
        line(
            event(
                "agent.run_started",
                emitter=f"agent-{n}",
                session=session,
                run=run,
                ts=ts,
                payload={"kind": kind, "drawing": f"d{n}.pdf"},
            )
        ),
    )


def _page(h: Hub, query: str = "") -> dict[str, Any]:
    r = h.client.get(f"/api/runs{query}", headers=BROWSER)
    assert r.status_code == 200, r.text
    page: dict[str, Any] = r.json()
    return page


def test_overview_rows_match_the_frontend_contract(h: Hub) -> None:
    _fixture_run(h)
    page = _page(h)
    assert page["next_cursor"] is None and len(page["runs"]) == 1
    row = page["runs"][0]
    assert set(row) == set(HUB_RUN_75) | OPTIONAL_75
    assert set(HubRun.model_fields) == set(row)
    assert set(RunSummary.model_fields) | {"run_id", "plan", "reviewer", "batch"} == set(row)
    HubRun.model_validate(HUB_RUN_75)  # the fixture row is a valid hub row
    assert row["link"] is None  # never the session's token link (docs/13 §5)
    assert (row["run_id"], row["session_id"]) == ("run-fixture0001", "ses-fixture0001")
    # the same phase rules as the host's run table, on the recorded run
    assert (row["phase"], row["checks_passed"], row["checks_total"]) == ("valid", 3, 3)
    assert (row["drawing"], row["part_name"], row["open_pois"]) == ("Fuss_dwg.pdf", "Fuss", 2)
    assert row["cost"] == pytest.approx(0.042)
    assert row["state"] == "running" and row["started_at"] == "2026-10-17T08:00:00.000000Z"
    assert (row["reviewer"], row["plan"], row["batch"]) == (None, None, None)  # v0 (#76, #77)
    assert h.client.get("/api/runs/run-fixture0001/run", headers=BROWSER).json() == row


def test_overview_pages_with_a_keyset_cursor(h: Hub) -> None:
    for n in range(5):
        _start(h, n, f"2026-10-17T08:0{n}:00.000000Z", kind="circuit" if n % 2 else "part")
    first = _page(h, "?limit=2")
    assert [r["run_id"] for r in first["runs"]] == ["run-0004", "run-0003"]
    # runs that arrive between two requests neither repeat a row nor drop one: a newer run comes
    # with the next refresh, a mirrored run that started earlier (host clock) on a later page
    _start(h, 9, "2026-10-17T08:09:00.000000Z")
    _start(h, 7, "2026-10-17T08:02:30.000000Z")
    second = _page(h, f"?limit=2&cursor={first['next_cursor']}")
    third = _page(h, f"?limit=2&cursor={second['next_cursor']}")
    assert [r["run_id"] for r in second["runs"] + third["runs"]] == [
        "run-0007",
        "run-0002",
        "run-0001",
        "run-0000",
    ]
    assert third["next_cursor"] is None
    assert [r["run_id"] for r in _page(h, "?limit=200")["runs"]][:2] == ["run-0009", "run-0004"]
    circuits = _page(h, "?kind=circuit")["runs"]
    assert [r["run_id"] for r in circuits] == ["run-0003", "run-0001"]
    assert all(r["run_kind"] == "circuit" for r in circuits)
    assert [r["run_id"] for r in _page(h, "?phase=provisioning&q=D9.PDF")["runs"]] == ["run-0009"]
    assert _page(h, "?phase=valid")["runs"] == [] and _page(h, "?batch=b-1")["runs"] == []
    for bad in ("?kind=gear", "?phase=done", "?cursor=nope", "?limit=0", "?limit=201", "?limit=x"):
        assert h.client.get(f"/api/runs{bad}", headers=BROWSER).status_code == 400, bad


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs?limit=²",
        "/api/runs?limit=١",
        "/api/runs?limit=99999999999999999999999",
        "/api/runs/run-fixture0001/events/history?after=²",
        "/api/runs/run-fixture0001/events/history?after=99999999999999999999999",
        "/api/runs/run-fixture0001/events/history?after=-1",
        "/api/runs/run-fixture0001/events/stream?after=²",
    ],
)
def test_odd_query_values_are_400_not_500(h: Hub, path: str) -> None:
    _fixture_run(h)
    assert h.client.get(path, headers=BROWSER).status_code == 400


def test_odd_numbers_elsewhere_are_ignored(h: Hub) -> None:
    _fixture_run(h)
    base = "/api/runs/run-fixture0001"
    assert h.client.get(f"{base}/review/sheet.png?dpi=²", headers=BROWSER).status_code == 404
    odd = {"Last-Event-ID": "²".encode("latin-1"), "Authorization": BROWSER["Authorization"]}
    assert h.client.get(f"{base}/events/history", headers=odd).status_code == 200


def test_run_view_reads_the_runs_events(h: Hub) -> None:
    _fixture_run(h)
    _start(h, 1, "2026-10-17T09:00:00.000000Z")  # another run's events stay out
    base = "/api/runs/run-fixture0001"
    rows = h.client.get(f"{base}/events/history", headers=BROWSER).json()
    assert len(rows) == len(fixture_events())
    assert all(r["event"]["run_id"] == "run-fixture0001" for r in rows)
    after = rows[9]["id"]
    later = h.client.get(f"{base}/events/history?after={after}", headers=BROWSER).json()
    assert later == rows[10:]
    assert h.client.get(f"{base}/review/gates", headers=BROWSER).json() == []  # #76
    assert h.client.get(f"{base}/review/items", headers=BROWSER).json() == []
    for path in ("/run", "/events/history", "/review/gates", "/artifacts", "/review/model/b.stl"):
        r = h.client.get(f"/api/runs/run-unknown{path}", headers=BROWSER)
        assert r.status_code == 404, path
    assert h.client.get("/runs/run-fixture0001", headers=BROWSER).status_code == 200
    config = h.client.get("/config.json", headers=BROWSER).json()
    assert config == {"mode": "hub", "api": {"runs": "/api/runs"}, "features": {"table": True}}


def _upload(
    h: Hub,
    body: bytes,
    *,
    name: str = "Fuss.stl",
    build: str = "build-fx2",
    role: str = "export",
    run: str = "run-fixture0001",
    headers: dict[str, str] = WORKER_A,
    sha256: str | None = None,
) -> Any:
    return h.client.put(
        f"/api/worker/runs/{run}/artifacts",
        params={"role": role, "build": build, "name": name},
        content=body,
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Sha256": sha256 or hashlib.sha256(body).hexdigest(),
        },
    )


def test_artifact_upload_and_download(h: Hub) -> None:
    _fixture_run(h)
    stl = b"solid fuss\nendsolid fuss\n"
    created = _upload(h, stl)
    assert created.status_code == 201, created.text
    record = created.json()
    assert "blob_key" not in record and record["content_type"] == "model/stl"
    assert _upload(h, stl).json() == record  # a retried upload
    assert _upload(h, stl).status_code == 200
    assert _upload(h, b"other").status_code == 409  # keys are immutable
    assert h.blobs.head("artifacts/ses-fixture0001/build-fx2/Fuss.stl") is not None
    base = "/api/runs/run-fixture0001"
    assert h.client.get(f"{base}/artifacts", headers=BROWSER).json() == [record]
    download = h.client.get(f"{base}/artifacts/{record['id']}", headers=BROWSER)
    assert download.content == stl
    assert download.headers["content-disposition"] == 'attachment; filename="Fuss.stl"'
    assert download.headers["content-security-policy"].startswith("sandbox")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert h.client.get(f"{base}/review/model/build-fx2.stl", headers=BROWSER).content == stl
    assert h.client.get(f"{base}/artifacts/art-nope", headers=BROWSER).status_code == 404
    # the content type comes from the name, never from the uploader
    page = _upload(h, b"<script>alert(1)</script>", name="x.html", role="report")
    assert page.json()["content_type"] == "application/octet-stream"
    # the upload shows in the run's timeline
    types = [
        r["event"]["type"] for r in h.client.get(f"{base}/events/history", headers=BROWSER).json()
    ]
    assert types.count("artifact.uploaded") == 2


def test_sheet_render_nearest_to_the_asked_dpi(h: Hub) -> None:
    _fixture_run(h)
    base = "/api/runs/run-fixture0001/review/sheet.png"
    assert h.client.get(base, headers=BROWSER).status_code == 404  # nothing uploaded yet
    for dpi in (150, 200):
        body = f"png {dpi}".encode()
        r = _upload(h, body, role="sheet", build="drawing-0123abcd", name=f"sheet-{dpi}.png")
        assert r.status_code == 201
    assert h.client.get(base, headers=BROWSER).content == b"png 150"
    assert h.client.get(f"{base}?dpi=190", headers=BROWSER).content == b"png 200"
    sheet = h.client.get(f"{base}?dpi=x", headers=BROWSER)
    assert sheet.content == b"png 150" and sheet.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"name": ".."}, 400),
        ({"name": "."}, 400),
        ({"name": "../../etc/passwd"}, 400),
        ({"name": "a/b.stl"}, 400),
        ({"name": "a\\b.stl"}, 400),
        ({"name": ".hidden"}, 400),
        ({"name": ""}, 400),
        ({"name": "a\x00.stl"}, 400),
        ({"name": "ä.stl"}, 400),
        ({"name": "x" * 129}, 400),
        ({"build": ".."}, 400),
        ({"build": "a/b"}, 400),
        ({"build": "%2e%2e"}, 400),
        ({"build": ""}, 400),
        ({"role": "input"}, 400),
        ({"role": "snapshot"}, 400),
        ({"role": ""}, 400),
        ({"sha256": "abc"}, 400),
        ({"sha256": "0" * 64}, 400),  # does not match the body
        ({"run": "run-unknown"}, 404),
        ({"run": "..%2F..%2Fx"}, 404),
        ({"headers": WORKER_B}, 403),
    ],
)
def test_artifact_keys_are_path_safe(h: Hub, params: dict[str, Any], status: int) -> None:
    _fixture_run(h)
    assert _upload(h, b"data", **params).status_code == status
    assert [info.key for info in h.blobs.list()] == []


def test_artifact_upload_limits(tmp_path: Path) -> None:
    with hub(tmp_path, max_artifact_bytes=10) as h:
        _fixture_run(h)
        assert _upload(h, b"x" * 11).status_code == 413
        r = h.client.put(
            "/api/worker/runs/run-fixture0001/artifacts",
            params={"role": "export", "build": "b", "name": "a.stl"},
            content=b"x",
            headers={**WORKER_A, "Content-Type": "text/plain", "X-Sha256": "0" * 64},
        )
        assert r.status_code == 415
        assert [info.key for info in h.blobs.list()] == []


def test_hub_records_are_json_models(h: Hub) -> None:
    _fixture_run(h)
    record = h.runs.get_run("run-fixture0001")
    assert record is not None and json.loads(record.model_dump_json())["worker_id"] == "worker-a"
    assert [r.run_id for r in h.runs.runs()] == ["run-fixture0001"]
