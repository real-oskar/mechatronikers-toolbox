// Dev-only: serves a local design data set (frontend/.dev-data, git-ignored) under the real API
// paths, so screens can be designed with real content. Enabled with MT_DEV_DATA=1; never built.
import { readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { resolve } from "node:path";

import type { Plugin } from "vite";

type Json = Record<string, unknown>;

export function mockApi(dir: string): Plugin {
  const read = (name: string) => JSON.parse(readFileSync(resolve(dir, name), "utf8"));
  const gates: Json[] = read("gates.json");
  const events: Json[] = read("events.json");
  const items: Json[] = [];
  let next = 1;
  const now = () => new Date().toISOString();

  // MT_DEV_OPEN_GATE=1: show the plan release as still waiting (to design the sign-off)
  if (process.env.MT_DEV_OPEN_GATE) {
    const spec = gates.find((g) => (g.request as Json).kind === "spec_review");
    if (spec) Object.assign(spec, { decision: null, open: true });
  }

  // agent POIs arrive with a gate (as in the review app store)
  for (const gate of gates) {
    const request = gate.request as Json;
    for (const item of [...(request.review_items as Json[]), ...(request.possibly_missing as Json[])]) {
      items.push({
        item: { ...item, id: `rev${request.revision}-${item.id}` },
        revision: request.revision,
        origin: "agent",
        created_at: request.created_at,
        created_by: "agent",
        proposed_value: null,
      });
    }
  }

  const body = (req: IncomingMessage) =>
    new Promise<Json>((ok) => {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => ok(raw ? JSON.parse(raw) : {}));
    });
  const send = (res: ServerResponse, data: unknown, status = 200) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(data));
  };
  const file = (res: ServerResponse, name: string, type: string) => {
    res.setHeader("Content-Type", type);
    res.end(readFileSync(resolve(dir, name)));
  };

  return {
    name: "mt-mock-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, nextMiddleware) => {
        const url = new URL(req.url ?? "/", "http://dev");
        const path = url.pathname;
        if (path === "/sessions") return file(res, "sessions.json", "application/json");
        if (path === "/events/history") {
          const after = Number(url.searchParams.get("after") ?? 0);
          return send(res, events.filter((e) => (e.id as number) > after).slice(0, 1000));
        }
        if (path === "/events/stream") {
          res.setHeader("Content-Type", "text/event-stream");
          res.write(": stream open\n\n");
          return; // keeps the connection open, no new events
        }
        if (path === "/api/review/gates" && req.method === "GET") return send(res, gates);
        const decision = path.match(/^\/api\/review\/gates\/([^/]+)\/decision$/);
        if (decision && req.method === "POST") {
          const gate = gates.find((g) => (g.request as Json).id === decision[1]);
          if (!gate) return send(res, { detail: "not found" }, 404);
          gate.decision = await body(req);
          gate.open = false;
          return send(res, gate);
        }
        if (path === "/api/review/items" && req.method === "GET") return send(res, items);
        if (path === "/api/review/items" && req.method === "POST") {
          const n = (await body(req)) as Json;
          const stored = {
            item: {
              id: `poi-dev${next++}`, kind: "user_note", severity: n.severity ?? "warning",
              message: n.message, reason: `marked by reviewer ${n.reviewer}`,
              dimension_ids: n.dimension_ids ?? [], feature_ids: n.feature_ids ?? [],
              sheet_ref: n.sheet_ref ?? null, model_ref: n.model_ref ?? null, status: "open", history: [],
            },
            revision: n.revision, origin: "reviewer", created_at: now(), created_by: n.reviewer,
            proposed_value: n.proposed_value ?? null,
          };
          items.push(stored);
          return send(res, stored, 201);
        }
        const act = path.match(/^\/api\/review\/items\/([^/]+)\/actions$/);
        if (act && req.method === "POST") {
          const stored = items.find((s) => (s.item as Json).id === act[1]);
          if (!stored) return send(res, { detail: "not found" }, 404);
          const a = await body(req);
          const item = stored.item as Json;
          (item.history as Json[]).push({
            at: now(), by: a.reviewer, action: a.action,
            old_value: a.action === "edited" ? stored.proposed_value : null,
            new_value: a.new_value ?? null, comment: a.comment ?? null,
          });
          if (a.action !== "note") item.status = a.action;
          if (a.action === "edited") stored.proposed_value = a.new_value;
          return send(res, stored);
        }
        if (path === "/api/review/sheet.png") return file(res, "sheet.png", "image/png");
        if (/^\/api\/review\/model\/[^/]+\.stl$/.test(path)) return file(res, "model.stl", "model/stl");
        nextMiddleware();
      });
    },
  };
}
