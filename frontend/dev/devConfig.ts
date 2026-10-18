// Dev-only: `GET /config.json` as the Python apps serve it (`mechatronikers.review_app.ui`),
// so one dev server can show any screen. MT_DEV_MODE=sandbox (default, run view), host (run
// table) or hub (overview of all runs, with fixture rows from dev/hubApi.ts). Never built.
import type { Plugin } from "vite";

const CONFIGS = {
  sandbox: {
    mode: "sandbox",
    api: { run: "/api", events: "/events", circuit: "/circuit/api" },
    features: { table: false, shelve: false },
    links: { runs: "http://127.0.0.1:8765/" },
  },
  host: { mode: "host", api: { runs: "/sessions" }, features: { table: true, shelve: true } },
  hub: { mode: "hub", api: { runs: "/api/runs" }, features: { table: true } },
};

export function devConfig(mode: string | undefined): Plugin {
  const config = CONFIGS[mode === "host" || mode === "hub" ? mode : "sandbox"];
  return {
    name: "mt-dev-config",
    configureServer(server) {
      server.middlewares.use("/config.json", (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(config));
      });
    },
  };
}
