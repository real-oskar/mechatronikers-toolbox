"""Contract of `ReviewRepository` (storage.repositories), run against every implementation, and
the review rules of `ReviewStore` on top of each one."""

import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mechatronikers.review_app.store import (
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewStore,
)
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
from mechatronikers.storage import ReviewRepository
from mechatronikers.telemetry import EventEmitter
from tests.fixtures.stores import REVIEW_REPOSITORIES, Factory

T0 = "2026-10-17T08:00:00.000000Z"
FINDING = ReviewItem(id="cov-1", kind="possibly_missing", message="4 circles Ø10.5", reason="count")


@pytest.fixture(params=REVIEW_REPOSITORIES)
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ReviewRepository]:
    factory: Factory = request.param
    with factory(tmp_path) as store:
        yield store


def _at(minute: int) -> str:
    return f"2026-10-17T08:{minute:02d}:00.000000Z"


def _request(id: str, created_at: str = T0, **extra: Any) -> GateRequest:
    fields: dict[str, Any] = {"id": id, "kind": "spec_review", "run_id": "run-1"}
    return GateRequest.model_validate(fields | {"created_at": created_at} | extra)


def _decision(gate_id: str, action: str = "approve", **extra: Any) -> GateDecision:
    fields: dict[str, Any] = {"gate_id": gate_id, "action": action, "reviewer": "Anna"}
    return GateDecision.model_validate(fields | {"decided_at": _at(30)} | extra)


def _item(id: str, revision: int = 1, created_at: str = T0, **extra: Any) -> StoredItem:
    item = FINDING.model_copy(update={"id": id})
    fields: dict[str, Any] = {"origin": "agent", "created_by": "agent"}
    return StoredItem.model_validate(
        fields | {"item": item, "revision": revision, "created_at": created_at} | extra
    )


def _circuit_gate(id: str) -> GateRequest:
    payload = CircuitGatePayload(
        schematic_id="sch-1",
        sheet_size=(297.0, 210.0),
        components=[Component(refdes="R1", kind="resistor", value="10k", bbox=(1, 2, 3, 4))],
        nets=[NetSummary(id="n1", name="VIN", terminals=["R1.1"], wires=2)],
    )
    return _request(id, run_kind="circuit", circuit=payload, drawing="divider.pdf")


# -- repository --------------------------------------------------------------------------


def test_empty_repository(repo: ReviewRepository) -> None:
    assert repo.gates() == [] and repo.items() == []
    assert repo.get_gate("gate-a") is None and repo.get_item("poi-a") is None


def test_gates_round_trip_and_are_ordered_by_creation(repo: ReviewRepository) -> None:
    late = GateState(request=_request("gate-late", _at(5), kind="result_review", revision=2))
    circuit = GateState(
        request=_circuit_gate("gate-circuit"),
        decision=_decision(
            "gate-circuit",
            corrections=[Correction(target="component", id="R1", field="value", new="47k")],
        ),
    )
    early = GateState(request=_request("gate-early", _at(1), part_name="Fuss"))
    for state in (late, circuit, early):
        repo.save_gate(state)
    assert repo.get_gate("gate-circuit") == circuit
    assert [g.request.id for g in repo.gates()] == ["gate-circuit", "gate-early", "gate-late"]
    assert repo.gates()[2] == late


def test_save_gate_replaces_it(repo: ReviewRepository) -> None:
    state = GateState(request=_request("gate-a"))
    repo.save_gate(state)
    state.decision = _decision("gate-a")
    repo.save_gate(state)
    assert repo.get_gate("gate-a") == state and len(repo.gates()) == 1
    stored = repo.get_gate("gate-a")
    assert stored is not None and not stored.open


def test_items_round_trip_filter_and_order(repo: ReviewRepository) -> None:
    sheet = SourceRef(bbox=(10.0, 20.0, 30.0, 40.0), view_id="front")
    reviewer = _item(
        "poi-b",
        2,
        _at(3),
        origin="reviewer",
        created_by="Anna",
        proposed_value=6.5,
        item=FINDING.model_copy(update={"id": "poi-b", "kind": "user_note", "sheet_ref": sheet}),
    )
    for stored in (reviewer, _item("rev1-b", 1, _at(2)), _item("rev2-a", 2, _at(3)),
                   _item("rev1-a", 1, _at(2))):  # fmt: skip
        repo.save_item(stored)
    assert repo.get_item("poi-b") == reviewer
    assert [s.item.id for s in repo.items()] == ["rev1-a", "rev1-b", "poi-b", "rev2-a"]
    assert [s.item.id for s in repo.items(2)] == ["poi-b", "rev2-a"]
    assert repo.items(3) == []


def test_save_item_replaces_it(repo: ReviewRepository) -> None:
    stored = _item("rev1-a")
    repo.save_item(stored)
    stored.item.status = "edited"
    stored.item.history.append(ReviewAction(at=_at(9), by="Anna", action="edited", new_value=7.0))
    stored.proposed_value = 7.0
    repo.save_item(stored)
    assert repo.get_item("rev1-a") == stored and len(repo.items()) == 1


def test_transaction_allows_reads_and_writes_inside(repo: ReviewRepository) -> None:
    with repo.transaction():
        repo.save_gate(GateState(request=_request("gate-a")))
        repo.save_item(_item("rev1-a"))
        assert [g.request.id for g in repo.gates()] == ["gate-a"]
        assert repo.get_item("rev1-a") is not None


def test_transactions_do_not_interleave(repo: ReviewRepository) -> None:
    order: list[str] = []

    def other() -> None:
        with repo.transaction():
            order.append("other")

    with repo.transaction():
        thread = threading.Thread(target=other)
        thread.start()
        time.sleep(0.2)  # the other thread tries meanwhile
        order.append("first")
    thread.join(timeout=5)
    assert order == ["first", "other"]


def test_transactions_are_not_nested(repo: ReviewRepository) -> None:
    """Nesting fails the same way everywhere, so mode 1 code cannot come to rely on it."""
    with repo.transaction():
        repo.save_gate(GateState(request=_request("gate-a")))
        with pytest.raises(RuntimeError, match="not nested"), repo.transaction():
            pass
    assert repo.get_gate("gate-a") is not None
    with repo.transaction():  # usable again afterwards
        assert len(repo.gates()) == 1


# -- review rules on top of the repository -----------------------------------------------


class Events(io.StringIO):
    def types(self) -> list[str]:
        return [json.loads(line)["type"] for line in self.getvalue().splitlines()]


@pytest.fixture
def store(repo: ReviewRepository, tmp_path: Path) -> tuple[ReviewStore, Events]:
    events = Events()
    emitter = EventEmitter("review-app", stream=events)
    return ReviewStore(tmp_path / "cache", emitter, lambda: T0, repository=repo), events


def test_store_keeps_its_records_in_the_repository(
    store: tuple[ReviewStore, Events], repo: ReviewRepository
) -> None:
    review, events = store
    request = _request("gate-1", review_items=[FINDING])
    opened = review.open_gate(request)
    assert repo.get_gate("gate-1") == opened
    assert [s.item.id for s in repo.items()] == ["rev1-cov-1"]  # agent POIs, keyed per revision
    assert review.open_gate(request) == opened  # idempotent: a restarted driver
    assert events.types() == ["review.gate_opened"]
    review.decide("gate-1", _decision("gate-1"))
    stored = repo.get_gate("gate-1")
    assert stored is not None and stored.decision is not None
    with pytest.raises(ReviewConflictError):
        review.decide("gate-1", _decision("gate-1"))
    assert events.types() == ["review.gate_opened", "review.gate_passed"]


def test_store_rules_hold_on_every_repository(store: tuple[ReviewStore, Events]) -> None:
    review, events = store
    with pytest.raises(ReviewNotFoundError):
        review.gate("gate-unknown")
    with pytest.raises(ValueError):
        review.gate("../escape")
    sheet = SourceRef(bbox=(0, 0, 1, 1))
    note = NewItem(message="burr", reviewer="Anna", revision=2, sheet_ref=sheet)
    with pytest.raises(ReviewConflictError):  # no valid model under review yet
        review.add_item(note)
    review.open_gate(_request("gate-2", kind="result_review", revision=2))
    added = review.add_item(note)
    edited = review.act(
        added.item.id, ItemActionRequest(action="edited", reviewer="Ben", new_value=4.2)
    )
    assert edited.proposed_value == 4.2 and edited.item.status == "edited"
    assert review.items(2) == [edited]
    with pytest.raises(ReviewNotFoundError):
        review.act("poi-unknown", ItemActionRequest(action="approved", reviewer="Ben"))
    review.decide("gate-2", _decision("gate-2", "finish"))
    with pytest.raises(ReviewConflictError):  # the model is no longer under review
        review.act(added.item.id, ItemActionRequest(action="approved", reviewer="Ben"))
    assert events.types()[-3:] == ["review.item_created", "review.edited", "review.gate_passed"]


def test_store_checks_circuit_corrections_against_the_gate(
    store: tuple[ReviewStore, Events],
) -> None:
    review, _ = store
    review.open_gate(_circuit_gate("gate-c"))
    unknown = Correction(target="net", id="n9", field="name", new="VOUT")
    with pytest.raises(ValueError, match="unknown correction targets"):
        review.decide("gate-c", _decision("gate-c", corrections=[unknown]))
    known = Correction(target="net", id="n1", field="name", new="VOUT")
    decided = review.decide("gate-c", _decision("gate-c", corrections=[known]))
    assert decided.decision is not None and decided.decision.corrections == [known]
