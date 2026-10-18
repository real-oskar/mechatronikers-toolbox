import { describe, expect, it } from "vitest";

import type { OverviewRow } from "../api/types";
import { HUB_RUNS } from "../test/hubRuns";
import { DEFAULT_SORT, kindOf, matches, NO_FILTER, nextSort, planOf, runHref, sortRows } from "./overview";

const ids = (rows: OverviewRow[]) => rows.map((r) => r.run_id);

describe("overview rules", () => {
  it("filters by run kind and phase", () => {
    const circuits = HUB_RUNS.filter((r) => matches(r, { ...NO_FILTER, kind: "circuit" }));
    expect(ids(circuits)).toEqual(["run-lm317001", "run-sallen01", "run-ecc83001"]);
    const valid = HUB_RUNS.filter((r) => matches(r, { kind: "part", phase: "valid" }));
    expect(ids(valid)).toEqual(["run-fuss0001"]);
    expect(HUB_RUNS.every((r) => matches(r, NO_FILTER))).toBe(true);
    expect(kindOf({ ...HUB_RUNS[0]!, run_kind: undefined })).toBe("part"); // rows from before #83
  });

  it("sorts by column, missing values last, ties in the server's order", () => {
    expect(ids(sortRows(HUB_RUNS, DEFAULT_SORT)).slice(0, 3)).toEqual(["run-dreist01", "run-ecc83001", "run-flansch01"]);
    expect(ids(sortRows(HUB_RUNS, { key: "cost", dir: "desc" }))[0]).toBe("run-befest01");
    const checks = sortRows(HUB_RUNS, { key: "checks", dir: "asc" });
    expect(checks[0]!.run_id).toBe("run-traeger01"); // 4 / 9
    expect(checks.at(-1)!.checks_total).toBeNull(); // no checks yet: last in both directions
    expect(sortRows(HUB_RUNS, { key: "checks", dir: "desc" }).at(-1)!.checks_total).toBeNull();
    expect(ids(sortRows(HUB_RUNS, { key: "plan", dir: "asc" })).slice(0, 2)).toEqual(["run-befest01", "run-dreist01"]);
    const same = HUB_RUNS.map((r) => ({ ...r, revision: 1 }));
    expect(ids(sortRows(same, { key: "revision", dir: "desc" }))).toEqual(ids(HUB_RUNS));
  });

  it("flips a sorted column and starts others descending, the plan A–Z", () => {
    expect(nextSort(DEFAULT_SORT, "started")).toEqual({ key: "started", dir: "asc" });
    expect(nextSort(DEFAULT_SORT, "cost")).toEqual({ key: "cost", dir: "desc" });
    expect(nextSort(DEFAULT_SORT, "plan")).toEqual({ key: "plan", dir: "asc" });
  });

  it("links a hub row to its run view and a host row to its review link", () => {
    const row = HUB_RUNS[0]!;
    expect(runHref(row, true)).toBe("/runs/run-fuss0001");
    expect(runHref({ ...row, run_id: "run a/b" }, true)).toBe("/runs/run%20a%2Fb");
    expect(runHref({ ...row, link: "http://127.0.0.1:40000/?t=tok" }, false)).toBe("http://127.0.0.1:40000/?t=tok");
    expect(planOf({ ...row, plan: null })).toBe("Fuss");
  });
});
