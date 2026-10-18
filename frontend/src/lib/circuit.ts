import type {
  AppEvent,
  BBox,
  CircuitComponent,
  CircuitGatePayload,
  CircuitNet,
  CircuitSpec,
  Correction,
  CorrectionTarget,
  GateState,
  ReviewItem,
  ValidationReport,
} from "../api/types";

// Circuit runs (docs/12): `analyse` → `check` → `review_gate_1` → `check` → `export` → `simulate`,
// driven by `circuit_mcp/run.py` with the agent's events. Pure helpers for the circuit panels.

/** Same literals as `ComponentKind` and `PinRole` in `spec/circuit.py` (the review API checks
 * corrections against them). */
export const COMPONENT_KINDS = [
  "resistor", "capacitor", "inductor", "diode", "bjt", "mosfet", "opamp", "voltage_source",
  "current_source", "transformer", "connector", "switch", "ic", "other",
] as const; // fmt: skip
export const PIN_ROLES = ["+", "-", "A", "K", "C", "B", "E", "D", "G", "S", "in+", "in-", "v+", "v-", "out"] as const;
export const MAX_VALUE = 200; // MAX_CORRECTION
export const MAX_NET_NAME = 64;

function lastIndex(events: AppEvent[], test: (e: AppEvent) => boolean): number {
  for (let i = events.length - 1; i >= 0; i -= 1) if (test(events[i]!)) return i;
  return -1;
}

const last = (events: AppEvent[], test: (e: AppEvent) => boolean): AppEvent | undefined => events[lastIndex(events, test)];

/** The schematic of this run: from the run driver's analyse step, else circuit-mcp, else gate 1. */
export function schematicId(events: AppEvent[], gates: GateState[]): string | null {
  const analysed = last(events, (e) => e.component === "agent" && e.type === "agent.step_completed" && e.payload.step === "analyse");
  const loaded = last(events, (e) => e.type === "circuit.loaded");
  const gate = [...gates].reverse().find((g) => g.request.circuit)?.request.circuit;
  for (const sid of [analysed?.payload.schematic_id, loaded?.payload.schematic_id, gate?.schematic_id]) {
    if (typeof sid === "string" && sid !== "") return sid;
  }
  return null;
}

/** The latest electrical rule check, in the checks shape of part builds (category `ERC`). */
export function ercReport(events: AppEvent[]): ValidationReport | null {
  const done = last(events, (e) => e.component === "agent" && e.type === "validation.completed");
  return (done?.payload.report as ValidationReport | undefined) ?? null;
}

export interface Netlist {
  format: "spice" | "ltspice" | "kicad-netlist" | "circuit-json";
  name: string;
}
const FORMATS = new Set<string>(["spice", "ltspice", "kicad-netlist", "circuit-json"]);

/** The netlists the export step wrote (file names from their artifact URIs). */
export function netlists(events: AppEvent[]): Netlist[] {
  const exported = last(events, (e) => e.component === "agent" && e.type === "agent.step_completed" && e.payload.step === "export");
  const artifacts = Array.isArray(exported?.payload.artifacts) ? (exported.payload.artifacts as unknown[]) : [];
  return artifacts.flatMap((a) => {
    const { format, uri } = (a ?? {}) as { format?: unknown; uri?: unknown };
    const name = typeof uri === "string" ? uri.split("/").pop() : undefined;
    return typeof format === "string" && FORMATS.has(format) && name ? [{ format: format as Netlist["format"], name }] : [];
  });
}

export interface OperatingPoint {
  ok: boolean;
  reason: string;
  voltages: [string, number][];
}

/** ngspice's operating point of the exported netlist (`circuit.simulated`), node by node. */
export function operatingPoint(events: AppEvent[]): OperatingPoint | null {
  const sim = last(events, (e) => e.type === "circuit.simulated");
  if (!sim) return null;
  const raw = sim.payload.node_voltages;
  const voltages = Object.entries(typeof raw === "object" && raw !== null ? raw : {})
    .filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]))
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
  return { ok: sim.payload.ok === true, reason: String(sim.payload.reason ?? ""), voltages };
}

/** What the panels show: the circuit with its wires. At an open gate the gate's own components
 * and nets (corrections must name what the gate showed, D84), with wires from the spec. */
export interface CircuitView {
  sheetSize: [number, number];
  components: CircuitComponent[];
  nets: CircuitNet[];
  reviewItems: ReviewItem[];
}

export function circuitView(spec: CircuitSpec | null, gate: CircuitGatePayload | null): CircuitView | null {
  if (gate) {
    const wires = new Map(spec?.nets.map((n) => [n.id, n.segments]) ?? []);
    return {
      sheetSize: gate.sheet_size ?? spec?.sheet_size ?? [297, 210],
      components: gate.components,
      nets: gate.nets.map((n) => ({ id: n.id, name: n.name, terminals: n.terminals, segments: wires.get(n.id) ?? [] })),
      reviewItems: spec?.review_items ?? [],
    };
  }
  if (!spec) return null;
  return { sheetSize: spec.sheet_size, components: spec.components, nets: spec.nets, reviewItems: spec.review_items };
}

export const netLabel = (net: { id: string; name: string | null }) => net.name ?? net.id;

export type Selection = { type: "part" | "net" | "item"; id: string };

function union(boxes: BBox[]): BBox | null {
  if (boxes.length === 0) return null;
  return [
    Math.min(...boxes.map((b) => b[0])),
    Math.min(...boxes.map((b) => b[1])),
    Math.max(...boxes.map((b) => b[2])),
    Math.max(...boxes.map((b) => b[3])),
  ];
}

/** The whole circuit on its sheet with a margin (printed values and labels sit next to the
 * symbols), and the zoom that shows it in a sheet frame about half as high as wide; schematics
 * often fill a small part of a large sheet. */
export function circuitFit(view: CircuitView, margin = 20): { box: BBox; zoom: number } | null {
  const box = union([
    ...view.components.map((c) => c.bbox),
    ...view.nets.flatMap((n) => n.segments.map(([[ax, ay], [bx, by]]): BBox => [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)])),
  ]);
  if (!box) return null;
  const padded: BBox = [box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin];
  const width = view.sheetSize[0];
  const fits = Math.min(width / (padded[2] - padded[0]), (0.55 * width) / (padded[3] - padded[1]));
  const zoom = Math.min(5, Math.max(1, 0.85 * fits));
  return { box: padded, zoom: Math.round(zoom * 10) / 10 };
}

/** The sheet box of a selected part, net (wires and terminals) or finding. */
export function selectionBox(sel: Selection | null, view: CircuitView | null, items: ReviewItem[]): BBox | null {
  if (!sel || !view) return null;
  if (sel.type === "part") return view.components.find((c) => c.refdes === sel.id)?.bbox ?? null;
  if (sel.type === "net") {
    const net = view.nets.find((n) => n.id === sel.id);
    if (!net) return null;
    const points = [
      ...net.segments.flat(),
      ...view.components.flatMap((c) => c.terminals.filter((t) => t.net_id === net.id).map((t) => t.at)),
    ];
    return union(points.map(([x, y]) => [x, y, x, y]));
  }
  const item = items.find((i) => i.id === sel.id);
  if (!item) return null;
  return item.sheet_ref?.bbox ?? union(view.components.filter((c) => item.feature_ids.includes(c.refdes)).map((c) => c.bbox));
}

// ------------------------------------------------------------------ corrections (D75, D84)

/** The reviewer's corrections, one per target and field, in the order they were made. */
export type Corrections = Map<string, Correction>;

export const correctionKey = (target: CorrectionTarget, id: string, field: string) => `${target}:${id}:${field}`;

export const MAX_CORRECTIONS = 100; // per decision (D84)

/** Free text as the review API takes it: whitespace of any kind (no-break and thin spaces from
 * datasheets too) as one space, other invisible characters dropped. */
export function cleanText(raw: string): string {
  return raw.replace(/\s+/gu, " ").replace(/\p{C}/gu, "").trim();
}

const QUANTITY = /^([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*([A-Za-z\u00b5\u03bc\u03a9\u2126]*)$/u;

/** A printed value as the netlist writer reads it: "4,7 kΩ" → "4.7k", "100 nF" → "100nF",
 * "2.2 µF" → "2.2uF". Model names and parameters ("1N4148", "dc=0 ampl=1") stay as typed. */
export function cleanValue(raw: string): string {
  const text = cleanText(raw);
  const m = QUANTITY.exec(text);
  if (!m) return text;
  const unit = m[2]!.replace(/[\u00b5\u03bc]/gu, "u").replace(/[\u03a9\u2126]/gu, "");
  return `${m[1]!.replace(",", ".")}${unit}`;
}

const LIMITS: Record<string, number> = { name: MAX_NET_NAME };
const CHOICES: Record<string, readonly string[]> = { kind: COMPONENT_KINDS, role: PIN_ROLES, polarity: ["npn", "pnp", "n", "p"] };

export type CorrectionProblem = "tooLong" | "notAllowed" | "tooMany";

/** Record or drop one correction, checked like the review API checks it (`Correction`, D84).
 * Text is cleaned first; an empty value or the value the gate showed drops the correction (a
 * correction always changes something), and so does an invalid one, with its problem. */
export function setCorrection(current: Corrections, c: Correction): { corrections: Corrections; problem: CorrectionProblem | null } {
  const next = new Map(current);
  const key = correctionKey(c.target, c.id, c.field);
  const value = c.field === "value" ? cleanValue(c.new) : cleanText(c.new);
  let problem: CorrectionProblem | null = null;
  if ([...value].length > (LIMITS[c.field] ?? MAX_VALUE)) problem = "tooLong"; // code points, like Python
  else if (CHOICES[c.field] && !CHOICES[c.field]!.includes(value) && value !== "") problem = "notAllowed";
  else if (!next.has(key) && next.size >= MAX_CORRECTIONS && value !== "" && value !== (c.old ?? "")) problem = "tooMany";
  if (problem || value === "" || value === (c.old ?? "")) next.delete(key);
  else next.set(key, { ...c, new: value });
  return { corrections: next, problem };
}

/** The corrections that still name something the gate shows (a new gate starts over). */
export function validCorrections(current: Corrections, gate: CircuitGatePayload | null): Correction[] {
  if (!gate) return [];
  const known: Record<CorrectionTarget, Set<string>> = {
    component: new Set(gate.components.map((c) => c.refdes)),
    terminal: new Set(gate.components.flatMap((c) => c.terminals.map((t) => t.id))),
    net: new Set(gate.nets.map((n) => n.id)),
  };
  return [...current.values()].filter((c) => known[c.target].has(c.id));
}
