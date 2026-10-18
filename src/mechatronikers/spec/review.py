"""Review payloads between the run driver, the review API and the run table (docs/06, #20).

The run driver (the part agent or the circuit run) opens a gate with a `GateRequest` (POST to the
review API) and polls until a `GateDecision` is recorded there. Review state lives in the session
volume, so it is part of the snapshot when the session is shelved.

One contract for both run kinds (D75, docs/13): `run_kind` says which domain panels apply. The part
fields stay flat; a circuit gate carries its own optional payload in `circuit`. Everything is
additive: stored state from before SPEC_VERSION 0.5.0 has no `run_kind` and loads as a part run.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from mechatronikers.spec.circuit import CircuitGatePayload, Correction
from mechatronikers.spec.part import Dimension, Feature
from mechatronikers.spec.poi import ModelRef, ReviewAction, ReviewItem, SourceRef
from mechatronikers.spec.validation import ValidationReport

MAX_NOTE = 2000  # characters, for every free text a reviewer enters
MAX_NAME = 80
MAX_CORRECTIONS = 100  # at maximum field lengths still below the review API's 64 kB body limit

RunKind = Literal["part", "circuit"]

# spec_review = gate 1 (approve/reject the plan), result_review = gate 2 (finish or update run on
# a valid model), gave_up = the fix loop stopped without a valid model (retry with hint or abort).
GateKind = Literal["spec_review", "result_review", "gave_up"]
GateAction = Literal["approve", "reject", "finish", "update", "retry", "abort"]
ACTIONS: dict[GateKind, tuple[GateAction, ...]] = {
    "spec_review": ("approve", "reject"),
    "result_review": ("finish", "update"),
    "gave_up": ("retry", "abort"),
}


class Assumption(BaseModel):
    """A value the planner had to assume, with its reason (metric M3)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str = "note"
    value: float | str | None = None
    reason: str = ""
    feature_ids: list[str] = Field(default_factory=list)  # the features built on it (D93)
    dimension_ids: list[str] = Field(default_factory=list)  # its method="assumed" dimensions


class GateRequest(BaseModel):
    """What the reviewer decides on: the interrupt payload of one review gate."""

    id: str  # stable across agent restarts (derived from the LangGraph interrupt id)
    kind: GateKind
    run_id: str
    run_kind: RunKind = "part"
    revision: int = Field(default=1, ge=1)
    created_at: str  # RFC 3339 UTC
    part_name: str | None = None
    features: list[str] = Field(default_factory=list)  # "type id", e.g. "hole bore"
    assumptions: list[Assumption] = Field(default_factory=list)
    overall_size_dims: list[str | None] | None = None
    review_items: list[ReviewItem] = Field(default_factory=list)  # consistency check
    possibly_missing: list[ReviewItem] = Field(default_factory=list)  # coverage check (D54)
    build_id: str | None = None
    validation: ValidationReport | None = None
    failures: list[str] = Field(default_factory=list)  # gave_up: the agent's last messages
    iteration: int = 0
    max_iterations: int | None = None
    # what the review app shows: the plan in words, the ballooned sheet, the model
    plan: list[Feature] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)  # checked, with sheet positions
    sheet_size: tuple[float, float] | None = None  # mm
    drawing: str | None = None  # file in the session input folder (drawing or schematic)
    # per-kind payload of a circuit run (components, nets); part fields are the ones above
    circuit: CircuitGatePayload | None = None

    @model_validator(mode="after")
    def _kind_payload(self) -> "GateRequest":
        if self.circuit is not None and self.run_kind != "circuit":
            raise ValueError("a circuit payload belongs to a circuit run")
        return self


class ItemReview(BaseModel):
    """A reviewer's action on one point of interest."""

    item_id: str
    action: ReviewAction


class StoredItem(BaseModel):
    """A POI as the review app keeps it: the item plus where and when it came from."""

    item: ReviewItem
    revision: int = Field(ge=1)
    origin: Literal["agent", "reviewer"]
    created_at: str
    created_by: str
    proposed_value: float | str | None = None


class UpdateRequest(BaseModel):
    """Gate 2 "update": the reviewer's POIs and decisions for a new revision."""

    revision: int = Field(ge=1)  # the revision the POIs were made on
    items: list[StoredItem] = Field(default_factory=list, max_length=200)  # reviewer POIs
    actions: list[ItemReview] = Field(default_factory=list, max_length=500)  # on existing POIs
    note: str | None = Field(default=None, max_length=MAX_NOTE)

    @model_validator(mode="after")
    def _not_empty(self) -> "UpdateRequest":
        if not self.items and not self.actions and not self.note:
            raise ValueError("an update needs at least one POI, action or note")
        texts = [s.item.message for s in self.items] + [
            a.action.comment or "" for a in self.actions
        ]
        if any(len(text) > MAX_NOTE for text in texts):
            raise ValueError(f"texts are limited to {MAX_NOTE} characters")
        return self


class GateDecision(BaseModel):
    gate_id: str
    action: GateAction
    reviewer: str = Field(min_length=1, max_length=MAX_NAME)
    decided_at: str  # RFC 3339 UTC
    reason: str | None = Field(default=None, max_length=MAX_NOTE)  # required for reject
    hint: str | None = Field(default=None, max_length=MAX_NOTE)  # required for retry
    update: UpdateRequest | None = None  # required for update
    # gate 1 of a circuit run: what the run driver changes through circuit-mcp before it goes on
    # (approve) or revises (reject, #57); checked against the gate's payload by the review API
    corrections: list[Correction] = Field(default_factory=list, max_length=MAX_CORRECTIONS)

    @model_validator(mode="after")
    def _complete(self) -> "GateDecision":
        if self.action == "reject" and not (self.reason or "").strip():
            raise ValueError("a rejection needs a reason")
        if self.action == "retry" and not (self.hint or "").strip():
            raise ValueError("a retry needs a hint for the agent")
        if self.action == "update" and self.update is None:
            raise ValueError("an update needs the reviewer's changes")
        if self.corrections and self.action not in ("approve", "reject"):
            raise ValueError("corrections are recorded with gate 1 (approve or reject)")
        return self


class GateState(BaseModel):
    request: GateRequest
    decision: GateDecision | None = None
    # Builds of this session that ran without runner namespaces (D95): such a build could write
    # the review state, so the gate state may be untrusted. Set by the review API on every read
    # from the sandbox process, never taken from the stored file.
    unisolated_builds: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def open(self) -> bool:
        return self.decision is None


class NewItem(BaseModel):
    """A reviewer POI marked on the drawing (sheet_ref) or on the model (model_ref)."""

    message: str = Field(min_length=1, max_length=MAX_NOTE)
    severity: Literal["info", "warning", "blocking"] = "warning"
    reviewer: str = Field(min_length=1, max_length=MAX_NAME)
    revision: int = Field(ge=1)
    sheet_ref: SourceRef | None = None
    model_ref: ModelRef | None = None
    dimension_ids: list[str] = Field(default_factory=list, max_length=50)
    feature_ids: list[str] = Field(default_factory=list, max_length=50)
    proposed_value: float | str | None = None

    @model_validator(mode="after")
    def _located(self) -> "NewItem":
        if self.sheet_ref is None and self.model_ref is None:
            raise ValueError("mark the POI on the drawing or on the model")
        if isinstance(self.proposed_value, str) and len(self.proposed_value) > MAX_NAME:
            raise ValueError("proposed value too long")
        return self


class ItemActionRequest(BaseModel):
    action: Literal["approved", "edited", "rejected", "note"]
    reviewer: str = Field(min_length=1, max_length=MAX_NAME)
    new_value: float | str | None = None  # edited
    comment: str | None = Field(default=None, max_length=MAX_NOTE)

    @model_validator(mode="after")
    def _complete(self) -> "ItemActionRequest":
        if self.action == "edited" and self.new_value is None:
            raise ValueError("an edit needs the new value")
        if self.action == "note" and not (self.comment or "").strip():
            raise ValueError("a note needs text")
        if isinstance(self.new_value, str) and len(self.new_value) > MAX_NAME:
            raise ValueError("new value too long")
        return self


Phase = Literal[
    "queued",  # hub only: waiting for a worker, no session yet (docs/13)
    "provisioning",
    "analysing",
    "checking",  # circuit: ERC (#110)
    "waiting_gate_1",
    "building",
    "fixing",
    "exporting",  # circuit: netlists (#110)
    "simulating",  # circuit: ngspice operating point (#110)
    "gave_up",
    "aborted",
    "valid",
    "released",  # part: the reviewer finished the first article inspection (gate 2, #152)
    "update_run",
    "shelved",
    "failed",
]
# Why a run ended without a model (#92): fix-loop cap, no improvement, a failed step, budget.
StopReason = Literal["cap", "plateau", "error", "budget"]


class RunSummary(BaseModel):
    """One row of the run table, derived from the session store and the session's events."""

    session_id: str
    run_kind: RunKind = "part"  # from agent.run_started; runs from before 0.5.0 are parts
    drawing: str | None = None
    part_name: str | None = None
    state: str  # SessionState
    phase: Phase
    checks_passed: int | None = None  # scored checks of the latest build
    checks_total: int | None = None
    open_pois: int = 0
    revision: int = 1
    fix_iterations: int = 0
    max_fix_iterations: int | None = None
    model: str | None = None
    cost: float = 0.0
    unisolated_builds: int = 0  # builds that ran without runner namespaces (D95)
    started_at: str
    last_activity: str | None = None
    link: str | None = None  # only for active sessions
    stop_reason: StopReason | None = None  # set when the run ended without a model (#92)
    failing_checks: list[str] = Field(default_factory=list)  # check ids of the last build (#92)
    failed_features: list[str] = Field(default_factory=list)  # not added in the last build (#92)
    # what shelving keeps (#52): the snapshot's retention class and days, its end once shelved
    retention_class: str | None = None
    retention_days: int | None = None
    expires_at: str | None = None


class HubRun(RunSummary):
    """One row of the hub's overview (`GET /api/runs`, docs/13): the summary's fields from the
    hub's trusted records, plus the hub's run id, plan, reviewer and batch. Mirrored by `HubRun`
    in `frontend/src/api/types.ts` (#75)."""

    run_id: str
    session_id: str | None = None  # type: ignore[assignment]  # null while the run is queued
    # Never filled: the session's link carries its gateway token, which never leaves the worker
    # host (docs/13 §5). The UI links each row to `/runs/<run_id>`.
    link: None = None
    plan: str | None = None  # the plan's name (uploaded drawing or schematic)
    reviewer: str | None = None  # who decided the run's last gate at the hub (D80)
    batch: str | None = None


class RunPage(BaseModel):
    """One page of `GET /api/runs?kind=&phase=&cursor=` (hub, docs/13): rows, newest first, and
    the keyset cursor of the next page (None on the last page)."""

    runs: list[HubRun]
    next_cursor: str | None = None
