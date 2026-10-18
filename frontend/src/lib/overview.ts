import type { OverviewRow, Phase, RunKind } from "../api/types";

// The overview table's rules (docs/13): the host's run table and the hub's overview share them.

export type SortKey = "plan" | "phase" | "checks" | "pois" | "revision" | "cost" | "started" | "activity";
export type SortDir = "asc" | "desc";
export interface Sort {
  key: SortKey;
  dir: SortDir;
}
export const DEFAULT_SORT: Sort = { key: "started", dir: "desc" }; // newest run first

export interface Filter {
  kind: RunKind | "all";
  phase: Phase | "all";
}
export const NO_FILTER: Filter = { kind: "all", phase: "all" };

/** The phases in the order a run passes them; the phase filter lists them like this. */
export const PHASES: readonly Phase[] = [
  "queued",
  "provisioning",
  "analysing",
  "checking",
  "waiting_gate_1",
  "building",
  "fixing",
  "exporting",
  "simulating",
  "gave_up",
  "aborted",
  "valid",
  "released",
  "update_run",
  "failed",
  "shelved",
];
const CIRCUIT_ONLY: readonly Phase[] = ["checking", "exporting", "simulating"];
const PART_ONLY: readonly Phase[] = ["building", "fixing", "gave_up", "aborted", "released", "update_run"];

/** The phases a run of this kind can be in (#110); all of them for "all". */
export function phasesFor(kind: RunKind | "all"): Phase[] {
  const other = kind === "part" ? CIRCUIT_ONLY : kind === "circuit" ? PART_ONLY : [];
  return PHASES.filter((p) => !other.includes(p));
}

export const kindOf = (row: OverviewRow): RunKind => (row.run_kind === "circuit" ? "circuit" : "part");

/** What the row is about: the hub's plan name, else the part, else the drawing. */
export const planOf = (row: OverviewRow): string | null => row.plan ?? row.part_name ?? row.drawing;

export function matches(row: OverviewRow, filter: Filter): boolean {
  return (filter.kind === "all" || kindOf(row) === filter.kind) && (filter.phase === "all" || row.phase === filter.phase);
}

// Rows without a value sort last in both directions; ties keep the server's order (stable sort).
function value(row: OverviewRow, key: SortKey): string | number | null {
  switch (key) {
    case "plan":
      return planOf(row)?.toLocaleLowerCase() ?? null;
    case "phase":
      return PHASES.indexOf(row.phase);
    case "checks":
      return row.checks_total ? (row.checks_passed ?? 0) / row.checks_total : null;
    case "pois":
      return row.open_pois;
    case "revision":
      return row.revision;
    case "cost":
      return row.cost;
    case "started":
      return row.started_at;
    case "activity":
      return row.last_activity;
  }
}

export function sortRows(rows: OverviewRow[], sort: Sort): OverviewRow[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a, sort.key);
    const y = value(b, sort.key);
    if (x === y) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return (x < y ? -1 : 1) * sign;
  });
}

/** Clicking a sorted column flips it; another column starts descending (numbers, newest first),
 * the plan ascending (A–Z). */
export function nextSort(current: Sort, key: SortKey): Sort {
  if (current.key === key) return { key, dir: current.dir === "asc" ? "desc" : "asc" };
  return { key, dir: key === "plan" ? "asc" : "desc" };
}

/** Where a row leads: the hub's run view at `/runs/<run id>`, or the host's review link (with the
 * session token) while the sandbox runs. */
export function runHref(row: OverviewRow, hub: boolean): string | null {
  if (!hub) return row.link;
  const id = row.run_id ?? row.session_id;
  return id ? `/runs/${encodeURIComponent(id)}` : null;
}
