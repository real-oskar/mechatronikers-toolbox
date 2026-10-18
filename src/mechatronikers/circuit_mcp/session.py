"""State behind the circuit-mcp tools: loaded schematics, edits, checks and exports (docs/12).

Each schematic's CircuitSpec is written to `<artifacts>/circuit/<id>/circuit.json` after every
change, so it is part of the session snapshot and the review page reads the same state.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from mechatronikers.circuit_mcp.analysis import analyse_schematic
from mechatronikers.circuit_mcp.checks import check_circuit
from mechatronikers.circuit_mcp.export import needed_models, to_kicad_netlist, to_ltspice, to_spice
from mechatronikers.circuit_mcp.simulate import SimulationResult, run_op
from mechatronikers.drawing_mcp.vector import render_region, render_sheet
from mechatronikers.spec.circuit import (
    BBox,
    CircuitArtifact,
    CircuitGatePayload,
    CircuitSpec,
    Component,
    ComponentKind,
    Correction,
    ErcReport,
    Net,
    PinRole,
)
from mechatronikers.spec.poi import ReviewAction, ReviewItem
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now

MAX_RENDER_MM = 500.0
MAX_DPI = 600
ExportFormat = Literal["spice", "ltspice", "kicad-netlist", "circuit-json"]
EXTENSIONS: dict[ExportFormat, str] = {
    "spice": ".cir",
    "ltspice": ".asc",
    "kicad-netlist": ".net",
    "circuit-json": ".circuit.json",
}


class RenderedImage(BaseModel):
    uri: str
    path: str
    bbox: BBox
    dpi: float
    sha256: str
    png: bytes  # returned to the model as MCP image content, not as JSON


class ComponentUpdate(BaseModel):
    """A correction by the model or a reviewer. Only the given fields change."""

    kind: ComponentKind | None = None
    value: str | None = None
    params: str | None = None
    polarity: Literal["npn", "pnp", "n", "p"] | None = None
    footprint: str | None = None
    roles: dict[str, PinRole] | None = None  # terminal id → role
    pins: dict[str, str] | None = None  # terminal id → pin number


class ReviewAppliedError(Exception):
    """A gate's corrections were applied before (HTTP 409)."""


class ExportResult(BaseModel):
    artifacts: list[CircuitArtifact]
    notes: list[str]  # what the files leave out or assume; also written into them as comments
    erc: ErcReport


class CircuitSession:
    def __init__(self, input_root: Path, artifacts_dir: Path, emitter: EventEmitter) -> None:
        self.input_root = input_root.resolve()
        self.artifacts_dir = artifacts_dir
        self.emitter = emitter
        self.specs: dict[str, CircuitSpec] = {}
        self.sources: dict[str, Path] = {}
        self.applied_gates: set[str] = set()  # gates whose corrections are in

    # -- loading -------------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        candidate = (self.input_root / path).resolve()
        if not candidate.is_relative_to(self.input_root):
            raise ValueError(f"schematics must be inside {self.input_root}")
        if not candidate.is_file():
            raise FileNotFoundError(f"no such schematic: {path}")
        if candidate.suffix.lower() != ".pdf":
            raise ValueError("only vector PDF schematics are supported so far (raster path: #22)")
        return candidate

    def load(self, path: str) -> tuple[str, CircuitSpec]:
        source = self._resolve(path)
        spec = analyse_schematic(source)
        schematic_id = spec.source_sha256[:12]
        self.specs[schematic_id] = spec
        self.sources[schematic_id] = source
        self._save(schematic_id)
        self.emitter.emit(
            "circuit.loaded",
            {
                "schematic_id": schematic_id,
                "file": source.name,
                "components": len(spec.components),
                "nets": len(spec.nets),
                "review_items": len(spec.review_items),
                **spec.stats.model_dump(),
            },
        )
        self._sheet_artifacts(schematic_id)
        return schematic_id, spec

    def _sheet_artifacts(self, schematic_id: str) -> None:
        """The sheet as PNG artifacts (role `sheet`), rendered at analyse time like a drawing's
        (docs/13), so the review UI never needs the PDF outside the sandbox."""
        try:
            renders = render_sheet(self.sources[schematic_id], self._dir(schematic_id))
        except Exception as exc:  # a convenience for the hub: never fail the load for it
            self.emitter.emit(
                "artifact.render_failed",
                {"role": "sheet", "schematic_id": schematic_id, "error": str(exc)[:300]},
                level="warning",
            )
            return
        for dpi, out in renders:
            data = out.read_bytes()
            self.emitter.emit(
                "artifact.created",
                {
                    "uri": self._uri(schematic_id, out.name),
                    "path": str(out),
                    "format": "png",
                    "role": "sheet",
                    "dpi": dpi,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "schematic_id": schematic_id,
                },
            )

    def get(self, schematic_id: str) -> CircuitSpec:
        if schematic_id not in self.specs:
            raise KeyError(f"unknown schematic {schematic_id!r}; load it first")
        return self.specs[schematic_id]

    def _dir(self, schematic_id: str) -> Path:
        out = self.artifacts_dir / "circuit" / schematic_id
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _save(self, schematic_id: str) -> Path:
        out = self._dir(schematic_id) / "circuit.json"
        out.write_text(self.specs[schematic_id].model_dump_json(indent=1))
        return out

    def _uri(self, schematic_id: str, name: str) -> str:
        return f"artifact://{self.emitter.session_id or 'local'}/circuit/{schematic_id}/{name}"

    # -- reading -------------------------------------------------------------------------

    def render(self, schematic_id: str, bbox: BBox, dpi: float = 300) -> RenderedImage:
        self.get(schematic_id)
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError("bbox must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0")
        if max(x1 - x0, y1 - y0) > MAX_RENDER_MM:
            raise ValueError(f"region larger than {MAX_RENDER_MM} mm")
        dpi = min(dpi, MAX_DPI)
        name = f"region-{hashlib.sha256(f'{bbox}{dpi}'.encode()).hexdigest()[:12]}.png"
        out = render_region(self.sources[schematic_id], bbox, self._dir(schematic_id) / name, dpi)
        data = out.read_bytes()
        return RenderedImage(
            uri=self._uri(schematic_id, name),
            path=str(out),
            bbox=bbox,
            dpi=dpi,
            sha256=hashlib.sha256(data).hexdigest(),
            png=data,
        )

    def render_component(self, schematic_id: str, refdes: str, dpi: float = 400) -> RenderedImage:
        comp = self.get(schematic_id).component(refdes)
        x0, y0, x1, y1 = comp.bbox
        return self.render(schematic_id, (x0 - 6, y0 - 6, x1 + 6, y1 + 6), dpi)

    # -- edits ---------------------------------------------------------------------------

    def update_component(
        self,
        schematic_id: str,
        refdes: str,
        update: ComponentUpdate,
        source: str = "vlm",
        gate_id: str | None = None,
    ) -> Component:
        """`gate_id`: a reviewer's correction from that gate, applied by the run driver; it is
        recorded as a human change."""
        source = _source(source, gate_id)
        spec = self.get(schematic_id)
        comp = spec.component(refdes)
        terminals = {t.id: t for t in comp.terminals}
        for tid in {**(update.roles or {}), **(update.pins or {})}:
            if tid not in terminals:
                raise KeyError(f"{refdes} has no terminal {tid!r}; known: {', '.join(terminals)}")
        changes = update.model_dump(exclude_none=True)
        old: dict[str, Any] = {}
        for field in ("kind", "value", "params", "polarity", "footprint"):
            if field in changes:
                old[field] = getattr(comp, field)
                setattr(comp, field, changes[field])
        for tid, role in (update.roles or {}).items():
            old[f"{tid}.role"] = terminals[tid].role
            terminals[tid].role = role
        for tid, pin in (update.pins or {}).items():
            old[f"{tid}.pin"] = terminals[tid].pin
            terminals[tid].pin = pin
        comp.status = "edited"
        comp.source = "human" if source == "human" else "vlm"
        self._save(schematic_id)
        payload = {"schematic_id": schematic_id, "refdes": refdes, "changes": changes, "old": old,
                   "by": source}  # fmt: skip
        self.emitter.emit("circuit.component_updated", _with_gate(payload, gate_id))
        return comp

    def set_net_name(
        self,
        schematic_id: str,
        net_id: str,
        name: str,
        source: str = "vlm",
        gate_id: str | None = None,
    ) -> Net:
        source = _source(source, gate_id)
        if not name or len(name) > 64 or not name.isprintable():
            raise ValueError("net names are 1-64 printable characters")
        net = self.get(schematic_id).net(net_id)
        old, net.name = net.name, name
        self._save(schematic_id)
        payload = {"schematic_id": schematic_id, "net_id": net_id, "old": old, "new": name,
                   "by": source}  # fmt: skip
        self.emitter.emit("circuit.net_renamed", _with_gate(payload, gate_id))
        return net

    def apply_corrections(
        self,
        payload: CircuitGatePayload,
        corrections: list[Correction],
        gate_id: str,
        reviewer: str = "reviewer",
    ) -> int:
        """A decided gate's corrections as the reviewer's changes (D75, #57). All of them are
        checked against the gate and the circuit first, so a bad one changes nothing. A value
        given to a component that had none resolves the item that said so (#129)."""
        if gate_id in self.applied_gates:
            raise ReviewAppliedError(f"the corrections of {gate_id} are already applied")
        unknown = payload.unknown_targets(corrections)
        if unknown:
            raise ValueError(f"corrections name what the gate did not show: {', '.join(unknown)}")
        spec = self.get(payload.schematic_id)
        calls = correction_calls(payload, corrections)
        updates: list[tuple[str, ComponentUpdate]] = []
        for tool, args in calls:  # every check before the first change
            if tool == "set_net_name":
                spec.net(args["net_id"])
                continue
            terminals = {t.id for t in spec.component(args["refdes"]).terminals}
            update = ComponentUpdate.model_validate(
                {k: v for k, v in args.items() if k not in ("schematic_id", "refdes")}
            )
            missing = set(update.roles or {}) | set(update.pins or {})
            if missing - terminals:
                raise KeyError(f"{args['refdes']} has no terminal {sorted(missing - terminals)}")
            updates.append((args["refdes"], update))
        had_no_value = {c.refdes for c in spec.components if c.value is None}
        for refdes, update in updates:
            self.update_component(payload.schematic_id, refdes, update, gate_id=gate_id)
        for tool, args in calls:
            if tool == "set_net_name":
                self.set_net_name(
                    payload.schematic_id, args["net_id"], args["name"], gate_id=gate_id
                )
        self.applied_gates.add(gate_id)
        for c in corrections:
            if c.target == "component" and c.field == "value" and c.id in had_no_value:
                self._resolve_missing_value(payload.schematic_id, c.id, c.new, reviewer, gate_id)
        return len(corrections)

    def _resolve_missing_value(
        self, schematic_id: str, refdes: str, value: str, reviewer: str, gate_id: str
    ) -> None:
        """The analysis's "No value found for <refdes>" is its only `possibly_missing` item about
        exactly one component (`_review` in analysis.py): once the reviewer gave that component a
        value, the item is resolved by their correction, recorded with the gate (#129, #152).
        Every other item stays open until the reviewer decides it (D70)."""
        for item in self.get(schematic_id).review_items:
            missing = item.kind == "possibly_missing" and item.feature_ids == [refdes]
            if missing and item.status == "open":
                note = f"resolved by the correction at {gate_id}: value {value}"
                self.review(schematic_id, item.id, "resolve", note, reviewer, gate_id)

    def review(
        self,
        schematic_id: str,
        item_id: str,
        action: Literal["approve", "reject", "resolve", "note"],
        note: str = "",
        actor: str = "reviewer",
        gate_id: str | None = None,
    ) -> ReviewItem:
        spec = self.get(schematic_id)
        item = next((r for r in spec.review_items if r.id == item_id), None)
        if item is None:
            raise KeyError(f"unknown review item {item_id!r}")
        done: dict[str, Literal["approved", "rejected", "resolved"]] = {
            "approve": "approved",
            "reject": "rejected",
            "resolve": "resolved",
        }
        if action != "note":
            item.status = done[action]
        item.history.append(
            ReviewAction(
                at=utc_now(),
                by=actor,
                action=done.get(action, "note"),
                comment=note[:2000] or None,
            )
        )
        self._save(schematic_id)
        # Same event names as the Drawing2CAD review app (review_app/store.py).
        kind = {"approve": "approved", "reject": "rejected", "resolve": "resolved"}
        payload = {"schematic_id": schematic_id, "item_id": item_id, "by": actor}
        if gate_id:  # decided at a gate (docs/12)
            payload["gate_id"] = gate_id
        self.emitter.emit(f"review.{kind.get(action, 'note_added')}", payload)
        return item

    # -- checks and exports --------------------------------------------------------------

    def check(self, schematic_id: str) -> ErcReport:
        report = check_circuit(self.get(schematic_id))
        self.emitter.emit(
            "circuit.checked",
            {
                "schematic_id": schematic_id,
                "passed": report.passed,
                "score": report.score,
                "failed": [c.id for c in report.checks if not c.passed],
            },
        )
        return report

    def simulate(self, schematic_id: str) -> SimulationResult:
        """DC operating point with ngspice; refused while parts are missing from the netlist."""
        text, notes = to_spice(self.get(schematic_id))
        skipped = [n for n in notes if not n.startswith("directive")]
        models = needed_models(text)
        if skipped:
            result = SimulationResult(
                ok=False, reason="not every part is in the netlist: " + "; ".join(skipped)[:300]
            )
        elif models:  # ngspice has no models of its own: it would stop at the first one (#152)
            result = SimulationResult(
                ok=False,
                reason=f"no SPICE model for {', '.join(models[:10])}: "
                "the netlist needs a .model or .subckt for it",
            )
        else:
            result = run_op(text, self._dir(schematic_id) / "sim")
        self.emitter.emit(
            "circuit.simulated",
            {
                "schematic_id": schematic_id,
                "ok": result.ok,
                "reason": result.reason[:300],
                "node_voltages": dict(list(result.node_voltages.items())[:50]),
            },
        )
        return result

    def export(self, schematic_id: str, formats: list[ExportFormat]) -> ExportResult:
        spec = self.get(schematic_id)
        stem = Path(spec.source_file).stem
        notes: list[str] = []
        artifacts: list[CircuitArtifact] = []
        for fmt in dict.fromkeys(formats):
            if fmt == "spice":
                text, fmt_notes = to_spice(spec)
            elif fmt == "ltspice":
                text, fmt_notes = to_ltspice(spec)
            elif fmt == "kicad-netlist":
                text, fmt_notes = to_kicad_netlist(spec)
            else:
                text, fmt_notes = json.dumps(spec.model_dump(mode="json"), indent=1), []
            out = self._dir(schematic_id) / f"{stem}{EXTENSIONS[fmt]}"
            out.write_text(text, encoding="utf-8")
            data = out.read_bytes()
            artifacts.append(
                CircuitArtifact(
                    format=fmt,
                    uri=self._uri(schematic_id, out.name),
                    path=str(out),
                    sha256=hashlib.sha256(data).hexdigest(),
                    bytes=len(data),
                )
            )
            notes += [f"{fmt}: {n}" for n in fmt_notes]
        report = check_circuit(spec)
        self.emitter.emit(
            "circuit.exported",
            {
                "schematic_id": schematic_id,
                "formats": [a.format for a in artifacts],
                "erc_passed": report.passed,
                "notes": len(notes),
            },
        )
        return ExportResult(artifacts=artifacts, notes=notes, erc=report)


_GATE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")  # the review app's id rule


def _source(source: str, gate_id: str | None) -> str:
    """A change applied for a review gate is the reviewer's."""
    if gate_id is None:
        return source
    if not _GATE_ID.match(gate_id):
        raise ValueError(f"invalid gate id {gate_id!r}")
    return "human"


def _with_gate(payload: dict[str, Any], gate_id: str | None) -> dict[str, Any]:
    return payload if gate_id is None else {**payload, "gate_id": gate_id}


def correction_calls(
    payload: CircuitGatePayload, corrections: list[Correction]
) -> list[tuple[str, dict[str, Any]]]:
    """A gate decision's corrections as `update_component` / `set_net_name` arguments: one
    update per component (component fields, terminal roles and pins), then one rename per net.
    A later correction of the same field wins. A terminal's component comes from the gate
    payload (printed pin numbers may contain dots)."""
    owner = {t.id: c.refdes for c in payload.components for t in c.terminals}
    sid = payload.schematic_id
    updates: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for c in corrections:
        if c.target == "net":
            names[c.id] = c.new
            continue
        refdes = c.id if c.target == "component" else owner[c.id]
        args = updates.setdefault(refdes, {"schematic_id": sid, "refdes": refdes})
        if c.target == "component":
            args[c.field] = c.new
        else:
            args.setdefault("roles" if c.field == "role" else "pins", {})[c.id] = c.new
    return [("update_component", args) for args in updates.values()] + [
        ("set_net_name", {"schematic_id": sid, "net_id": net_id, "name": name})
        for net_id, name in names.items()
    ]
