import type { AppEvent, Check, Phase, ReviewItem, StopReason, ValidationReport } from "../api/types";

// Same rules as `mechatronikers.session.runs.phase` (the run table), from events alone.
const ANALYSING = new Set(["analyse", "read", "check", "plan", "replan"]);
// A circuit run's own steps (#110): analyse, ERC, gate 1, netlists, ngspice.
const CIRCUIT_STEPS: Record<string, Phase> = {
  analyse: "analysing",
  check: "checking",
  review_gate_1: "waiting_gate_1",
  export: "exporting",
  simulate: "simulating",
};
const ENDED: Record<string, Phase> = {
  done: "valid", // a part becomes "released" only after "Finish inspection" (below)
  build_failed: "gave_up",
  rejected: "failed",
  aborted: "aborted",
  error: "failed",
  budget_exceeded: "failed",
};
// Why a run ended without a model (#92); same rules as `session.runs.stop_reason`.
const STOPPED: Record<string, StopReason> = { error: "error", budget_exceeded: "budget" };
const PAUSED: Record<string, Phase> = {
  spec_review: "waiting_gate_1",
  result_review: "valid",
  gave_up: "gave_up",
};
const SESSION_STATE: Record<string, string> = {
  "session.started": "running",
  "session.resumed": "running",
  "session.awaiting_review": "awaiting_review",
  "session.snapshot_started": "snapshotting",
  "session.shelved": "shelved",
  "session.failed": "failed",
};

export interface RunStatus {
  phase: Phase;
  /** `kind` of the first `agent.run_started` (docs/13); runs without one are parts. */
  kind: string;
  sessionState: string;
  revision: number;
  fixRound: number;
  maxIterations: number | null;
  drawing: string | null;
  partName: string | null;
  model: string | null;
  cost: number;
  startedAt: string | null;
  lastActivity: string | null;
  latestReport: ValidationReport | null;
  latestFeatureIds: string[];
  /** D58: the latest build passed every scored check and the run reached export. */
  valid: boolean;
  gaveUp: boolean;
  lastFailures: string[];
  /** POIs of the current revision without a final decision; a release does not close them (D70). */
  openPois: number;
  /** D95: builds that ran without sandbox isolation (`runner.isolation_unavailable` warnings). */
  unisolatedBuilds: number;
  /** Why the run ended without a model, the round limit and a failed step's message (#92). */
  stopReason: StopReason | null;
  maxRounds: number | null;
  stopError: string | null;
  /** Features the last build of a give-up could not add (#92). */
  failedFeatures: string[];
}

function lastIndex(events: AppEvent[], type: string): number {
  for (let i = events.length - 1; i >= 0; i -= 1) if (events[i]?.type === type) return i;
  return -1;
}

/** The reviewer finished the first article inspection (#152): the run driver (host) resumed from
 * gate 2 with "finish" before the run ended. A run that ended `done` without gate 2 (e.g.
 * `--auto-approve`) stays "valid". Same rule as `session.runs`. */
function finishedInspection(events: AppEvent[], end: number): boolean {
  for (let i = end - 1; i >= 0; i -= 1) {
    const e = events[i]!;
    // like `session.runs`: a container line can never be the proof (docs/13 §1)
    if (e.type === "agent.resumed" && e.component === "agent" && e.source !== "container") {
      return e.payload.kind === "result_review" && e.payload.action === "finish";
    }
  }
  return false;
}

const CLOSED = new Set(["review.approved", "review.rejected", "review.resolved"]);

/** Same rule as `session.runs.open_pois`: the current revision's POIs (not failed checks) minus
 * those a reviewer decided, or a gate 1 correction resolved (#129). */
export function openPois(events: AppEvent[], revision: number): number {
  const items = new Set<string>();
  for (const e of events) {
    if (e.type === "review.item_created" && Number(e.payload.revision ?? 1) === revision) {
      const item = e.payload.item as Partial<ReviewItem> | undefined;
      if (item?.id && item.kind !== "validation_failure") items.add(`${e.payload.origin ?? "agent"}:${item.id}`);
    } else if (CLOSED.has(e.type) && typeof e.payload.item_id === "string") {
      const id = e.payload.item_id;
      const dash = id.indexOf("-");
      items.delete(`reviewer:${id}`);
      items.delete(`agent:${dash > 0 && id.startsWith("rev") ? id.slice(dash + 1) : id}`); // rev<n>-<id> in the review app
    }
  }
  return items.size;
}

export function failingChecks(report: ValidationReport | null): Check[] {
  return report ? report.checks.filter((c) => !c.passed) : [];
}

export function runStatus(events: AppEvent[]): RunStatus {
  let sessionState = "created";
  let revision = 1;
  for (const e of events) {
    sessionState = SESSION_STATE[e.type] ?? sessionState;
    revision = Math.max(revision, Number(e.payload.revision ?? 1));
  }
  const started = events[lastIndex(events, "agent.run_started")];
  const first = events.find((e) => e.type === "agent.run_started")?.payload.kind;
  const kind = typeof first === "string" && first !== "" ? first : "part";
  const finishedAt = lastIndex(events, "agent.run_finished");
  const stepAt = lastIndex(events, "agent.step_started");
  const finished = events[finishedAt];
  const ended = finished !== undefined && finishedAt > stepAt;

  let phase: Phase;
  if (sessionState === "shelved" || sessionState === "snapshotting") phase = "shelved";
  else if (sessionState === "failed") phase = "failed";
  else if (ended && PAUSED[String(finished.payload.paused_at)])
    phase = PAUSED[String(finished.payload.paused_at)]!;
  else if (ended && kind === "part" && finished.payload.status === "done" && finishedInspection(events, finishedAt))
    phase = "released";
  else if (ended && ENDED[String(finished.payload.status)]) phase = ENDED[String(finished.payload.status)]!;
  else if (stepAt < 0) phase = "provisioning";
  else if (kind === "circuit") phase = CIRCUIT_STEPS[String(events[stepAt]?.payload.step)] ?? "analysing";
  else {
    const step = String(events[stepAt]?.payload.step);
    phase = ANALYSING.has(step)
      ? "analysing"
      : step === "review_gate_1"
        ? "waiting_gate_1"
        : step === "fix"
          ? "fixing"
          : step === "revise" || revision > 1
            ? "update_run"
            : "building";
  }

  const status = ended ? String(finished.payload.status) : "";
  const gaveUpEnd = ended && (status === "build_failed" || status === "aborted" || finished.payload.paused_at === "gave_up");
  const buildFailed = gaveUpEnd ? events[lastIndex(events, "agent.build_failed")] : undefined;
  const reason = buildFailed?.payload.reason;
  const stopReason: StopReason | null = gaveUpEnd
    ? reason === "cap" || reason === "plateau"
      ? reason
      : null
    : (STOPPED[status] ?? null);
  const stopFailures = (buildFailed?.payload.failures as { feature_id?: string }[] | undefined) ?? [];

  const validations = events.filter((e) => e.type === "validation.completed");
  const finalBuild = ended ? (finished.payload.build_id as string | null) : null;
  const latest =
    (finalBuild && validations.find((e) => e.payload.build_id === finalBuild)) ||
    validations[validations.length - 1];
  const latestReport = (latest?.payload.report as ValidationReport | undefined) ?? null;
  const passed = Number(finished?.payload.checks_passed ?? -1);
  const total = Number(finished?.payload.checks_total ?? 0);
  const valid = (phase === "valid" || phase === "released") && total > 0 && passed === total;

  // The failure POIs of the last build/fix round are the agent's last messages.
  const failures = events.filter(
    (e) => e.type === "review.item_created" && (e.payload.item as ReviewItem | undefined)?.kind === "validation_failure",
  );
  const lastRound = failures.length ? Number(failures[failures.length - 1]!.payload.iteration ?? 0) : 0;
  const lastFailures = failures
    .filter((e) => Number(e.payload.iteration ?? 0) === lastRound)
    .map((e) => (e.payload.item as ReviewItem).message);

  return {
    phase,
    kind,
    sessionState,
    revision,
    fixRound: events.filter((e) => e.type === "agent.step_started" && e.payload.step === "fix").length,
    maxIterations: (started?.payload.max_fix_iterations as number | undefined) ?? null,
    drawing: (started?.payload.drawing as string | undefined) ?? null,
    partName: (finished?.payload.part_name as string | undefined) ?? null,
    model: (started?.payload.model as string | undefined) ?? null,
    cost: events
      .filter((e) => e.type === "model.response")
      .reduce((sum, e) => sum + Number(e.payload.cost ?? 0), 0),
    startedAt: events[0]?.ts ?? null,
    // when the host last heard from the session (sandbox clocks can drift, T13)
    lastActivity: events.reduce<string | null>((latest, e) => {
      const at = e.recv_ts ?? e.ts;
      return latest === null || at > latest ? at : latest;
    }, null),
    latestReport,
    latestFeatureIds: (latest?.payload.feature_ids as string[] | undefined) ?? [],
    valid,
    gaveUp: phase === "gave_up",
    lastFailures,
    openPois: openPois(events, revision),
    unisolatedBuilds: new Set(
      events
        .filter((e) => e.type === "runner.isolation_unavailable" && e.level === "warning")
        .map((e) => String(e.payload.build_id)),
    ).size,
    stopReason,
    maxRounds: (buildFailed?.payload.max_iterations as number | undefined) ?? null,
    stopError: stopReason === "error" ? ((finished?.payload.error as string | undefined) ?? null) : null,
    failedFeatures: stopFailures.map((f) => f.feature_id ?? "").filter((id) => id !== "" && id !== "*"),
  };
}

export const PHASE_LABELS: Record<Phase, string> = {
  queued: "queued",
  provisioning: "provisioning",
  analysing: "analysing",
  checking: "checking",
  waiting_gate_1: "waiting for gate 1",
  building: "building",
  fixing: "fixing",
  exporting: "exporting",
  simulating: "simulating",
  gave_up: "gave up",
  aborted: "aborted",
  valid: "valid, ready for review",
  released: "released after inspection",
  update_run: "update run",
  shelved: "shelved",
  failed: "failed",
};
