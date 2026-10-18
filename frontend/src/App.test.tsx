import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunSummary, StoredEvent } from "./api/types";
import { App } from "./App";
import { parseConfig } from "./config";
import { kinds } from "./kinds";
import { circuitEvents, circuitGate, circuitSpec, SID } from "./test/circuit";
import { fixtureEvents } from "./test/fixture";
import { renderIn } from "./test/render";

vi.mock("./components/ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

const SANDBOX = parseConfig({ mode: "sandbox", api: { run: "/api", events: "/events" }, features: { table: false } });
const HOST = parseConfig({ mode: "host", api: { runs: "/sessions" }, features: { table: true } });
const HUB = parseConfig({ mode: "hub", api: { runs: "/api/runs" }, features: { table: true } });

const row: RunSummary = {
  session_id: "ses-fixture0001", drawing: "Fuss_dwg.pdf", part_name: "Fuss", state: "running",
  phase: "valid", checks_passed: 3, checks_total: 3, open_pois: 2, revision: 1, fix_iterations: 1,
  max_fix_iterations: 3, model: "vendor/vision-model", cost: 0.042,
  started_at: "2026-10-17T08:00:00.000000Z", last_activity: "2026-10-17T08:02:26.100000Z",
  link: "http://127.0.0.1:40000/?t=tok",
}; // fmt: skip

const streams: string[] = [];
class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((m: MessageEvent<string>) => void) | null = null;
  constructor(url: string) {
    streams.push(url);
  }
  close() {}
}

/** A server: each path answers with its JSON body; anything else is a 404. */
function serve(routes: Record<string, unknown>) {
  const fetch = vi.fn(async (url: string) => {
    const path = url.split("?")[0]!;
    return path in routes
      ? new Response(JSON.stringify(routes[path]), { status: 200 })
      : new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
  });
  vi.stubGlobal("fetch", fetch);
  return () => fetch.mock.calls.map(([url]) => url);
}

const withKind = (events: StoredEvent[], kind: string): StoredEvent[] =>
  events.map((e) =>
    e.event.type === "agent.run_started" ? { ...e, event: { ...e.event, payload: { ...e.event.payload, kind } } } : e,
  );

// The run panels and the table are lazy chunks. Import them once up front, so a slow machine (the
// judges' docker build) spends the import time here and not inside findBy's timeout; the margin
// below covers rendering the run view under load.
const LOADED = { timeout: 10_000 };
beforeAll(async () => {
  await Promise.all([kinds.part(), kinds.circuit(), import("./components/RunTable")]);
}, 60_000);

beforeEach(() => {
  streams.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("App: one build, the config picks the screen", () => {
  it("shows the run view at / in the sandbox, from /api and /events", async () => {
    const urls = serve({ "/events/history": fixtureEvents(), "/api/review/gates": [], "/api/review/items": [] });
    renderIn(<App config={SANDBOX} />);
    const sheet = await screen.findByRole("navigation", { name: "Routing sheet" }, LOADED);
    expect(within(sheet).getByText("Release plan")).toBeInTheDocument();
    expect(urls()).toEqual(expect.arrayContaining(["/events/history?after=0", "/api/review/gates", "/api/review/items"]));
    expect(urls()).not.toContain("/sessions");
    expect(streams).toEqual([`/events/stream?after=${fixtureEvents().length}`]);
  });

  it("shows the run table at / on the host, from /sessions", async () => {
    const urls = serve({ "/sessions": [row] });
    renderIn(<App config={HOST} />);
    expect(await screen.findByText("Fuss", {}, LOADED)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", row.link);
    expect(urls().every((u) => u === "/sessions")).toBe(true);
    expect(streams).toEqual([]);
  });

  it("shows the overview of all runs at / in the hub and opens a run in place (#75)", async () => {
    const base = "/api/runs/run-fuss0001";
    const urls = serve({
      "/api/runs": { runs: [{ ...row, run_id: "run-fuss0001", plan: "Fuss", reviewer: "Anna" }], next_cursor: null },
      [`${base}/events/history`]: fixtureEvents(),
      [`${base}/review/gates`]: [],
      [`${base}/review/items`]: [],
    });
    renderIn(<App config={HUB} />);
    expect(await screen.findByRole("heading", { name: "All runs" }, LOADED)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("link", { name: "Open" }));
    expect(await screen.findByRole("navigation", { name: "Routing sheet" }, LOADED)).toBeInTheDocument();
    expect(urls()).toEqual(expect.arrayContaining(["/api/runs", `${base}/events/history?after=0`]));
  });

  it("shows a hub run at /runs/<id> from the bases below the runs base", async () => {
    window.history.replaceState(null, "", "/runs/run-1");
    const base = "/api/runs/run-1";
    const urls = serve({
      [`${base}/events/history`]: withKind(fixtureEvents(), "circuit"),
      [`${base}/review/gates`]: [],
      [`${base}/review/items`]: [],
    });
    renderIn(<App config={HUB} />);
    expect(await screen.findByRole("region", { name: "Electrical rule check" }, LOADED)).toBeInTheDocument();
    expect(urls()).toContain(`${base}/events/history?after=0`);
    expect(streams).toEqual([`${base}/events/stream?after=${fixtureEvents().length}`]);
  });

  it("reads a circuit run's circuit from the sandbox's circuit add-on", async () => {
    const config = parseConfig({ mode: "sandbox", api: { run: "/api", events: "/events", circuit: "/circuit/api" } });
    const urls = serve({
      "/events/history": circuitEvents("gate"),
      "/api/review/gates": [circuitGate()],
      "/api/review/items": [],
      [`/circuit/api/schematics/${SID}`]: circuitSpec(),
    });
    renderIn(<App config={config} />);
    expect(await screen.findByRole("region", { name: "Approve the circuit?" }, LOADED)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "R2 · resistor · 4k7" }, LOADED)).toBeInTheDocument();
    expect(urls()).toContain(`/circuit/api/schematics/${SID}`);
  });

  it("says when a route has no page in this mode", () => {
    window.history.replaceState(null, "", "/runs/run-1");
    serve({});
    renderIn(<App config={SANDBOX} />);
    expect(screen.getByText("There is no page at /runs/run-1.")).toBeInTheDocument();
  });
});
