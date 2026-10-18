import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from mechatronikers.agent.runner import HttpReviewClient
from mechatronikers.review_app.api import create_review_api
from mechatronikers.review_app.store import ReviewStore
from mechatronikers.spec import ReviewItem
from mechatronikers.spec.review import GateDecision, GateRequest
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now

FINDING = ReviewItem(
    id="cov-1", kind="possibly_missing", message="View v0 shows 4 circles Ø10.5", reason="count"
)


class Api:
    def __init__(self, root: Path) -> None:
        self.events = io.StringIO()
        self.store = ReviewStore(root, EventEmitter("review-app", stream=self.events))
        self.client = TestClient(create_review_api(self.store))

    def types(self) -> list[str]:
        return [json.loads(line)["type"] for line in self.events.getvalue().splitlines()]

    def last(self, type: str) -> dict[str, Any]:
        events = [json.loads(line) for line in self.events.getvalue().splitlines()]
        payload: dict[str, Any] = [e for e in events if e["type"] == type][-1]["payload"]
        return payload


@pytest.fixture
def api(tmp_path: Path) -> Api:
    return Api(tmp_path / "review")


def _gate(id: str, kind: str = "spec_review", **extra: Any) -> dict[str, Any]:
    request = GateRequest(id=id, kind=kind, run_id="run-1", created_at=utc_now(), **extra)  # type: ignore[arg-type]
    return request.model_dump(mode="json")


def _decision(gate_id: str, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "action": action,
        "reviewer": "Anna",
        "decided_at": utc_now(),
    } | extra


def test_gate_request_and_decision_round_trip(api: Api) -> None:
    created = api.client.post("/review/gates", json=_gate("gate-a", part_name="Fuss"))
    assert created.status_code == 201 and created.json()["open"] is True
    assert api.client.get("/review/gates/gate-a").json()["request"]["part_name"] == "Fuss"
    decided = api.client.post("/review/gates/gate-a/decision", json=_decision("gate-a", "approve"))
    assert decided.status_code == 200 and decided.json()["decision"]["reviewer"] == "Anna"
    again = api.client.post("/review/gates/gate-a/decision", json=_decision("gate-a", "approve"))
    assert again.status_code == 409
    # an agent that restarts re-opens the same gate and gets the decision back
    reopened = api.client.post("/review/gates", json=_gate("gate-a"))
    assert reopened.json()["decision"]["action"] == "approve"
    assert api.types() == ["review.gate_opened", "review.gate_passed"]
    assert api.last("review.gate_passed")["gate"] == 1


def test_decisions_are_validated(api: Api) -> None:
    api.client.post("/review/gates", json=_gate("gate-b"))
    no_reason = api.client.post("/review/gates/gate-b/decision", json=_decision("gate-b", "reject"))
    assert no_reason.status_code == 422
    wrong_kind = api.client.post(
        "/review/gates/gate-b/decision", json=_decision("gate-b", "finish")
    )
    assert wrong_kind.status_code == 422 and "approve, reject" in wrong_kind.json()["detail"]
    too_long = _decision("gate-b", "reject", reason="x" * 2001)
    assert api.client.post("/review/gates/gate-b/decision", json=too_long).status_code == 422
    assert api.client.get("/review/gates/nope").status_code == 404
    assert api.client.get("/review/gates/..%2Fetc").status_code in (404, 422)
    big = {"message": "x" * 70_000}
    assert api.client.post("/review/items", json=big).status_code == 413
    rejected = _decision("gate-b", "reject", reason="wrong frame")
    assert api.client.post("/review/gates/gate-b/decision", json=rejected).status_code == 200
    assert api.last("review.rejected")["reason"] == "wrong frame"


def test_pois_only_on_a_valid_model_and_every_action_is_an_event(api: Api) -> None:
    poi = {
        "message": "Fase fehlt",
        "reviewer": "Anna",
        "revision": 1,
        "sheet_ref": {"view_id": "v0", "bbox": [10, 10, 30, 30]},
        "proposed_value": 2,
    }
    assert api.client.post("/review/items", json=poi).status_code == 409  # no valid model yet
    api.client.post(
        "/review/gates", json=_gate("gate-2", "result_review", possibly_missing=[FINDING])
    )
    items = api.client.get("/review/items?revision=1").json()
    assert [(i["item"]["id"], i["origin"]) for i in items] == [("rev1-cov-1", "agent")]
    unlocated = {k: v for k, v in poi.items() if k != "sheet_ref"}
    assert api.client.post("/review/items", json=unlocated).status_code == 422
    created = api.client.post("/review/items", json=poi)
    assert created.status_code == 201 and created.json()["item"]["kind"] == "user_note"
    poi_id = created.json()["item"]["id"]
    assert api.last("review.item_created")["item"]["message"] == "Fase fehlt"

    act = api.client.post
    assert (
        act(
            "/review/items/rev1-cov-1/actions", json={"action": "approved", "reviewer": "Anna"}
        ).json()["item"]["status"]
        == "approved"
    )
    edit = {"action": "edited", "reviewer": "Anna", "new_value": 1.5}
    assert act(f"/review/items/{poi_id}/actions", json=edit).json()["proposed_value"] == 1.5
    assert (api.last("review.edited")["old_value"], api.last("review.edited")["new_value"]) == (
        2,
        1.5,
    )
    empty_note = {"action": "note", "reviewer": "Anna"}
    assert act(f"/review/items/{poi_id}/actions", json=empty_note).status_code == 422
    note = {"action": "note", "reviewer": "Anna", "comment": "siehe Schnitt A-A"}
    assert act(f"/review/items/{poi_id}/actions", json=note).status_code == 200
    reject = {"action": "rejected", "reviewer": "Anna", "comment": "kein Fehler"}
    assert (
        act("/review/items/rev1-cov-1/actions", json=reject).json()["item"]["status"] == "rejected"
    )
    assert api.types()[-5:] == [
        "review.item_created",
        "review.approved",
        "review.edited",
        "review.note_added",
        "review.rejected",
    ]


def test_update_request_is_validated(api: Api) -> None:
    api.client.post("/review/gates", json=_gate("gate-u", "result_review", revision=2))
    decide = api.client.post
    missing = _decision("gate-u", "update")
    assert decide("/review/gates/gate-u/decision", json=missing).status_code == 422
    empty = _decision("gate-u", "update", update={"revision": 2})
    assert decide("/review/gates/gate-u/decision", json=empty).status_code == 422
    stale = _decision("gate-u", "update", update={"revision": 1, "note": "x"})
    assert decide("/review/gates/gate-u/decision", json=stale).status_code == 422
    item = FINDING.model_copy(update={"id": "poi-1", "kind": "user_note"}).model_dump(mode="json")
    stored = {"item": item, "revision": 2, "origin": "reviewer", "created_at": utc_now(),
              "created_by": "Anna", "proposed_value": 6.5}  # fmt: skip
    update = _decision("gate-u", "update", update={"revision": 2, "items": [stored]})
    assert decide("/review/gates/gate-u/decision", json=update).status_code == 200
    assert api.last("review.update_requested")["items"] == 1


def _circuit_gate(id: str) -> dict[str, Any]:
    circuit = {
        "schematic_id": "abc123",
        "sheet_size": [297, 210],
        "components": [
            {"refdes": "R1", "kind": "resistor", "value": "10k", "bbox": [30, 10, 40, 14],
             "terminals": [{"id": "R1.1", "at": [30, 12], "direction": [-1, 0]}]},
        ],
        "nets": [{"id": "n1", "name": None, "terminals": ["R1.1"], "wires": 1}],
    }  # fmt: skip
    return _gate(id, run_kind="circuit", drawing="divider.pdf", circuit=circuit)


def test_circuit_gate_and_corrections_round_trip(api: Api) -> None:
    created = api.client.post("/review/gates", json=_circuit_gate("gate-run-1-1"))
    assert created.status_code == 201
    request = created.json()["request"]
    assert request["run_kind"] == "circuit" and request["circuit"]["schematic_id"] == "abc123"
    assert api.last("review.gate_opened")["run_kind"] == "circuit"
    corrections = [
        {"target": "component", "id": "R1", "field": "value", "old": "10k", "new": "47k"},
        {"target": "terminal", "id": "R1.1", "field": "pin", "new": "1"},
        {"target": "net", "id": "n1", "field": "name", "new": "OUT"},
    ]
    decide = api.client.post
    unknown = _decision("gate-run-1-1", "approve", corrections=[{**corrections[0], "id": "R9"}])
    response = decide("/review/gates/gate-run-1-1/decision", json=unknown)
    assert response.status_code == 422 and "component R9" in response.json()["detail"]
    invalid = _decision("gate-run-1-1", "approve", corrections=[{**corrections[2], "new": "\n"}])
    assert decide("/review/gates/gate-run-1-1/decision", json=invalid).status_code == 422
    revise = _decision("gate-run-1-1", "reject", reason="R1 is 47k", corrections=corrections)
    assert decide("/review/gates/gate-run-1-1/decision", json=revise).status_code == 200
    # what the run driver reads back on its next poll (#57 applies them through circuit-mcp)
    stored = api.client.get("/review/gates/gate-run-1-1").json()["decision"]["corrections"]
    assert stored == [{"old": None, **c} for c in corrections]
    event = api.last("review.rejected")
    assert (event["run_kind"], event["corrections"], event["reason"]) == ("circuit", 3, "R1 is 47k")


def test_corrections_only_on_circuit_gates(api: Api) -> None:
    api.client.post("/review/gates", json=_gate("gate-p"))
    assert api.last("review.gate_opened")["run_kind"] == "part"
    fix = {"target": "component", "id": "R1", "field": "value", "new": "47k"}
    response = api.client.post(
        "/review/gates/gate-p/decision", json=_decision("gate-p", "approve", corrections=[fix])
    )
    assert response.status_code == 422 and "circuit run" in response.json()["detail"]
    assert api.client.get("/review/gates/gate-p").json()["open"] is True
    no_payload = {**_circuit_gate("gate-q"), "circuit": None}  # optional, but then no corrections
    assert api.client.post("/review/gates", json=no_payload).status_code == 201
    response = api.client.post(
        "/review/gates/gate-q/decision", json=_decision("gate-q", "approve", corrections=[fix])
    )
    assert response.status_code == 422


def test_state_stored_before_run_kinds_still_loads(api: Api) -> None:
    old = _gate("gate-old", part_name="Fuss")
    del old["run_kind"], old["circuit"]
    (api.store.root / "gates" / "gate-old.json").write_text(
        json.dumps({"request": old, "decision": None, "open": True})
    )
    gates = api.client.get("/review/gates").json()
    assert [(g["request"]["id"], g["request"]["run_kind"]) for g in gates] == [("gate-old", "part")]
    decided = api.client.post(
        "/review/gates/gate-old/decision", json=_decision("gate-old", "approve")
    )
    assert decided.status_code == 200 and decided.json()["decision"]["corrections"] == []


def test_http_review_client_waits_for_the_decision(api: Api) -> None:
    app = Starlette(routes=[Mount("/api", create_review_api(api.store))])

    async def scenario() -> GateDecision:
        client = HttpReviewClient(
            "http://sandbox", "tok", poll_s=0.01, transport=httpx.ASGITransport(app=app)
        )
        request = GateRequest(id="gate-c", kind="spec_review", run_id="r", created_at=utc_now())
        waiting = asyncio.create_task(client.decide(request))
        while not api.store.gates():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert not waiting.done()  # still polling
        api.store.decide(
            "gate-c",
            GateDecision(gate_id="gate-c", action="approve", reviewer="Anna", decided_at=utc_now()),
        )
        decision = await waiting
        await client.aclose()
        return decision

    assert asyncio.run(scenario()).action == "approve"


def test_sheet_and_model_files_for_the_poi_view(tmp_path: Path) -> None:
    from tests.fixtures.drawings import make_drawing

    (tmp_path / "input").mkdir()
    make_drawing(tmp_path / "input" / "sheet.pdf")
    build = tmp_path / "artifacts" / "build-abc123"
    build.mkdir(parents=True)
    (build / "Plate.stl").write_bytes(b"solid plate")
    store = ReviewStore(tmp_path / "review", EventEmitter("review-app", stream=io.StringIO()))
    client = TestClient(create_review_api(store, tmp_path / "input", tmp_path / "artifacts"))
    sheet = client.get("/review/sheet.png?dpi=60")
    assert sheet.status_code == 200 and sheet.content.startswith(b"\x89PNG")
    assert (tmp_path / "review" / "sheet-sheet-60.png").is_file()  # rendered once, then served
    model = client.get("/review/model/build-abc123.stl")
    assert model.content == b"solid plate" and model.headers["content-type"] == "model/stl"
    assert client.get("/review/model/build-ffffff.stl").status_code == 404
    assert client.get("/review/model/..%2Fsecret.stl").status_code == 404


def test_finish_inspection_leaves_open_points_open(api: Api) -> None:
    """D70, #152 (option B): finishing the inspection releases the part; unresolved points stay
    review items, nothing is approved on the reviewer's behalf."""
    second = ReviewItem(id="a-1", kind="assumption", message="Fillet R1 assumed", reason="note")
    api.client.post(
        "/review/gates", json=_gate("gate-2", "result_review", possibly_missing=[FINDING, second])
    )
    rejected = {"action": "rejected", "reviewer": "Anna", "comment": "kein Fehler"}
    api.client.post("/review/items/rev1-cov-1/actions", json=rejected)
    api.client.post("/review/gates/gate-2/decision", json=_decision("gate-2", "finish"))
    items = {i["item"]["id"]: i["item"] for i in api.client.get("/review/items").json()}
    assert (items["rev1-cov-1"]["status"], items["rev1-a-1"]["status"]) == ("rejected", "open")
    assert items["rev1-a-1"]["history"] == []
    assert api.types()[-1] == "review.gate_passed" and "review.approved" not in api.types()


def test_exported_files_are_served_read_only_from_their_build(tmp_path: Path) -> None:
    """`/review/artifacts/<build>/<file>`: exports only, from the build's own folder (#52, #119)."""
    artifacts = tmp_path / "artifacts"
    build = artifacts / "build-abc123"
    build.mkdir(parents=True)
    (build / "Plate.ap242.step").write_bytes(b"ISO-10303-21;")
    (build / "Plate.features.json").write_text("{}")
    (build / "part.brep").write_bytes(b"brep")  # not an export
    (build / ".hidden.stl").write_bytes(b"x")
    (artifacts / "drawing-1").mkdir()
    (artifacts / "drawing-1" / "sheet.stl").write_bytes(b"x")
    (tmp_path / "secret.stl").write_bytes(b"secret")
    (build / "link.stl").symlink_to(tmp_path / "secret.stl")
    outside = tmp_path / "outside"  # a build folder that is a symlink out of artifacts/
    outside.mkdir()
    (outside / "Plate.ap242.step").write_bytes(b"ISO-10303-21; outside")
    (artifacts / "build-def456").symlink_to(outside)
    events = io.StringIO()
    store = ReviewStore(tmp_path / "review", EventEmitter("review-app", stream=events))
    client = TestClient(create_review_api(store, tmp_path / "input", artifacts))

    step = client.get("/review/artifacts/build-abc123/Plate.ap242.step")
    assert step.status_code == 200 and step.content == b"ISO-10303-21;"
    assert step.headers["content-type"] == "application/step"
    assert 'filename="Plate.ap242.step"' in step.headers["content-disposition"]
    features = client.get("/review/artifacts/build-abc123/Plate.features.json")
    assert features.headers["content-type"] == "application/json"
    for path in (
        "build-abc123/part.brep",
        "build-abc123/.hidden.stl",
        "build-abc123/link.stl",
        "build-def456/Plate.ap242.step",
        "build-abc123/missing.stl",
        "drawing-1/sheet.stl",
        "build-abc123/..%2F..%2Fsecret.stl",
        "..%2Fbuild-abc123/Plate.ap242.step",
        "build-abc123/%2E%2E",
    ):
        assert client.get(f"/review/artifacts/{path}").status_code == 404, path
    assert client.post("/review/artifacts/build-abc123/Plate.ap242.step").status_code == 405
    downloads = [json.loads(line) for line in events.getvalue().splitlines()]
    assert [e["payload"] for e in downloads if e["type"] == "artifact.downloaded"] == [
        {"build_id": "build-abc123", "file": "Plate.ap242.step"},
        {"build_id": "build-abc123", "file": "Plate.features.json"},
    ]
    assert (
        TestClient(create_review_api(store))
        .get("/review/artifacts/build-abc123/Plate.ap242.step")
        .status_code
        == 404
    )


def _exports(tmp_path: Path) -> tuple[TestClient, io.StringIO, Path]:
    artifacts = tmp_path / "artifacts"
    build = artifacts / "build-abc123"
    build.mkdir(parents=True)
    for name, data in {
        "Plate.ap242.step": b"ISO-10303-21; AP242",
        "Plate.ap214.step": b"ISO-10303-21; AP214",
        "Plate.stl": b"solid Plate",
        "Plate.features.json": b"{}",
        "Plate.review.json": b"[]",
        "part.brep": b"brep",  # not an export
        ".hidden.stl": b"x",
    }.items():
        (build / name).write_bytes(data)
    (tmp_path / "secret.stl").write_bytes(b"secret")
    (build / "link.stl").symlink_to(tmp_path / "secret.stl")
    (artifacts / "build-def456").mkdir()
    (artifacts / "build-def456" / "part.brep").write_bytes(b"brep")
    events = io.StringIO()
    store = ReviewStore(tmp_path / "review", EventEmitter("review-app", stream=events))
    return TestClient(create_review_api(store, tmp_path / "input", artifacts)), events, build


def _downloads(events: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in events.getvalue().splitlines()]
    return [e["payload"] for e in lines if e["type"] == "artifact.downloaded"]


def test_a_download_records_the_revision_it_was_made_from(tmp_path: Path) -> None:
    """`artifact.downloaded` names file, build and the run view's revision (#52)."""
    client, events, _ = _exports(tmp_path)
    assert client.get("/review/artifacts/build-abc123/Plate.stl?revision=2").status_code == 200
    for bad in ("0", "-1", "x", "²", "10000"):
        url = f"/review/artifacts/build-abc123/Plate.stl?revision={bad}"
        assert client.get(url).status_code == 422, bad
    assert _downloads(events) == [{"build_id": "build-abc123", "file": "Plate.stl", "revision": 2}]


def test_zip_of_all_exports_of_a_build(tmp_path: Path) -> None:
    """`/review/artifacts/<build>.zip`: every export of the build, nothing else (#52)."""
    client, events, build = _exports(tmp_path)
    response = client.get("/review/artifacts/build-abc123.zip?revision=3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="build-abc123.zip"' in response.headers["content-disposition"]
    names = [
        "Plate.ap214.step",
        "Plate.ap242.step",
        "Plate.features.json",
        "Plate.review.json",
        "Plate.stl",
    ]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == names  # no brep, no hidden file, no link out of the folder
        assert all(archive.read(n) == (build / n).read_bytes() for n in names)
    assert _downloads(events) == [
        {"build_id": "build-abc123", "file": "build-abc123.zip", "files": names, "revision": 3}
    ]
    assert list((tmp_path / "review" / "tmp").iterdir()) == []  # the temporary ZIP is gone

    for path in ("build-def456", "build-000000", "drawing-1", "..", "build-abc123%2F.."):
        assert client.get(f"/review/artifacts/{path}.zip").status_code == 404, path
    assert client.get("/review/artifacts/build-abc123.zip?revision=x").status_code == 422
    assert len(_downloads(events)) == 1


def test_zip_refuses_oversized_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, events, _ = _exports(tmp_path)
    monkeypatch.setattr("mechatronikers.review_app.api.MAX_ZIP_BYTES", 10)
    assert client.get("/review/artifacts/build-abc123.zip").status_code == 413
    assert _downloads(events) == []


def test_head_requests_are_not_downloads(tmp_path: Path) -> None:
    """Link previews and download managers probe with HEAD: no event, and no ZIP built."""
    client, events, _ = _exports(tmp_path)
    single = client.head("/review/artifacts/build-abc123/Plate.stl?revision=1")
    assert single.status_code == 200 and single.headers["content-type"] == "model/stl"
    whole = client.head("/review/artifacts/build-abc123.zip?revision=1")
    assert whole.status_code == 200 and whole.headers["content-type"] == "application/zip"
    assert 'filename="build-abc123.zip"' in whole.headers["content-disposition"]
    assert client.head("/review/artifacts/build-def456.zip").status_code == 404
    assert _downloads(events) == []
    assert not (tmp_path / "review" / "tmp").exists()
