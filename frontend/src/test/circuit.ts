import type {
  AppEvent,
  CircuitComponent,
  CircuitGatePayload,
  CircuitSpec,
  GateState,
  ReviewItem,
  StoredEvent,
  ValidationReport,
} from "../api/types";

// A small circuit run (like `circuit_mcp/run.py` on a divider): V1 drives R1 into a node with R2
// and D1 to ground. R2's value needs a look; N2 has no printed name.

export const SID = "2a2a349267aa";

const term = (id: string, at: [number, number], net: string, role: string | null = null) => ({
  id,
  at,
  direction: [0, 1] as [number, number],
  pin: null,
  name: null,
  role,
  net_id: net,
});

const part = (refdes: string, kind: string, value: string | null, bbox: [number, number, number, number], terminals: CircuitComponent["terminals"], status = "auto_ok"): CircuitComponent => ({
  refdes, kind, polarity: null, value, params: null, footprint: null, bbox, terminals, status, confidence: 1, source: "vector",
}); // fmt: skip

export const finding: ReviewItem = {
  id: "ri-1",
  kind: "mismatch",
  severity: "warning",
  message: "Value of R2 is hard to read",
  reason: "two texts near the symbol",
  dimension_ids: [],
  feature_ids: ["R2"],
  sheet_ref: { sheet: 0, view_id: null, bbox: [118, 96, 124, 104] },
  model_ref: null,
  status: "open",
  history: [],
};

export function circuitSpec(): CircuitSpec {
  return {
    source_file: "divider.pdf",
    sheet_size: [297, 210],
    drawing_area: [12, 12, 285, 198],
    components: [
      part("V1", "voltage_source", "5", [98, 98, 106, 106], [term("V1.1", [102, 98], "N1", "+"), term("V1.2", [102, 106], "N3", "-")]),
      part("R1", "resistor", "10k", [108, 92, 116, 95], [term("R1.1", [108, 93.5], "N1"), term("R1.2", [116, 93.5], "N2")]),
      part("R2", "resistor", "4k7", [119, 98, 122, 104], [term("R2.1", [120.5, 98], "N2"), term("R2.2", [120.5, 104], "N3")], "needs_review"),
      part("D1", "diode", "1N4148", [128, 98, 131, 104], [term("D1.1", [129.5, 98], "N2", "A"), term("D1.2", [129.5, 104], "N3", "K")]),
    ],
    nets: [
      { id: "N1", name: "VIN", terminals: ["V1.1", "R1.1"], segments: [[[102, 98], [102, 93.5]], [[102, 93.5], [108, 93.5]]] },
      { id: "N2", name: null, terminals: ["R1.2", "R2.1", "D1.1"], segments: [[[116, 93.5], [129.5, 93.5]], [[120.5, 93.5], [120.5, 98]], [[129.5, 93.5], [129.5, 98]]] },
      { id: "N3", name: "GND", terminals: ["V1.2", "R2.2", "D1.2"], segments: [[[102, 106], [129.5, 106]]] },
    ],
    directives: [],
    review_items: [finding],
  };
}

export function circuitPayload(): CircuitGatePayload {
  const spec = circuitSpec();
  return {
    schematic_id: SID,
    sheet_size: spec.sheet_size,
    components: spec.components,
    nets: spec.nets.map((n) => ({ id: n.id, name: n.name, terminals: n.terminals, wires: n.segments.length })),
  };
}

export function circuitGate(open = true): GateState {
  return {
    open,
    decision: open
      ? null
      : {
          gate_id: "gate-run-1-1",
          action: "approve",
          reviewer: "Anna",
          decided_at: "2026-10-17T08:00:05.000000Z",
          corrections: [{ target: "component", id: "R2", field: "value", old: "4k7", new: "47k" }],
        },
    request: {
      id: "gate-run-1-1",
      kind: "spec_review",
      run_id: "run-1",
      run_kind: "circuit",
      revision: 1,
      created_at: "2026-10-17T08:00:03.000000Z",
      part_name: "divider",
      features: ["V1 · voltage_source · 5", "R1 · resistor · 10k", "R2 · resistor · 4k7", "D1 · diode · 1N4148"],
      assumptions: [],
      overall_size_dims: null,
      review_items: [finding],
      possibly_missing: [],
      build_id: null,
      validation: null,
      failures: [],
      iteration: 0,
      max_iterations: null,
      sheet_size: [297, 210],
      drawing: "divider.pdf",
      circuit: circuitPayload(),
    },
  };
}

function erc(passed: boolean): ValidationReport {
  const check = (id: string, ok: boolean, detail = "") => ({ id, category: "ERC", expected: null, measured: null, tolerance: null, passed: ok, detail, feature_ids: ok ? [] : ["R2.2"] });
  return {
    build_id: "erc-r1",
    checks: [check("E1.unique_refdes", true), check("E3.floating_terminal", passed, passed ? "" : "R2.2 is not connected")],
    advisory: [check("A1.unnamed_nets", false, "1 net without a name")],
    passed,
    score: passed ? 1 : 0.5,
  };
} // fmt: skip

/** Gate 1 of revision 2 (#57): the corrected circuit, waiting; gate 1 of revision 1 rejected. */
export function revisedGates(): GateState[] {
  const first = circuitGate(false);
  first.decision = { ...first.decision!, action: "reject", reason: "R2 is 47k on paper" };
  const second = circuitGate(true);
  second.request = { ...second.request, id: "gate-run-1-1-r2", revision: 2 };
  second.request.circuit!.components = second.request.circuit!.components.map((c) => (c.refdes === "R2" ? { ...c, value: "47k", status: "edited" } : c));
  return [first, second];
}

/** The run's events up to `stage`: waiting at gate 1, done (approved, exported, simulated) or
 * rejected. Timestamps one second apart; ids from 1. */
export function circuitEvents(stage: "gate" | "done" | "rejected" | "revised" = "gate", { passed = true } = {}): StoredEvent[] {
  const lines: [string, string, Record<string, unknown>][] = [
    ["agent", "agent.run_started", { drawing: "divider.pdf", kind: "circuit", model: null, auto_approve: false }],
    ["agent", "agent.step_started", { step: "analyse" }],
    ["circuit-mcp", "circuit.loaded", { schematic_id: SID, file: "divider.pdf", components: 4, nets: 3, review_items: 1, wires: 6, junctions: 1 }],
    ["agent", "review.item_created", { item: finding, step: "analyse", origin: "agent", revision: 1 }],
    ["agent", "agent.step_completed", { step: "analyse", schematic_id: SID, components: 4 }],
    ["agent", "agent.step_started", { step: "check" }],
    ["agent", "validation.completed", { build_id: "erc-r1", passed, feature_ids: [], report: erc(passed) }],
    ["agent", "agent.step_completed", { step: "check" }],
    ["agent", "agent.step_started", { step: "review_gate_1" }],
    ["review-app", "review.gate_opened", { gate_id: "gate-run-1-1", kind: "spec_review", run_kind: "circuit" }],
    ["agent", "agent.interrupt", { gate_id: "gate-run-1-1", kind: "spec_review" }],
  ];
  if (stage === "rejected") {
    lines.push(
      ["review-app", "review.rejected", { gate_id: "gate-run-1-1", reviewer: "Anna", run_kind: "circuit" }],
      ["agent", "agent.resumed", { gate_id: "gate-run-1-1", action: "reject" }],
      ["agent", "agent.step_completed", { step: "review_gate_1", action: "reject" }],
      ["agent", "agent.run_finished", { status: "rejected", reason: "R2 is 47k on paper", part_name: "divider" }],
    );
  }
  if (stage === "revised") {
    // #57: the reviewer rejects with a correction; the run revises and opens gate 1 again
    const fix = { target: "component", id: "R2", field: "value", old: "4k7", new: "47k" };
    lines.push(
      ["review-app", "review.rejected", { gate_id: "gate-run-1-1", reviewer: "Anna", run_kind: "circuit", corrections: 1 }],
      ["agent", "agent.resumed", { gate_id: "gate-run-1-1", action: "reject" }],
      ["agent", "agent.step_completed", { step: "review_gate_1", revision: 1, action: "reject" }],
      ["agent", "agent.step_started", { step: "revise", revision: 1 }],
      ["circuit-mcp", "circuit.component_updated", { schematic_id: SID, refdes: "R2", changes: { value: "47k" }, by: "human", gate_id: "gate-run-1-1" }],
      ["agent", "agent.corrections_applied", { gate_id: "gate-run-1-1", revision: 1, corrections: [fix] }],
      ["agent", "agent.step_completed", { step: "revise", revision: 1, corrections: 1, reason: "R2 is 47k on paper" }],
      ["agent", "agent.revision_started", { revision: 2, gate_id: "gate-run-1-1", corrections: 1, max_rounds: 3 }],
      ["agent", "agent.step_started", { step: "check", revision: 2 }],
      ["agent", "validation.completed", { build_id: "erc-r2", passed, feature_ids: [], report: erc(passed) }],
      ["agent", "agent.step_completed", { step: "check", revision: 2 }],
      ["agent", "agent.step_started", { step: "review_gate_1", revision: 2 }],
      ["review-app", "review.gate_opened", { gate_id: "gate-run-1-1-r2", kind: "spec_review", run_kind: "circuit", revision: 2 }],
      ["agent", "agent.interrupt", { gate_id: "gate-run-1-1-r2", kind: "spec_review", revision: 2 }],
    );
  }
  if (stage === "done") {
    const artifacts = ["divider.cir", "divider.asc", "divider.net"].map((f, i) => ({
      format: ["spice", "ltspice", "kicad-netlist"][i],
      uri: `artifact://ses-1/circuit/${SID}/${f}`,
    }));
    lines.push(
      ["review-app", "review.gate_passed", { gate_id: "gate-run-1-1", reviewer: "Anna", run_kind: "circuit", corrections: 1 }],
      ["agent", "agent.resumed", { gate_id: "gate-run-1-1", action: "approve" }],
      ["agent", "agent.step_completed", { step: "review_gate_1" }],
      ["agent", "agent.step_started", { step: "check" }],
      ["agent", "validation.completed", { build_id: "erc-r1", passed, feature_ids: [], report: erc(passed) }],
      ["agent", "agent.step_completed", { step: "check" }],
      ["agent", "agent.step_started", { step: "export" }],
      ["agent", "agent.step_completed", { step: "export", artifacts, notes: 0 }],
      ["agent", "agent.step_started", { step: "simulate" }],
      ["circuit-mcp", "circuit.simulated", { schematic_id: SID, ok: true, reason: "", node_voltages: { vin: 5, n2: 0.000340426, gnd: 0 } }],
      ["agent", "agent.step_completed", { step: "simulate", ok: true }],
      ["agent", "agent.run_finished", { status: "done", part_name: "divider", schematic_id: SID, checks_passed: passed ? 2 : 1, checks_total: 2, artifacts }],
    );
  }
  return lines.map(([component, type, payload], i) => {
    const event: AppEvent = {
      ts: new Date(Date.UTC(2026, 9, 17, 8, 0, i)).toISOString().replace("Z", "000Z"),
      seq: i + 1,
      component,
      type,
      level: "info",
      payload,
    };
    return { id: i + 1, event };
  });
}
