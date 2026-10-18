import { describe, expect, it } from "vitest";

import { parseConfig, SANDBOX_CONFIG } from "./config";
import { matchRoute, runBases } from "./router";

// The bodies the Python apps serve (tests/test_run_table.py) and the hub's from docs/13.
const sandbox = {
  mode: "sandbox",
  api: { run: "/api", events: "/events", circuit: "/circuit/api" },
  features: { table: false, shelve: false },
  links: { runs: "http://127.0.0.1:8765/" },
};
const host = { mode: "host", api: { runs: "/sessions" }, features: { table: true, shelve: true } };
const hub = { mode: "hub", api: { runs: "/api/runs" }, auth: "basic", features: { table: true, upload: true } };

describe("runtime config", () => {
  it("reads the config of each mode", () => {
    expect(parseConfig(sandbox)).toEqual({ ...SANDBOX_CONFIG, links: { runs: "http://127.0.0.1:8765/" } });
    expect(parseConfig(host)).toEqual({
      mode: "host",
      api: { runs: "/sessions" },
      features: { table: true, shelve: true },
      links: {},
    });
    expect(parseConfig(hub)).toEqual({
      mode: "hub",
      api: { runs: "/api/runs" },
      features: { table: true, shelve: false },
      links: {},
    });
  });

  it("links only to http(s) pages (#52)", () => {
    const linked = (runs: unknown) => parseConfig({ mode: "sandbox", api: {}, links: { runs } }).links;
    expect(linked("http://localhost:8765")).toEqual({ runs: "http://localhost:8765/" });
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "/sessions", "", 42, null]) {
      expect(linked(bad)).toEqual({});
    }
  });

  it("rejects an unknown mode and keeps every base on this origin", () => {
    expect(() => parseConfig({ mode: "cloud", api: {} })).toThrow("unknown mode");
    expect(() => parseConfig(null)).toThrow("unknown mode");
    const config = parseConfig({ mode: "sandbox", api: { run: "https://elsewhere/api", events: "//elsewhere", runs: "/x/" } });
    expect(config.api).toEqual({ runs: "/x" });
    expect(config.features.table).toBe(false);
  });
});

describe("routes", () => {
  it("knows / and /runs/<id>", () => {
    expect(matchRoute("/")).toEqual({ page: "home" });
    expect(matchRoute("/index.html")).toEqual({ page: "home" });
    expect(matchRoute("/runs/run-1")).toEqual({ page: "run", runId: "run-1" });
    expect(matchRoute("/runs/run%201/")).toEqual({ page: "run", runId: "run 1" });
    expect(matchRoute("/runs/%E0%A4%A")).toEqual({ page: "missing", path: "/runs/%E0%A4%A" });
    expect(matchRoute("/runs/a/b")).toEqual({ page: "missing", path: "/runs/a/b" });
  });

  it("takes the run bases from the config at / and from the route in the hub", () => {
    expect(runBases(parseConfig(sandbox), { page: "home" })).toEqual({ run: "/api", events: "/events", circuit: "/circuit/api" });
    expect(runBases(parseConfig(host), { page: "home" })).toBeNull(); // the table
    expect(runBases(parseConfig(hub), { page: "run", runId: "run-1" })).toEqual({
      run: "/api/runs/run-1",
      events: "/api/runs/run-1/events",
    });
    expect(runBases(parseConfig(sandbox), { page: "run", runId: "run-1" })).toBeNull();
    expect(runBases(parseConfig(host), { page: "run", runId: "run-1" })).toBeNull();
  });
});
