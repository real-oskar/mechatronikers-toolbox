import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { AppEvent, StoredEvent } from "../api/types";

// The hand-made run shared with the Python tests (tests/test_run_table.py); tests run in frontend/.
const path = resolve(process.cwd(), "..", "tests", "fixtures", "events", "fuss-run.ndjson");

export function fixtureEvents(): StoredEvent[] {
  return readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .map((line, i) => ({ id: i + 1, event: JSON.parse(line) as AppEvent }));
}

/** Events up to and including the nth event of a type (and step). */
export function until(events: StoredEvent[], type: string, step?: string, nth = 1): StoredEvent[] {
  let seen = 0;
  const index = events.findIndex((e) => {
    if (e.event.type !== type || (step !== undefined && e.event.payload.step !== step)) return false;
    seen += 1;
    return seen === nth;
  });
  if (index < 0) throw new Error(`no ${type} ${step ?? ""}`);
  return events.slice(0, index + 1);
}

/** #65: the reviewer aborts after a give-up; the agent re-enters give_up and ends `aborted`. */
export function aborted(): StoredEvent[] {
  const events = fixtureEvents();
  const last = events[events.length - 1]!;
  const step = events.find((e) => e.event.type === "agent.step_started")!;
  return [
    ...events.slice(0, -1),
    { ...step, id: last.id, event: { ...step.event, payload: { step: "give_up" } } },
    { ...last, id: last.id + 1, event: { ...last.event, payload: { ...last.event.payload, status: "aborted" } } },
  ];
}

/** #92: the agent gives up (give_up step with its `agent.build_failed`), then the run ends with
 * `status` (`build_failed`, `aborted`, or `awaiting_review` at the gave_up gate). */
export function gaveUpRun(status: string, buildFailed: Record<string, unknown>): StoredEvent[] {
  const events = fixtureEvents();
  const last = events[events.length - 1]!;
  const step = events.find((e) => e.event.type === "agent.step_started")!;
  const paused = status === "awaiting_review" ? "gave_up" : null;
  return [
    ...events.slice(0, -1),
    { ...step, id: last.id, event: { ...step.event, payload: { step: "give_up" } } },
    { ...step, id: last.id + 1, event: { ...step.event, type: "agent.build_failed", level: "error", payload: buildFailed } },
    { ...last, id: last.id + 2, event: { ...last.event, payload: { ...last.event.payload, status, paused_at: paused } } },
  ];
}

/** #92: the run ends after a failed step or at the budget. */
export function stoppedRun(status: string, error: string | null): StoredEvent[] {
  const events = fixtureEvents();
  const last = events[events.length - 1]!;
  return [...events.slice(0, -1), { ...last, event: { ...last.event, payload: { ...last.event.payload, status, error } } }];
}

export const CAP = {
  reason: "cap",
  checks: [],
  iteration: 3,
  max_iterations: 3,
  failures: [
    { feature_id: "h1", message: "added h1 rolled back: removed no material" },
    { feature_id: "*", message: "validate_part failed: V2.hole_h1.diameter" },
  ],
};

/** D95: the first build ran without sandbox isolation (one warning), and a later one was refused
 * (an error, which does not count: that build never ran). */
export function unisolated(events: StoredEvent[]): StoredEvent[] {
  const at = events.findIndex((e) => e.event.type === "agent.step_started" && e.event.payload.step === "build");
  const base = events[at]!.event;
  const isolation = (id: number, level: "warning" | "error", build: string): StoredEvent => ({
    id,
    event: {
      ...base,
      component: "cad-mcp",
      type: "runner.isolation_unavailable",
      level,
      payload: { build_id: build, reason: "unshare: Operation not permitted", policy: "auto" },
    },
  });
  return [
    ...events.slice(0, at + 1),
    isolation(9001, "warning", "build-fx1"),
    isolation(9002, "error", "build-fx9"),
    ...events.slice(at + 1),
  ];
}

/** A run recorded for the demo video (#152): `demo-fuss` (part, inspection finished) or
 * `demo-rectifier` (circuit, ngspice without a 1N4148 model). Same files as the Python replay. */
export function demoEvents(name: "demo-fuss" | "demo-rectifier"): StoredEvent[] {
  const demo = resolve(process.cwd(), "..", "tests", "fixtures", "events", `${name}.ndjson`);
  return readFileSync(demo, "utf8")
    .trim()
    .split("\n")
    .map((line, i) => ({ id: i + 1, event: JSON.parse(line) as AppEvent }));
}
