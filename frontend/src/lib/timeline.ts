import type { AppEvent, Level, ReviewItem, StoredEvent } from "../api/types";

export type StepStatus = "running" | "done" | "failed" | "waiting";

export interface Poi {
  step: string;
  origin: string;
  revision: number;
  item: ReviewItem;
}

export interface Step {
  key: string;
  name: string; // provisioning, analyse, read, check, plan, replan, review_gate_1, build, fix, …
  label: string;
  status: StepStatus;
  /** Done, but its completion says not everything worked (level warning or `ok: false`). */
  warned: boolean;
  start: string;
  end?: string;
  durationMs?: number;
  revision: number;
  events: StoredEvent[];
  hostTs: Map<number, string>; // event id → time on the host clock (see hostTimes)
  pois: Poi[];
  modelCalls: number;
  toolCalls: number;
  cost: number;
  errors: string[];
}

const LABELS: Record<string, string> = {
  provisioning: "Provisioning",
  analyse: "Analyse",
  read: "Read",
  check: "Check",
  plan: "Plan",
  replan: "Replan",
  review_gate_1: "Gate 1 · spec review",
  build: "Build",
  fix: "Fix",
  export: "Export",
  revise: "Revise",
};

const SKEW_MS = 2000;

/** Event times on the host clock. Emitters in the Podman VM can drift by minutes (T13), so each
 * emitter's offset is its smallest receive-minus-emit gap, applied when it exceeds 2 s. The
 * estimate includes the log delivery latency, so corrected times are good to that latency. */
export function hostTimes(events: StoredEvent[]): Map<number, number> {
  const key = (e: StoredEvent) => e.event.emitter_id ?? e.event.component;
  const offsets = new Map<string, number>();
  for (const stored of events) {
    if (!stored.event.recv_ts) continue;
    const gap = Date.parse(stored.event.recv_ts) - Date.parse(stored.event.ts);
    offsets.set(key(stored), Math.min(offsets.get(key(stored)) ?? Infinity, gap));
  }
  const times = new Map<number, number>();
  for (const stored of events) {
    const offset = offsets.get(key(stored)) ?? 0;
    times.set(stored.id, Date.parse(stored.event.ts) + (Math.abs(offset) > SKEW_MS ? offset : 0));
  }
  return times;
}

function newStep(name: string, start: number, revision: number, nth: number): Step {
  let label = LABELS[name] ?? name;
  if (name === "fix") label = `Fix #${nth}`;
  else if (nth > 1 && name !== "provisioning") label = `${label} #${nth}`;
  if (revision > 1) label = `${label} · rev ${revision}`;
  return {
    key: `${name}-${nth}-r${revision}`,
    name,
    label,
    status: "running",
    warned: false,
    start: new Date(start).toISOString(),
    revision,
    events: [],
    hostTs: new Map(),
    pois: [],
    modelCalls: 0,
    toolCalls: 0,
    cost: 0,
    errors: [],
  };
}

function close(step: Step, end: number, status: StepStatus): void {
  step.status = status;
  step.end = new Date(end).toISOString();
  step.durationMs = Date.parse(step.end) - Date.parse(step.start);
}

function addTo(step: Step, stored: StoredEvent): void {
  const e = stored.event;
  step.events.push(stored);
  if (e.type === "model.response") {
    step.modelCalls += 1;
    step.cost += Number(e.payload.cost ?? 0);
  } else if (e.type === "tool.called") {
    step.toolCalls += 1;
  } else if (e.type === "review.item_created" && e.payload.item) {
    step.pois.push({
      step: String(e.payload.step ?? step.name),
      origin: String(e.payload.origin ?? "agent"),
      revision: Number(e.payload.revision ?? step.revision),
      item: e.payload.item as ReviewItem,
    });
  }
  if (e.type === "agent.step_failed") step.errors.push(String(e.payload.error ?? "step failed"));
  else if (e.level === "error") step.errors.push(`${e.type}: ${JSON.stringify(e.payload)}`);
}

/** Group a session's events into timeline steps. Agent steps open and close with their own
 * events; events of other components (sandbox, MCP servers, review app) belong to the step whose
 * time window contains them. Everything before the first agent step is provisioning. */
export function groupSteps(events: StoredEvent[]): Step[] {
  const times = hostTimes(events);
  const time = (e: StoredEvent) => times.get(e.id) ?? Date.parse(e.event.ts);
  const sorted = [...events].sort((a, b) => time(a) - time(b) || a.id - b.id);
  const steps: Step[] = [];
  const counts: Record<string, number> = {};
  let open: Step | undefined;
  const interrupted = new Set<Step>();
  let revision = 1;
  let provisioning: Step | undefined;
  const pending: StoredEvent[] = []; // non-agent events, assigned after the windows are known

  for (const stored of sorted) {
    const e = stored.event;
    if (e.type === "review.update_requested") revision += 1;
    // a circuit run's revise round (#57); the part agent's comes after update_requested
    if (e.component === "agent" && e.type === "agent.revision_started") {
      revision = Math.max(revision, Number(e.payload.revision) || revision);
    }
    if (e.component === "agent" && e.type === "agent.step_started") {
      const name = String(e.payload.step);
      // LangGraph re-runs an interrupted node from its start on resume: same step, continued.
      if (open && open.name === name && interrupted.has(open)) {
        addTo(open, stored);
        continue;
      }
      if (open) {
        // never completed (e.g. the agent stopped): close it where the next step begins
        open.errors.push("no completion event");
        close(open, time(stored), "failed");
      }
      counts[name] = (counts[name] ?? 0) + 1;
      open = newStep(name, time(stored), revision, counts[name]);
      steps.push(open);
      addTo(open, stored);
      continue;
    }
    if (e.component === "agent" && open && (e.type === "agent.step_completed" || e.type === "agent.step_failed")) {
      addTo(open, stored);
      open.warned = e.type === "agent.step_completed" && (e.level === "warning" || e.payload.ok === false);
      close(open, time(stored), e.type === "agent.step_failed" ? "failed" : "done");
      open = undefined;
      continue;
    }
    if (e.component === "agent" && e.type === "agent.interrupt" && open) {
      open.status = "waiting";
      interrupted.add(open);
    }
    if (e.component === "agent" && e.type === "agent.resumed" && open) open.status = "running";
    if (steps.length === 0) {
      provisioning ??= newStep("provisioning", time(stored), 1, 1);
      addTo(provisioning, stored);
      continue;
    }
    if (e.component === "agent" && open) addTo(open, stored);
    else pending.push(stored);
  }

  for (const stored of pending) {
    const t = time(stored);
    const inside = steps.find(
      (s) => Date.parse(s.start) <= t && (s.end === undefined || t <= Date.parse(s.end)),
    );
    const target = inside ?? [...steps].reverse().find((s) => Date.parse(s.start) <= t) ?? steps[0];
    if (target) addTo(target, stored);
  }
  for (const step of [...steps, ...(provisioning ? [provisioning] : [])]) {
    step.events.sort((a, b) => time(a) - time(b) || a.id - b.id);
    for (const e of step.events) step.hostTs.set(e.id, new Date(time(e)).toISOString());
  }
  if (provisioning) {
    const last = provisioning.events[provisioning.events.length - 1];
    if (last) close(provisioning, time(last), "done");
    return [provisioning, ...steps];
  }
  return steps;
}

export interface EventFilter {
  components: Set<string>; // empty = all
  levels: Set<Level>; // empty = all
  type: string; // substring
}

export function matches(event: AppEvent, filter: EventFilter): boolean {
  return (
    (filter.components.size === 0 || filter.components.has(event.component)) &&
    (filter.levels.size === 0 || filter.levels.has(event.level)) &&
    (filter.type === "" || event.type.includes(filter.type))
  );
}
