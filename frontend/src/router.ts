import { useSyncExternalStore } from "react";

import type { RunBases } from "./api/client";
import type { RuntimeConfig } from "./config";

// A tiny History-API router (docs/13): `/` and `/runs/<run_id>`; no dependency needed.
export type Route = { page: "home" } | { page: "run"; runId: string } | { page: "missing"; path: string };

export function matchRoute(path: string): Route {
  if (path === "/" || path === "/index.html") return { page: "home" };
  const run = /^\/runs\/([^/]+)\/?$/.exec(path)?.[1];
  if (run) {
    try {
      return { page: "run", runId: decodeURIComponent(run) };
    } catch {
      // malformed escape: falls through to "missing"
    }
  }
  return { page: "missing", path };
}

const subscribe = (onChange: () => void) => {
  window.addEventListener("popstate", onChange);
  return () => window.removeEventListener("popstate", onChange);
};

/** Go to a page of this app without reloading it (links inside the SPA). */
export function navigate(path: string) {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useRoute(): Route {
  return matchRoute(useSyncExternalStore(subscribe, () => window.location.pathname));
}

/** Where a run view reads its run and events: from the config at `/` (sandbox), from the route
 * below the runs base in the hub. `null` when the route has no run view in this mode. */
export function runBases(config: RuntimeConfig, route: Route): RunBases | null {
  const { run, events, runs, circuit } = config.api;
  if (route.page === "home" && !config.features.table && run && events) return { run, events, circuit };
  if (route.page === "run" && config.mode === "hub" && runs) {
    const id = `${runs}/${encodeURIComponent(route.runId)}`;
    return { run: id, events: `${id}/events` };
  }
  return null;
}
