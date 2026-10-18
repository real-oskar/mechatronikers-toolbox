// Dev-only: `GET /api/runs` as the hub will serve it (docs/13; the hub comes with #74), from the
// overview fixture, with the kind/phase filters and cursor pages. Active runs keep signalling, so
// the table's live update can be watched. Each run's view (`/runs/<id>`) shows the shared fixture
// run (tests/fixtures/events). Enabled with MT_DEV_MODE=hub; never built.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Plugin } from "vite";

import { HUB_RUNS } from "../src/test/hubRuns.ts";

const EVENTS = resolve(import.meta.dirname, "../../tests/fixtures/events/fuss-run.ndjson");

const PAGE = 4; // small pages, so the client's cursor handling shows in dev
const ACTIVE = new Set(["provisioning", "analysing", "building", "fixing", "update_run"]);

export function hubApi(): Plugin {
  const started = Date.now();
  return {
    name: "mt-hub-api",
    configureServer(server) {
      server.middlewares.use("/api/runs", (req, res, next) => {
        const url = new URL(req.url ?? "/", "http://dev");
        if (req.method !== "GET") return next();
        const run = /^\/([^/]+)\/(events\/history|events\/stream|review\/gates|review\/items)$/.exec(url.pathname);
        if (run) return runView(decodeURIComponent(run[1]!), run[2]!, url, res);
        if (url.pathname !== "/") return next();
        const kind = url.searchParams.get("kind");
        const phase = url.searchParams.get("phase");
        const now = new Date().toISOString();
        const seconds = (Date.now() - started) / 1000;
        const rows = HUB_RUNS.filter((r) => (!kind || (r.run_kind ?? "part") === kind) && (!phase || r.phase === phase)).map(
          (r) => (ACTIVE.has(r.phase) ? { ...r, last_activity: now, cost: r.cost + 0.001 * Math.floor(seconds / 3) } : r),
        );
        const offset = Number(url.searchParams.get("cursor") ?? 0);
        const page = rows.slice(offset, offset + PAGE);
        const next_cursor = offset + PAGE < rows.length ? String(offset + PAGE) : null;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ runs: page, next_cursor }));
      });
    },
  };
}

function runView(runId: string, route: string, url: URL, res: import("node:http").ServerResponse) {
  if (route === "events/stream") {
    res.setHeader("Content-Type", "text/event-stream");
    res.write(": stream open\n\n"); // the fixture run is complete: nothing follows
    return;
  }
  res.setHeader("Content-Type", "application/json");
  if (route !== "events/history") return res.end("[]");
  const kind = HUB_RUNS.find((r) => r.run_id === runId)?.run_kind ?? "part";
  const after = Number(url.searchParams.get("after") ?? 0);
  const events = readFileSync(EVENTS, "utf8")
    .trim()
    .split("\n")
    .map((line, i) => {
      const event = JSON.parse(line);
      if (event.type === "agent.run_started") event.payload = { ...event.payload, kind };
      return { id: i + 1, event };
    });
  res.end(JSON.stringify(events.filter((e) => e.id > after)));
}
