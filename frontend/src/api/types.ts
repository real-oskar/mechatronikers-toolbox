// Mirrors of the Pydantic models in `mechatronikers.spec` / `.telemetry` (the Python code is
// authoritative). Only the fields the UI reads are listed.

export type Level = "debug" | "info" | "warning" | "error";

export interface AppEvent {
  ts: string; // RFC 3339 UTC, emitter's clock
  recv_ts?: string; // set by the collector (host clock)
  source?: "host" | "container"; // set by the hub's ingest; unset in mode 1
  seq: number;
  emitter_id?: string;
  session_id?: string;
  run_id?: string;
  component: string;
  type: string;
  level: Level;
  duration_ms?: number;
  payload: Record<string, unknown>;
}

export interface StoredEvent {
  id: number; // collector ingest id: order and resume point
  event: AppEvent;
}

export type BBox = [number, number, number, number];

export interface SourceRef {
  sheet: number;
  view_id: string | null;
  bbox: BBox;
}

export interface ModelRef {
  face_ids: number[];
  point: [number, number, number] | null;
}

export type Severity = "info" | "warning" | "blocking";

export interface ReviewAction {
  at: string;
  by: string;
  action: "approved" | "edited" | "rejected" | "resolved" | "note";
  old_value: number | string | null;
  new_value: number | string | null;
  comment: string | null;
}

export interface ReviewItem {
  id: string;
  kind: string;
  severity: Severity;
  message: string;
  reason: string;
  dimension_ids: string[];
  feature_ids: string[];
  sheet_ref: SourceRef | null;
  model_ref: ModelRef | null;
  status: string;
  history: ReviewAction[];
}

/** One check of a part build (V0–V4) or a circuit run (category "ERC", E1–E7 and A1–A3 as
 * check ids): the checks shape both run kinds share (docs/13). */
export interface Check {
  id: string;
  category: string;
  expected: unknown;
  measured: unknown;
  tolerance: number | null;
  passed: boolean;
  detail: string | null;
  feature_ids?: string[]; // ERC: refdes, terminal and net ids
}

export interface ValidationReport {
  build_id: string;
  checks: Check[];
  advisory: Check[];
  passed: boolean;
  score: number;
}

export type RunKind = "part" | "circuit";

export type GateKind = "spec_review" | "result_review" | "gave_up";
export type GateAction = "approve" | "reject" | "finish" | "update" | "retry" | "abort";

export interface Assumption {
  id: string;
  kind: string;
  value: number | string | null;
  reason: string;
}

export interface Dimension {
  id: string;
  kind: "linear" | "diameter" | "radius" | "angle" | "thread" | "chamfer" | "other";
  stated: number | null; // printed value
  stated_text: string | null; // e.g. "3x Ø12"
  measured: number | null; // from the drawing geometry, already divided by the view scale
  value: number;
  source: SourceRef | null;
  method: string;
  confidence: number;
  status: string; // auto_ok | needs_review | rejected | …
  reason: string | null;
  feature_ids: string[];
}

/** A planned feature (spec Feature); only the fields the descriptions use. */
export interface PlannedFeature {
  type: string;
  id: string;
  depth?: number | null;
  height?: number | null;
  diameter?: number;
  hole_type?: string;
  thread?: string | null;
  cb_diameter?: number | null;
  cb_depth?: number | null;
  cs_diameter?: number | null;
  radius?: number;
  distance?: number;
  length?: number;
  width?: number;
  angle?: number;
  reason?: string;
  profile?: { segments: { kind: string }[] };
  seed?: PlannedFeature;
  pattern?: { kind: string; count?: number; count_1?: number; count_2?: number };
}

// Circuit run payload (`spec/circuit.py`); only the fields the UI reads.
export interface CircuitTerminal {
  id: string; // "<refdes>.<n>"
  at: [number, number];
  direction: [number, number];
  pin: string | null;
  name: string | null;
  role: string | null;
  net_id: string | null;
}

export interface CircuitComponent {
  refdes: string;
  kind: string;
  polarity: "npn" | "pnp" | "n" | "p" | null;
  value: string | null;
  params: string | null;
  footprint: string | null;
  bbox: BBox;
  terminals: CircuitTerminal[];
  status: string;
  confidence: number;
  source: "vector" | "vlm" | "human";
}

export interface NetSummary {
  id: string;
  name: string | null;
  terminals: string[];
  wires: number;
}

/** A net with its wires (sheet mm), as the circuit spec holds it: the overlay draws them. */
export interface CircuitNet {
  id: string;
  name: string | null;
  terminals: string[];
  segments: [[number, number], [number, number]][];
}

/** `GET <circuit>/schematics/<id>` (`CircuitSpec`): the circuit as circuit-mcp holds it now. */
export interface CircuitSpec {
  source_file: string;
  sheet_size: [number, number];
  drawing_area: BBox;
  components: CircuitComponent[];
  nets: CircuitNet[];
  directives: string[];
  review_items: ReviewItem[];
}

/** Gate 1 of a circuit run: the circuit as read (wire geometry comes from the circuit spec). */
export interface CircuitGatePayload {
  schematic_id: string;
  sheet_size: [number, number] | null;
  components: CircuitComponent[];
  nets: NetSummary[];
}

export type CorrectionTarget = "component" | "terminal" | "net";

/** A reviewer's correction recorded with a circuit gate 1 decision; the run driver applies it
 * through circuit-mcp (D75, #57). Fields: component kind/value/params/polarity/footprint,
 * terminal role/pin, net name. */
export interface Correction {
  target: CorrectionTarget;
  id: string; // refdes, terminal id or net id
  field: string;
  old?: string | null;
  new: string;
}

export interface GateRequest {
  id: string;
  kind: GateKind;
  run_id: string;
  run_kind?: RunKind; // missing in gates stored before SPEC_VERSION 0.5.0: "part"
  revision: number;
  created_at: string;
  part_name: string | null;
  features: string[];
  assumptions: Assumption[];
  overall_size_dims: (string | null)[] | null;
  review_items: ReviewItem[];
  possibly_missing: ReviewItem[];
  build_id: string | null;
  validation: ValidationReport | null;
  failures: string[];
  iteration: number;
  max_iterations: number | null;
  // what the review app shows: the plan in words, the ballooned sheet, the model
  plan?: PlannedFeature[];
  dimensions?: Dimension[];
  sheet_size?: [number, number] | null;
  drawing?: string | null;
  circuit?: CircuitGatePayload | null; // circuit runs
}

export interface ItemReview {
  item_id: string;
  action: ReviewAction;
}

export interface UpdateRequest {
  revision: number;
  items: StoredItem[];
  actions: ItemReview[];
  note?: string | null;
}

export interface GateDecision {
  gate_id: string;
  action: GateAction;
  reviewer: string;
  decided_at: string;
  reason?: string | null;
  hint?: string | null;
  update?: UpdateRequest | null;
  corrections?: Correction[]; // circuit gate 1 (approve or reject)
}

export interface StoredItem {
  item: ReviewItem;
  revision: number;
  origin: "agent" | "reviewer";
  created_at: string;
  created_by: string;
  proposed_value: number | string | null;
}

export interface NewItem {
  message: string;
  severity: Severity;
  reviewer: string;
  revision: number;
  sheet_ref?: SourceRef | null;
  model_ref?: ModelRef | null;
  dimension_ids?: string[];
  feature_ids?: string[];
  proposed_value?: number | string | null;
}

export interface ItemActionRequest {
  action: "approved" | "edited" | "rejected" | "note";
  reviewer: string;
  new_value?: number | string | null;
  comment?: string | null;
}

export interface GateState {
  request: GateRequest;
  decision: GateDecision | null;
  open: boolean;
  /** D95: builds of this run that ran without sandbox isolation (set by the review API). */
  unisolated_builds?: number;
}

export type Phase =
  | "queued" // hub: waiting for a worker, no session yet (docs/13)
  | "provisioning"
  | "analysing"
  | "checking" // circuit: ERC (#110)
  | "waiting_gate_1"
  | "building"
  | "fixing"
  | "exporting" // circuit: netlists (#110)
  | "simulating" // circuit: ngspice operating point (#110)
  | "gave_up"
  | "aborted"
  | "valid"
  | "released" // part: the reviewer finished the first article inspection (#152)
  | "update_run"
  | "shelved"
  | "failed";

export interface RunSummary {
  session_id: string;
  run_kind?: RunKind; // from agent.run_started.kind; missing: "part"
  drawing: string | null;
  part_name: string | null;
  state: string;
  phase: Phase;
  checks_passed: number | null;
  checks_total: number | null;
  open_pois: number;
  revision: number;
  fix_iterations: number;
  max_fix_iterations: number | null;
  model: string | null;
  cost: number;
  unisolated_builds?: number; // D95: builds that ran without sandbox isolation
  started_at: string;
  last_activity: string | null;
  link: string | null;
  // why the run ended without a model, and what failed in its last build (#92)
  stop_reason?: StopReason | null;
  failing_checks?: string[];
  failed_features?: string[];
  // what "finish and shelve" keeps, and until when once shelved (#52)
  retention_class?: string | null;
  retention_days?: number | null;
  expires_at?: string | null;
}

/** `POST /sessions/<id>/shelve` on the host (#52): the session's snapshot and its expiry. */
export interface ShelveResult {
  session_id: string;
  state: "shelved";
  snapshot_id: string;
  retention_class: string;
  expires_at: string;
}

/** Why a run ended without a model (#92): rework limit, no improvement, a failed step, budget. */
export type StopReason = "cap" | "plateau" | "error" | "budget";

export const MAX_NOTE = 2000; // same cap as spec/review.py
export const MAX_NAME = 80;

/** One overview row in the hub (`GET /api/runs`, docs/13): the summary's fields plus the hub's run
 * id, plan, reviewer and batch, from the hub's trusted records. Proposed shape until the hub serves
 * it (#74, #75). */
export interface HubRun extends Omit<RunSummary, "session_id" | "link"> {
  run_id: string;
  session_id: string | null; // null while the run is queued
  link: null; // never filled in the hub: it would carry the session's gateway token (docs/13)
  plan?: string | null; // the plan's name (uploaded drawing or schematic)
  reviewer?: string | null; // who decided the run's last gate
  batch?: string | null;
}

/** One page of `GET /api/runs?kind=&phase=&cursor=` (hub): rows and the next page's cursor. */
export interface RunPage {
  runs: HubRun[];
  next_cursor: string | null;
}

/** A row of the overview table: the host's `RunSummary` or the hub's `HubRun`. */
export type OverviewRow = Omit<RunSummary, "session_id"> & {
  session_id: string | null;
  run_id?: string;
  plan?: string | null;
  reviewer?: string | null;
  batch?: string | null;
};
