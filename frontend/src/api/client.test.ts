import { afterEach, describe, expect, it, vi } from "vitest";

import { createRunApi, listOverview, listRuns, MAX_PAGES } from "./client";

afterEach(() => vi.unstubAllGlobals());

function fakeFetch() {
  const fetch = vi.fn<(url: string) => Promise<Response>>(async () => new Response("[]", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("API client", () => {
  it("calls every path below the bases it was made with", async () => {
    const fetch = fakeFetch();
    const hub = createRunApi({ run: "/api/runs/run-1", events: "/api/runs/run-1/events" });
    await hub.gates();
    await hub.items(2);
    await hub.history(5);
    await hub.act("rev1-r 1", { action: "note", reviewer: "Anna", new_value: null, comment: "ok" });
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/runs/run-1/review/gates",
      "/api/runs/run-1/review/items?revision=2",
      "/api/runs/run-1/events/history?after=5",
      "/api/runs/run-1/review/items/rev1-r%201/actions",
    ]);
    expect(hub.sheetUrl(200)).toBe("/api/runs/run-1/review/sheet.png?dpi=200");
    expect(hub.modelUrl("b-1")).toBe("/api/runs/run-1/review/model/b-1.stl");
    expect(hub.circuitSheetUrl("2a2a")).toBe("/api/runs/run-1/circuit/schematics/2a2a/sheet.png");
  });

  it("keeps the mode 1 paths and lists runs from the runs base", async () => {
    const fetch = fakeFetch();
    const sandbox = createRunApi({ run: "/api", events: "/events" });
    await sandbox.gates();
    await sandbox.history();
    await listRuns("/sessions");
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(["/api/review/gates", "/events/history?after=0", "/sessions"]);
    expect(sandbox.sheetUrl()).toBe("/api/review/sheet.png?dpi=150");
  });

  it("reads a circuit run's circuit from the circuit base (the sandbox's add-on in mode 1)", async () => {
    const fetch = fakeFetch();
    const sandbox = createRunApi({ run: "/api", events: "/events", circuit: "/circuit/api" });
    await sandbox.circuitSpec("2a2a/../x");
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(["/circuit/api/schematics/2a2a%2F..%2Fx"]);
    expect(sandbox.circuitSheetUrl("2a2a")).toBe("/circuit/api/schematics/2a2a/sheet.png");
    expect(sandbox.circuitFileUrl("2a2a", "rectifier sch.cir")).toBe("/circuit/api/schematics/2a2a/files/rectifier%20sch.cir");
  });

  it("pages through the hub's overview with the server-side filters (docs/13)", async () => {
    const pages: Record<string, unknown> = {
      "/api/runs?kind=circuit&phase=valid": { runs: [{ run_id: "a" }], next_cursor: "c1" },
      "/api/runs?kind=circuit&phase=valid&cursor=c1": { runs: [{ run_id: "b" }], next_cursor: null },
    };
    const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(pages[url]), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const { rows, truncated } = await listOverview("/api/runs", true, { kind: "circuit", phase: "valid" });
    expect(rows.map((r) => r.run_id)).toEqual(["a", "b"]);
    expect(truncated).toBe(false);
  });

  it("stops after MAX_PAGES, counts a run seen on two pages once, and lists the host's runs", async () => {
    let n = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ runs: [{ run_id: `r${n++}` }], next_cursor: "more" }))),
    );
    const endless = await listOverview("/api/runs", true);
    expect([endless.rows.length, endless.truncated]).toEqual([MAX_PAGES, true]);
    // a run started between two page requests shifts the list: the first page's last row repeats
    const pages: Record<string, unknown> = {
      "/api/runs": { runs: [{ run_id: "a", cost: 1 }, { run_id: "b" }], next_cursor: "c1" },
      "/api/runs?cursor=c1": { runs: [{ run_id: "b" }, { run_id: "c" }], next_cursor: null },
    };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(pages[url]))));
    expect((await listOverview("/api/runs", true)).rows.map((r) => r.run_id)).toEqual(["a", "b", "c"]);
    const fetch = fakeFetch();
    expect(await listOverview("/sessions", false, { kind: "part" })).toEqual({ rows: [], truncated: false });
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(["/sessions"]); // the host filters locally
  });
});
