import { describe, expect, it } from "vitest";

import { gate, storedFor } from "../test/gates";
import { buildCharacteristics, pendingChanges } from "./inspection";

describe("buildCharacteristics", () => {
  it("numbers printed dimensions across the sheet, then findings, then the reviewer's points", () => {
    const g = gate();
    const stored = storedFor(g);
    stored.push({
      item: { ...stored[0]!.item, id: "poi-1", kind: "user_note", message: "chamfer missing", severity: "blocking" },
      revision: 1, origin: "reviewer", created_at: "2026-10-17T08:05:00Z", created_by: "Anna", proposed_value: 2,
    }); // fmt: skip
    const chars = buildCharacteristics(g.request, stored);
    expect(chars.map((c) => [c.balloon, c.label, c.status, c.source])).toEqual([
      [1, "100", "ok", "reading"], // top of the sheet first
      [2, "Ø57", "check", "reading"], // the mismatch finding belongs to this dimension
      [3, "4 circles Ø5.5, plan has 2", "check", "plan"],
      [4, "chamfer missing", "nok", "reviewer"],
    ]);
    expect(chars[1]!.itemId).toBe("rev1-r-1"); // reviewer actions attach to the finding
    expect(chars[0]!.itemId).toBeNull();
    expect(chars[3]!.proposedValue).toBe(2);
  });

  it("shows the reviewer's decisions instead of the agent's status", () => {
    const g = gate();
    const stored = storedFor(g);
    stored[0]!.item.status = "approved";
    stored[1]!.item.status = "rejected";
    const chars = buildCharacteristics(g.request, stored);
    expect(chars.find((c) => c.key === "d-57")!.status).toBe("approved");
    expect(chars.find((c) => c.kind === "finding")!.status).toBe("rejected");
  });

  it("counts new points and decisions as changes for an update run", () => {
    const g = gate();
    const stored = storedFor(g);
    expect(pendingChanges(stored, 1)).toEqual({ items: [], actions: [] });
    stored[0]!.item.history.push({ at: "t", by: "Anna", action: "approved", old_value: null, new_value: null, comment: null });
    expect(pendingChanges(stored, 1).actions).toHaveLength(1);
  });
});
