import type { HubRun } from "../api/types";

// Overview rows as the hub will serve them (`GET /api/runs`, docs/13): part and circuit runs in
// every phase. Shared by the tests and the dev server (MT_DEV_MODE=hub), until the hub exists (#74).

const run = (over: Partial<HubRun> & Pick<HubRun, "run_id" | "phase">): HubRun => ({
  session_id: `ses-${over.run_id.slice(4)}`,
  run_kind: "part",
  drawing: null,
  part_name: null,
  state: "running",
  checks_passed: null,
  checks_total: null,
  open_pois: 0,
  revision: 1,
  fix_iterations: 0,
  max_fix_iterations: 3,
  model: "vendor/vision-model",
  cost: 0,
  started_at: "2026-10-17T08:00:00.000000Z",
  last_activity: "2026-10-17T08:00:00.000000Z",
  link: null,
  plan: null,
  reviewer: null,
  batch: null,
  ...over,
});

export const HUB_RUNS: HubRun[] = [
  run({
    run_id: "run-fuss0001", phase: "valid", plan: "Fuss", drawing: "Fuss_dwg.pdf", part_name: "Fuss",
    checks_passed: 12, checks_total: 12, open_pois: 2, revision: 2, fix_iterations: 1, cost: 0.084,
    reviewer: "Anna", started_at: "2026-10-17T07:12:00.000000Z", last_activity: "2026-10-17T07:31:40.000000Z",
  }),
  run({
    run_id: "run-flansch01", phase: "fixing", plan: "Flansch", drawing: "Flansch_dwg.pdf",
    part_name: "Flansch", checks_passed: 7, checks_total: 10, open_pois: 3, fix_iterations: 2, cost: 0.061,
    reviewer: "Ben", started_at: "2026-10-17T08:05:00.000000Z", last_activity: "2026-10-17T08:09:12.000000Z",
  }),
  run({
    run_id: "run-maschine1", phase: "gave_up", plan: "Maschinenteil", drawing: "Maschinenteil_dwg.pdf",
    part_name: "Maschinenteil", checks_passed: 9, checks_total: 12, open_pois: 5, fix_iterations: 3,
    cost: 0.112, reviewer: "Anna", started_at: "2026-10-17T06:40:00.000000Z", stop_reason: "cap",
    failing_checks: ["V2.bbox_z", "V2.hole_count"], failed_features: ["pocket-2"],
    last_activity: "2026-10-17T07:02:05.000000Z",
  }),
  run({
    run_id: "run-traeger01", phase: "aborted", plan: "Traegerplatte", drawing: "Traegerplatte_dwg.pdf",
    part_name: "Traegerplatte", checks_passed: 4, checks_total: 9, open_pois: 1, fix_iterations: 2,
    cost: 0.074, reviewer: "Ben", state: "awaiting_review", started_at: "2026-10-17T06:10:00.000000Z",
    stop_reason: "plateau", failed_features: ["slot-1"],
    last_activity: "2026-10-17T06:31:00.000000Z",
  }),
  run({
    run_id: "run-befest01", phase: "failed", plan: "Befestigung", drawing: "Befestigung_dwg.pdf",
    part_name: "Befestigung", cost: 1.0, state: "running", started_at: "2026-10-17T05:50:00.000000Z",
    stop_reason: "budget",
    last_activity: "2026-10-17T05:52:30.000000Z", batch: "b-nightly",
  }),
  run({
    run_id: "run-welle0001", phase: "update_run", plan: "Welle", drawing: "Welle_dwg.pdf", part_name: "Welle",
    checks_passed: 11, checks_total: 11, open_pois: 1, revision: 2, cost: 0.093, reviewer: "Clara",
    started_at: "2026-10-17T07:50:00.000000Z", last_activity: "2026-10-17T08:08:00.000000Z",
  }),
  run({
    run_id: "run-haken0001", phase: "shelved", plan: "Haken", drawing: "Haken_dwg.pdf", part_name: "Haken",
    checks_passed: 8, checks_total: 8, revision: 1, cost: 0.051, reviewer: "Clara", state: "shelved",
    started_at: "2026-10-16T15:00:00.000000Z", last_activity: "2026-10-16T15:40:00.000000Z",
  }),
  run({
    run_id: "run-dreist01", phase: "provisioning", plan: "Dreistern", drawing: "Dreistern_dwg.pdf",
    started_at: "2026-10-17T08:10:00.000000Z", last_activity: "2026-10-17T08:10:04.000000Z",
    batch: "b-nightly",
  }),
  run({
    run_id: "run-rotat001", phase: "queued", session_id: null, plan: "Rotation", drawing: "Rotation_dwg.pdf",
    started_at: "2026-10-17T08:00:30.000000Z", last_activity: null, batch: "b-nightly",
  }),
  run({
    run_id: "run-lm317001", run_kind: "circuit", phase: "waiting_gate_1", plan: "lm317_supply",
    drawing: "lm317_supply_sch.pdf", part_name: "lm317_supply", checks_passed: 6, checks_total: 7,
    open_pois: 2, max_fix_iterations: null, cost: 0.02, started_at: "2026-10-17T07:58:00.000000Z",
    last_activity: "2026-10-17T08:00:40.000000Z",
  }),
  run({
    run_id: "run-sallen01", run_kind: "circuit", phase: "valid", plan: "sallen_key",
    drawing: "sallen_key_sch.pdf", part_name: "sallen_key", checks_passed: 7, checks_total: 7,
    max_fix_iterations: null, cost: 0.018, reviewer: "Ben", started_at: "2026-10-17T07:20:00.000000Z",
    last_activity: "2026-10-17T07:24:10.000000Z",
  }),
  run({
    run_id: "run-ecc83001", run_kind: "circuit", phase: "analysing", plan: "ecc83",
    drawing: "ecc83_sch.pdf", max_fix_iterations: null, started_at: "2026-10-17T08:09:30.000000Z",
    last_activity: "2026-10-17T08:09:50.000000Z",
  }),
]; // fmt: skip
