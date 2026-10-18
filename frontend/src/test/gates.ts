import type { GateKind, GateState, ReviewItem, StoredItem } from "../api/types";

const item = (over: Partial<ReviewItem>): ReviewItem => ({
  id: "r-1",
  kind: "mismatch",
  severity: "warning",
  message: "Printed Ø57, circle measures Ø56.4",
  reason: "fixture",
  dimension_ids: [],
  feature_ids: [],
  sheet_ref: null,
  model_ref: null,
  status: "open",
  history: [],
  ...over,
});

/** A small gate: two printed dimensions (the lower-right one first on the sheet is 2nd), a
 * finding about the second, a possibly missing feature. */
export function gate(kind: GateKind = "result_review", open = true): GateState {
  return {
    open,
    decision: null,
    request: {
      id: `gate-${kind}`,
      kind,
      run_id: "run-1",
      revision: 1,
      created_at: "2026-10-17T08:00:00.000000Z",
      part_name: "Plate",
      features: ["extrude_base base", "hole bore"],
      plan: [
        { type: "extrude_base", id: "base", depth: 10, profile: { segments: [{ kind: "line" }, { kind: "arc" }] } },
        { type: "hole", id: "bore", diameter: 6.6, hole_type: "counterbore", cb_diameter: 11, cb_depth: 6.8 },
      ],
      assumptions: [{ id: "a-1", kind: "radius", value: 1, reason: "general note" }],
      overall_size_dims: ["d-100", null, null],
      review_items: [item({ id: "r-1", dimension_ids: ["d-57"] })],
      possibly_missing: [
        item({ id: "cov-1", kind: "possibly_missing", message: "4 circles Ø5.5, plan has 2", sheet_ref: { sheet: 0, view_id: "v0", bbox: [200, 150, 230, 180] } }),
      ],
      build_id: "build-abc123",
      validation: null,
      failures: [],
      iteration: 0,
      max_iterations: 3,
      dimensions: [
        { id: "d-57", kind: "diameter", stated: 57, stated_text: "Ø57", measured: 56.4, value: 57, source: { sheet: 0, view_id: "v0", bbox: [150, 100, 160, 104] }, method: "vlm", confidence: 0.7, status: "needs_review", reason: "mismatch", feature_ids: [] },
        { id: "d-100", kind: "linear", stated: 100, stated_text: "100", measured: 100, value: 100, source: { sheet: 0, view_id: "v0", bbox: [60, 20, 70, 24] }, method: "vlm", confidence: 0.9, status: "auto_ok", reason: null, feature_ids: [] },
      ],
      sheet_size: [420, 297],
      drawing: "Plate_dwg.pdf",
    },
  };
}

/** What the review app stores for the gate's agent POIs (rev<n>-<id>). */
export function storedFor(g: GateState): StoredItem[] {
  return [...g.request.review_items, ...g.request.possibly_missing].map((i) => ({
    item: { ...i, id: `rev1-${i.id}` },
    revision: 1,
    origin: "agent",
    created_at: g.request.created_at,
    created_by: "agent",
    proposed_value: null,
  }));
}
