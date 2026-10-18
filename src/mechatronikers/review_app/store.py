"""Review state in the session volume (`/session/review`): gates, POIs and reviewer actions.

One JSON file per gate and per POI, written atomically, so the state survives a restart of the
sandbox process and is part of the snapshot when the session is shelved (D21). The store only
records decisions and POIs; it never changes geometry (every rebuild goes agent → cad-mcp).

`ReviewStore` holds the review rules; it keeps its records in a `ReviewRepository`: these JSON
files in mode 1 (`JsonReviewRepository`), the hub's rows of one run in mode 2 (D80, #73).
"""

import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from mechatronikers.spec import ReviewAction, ReviewItem
from mechatronikers.spec.review import (
    ACTIONS,
    GateDecision,
    GateRequest,
    GateState,
    ItemActionRequest,
    NewItem,
    StoredItem,
)
from mechatronikers.storage.repositories import ReviewRepository
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now

_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_M = TypeVar("_M", bound=BaseModel)

# Event per gate action (docs/08); gate 1 = spec review, gate 2 = result review.
_GATE_EVENTS = {
    "approve": "review.gate_passed",
    "reject": "review.rejected",
    "finish": "review.gate_passed",
    "update": "review.update_requested",
    "retry": "review.retry_requested",
    "abort": "review.aborted",
}
_GATE_NUMBER = {"spec_review": 1, "result_review": 2, "gave_up": None}
_ITEM_EVENTS = {
    "approved": "review.approved",
    "edited": "review.edited",
    "rejected": "review.rejected",
    "note": "review.note_added",
}


class ReviewNotFoundError(KeyError):
    pass


class ReviewConflictError(Exception):
    """The request is valid but not allowed in the current state (HTTP 409)."""


def check_id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError(f"invalid id {value!r}")
    return value


def _check_corrections(request: GateRequest, decision: GateDecision) -> None:
    """Corrections only on a circuit gate, and only of what that gate showed the reviewer."""
    if request.circuit is None:
        raise ValueError("corrections are only recorded on gate 1 of a circuit run")
    unknown = request.circuit.unknown_targets(decision.corrections)
    if unknown:
        raise ValueError(f"unknown correction targets: {', '.join(unknown[:10])}")


class JsonReviewRepository:
    """`ReviewRepository` as JSON files: `<root>/gates/<gate_id>.json`, `<root>/items/<id>.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._local = threading.local()
        for sub in ("gates", "items"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, id: str) -> Path:
        return self.root / kind / f"{check_id(id)}.json"

    @staticmethod
    def _write(path: Path, model: BaseModel) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(model.model_dump_json(indent=2))
        tmp.replace(path)

    @staticmethod
    def _read(path: Path, model: type[_M]) -> _M | None:
        if not path.is_file():
            return None
        return model.model_validate_json(path.read_text())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # not nested, as in the hub's repository (the contract): fail instead of nesting
        if getattr(self._local, "active", False):
            raise RuntimeError("review transactions are not nested")
        with self._lock:
            self._local.active = True
            try:
                yield
            finally:
                self._local.active = False

    def gates(self) -> list[GateState]:
        states = [
            GateState.model_validate_json(p.read_text())
            for p in (self.root / "gates").glob("*.json")
        ]
        return sorted(states, key=lambda s: s.request.created_at)

    def get_gate(self, gate_id: str) -> GateState | None:
        return self._read(self._path("gates", gate_id), GateState)

    def save_gate(self, state: GateState) -> None:
        self._write(self._path("gates", state.request.id), state)

    def items(self, revision: int | None = None) -> list[StoredItem]:
        stored = [
            StoredItem.model_validate_json(p.read_text())
            for p in (self.root / "items").glob("*.json")
        ]
        if revision is not None:
            stored = [s for s in stored if s.revision == revision]
        return sorted(stored, key=lambda s: (s.revision, s.created_at, s.item.id))

    def get_item(self, item_id: str) -> StoredItem | None:
        return self._read(self._path("items", item_id), StoredItem)

    def save_item(self, item: StoredItem) -> None:
        self._write(self._path("items", item.item.id), item)


class ReviewStore:
    """The review rules (D62, D70): gates open idempotently and are decided once, POIs are added
    and reviewed only on a valid model under review, and every change emits its event.

    `root` is the review folder: the JSON records unless a `repository` is given, and the
    rendered sheets of the review API.
    """

    def __init__(
        self,
        root: Path,
        emitter: EventEmitter,
        clock: Callable[[], str] = utc_now,
        *,
        repository: ReviewRepository | None = None,
    ) -> None:
        self.root = root
        self.emitter = emitter
        self._clock = clock
        self.repository: ReviewRepository = (
            JsonReviewRepository(root) if repository is None else repository
        )
        root.mkdir(parents=True, exist_ok=True)

    # -- gates ---------------------------------------------------------------------------

    def gates(self) -> list[GateState]:
        return self.repository.gates()

    def gate(self, gate_id: str) -> GateState:
        state = self.repository.get_gate(check_id(gate_id))
        if state is None:
            raise ReviewNotFoundError(gate_id)
        return state

    def open_gate(self, request: GateRequest) -> GateState:
        """Idempotent: an agent that restarts re-opens the same gate and gets its state back."""
        check_id(request.id)
        with self.repository.transaction():
            stored = self.repository.get_gate(request.id)
            if stored is not None:
                return stored
            state = GateState(request=request)
            self.repository.save_gate(state)
            for item in request.review_items + request.possibly_missing:
                self._register_agent_item(item, request.revision)
        self.emitter.emit(
            "review.gate_opened",
            {
                "gate_id": request.id,
                "kind": request.kind,
                "gate": _GATE_NUMBER[request.kind],
                "run_id": request.run_id,
                "run_kind": request.run_kind,
                "revision": request.revision,
            },
        )
        return state

    def decide(self, gate_id: str, decision: GateDecision) -> GateState:
        with self.repository.transaction():
            state = self.gate(gate_id)
            if decision.gate_id != gate_id:
                raise ValueError("decision is for another gate")
            if state.decision is not None:
                raise ReviewConflictError(f"gate {gate_id} is already decided")
            allowed = ACTIONS[state.request.kind]
            if decision.action not in allowed:
                raise ValueError(f"{state.request.kind} allows {', '.join(allowed)}")
            if decision.update is not None and decision.update.revision != state.request.revision:
                raise ValueError("the update must be made on the revision under review")
            if decision.corrections:
                _check_corrections(state.request, decision)
            state.decision = decision
            self.repository.save_gate(state)
        payload: dict[str, Any] = {
            "gate_id": gate_id,
            "gate": _GATE_NUMBER[state.request.kind],
            "kind": state.request.kind,
            "run_kind": state.request.run_kind,
            "action": decision.action,
            "reviewer": decision.reviewer,
            "revision": state.request.revision,
            "automatic": False,
        }
        if decision.reason:
            payload["reason"] = decision.reason
        if decision.hint:
            payload["hint"] = decision.hint
        if decision.update is not None:
            payload["items"] = len(decision.update.items)
            payload["actions"] = len(decision.update.actions)
        if decision.corrections:
            payload["corrections"] = len(decision.corrections)
        self.emitter.emit(_GATE_EVENTS[decision.action], payload)
        return state

    def _open_result_review(self, revision: int) -> bool:
        return any(
            s.open and s.request.kind == "result_review" and s.request.revision == revision
            for s in self.gates()
        )

    # -- points of interest ----------------------------------------------------------------

    def items(self, revision: int | None = None) -> list[StoredItem]:
        return self.repository.items(revision)

    def _register_agent_item(self, item: ReviewItem, revision: int) -> None:
        """Agent POIs arrive with a gate; the agent already emitted review.item_created."""
        key = check_id(f"rev{revision}-{item.id}")
        if self.repository.get_item(key) is None:
            stored = StoredItem(
                item=item.model_copy(update={"id": key}),
                revision=revision,
                origin="agent",
                created_at=self._clock(),
                created_by="agent",
            )
            self.repository.save_item(stored)

    def add_item(self, new: NewItem) -> StoredItem:
        with self.repository.transaction():
            if not self._open_result_review(new.revision):
                raise ReviewConflictError(
                    "POIs can only be added to a valid model that is under review"
                )
            item = ReviewItem(
                id=f"poi-{uuid.uuid4().hex[:10]}",
                kind="user_note",
                severity=new.severity,
                message=new.message,
                reason=f"marked by reviewer {new.reviewer}",
                dimension_ids=new.dimension_ids,
                feature_ids=new.feature_ids,
                sheet_ref=new.sheet_ref,
                model_ref=new.model_ref,
            )
            stored = StoredItem(
                item=item,
                revision=new.revision,
                origin="reviewer",
                created_at=self._clock(),
                created_by=new.reviewer,
                proposed_value=new.proposed_value,
            )
            self.repository.save_item(stored)
        self.emitter.emit(
            "review.item_created",
            {
                "item": item.model_dump(mode="json"),
                "revision": new.revision,
                "origin": "reviewer",
                "reviewer": new.reviewer,
                "proposed_value": new.proposed_value,
            },
        )
        return stored

    def act(self, item_id: str, request: ItemActionRequest) -> StoredItem:
        with self.repository.transaction():
            stored = self.repository.get_item(check_id(item_id))
            if stored is None:
                raise ReviewNotFoundError(item_id)
            if not self._open_result_review(stored.revision):
                raise ReviewConflictError("POIs can only be reviewed on a valid model")
            old = stored.proposed_value
            action = ReviewAction(
                at=self._clock(),
                by=request.reviewer,
                action=request.action,
                old_value=old if request.action == "edited" else None,
                new_value=request.new_value,
                comment=request.comment,
            )
            item = stored.item
            item.history.append(action)
            if request.action != "note":
                item.status = request.action
            if request.action == "edited":
                stored.proposed_value = request.new_value
            self.repository.save_item(stored)
        payload: dict[str, Any] = {
            "item_id": item_id,
            "revision": stored.revision,
            "origin": stored.origin,
            "reviewer": request.reviewer,
        }
        if request.action == "edited":
            payload |= {"old_value": old, "new_value": request.new_value}
        if request.comment:
            payload["comment"] = request.comment
        self.emitter.emit(_ITEM_EVENTS[request.action], payload)
        return stored
