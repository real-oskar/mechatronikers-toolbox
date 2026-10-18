"""LangGraph agent: analyse → read → check → plan (→ replan) → review gate 1 → build ⇄ fix → export.

The model is used for reading lettering, planning the feature list and fixing failed features.
Everything else is deterministic tool calls, so runs are cheap, reproducible and logged.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from mechatronikers.agent import prompts, unstated
from mechatronikers.agent.answers import answer_errors, parse_answer, repair_note
from mechatronikers.agent.budget import BudgetExceededError
from mechatronikers.agent.config import Language
from mechatronikers.agent.llm import (
    Block,
    ModelClient,
    ModelTimeoutError,
    extract_json,
    image_block,
    text_block,
)
from mechatronikers.agent.tools import SandboxTools, ToolError
from mechatronikers.spec import Feature, ReviewItem
from mechatronikers.spec.review import StopReason
from mechatronikers.telemetry import EventEmitter

LABEL_BATCH = 24
SHEET_DPI = 110
TITLE_DPI = 160
CROP_DPI = 200
MAX_REPLAN_CROPS = 8
MAX_NORMALIZED_PATHS = 20

TOOL_FOR_TYPE = {
    "extrude_base": "create_extrude_base",
    "revolve_base": "create_revolve_base",
    "hole": "add_hole",
    "hole_pattern": "add_hole_pattern",
    "pocket": "add_pocket",
    "boss": "add_boss",
    "slot": "add_slot",
    "fillet": "add_fillet",
    "chamfer": "add_chamfer",
    "script": "add_script_feature",
}

Status = Literal[
    "running", "done", "build_failed", "rejected", "aborted", "budget_exceeded", "error"
]


class AgentState(TypedDict, total=False):
    drawing: str  # path relative to the session input folder
    lang: Language  # run language of the reasons the model writes (#54); checkpointed for resume
    drawing_id: str
    summary: dict[str, Any]
    labels: list[dict[str, Any]]
    readings: dict[str, dict[str, Any]]
    title: dict[str, Any]
    consistency: dict[str, Any]
    spec: dict[str, Any]
    coverage: dict[str, Any]  # possibly missing features: drawing evidence vs plan (D54)
    failures: list[dict[str, str]]
    failure_history: list[int]
    iteration: int
    validation: dict[str, Any]
    build_notes: list[str]
    approved: bool
    revision: int  # 1 for the first model; gate 2 update runs add revisions (#20)
    update: dict[str, Any]  # gate 2 "update": the reviewer's points and decisions (UpdateRequest)
    hint: str  # a reviewer's hint after a give-up, for the next fix rounds
    artifacts: list[dict[str, Any]]
    status: Status
    error: str
    stop_reason: StopReason  # give_up: fix-loop cap or plateau (#92)


Node = Callable[["AgentState"], Awaitable["AgentState"]]


def base_failed(failures: list[dict[str, str]], spec: Mapping[str, Any]) -> bool:
    """The build stopped at the base body: nothing was validated in that round."""
    features = spec.get("features") or []
    return bool(failures and features) and failures[0]["feature_id"] == features[0]["id"]


def failing_checks(state: Mapping[str, Any]) -> list[str]:
    """Ids of the failed scored checks of the last round's build. Empty if that build stopped at
    the base body: the stored validation is then from an earlier round (#92)."""
    if base_failed(state.get("failures") or [], state.get("spec") or {}):
        return []
    report = (state.get("validation") or {}).get("report") or {}
    return [c["id"] for c in report.get("checks", []) if not c["passed"]]


def failed_features(state: Mapping[str, Any]) -> list[str]:
    """Features the last round's build could not add (#92)."""
    return [f["feature_id"] for f in state.get("failures") or [] if f["feature_id"] != "*"]


def stop_reason(state: Mapping[str, Any]) -> StopReason | None:
    """Why a run ended without a model (#92); None while it runs or after a model."""
    status = state.get("status")
    if status == "error":
        return "error"
    if status == "budget_exceeded":
        return "budget"
    if status in ("build_failed", "aborted"):
        reason: StopReason | None = state.get("stop_reason")
        return reason
    return None


def normalize_assumptions(items: Any) -> list[dict[str, Any]]:
    """Models sometimes answer assumptions as plain sentences; keep them as {id, reason}."""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items if isinstance(items, list) else [], 1):
        if isinstance(item, dict):
            out.append(
                {
                    "id": str(item.get("id") or f"a-{i}"),
                    **{k: v for k, v in item.items() if k != "id"},
                }
            )
        elif isinstance(item, str) and item.strip():
            out.append({"id": f"a-{i}", "kind": "note", "value": None, "reason": item.strip()})
    return out


def assumption_item(a: dict[str, Any]) -> ReviewItem:
    value = a.get("value")
    return ReviewItem(
        id=str(a["id"]),
        kind="assumption",
        message=f"Assumed {a.get('kind') or 'value'}" + ("" if value is None else f" = {value}"),
        reason=str(a.get("reason") or "no reason given"),
        feature_ids=list(a.get("feature_ids", [])),
        dimension_ids=list(a.get("dimension_ids", [])),
    )


def checked_dimensions(consistency: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in consistency.get("dimensions", []) if d["status"] != "rejected"]


def with_unstated(
    spec: dict[str, Any],
    consistency: dict[str, Any],
    answer: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A planning step's spec with what the drawing does not state made explicit (D93):
    assumed radii and chamfers as method="assumed" dimensions, symmetric extrusions as
    assumptions, and the features the planner saw without dimensions (append-only: an answer
    can link or add entries, never change or drop one)."""
    planned = unstated.restore_general(spec["assumptions"], (previous or {}).get("assumptions", []))
    features, assumptions, assumed = unstated.audit(
        spec["features"], planned, checked_dimensions(consistency)
    )
    seen = unstated.merge_undimensioned(
        (previous or {}).get("undimensioned", []),
        unstated.normalize_undimensioned(answer.get("undimensioned")),
    )
    return {
        **spec,
        "features": features,
        "assumptions": assumptions,
        "assumed_dimensions": assumed,
        "undimensioned": seen,
    }


def undimensioned_items(spec: dict[str, Any], views: list[dict[str, Any]]) -> list[ReviewItem]:
    """The features the planner saw without dimensions, located on the drawing (D93)."""
    features = unstated.assumable(spec.get("features", []))
    return unstated.undimensioned_items(spec.get("undimensioned", []), views, features)


def unstated_items(spec: dict[str, Any], views: list[dict[str, Any]]) -> list[ReviewItem]:
    """The assumptions and undimensioned features of a plan, as review items."""
    assumptions = [assumption_item(a) for a in spec.get("assumptions", [])]
    return assumptions + undimensioned_items(spec, views)


def unstated_context(spec: dict[str, Any]) -> str:
    """What a later planning step must keep (or update): the plan's assumptions and features
    without dimensions."""
    return (
        f"Current assumptions: {json.dumps(spec.get('assumptions', []))}\n"
        f"Features without dimensions (seen, listed for review): "
        f"{json.dumps(spec.get('undimensioned', []))}\n"
    )


def size_dims(value: Any) -> list[str | None] | None:
    """The planner's overall-size answer: per axis a dimension id or null (D53)."""
    if not isinstance(value, list) or len(value) != 3:
        return None
    return [v if isinstance(v, str) and v else None for v in value]


def note_item(index: int, note: str) -> ReviewItem:
    return ReviewItem(
        id=f"note-{index}", kind="unsupported", severity="info", message=note, reason="build note"
    )


def failure_item(index: int, failure: dict[str, str]) -> ReviewItem:
    feature = failure["feature_id"]
    return ReviewItem(
        id=f"fail-{index}",
        kind="validation_failure",
        severity="blocking",
        message=failure["message"],
        reason="the build or a scored check failed; the fix loop works on it",
        feature_ids=[] if feature == "*" else [feature],
    )


def exported_build(artifacts: list[dict[str, Any]]) -> str | None:
    """Build id of the exported STL (artifact://<session>/<build>/<file>)."""
    for ref in artifacts:
        if ref.get("format") == "stl":
            parts = str(ref.get("uri", "")).split("/")
            return parts[3] if len(parts) > 3 else None
    return None


def apply_edits(
    consistency: dict[str, Any], update: dict[str, Any], reviewer: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reviewer-edited values become the values the part is built with (method "human"); the
    printed value stays as stated. Returns the new consistency report and the changes."""
    findings = {
        str(i["id"]): i for i in consistency.get("review_items", []) + update.get("agent_items", [])
    }
    edits: dict[str, Any] = {}
    for stored in update.get("items", []):  # StoredItem: the point and the reviewer's value
        value = stored.get("proposed_value")
        for dim_id in stored["item"].get("dimension_ids", []):
            if isinstance(value, int | float):
                edits[dim_id] = value
    for entry in update.get("actions", []):
        action = entry.get("action") or {}
        finding = findings.get(str(entry.get("item_id", "")).split("-", 1)[-1])
        if action.get("action") == "edited" and isinstance(action.get("new_value"), int | float):
            for dim_id in (finding or {}).get("dimension_ids", []):
                edits[dim_id] = action["new_value"]
    changes = []
    dimensions = []
    for d in consistency.get("dimensions", []):
        if d["id"] in edits and edits[d["id"]] != d["value"]:
            new = edits[d["id"]]
            changes.append({"dimension_id": d["id"], "old_value": d["value"], "new_value": new})
            d = {
                **d,
                "value": new,
                "method": "human",
                "status": "edited",
                "reason": f"changed by {reviewer}: {d['value']} -> {new}",
            }
        dimensions.append(d)
    return {**consistency, "dimensions": dimensions}, changes


def run_language(state: AgentState) -> Language:
    """Runs checkpointed before #54 have no language: they were English."""
    return state.get("lang") or "en"


def _dimension_lines(consistency: dict[str, Any]) -> str:
    lines = []
    for d in consistency.get("dimensions", []):
        lines.append(
            f"- {d['id']}: '{d.get('stated_text')}' kind={d['kind']} value={d['value']} "
            f"measured={d.get('measured')} view={(d.get('source') or {}).get('view_id')} "
            f"status={d['status']}"
        )
    return "\n".join(lines) or "(none)"


def build_graph(
    tools: SandboxTools,
    llm: ModelClient,
    emitter: EventEmitter,
    *,
    max_fix_iterations: int = 3,
    auto_approve: bool = False,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:  # type: ignore[type-arg]
    def step(name: str, *, started: bool = True) -> Callable[[Node], Node]:
        """Node wrapper: events, budget/tool errors end the run cleanly. With `started=False`
        the node emits its own `agent.step_started`: nodes with a review gate emit it only when
        the gate opens, not again when LangGraph reruns them on resume (#66, #92)."""

        def wrap(fn: Node) -> Node:
            async def node(state: AgentState) -> AgentState:
                if started:
                    emitter.emit("agent.step_started", {"step": name})
                try:
                    update = await fn(state)
                except BudgetExceededError as exc:
                    emitter.emit(
                        "agent.budget_exceeded", {"step": name, "error": str(exc)}, level="warning"
                    )
                    return {"status": "budget_exceeded", "error": str(exc)}
                except (ToolError, ModelTimeoutError, ValueError, ValidationError) as exc:
                    emitter.emit(
                        "agent.step_failed", {"step": name, "error": str(exc)}, level="error"
                    )
                    return {"status": "error", "error": f"{name}: {exc}"}
                emitter.emit("agent.step_completed", {"step": name})
                return update

            node.__name__ = name
            return node

        return wrap

    def found(
        state: AgentState, step_name: str, items: list[ReviewItem] | list[dict[str, Any]]
    ) -> None:
        """One event per POI, so the timeline can show what each step found (#20)."""
        for raw in items:
            item = raw if isinstance(raw, ReviewItem) else ReviewItem.model_validate(raw)
            emitter.emit(
                "review.item_created",
                {
                    "step": step_name,
                    "origin": "agent",
                    "revision": state.get("revision", 1),
                    "iteration": state.get("iteration", 0),
                    "item": item.model_dump(mode="json"),
                },
            )

    def found_unstated(
        state: AgentState, step_name: str, spec: dict[str, Any], before: dict[str, Any] | None
    ) -> None:
        """Assumptions and undimensioned features this step added or changed (D93)."""
        views = state["summary"].get("views", [])
        known = [i.model_dump() for i in unstated_items(before or {}, views)]
        found(
            state,
            step_name,
            [i for i in unstated_items(spec, views) if i.model_dump() not in known],
        )

    def gate_payload(state: AgentState, kind: str, **extra: Any) -> dict[str, Any]:
        """What a reviewer decides on; the review app shows the plan, sheet and model."""
        spec = state["spec"]
        consistency = state["consistency"]
        return {
            "kind": kind,
            "revision": state.get("revision", 1),
            "part_name": spec["part_name"],
            "features": [f"{f['type']} {f['id']}" for f in spec["features"]],
            "plan": spec["features"],
            "assumptions": spec["assumptions"],
            "overall_size_dims": spec.get("overall_size_dims"),
            # findings on the drawing: the consistency check, features without dimensions (D93)
            "review_items": consistency.get("review_items", [])
            + [
                i.model_dump(mode="json")
                for i in undimensioned_items(spec, state["summary"].get("views", []))
            ],
            "possibly_missing": state.get("coverage", {}).get("review_items", []),
            "dimensions": [
                d for d in consistency.get("dimensions", []) if d["status"] != "rejected"
            ],
            "sheet_size": state["summary"].get("sheet_size"),
            "drawing": state["drawing"],
            "iteration": state.get("iteration", 0),
            **extra,
        }

    async def coverage(state: AgentState, features: list[dict[str, Any]]) -> dict[str, Any]:
        """Advisory: a failing check must not end the run."""
        try:
            report: dict[str, Any] = await tools.json(
                "check_coverage", {"drawing_id": state["drawing_id"], "features": features}
            )
            return report
        except ToolError as exc:
            emitter.emit("agent.coverage_failed", {"error": str(exc)}, level="warning")
            return {"error": str(exc), "review_items": []}

    async def ask_features(
        step_name: str, call: str, system: str, content: list[Block]
    ) -> tuple[dict[str, Any], list[Feature]]:
        """A planning step's answer and its feature list (#147). An answer that is no JSON or
        no valid feature list is asked for once more, with the original task and the errors; if
        that answer is invalid too, the step fails as before. Only the answer is repaired: a
        failed call (timeout, budget) raises from `llm.ask` and fails the step."""
        text = await llm.ask(call, system, content)
        try:
            parsed = parse_answer(text)
        except ValueError as exc:  # JSON errors and pydantic's ValidationError
            errors = answer_errors(exc)
            repair = [*content, text_block(repair_note(errors))]
            parsed = parse_answer(await llm.ask(f"{call}_repair", system, repair))
            emitter.emit(
                "agent.answer_repaired",
                {"step": step_name, "call": call, "errors": len(errors), "messages": errors[:5]},
                level="warning",
            )
        if parsed.dropped:  # explicit nulls the spec default replaced: traceable, not silent
            emitter.emit(
                "agent.answer_normalized",
                {
                    "step": step_name,
                    "call": call,
                    "count": len(parsed.dropped),
                    "paths": parsed.dropped[:MAX_NORMALIZED_PATHS],
                },
            )
        return parsed.answer, parsed.features

    async def sheet_image(state: AgentState) -> list[dict[str, Any]]:
        s = state["summary"]
        x0, y0, x1, y1 = s["drawing_area"] if "drawing_area" in s else (0, 0, *s["sheet_size"])
        sheet = await tools.blocks(
            "render_region",
            {"drawing_id": state["drawing_id"], "bbox": [x0, y0, x1, y1], "dpi": SHEET_DPI},
        )
        return [image_block(b) for b in sheet if b.get("type") == "image"]

    @step("analyse")
    async def analyse(state: AgentState) -> AgentState:
        loaded = await tools.json("load_drawing", {"path": state["drawing"]})
        labels = await tools.json("list_labels", {"drawing_id": loaded["drawing_id"]})
        return {
            "drawing_id": loaded["drawing_id"],
            "summary": loaded,
            "labels": labels if isinstance(labels, list) else [labels],
            "status": "running",
            "iteration": 0,
            "failure_history": [],
        }

    @step("read")
    async def read(state: AgentState) -> AgentState:
        drawing_id = state["drawing_id"]
        drawing_labels = [lb for lb in state["labels"] if lb["region"] == "drawing"]
        readings: dict[str, dict[str, Any]] = {}

        async def read_batch(ids: list[str], step_name: str) -> None:
            blocks = await tools.blocks(
                "render_labels", {"drawing_id": drawing_id, "label_ids": ids}
            )
            content = [text_block(f"Read these {len(ids)} labels; answer for every id.")] + [
                image_block(b) if b.get("type") == "image" else text_block(b.get("text", ""))
                for b in blocks
            ]
            answer = extract_json(await llm.ask(step_name, prompts.READ_LABELS, content))
            for item in answer.get("labels", []):
                if item.get("id") in ids:
                    readings[item["id"]] = item

        all_ids = [lb["id"] for lb in drawing_labels]
        for i in range(0, len(all_ids), LABEL_BATCH):
            await read_batch(all_ids[i : i + LABEL_BATCH], "read_labels")
        # Models sometimes skip crops in a batch: ask once more, only for the missing ones.
        missing = [i for i in all_ids if i not in readings]
        if missing:
            emitter.emit("agent.labels_missing", {"label_ids": missing})
            for i in range(0, len(missing), LABEL_BATCH):
                await read_batch(missing[i : i + LABEL_BATCH], "read_labels_retry")
        for label_id, item in readings.items():
            if item.get("readable", True) and item.get("text"):
                await tools.json(
                    "set_label_text",
                    {
                        "drawing_id": drawing_id,
                        "label_id": label_id,
                        "text": item["text"],
                        "source": "vlm",
                    },
                )
        title: dict[str, Any] = {}
        block = state["summary"].get("title_block")
        if block:
            image = await tools.blocks(
                "render_region", {"drawing_id": drawing_id, "bbox": block, "dpi": TITLE_DPI}
            )
            content = [text_block("Read this title block.")] + [
                image_block(b) for b in image if b.get("type") == "image"
            ]
            title = extract_json(await llm.ask("read_title", prompts.READ_TITLE, content))
        return {"readings": readings, "title": title}

    @step("check")
    async def check(state: AgentState) -> AgentState:
        claims = [
            {"label_id": lid, "text": r["text"], "underlined": bool(r.get("underlined"))}
            for lid, r in state["readings"].items()
            if r.get("readable", True) and r.get("text")
        ]
        report = await tools.json(
            "check_consistency",
            {
                "drawing_id": state["drawing_id"],
                "sheet_scale": state["title"].get("scale"),
                "claims": claims,
            },
        )
        found(state, "check", report.get("review_items", []))
        return {"consistency": report}

    @step("plan")
    async def plan(state: AgentState) -> AgentState:
        s = state["summary"]
        notes = [
            r["text"] for r in state["readings"].values() if r.get("text") and len(r["text"]) > 12
        ]
        review = [r["message"] for r in state["consistency"].get("review_items", [])]
        text = (
            f"Part: {state['title'].get('part_name')}, sheet scale {state['title'].get('scale')}.\n"
            f"Views (sheet mm): {json.dumps(s.get('views', []))}\n"
            f"Checked dimensions:\n{_dimension_lines(state['consistency'])}\n"
            f"Review items: {json.dumps(review)}\n"
            f"Notes on the sheet: {json.dumps(notes)}"
        )
        content = [text_block(text), *await sheet_image(state)]
        system = prompts.in_language(prompts.PLAN_SPEC, run_language(state))
        answer, features = await ask_features("plan", "plan_spec", system, content)
        planned = [f.model_dump(mode="json") for f in features]
        assumptions = normalize_assumptions(answer.get("assumptions", []))
        spec = {
            "part_name": answer.get("part_name") or state["title"].get("part_name") or "part",
            "overall_size_dims": size_dims(answer.get("overall_size_dims")),
            "features": planned,
            "assumptions": assumptions,
        }
        spec = with_unstated(spec, state["consistency"], answer)
        result = await coverage(state, spec["features"])
        found_unstated(state, "plan", spec, None)
        found(state, "plan", result["review_items"])
        return {"spec": spec, "coverage": result}

    @step("replan")
    async def replan(state: AgentState) -> AgentState:
        """One round on the coverage findings (D54): add what the drawing shows but the plan
        lacks. Kept only if it does not leave more findings than before."""
        findings = state["coverage"]["review_items"]
        size = json.dumps(state["spec"].get("overall_size_dims"))
        lines = "\n".join(f"- {i['id']}: {i['message']} ({i['reason']})" for i in findings)
        content = [
            text_block(
                f"Current features:\n{json.dumps(state['spec']['features'])}\n"
                f"overall_size_dims (x, y, z): {size}\n"
                f"Checked dimensions:\n{_dimension_lines(state['consistency'])}\n"
                + unstated_context(state["spec"])
                + f"Findings:\n{lines}\n"
                "The whole sheet follows, then a crop of the drawing for each finding."
            ),
            *await sheet_image(state),
        ]
        for item in findings[:MAX_REPLAN_CROPS]:
            bbox = (item.get("sheet_ref") or {}).get("bbox")
            if bbox:
                crop = await tools.blocks(
                    "render_region",
                    {"drawing_id": state["drawing_id"], "bbox": bbox, "dpi": CROP_DPI},
                )
                content.append(text_block(f"Finding {item['id']}:"))
                content += [image_block(b) for b in crop if b.get("type") == "image"]
        system = prompts.in_language(prompts.REPLAN_SPEC, run_language(state))
        answer, features = await ask_features("replan", "replan_spec", system, content)
        planned = [f.model_dump(mode="json") for f in features]
        assumptions = normalize_assumptions(answer.get("assumptions", state["spec"]["assumptions"]))
        audited = with_unstated(
            {**state["spec"], "features": planned, "assumptions": assumptions},
            state["consistency"],
            answer,
            state["spec"],
        )
        result = await coverage(state, audited["features"])
        counts = {"findings_before": len(findings), "findings_after": len(result["review_items"])}
        if "error" in result or counts["findings_after"] > counts["findings_before"]:
            emitter.emit("agent.replan_discarded", counts, level="warning")
            return {}
        dims = state["spec"].get("overall_size_dims")
        if "overall_size_dims" in answer:
            dims = size_dims(answer["overall_size_dims"])
        spec = {**audited, "overall_size_dims": dims}
        emitter.emit("agent.replanned", counts)
        found_unstated(state, "replan", spec, state["spec"])
        found(state, "replan", result["review_items"])
        return {"spec": spec, "coverage": result}

    def gate(payload: dict[str, Any], opened: Callable[[], None]) -> Any:
        """interrupt() raises when the gate opens and returns the decision when the node reruns
        on resume: the opening events go out once, when it raises (#66)."""
        try:
            return interrupt(payload)
        except GraphInterrupt:
            opened()
            raise

    def started(name: str) -> Callable[[], None]:
        def emit() -> None:
            emitter.emit("agent.step_started", {"step": name})

        return emit

    @step("review_gate_1", started=False)
    async def review_gate(state: AgentState) -> AgentState:
        if auto_approve:
            started("review_gate_1")()
            emitter.emit("review.gate_passed", {"gate": 1, "automatic": True, "run_kind": "part"})
            return {"approved": True}
        decision = gate(gate_payload(state, "spec_review", gate=1), started("review_gate_1"))
        # A GateDecision from the review app; the review app records the review.* event.
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = decision.get("action") == "approve" or decision.get("approved") is True
        if approved:
            return {"approved": True}
        reason = decision.get("reason") or "no reason given"
        return {"approved": False, "status": "rejected", "error": f"plan rejected: {reason}"}

    @step("build")
    async def build(state: AgentState) -> AgentState:
        spec = state["spec"]
        # The checked dimensions and the plan's method="assumed" ones (D93).
        dimensions = checked_dimensions(state["consistency"]) + spec.get("assumed_dimensions", [])
        part = await tools.json(
            "new_part",
            {
                "part_name": spec["part_name"],
                "overall_size_dims": spec.get("overall_size_dims"),
                "dimensions": dimensions,
            },
        )
        # Axes without a printed overall dimension are not checked: listed for the reviewer.
        notes: list[str] = part.get("notes", [])
        failures: list[dict[str, str]] = []
        known = {d["id"] for d in dimensions}
        for index, feature in enumerate(spec["features"]):
            feature = {
                **feature,
                "dimension_ids": [i for i in feature.get("dimension_ids", []) if i in known],
            }
            try:
                result = await tools.json(TOOL_FOR_TYPE[feature["type"]], {"feature": feature})
            except ToolError as exc:  # rejected before building (e.g. structural rule)
                result = {"applied": False, "message": str(exc)}
            if not result["applied"]:
                failures.append({"feature_id": feature["id"], "message": result["message"]})
                if index == 0:
                    # Everything else needs the base body: report it once, skip the rest.
                    skipped = [f["id"] for f in spec["features"][1:]]
                    failures.append(
                        {
                            "feature_id": "*",
                            "message": f"skipped until the base body builds: {skipped}",
                        }
                    )
                    break
        step_name = "fix" if state.get("iteration", 0) else "build"
        found(state, step_name, [note_item(i, n) for i, n in enumerate(notes, 1)])
        if base_failed(failures, spec):
            found(state, step_name, [failure_item(i, f) for i, f in enumerate(failures, 1)])
            history = [*state.get("failure_history", []), len(spec["features"])]
            return {"failures": failures, "failure_history": history, "build_notes": notes}
        validation = await tools.json("validate_part", {})
        # Every failed check blocks, the overall size too: it comes from printed dimensions (D53).
        failed = [
            f"{c['id']} (expected {c['expected']}, measured {c['measured']})"
            for c in validation["report"]["checks"]
            if not c["passed"]
        ]
        if failed and not failures:
            failures.append(
                {"feature_id": "*", "message": "validate_part failed: " + ", ".join(failed)}
            )
        found(state, step_name, [failure_item(i, f) for i, f in enumerate(failures, 1)])
        history = [*state.get("failure_history", []), len(failures)]
        return {
            "failures": failures,
            "validation": validation,
            "failure_history": history,
            "build_notes": notes,
        }

    @step("fix")
    async def fix(state: AgentState) -> AgentState:
        text = (
            f"Current features:\n{json.dumps(state['spec']['features'])}\n"
            f"overall_size_dims (x, y, z): {json.dumps(state['spec'].get('overall_size_dims'))}\n"
            f"Failures:\n{json.dumps(state['failures'])}\n"
            f"Checked dimensions:\n{_dimension_lines(state['consistency'])}\n"
            + unstated_context(state["spec"])
        )
        if state.get("hint"):
            text += f"\nA reviewer's hint for this round (follow it): {state['hint']}"
        system = prompts.in_language(prompts.FIX_SPEC, run_language(state))
        answer, features = await ask_features("fix", "fix_spec", system, [text_block(text)])
        dims = state["spec"].get("overall_size_dims")
        if "overall_size_dims" in answer:
            dims = size_dims(answer["overall_size_dims"])
        planned = [f.model_dump(mode="json") for f in features]
        spec = {
            **state["spec"],
            "overall_size_dims": dims,
            "features": planned,
            "assumptions": normalize_assumptions(
                answer.get("assumptions", state["spec"]["assumptions"])
            ),
        }
        spec = with_unstated(spec, state["consistency"], answer, state["spec"])
        result = await coverage(state, spec["features"])
        iteration = state.get("iteration", 0) + 1
        found_unstated({**state, "iteration": iteration}, "fix", spec, state["spec"])
        found({**state, "iteration": iteration}, "fix", result["review_items"])
        return {"spec": spec, "iteration": iteration, "coverage": result}

    @step("export")
    async def export(state: AgentState) -> AgentState:
        artifacts = await tools.json(
            "export",
            {"formats": ["step-ap242", "step-ap214", "stl", "features-json", "review-json"]},
        )
        views = await tools.json("render_views", {"views": ["front", "top", "iso"]})
        refs = (artifacts if isinstance(artifacts, list) else [artifacts]) + (
            views if isinstance(views, list) else [views]
        )
        return {"artifacts": refs, "status": "done", "hint": ""}

    @step("review_gate_2", started=False)
    async def result_gate(state: AgentState) -> AgentState:
        """Gate 2 (D60): finish, or an update run with the reviewer's points (valid model)."""
        if auto_approve:  # test bench: no reviewer, the run ends after export
            started("review_gate_2")()
            return {"update": {}}
        validation = state.get("validation") or {}
        decision = gate(
            gate_payload(
                state,
                "result_review",
                gate=2,
                build_id=exported_build(state.get("artifacts", [])),
                validation=validation.get("report"),
            ),
            started("review_gate_2"),
        )
        decision = decision if isinstance(decision, dict) else {}
        if decision.get("action") == "update" and decision.get("update"):
            return {"update": {**decision["update"], "reviewer": decision.get("reviewer")}}
        return {"update": {}}

    @step("revise")
    async def revise(state: AgentState) -> AgentState:
        """An update run: the reviewer's points and edits go into a new revision of the plan."""
        update = state["update"]
        reviewer = str(update.get("reviewer") or "reviewer")
        consistency, changes = apply_edits(state["consistency"], update, reviewer)
        for change in changes:
            emitter.emit("spec.dimension_changed", {**change, "by": reviewer, "method": "human"})
        points = []
        for stored in update.get("items", []):
            i, value = stored["item"], stored.get("proposed_value")
            point = (i.get("model_ref") or {}).get("point")
            points.append(
                f"- {i['id']} ({i.get('severity', 'warning')}): {i['message']}"
                + (f"; proposed value {value}" if value is not None else "")
                + (f"; dimensions {i['dimension_ids']}" if i.get("dimension_ids") else "")
                + (f"; features {i['feature_ids']}" if i.get("feature_ids") else "")
                + (f"; on the model at {point}" if point else "")
            )
        decisions = [
            f"- {a['item_id']}: {a['action'].get('action')}"
            + (
                f" -> {a['action'].get('new_value')}"
                if a["action"].get("new_value") is not None
                else ""
            )
            + (f" ({a['action'].get('comment')})" if a["action"].get("comment") else "")
            for a in update.get("actions", [])
        ]
        dims_text = _dimension_lines(consistency)
        points_text = "\n".join(points) or "(none)"
        decisions_text = "\n".join(decisions) or "(none)"
        content = [
            text_block(
                f"Current features:\n{json.dumps(state['spec']['features'])}\n"
                f"Checked dimensions (with the reviewer's edits):\n{dims_text}\n"
                + unstated_context(state["spec"])
                + f"Reviewer's points:\n{points_text}\n"
                f"Reviewer's decisions on earlier findings:\n{decisions_text}\n"
                f"Note: {update.get('note') or '(none)'}\n"
                "The whole sheet follows, then a crop for each point marked on the drawing."
            ),
            *await sheet_image(state),
        ]
        for stored in update.get("items", [])[:MAX_REPLAN_CROPS]:
            item = stored["item"]
            bbox = (item.get("sheet_ref") or {}).get("bbox")
            if bbox:
                crop = await tools.blocks(
                    "render_region",
                    {"drawing_id": state["drawing_id"], "bbox": bbox, "dpi": CROP_DPI},
                )
                content.append(text_block(f"Point {item['id']}:"))
                content += [image_block(b) for b in crop if b.get("type") == "image"]
        system = prompts.in_language(prompts.REVISE_SPEC, run_language(state))
        answer, features = await ask_features("revise", "revise_spec", system, content)
        planned = [f.model_dump(mode="json") for f in features]
        # What the model could not resolve stays visible for the next review (never invented).
        # GPT-style answers write "unresolved": null for none.
        points_given = answer.get("unresolved")
        unresolved_points = points_given if isinstance(points_given, list) else []
        unresolved = [
            ReviewItem(
                id=f"open-{i}",
                kind="unsupported",
                message=f"Not resolved: {u.get('id', '?')}",
                reason=str(u.get("reason") or "no reason given"),
            ).model_dump(mode="json")
            for i, u in enumerate(unresolved_points, 1)
            if isinstance(u, dict)
        ]
        consistency = {
            **consistency,
            "review_items": consistency.get("review_items", []) + unresolved,
        }
        dims = state["spec"].get("overall_size_dims")
        if "overall_size_dims" in answer:
            dims = size_dims(answer["overall_size_dims"])
        revision = state.get("revision", 1) + 1
        spec = {
            **state["spec"],
            "overall_size_dims": dims,
            "features": planned,
            "assumptions": normalize_assumptions(
                answer.get("assumptions", state["spec"]["assumptions"])
            ),
        }
        spec = with_unstated(spec, consistency, answer, state["spec"])
        next_state: AgentState = {**state, "revision": revision, "iteration": 0}
        result = await coverage(next_state, spec["features"])
        found_unstated(next_state, "revise", spec, state["spec"])
        found(next_state, "revise", unresolved + result["review_items"])
        emitter.emit(
            "agent.revision_started",
            {
                "revision": revision,
                "points": len(update.get("items", [])),
                "decisions": len(update.get("actions", [])),
                "edited_dimensions": len(changes),
            },
        )
        return {
            "spec": spec,
            "consistency": consistency,
            "coverage": result,
            "revision": revision,
            "iteration": 0,
            "failure_history": [],
            "update": {},
            "status": "running",
        }

    def stopped(state: AgentState) -> bool:
        return state.get("status") in ("budget_exceeded", "error", "rejected", "aborted")

    def after(next_node: str):  # type: ignore[no-untyped-def]
        return lambda state: END if stopped(state) else next_node

    def after_plan(state: AgentState) -> str:
        if stopped(state):
            return END
        return "replan" if state.get("coverage", {}).get("review_items") else "review_gate_1"

    def after_build(state: AgentState) -> str:
        if stopped(state):
            return END
        if not state["failures"]:
            return "export"
        history = state["failure_history"]
        # Give up only if two fix rounds in a row brought no improvement.
        plateau = len(history) >= 3 and history[-1] >= history[-3]
        if state.get("iteration", 0) >= max_fix_iterations or plateau:
            return "give_up"
        return "fix"

    @step("give_up", started=False)
    async def give_up(state: AgentState) -> AgentState:
        """No valid model (cap or plateau): the reviewer retries with a hint or aborts (D59).

        LangGraph runs the node again from its start when the run resumes with the decision, so
        the step's opening events go out only when the gate opens (interrupt() raises), not
        again when interrupt() returns the decision (#66).
        """

        # The same rule as after_build: the cap wins when both apply.
        iteration = state.get("iteration", 0)
        reason: StopReason = "cap" if iteration >= max_fix_iterations else "plateau"

        def opened() -> None:
            emitter.emit("agent.step_started", {"step": "give_up"})
            emitter.emit(
                "agent.build_failed",
                {
                    "failures": state["failures"],
                    "reason": reason,  # what the gate card, run table and test bench show (#92)
                    "checks": failing_checks(state),
                    "iteration": iteration,
                    "max_iterations": max_fix_iterations,
                },
                level="error",
            )

        if auto_approve:  # test bench: the run ends here
            opened()
            return {"status": "build_failed", "stop_reason": reason}
        validation = state.get("validation") or {}
        payload = gate_payload(
            state,
            "gave_up",
            failures=[f["message"] for f in state["failures"]],
            validation=validation.get("report"),
            max_iterations=max_fix_iterations,
        )
        decision = gate(payload, opened)
        decision = decision if isinstance(decision, dict) else {}
        if decision.get("action") == "retry" and decision.get("hint"):
            # A new round: the plateau history starts again.
            return {"hint": str(decision["hint"]), "iteration": 0, "failure_history": []}
        return {"status": "aborted", "error": "aborted by the reviewer", "stop_reason": reason}

    def after_result(state: AgentState) -> str:
        return "revise" if state.get("update") else END

    def after_give_up(state: AgentState) -> str:
        return END if state.get("status") in ("build_failed", "aborted") else "fix"

    graph = StateGraph(AgentState)
    for name, fn in [
        ("analyse", analyse), ("read", read), ("check", check), ("plan", plan),
        ("replan", replan), ("review_gate_1", review_gate), ("build", build), ("fix", fix),
        ("export", export), ("review_gate_2", result_gate), ("revise", revise),
        ("give_up", give_up),
    ]:  # fmt: skip
        # LangGraph's overloads do not cover async callables typed via an alias.
        graph.add_node(name, fn)  # type: ignore[call-overload]
    graph.add_edge(START, "analyse")
    graph.add_conditional_edges("analyse", after("read"))
    graph.add_conditional_edges("read", after("check"))
    graph.add_conditional_edges("check", after("plan"))
    graph.add_conditional_edges("plan", after_plan)
    graph.add_conditional_edges("replan", after("review_gate_1"))
    graph.add_conditional_edges("review_gate_1", after("build"))
    graph.add_conditional_edges("build", after_build)
    graph.add_conditional_edges("fix", after("build"))
    graph.add_conditional_edges("export", after("review_gate_2"))
    graph.add_conditional_edges("review_gate_2", after_result)
    graph.add_conditional_edges("revise", after("build"))
    graph.add_conditional_edges("give_up", after_give_up)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
