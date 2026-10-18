import { createContext, useContext } from "react";

// `GET /config.json`, served by whichever app serves this build (D74). Mirrors `UiConfig` in
// `mechatronikers.review_app.ui` (Python is authoritative).
export type Mode = "sandbox" | "host" | "hub";

export interface RuntimeConfig {
  mode: Mode;
  /** API bases: `run` + `events` for a run view at `/`, `runs` for the table (and hub runs);
   * `circuit` for the circuit add-on of a sandbox run (default: below `run`). */
  api: { run?: string; events?: string; runs?: string; circuit?: string };
  /** `table`: `/` shows the run table; `shelve`: its rows offer "finish and shelve" (host, #52). */
  features: { table: boolean; shelve: boolean };
  /** Pages of other apps, as plain links: `runs` is the host's run table (from a sandbox). */
  links: { runs?: string };
}

/** Mode 1 in the sandbox; also the default context for components rendered without an app. */
export const SANDBOX_CONFIG: RuntimeConfig = {
  mode: "sandbox",
  api: { run: "/api", events: "/events", circuit: "/circuit/api" },
  features: { table: false, shelve: false },
  links: {},
};

const MODES: readonly string[] = ["sandbox", "host", "hub"];

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Same-origin absolute paths only: a base never points the UI at another host. */
function base(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return undefined;
  return value.replace(/\/+$/, "") || undefined;
}

/** A link to another app's page: an absolute http(s) URL, never a script or data URL. */
function link(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function parseConfig(raw: unknown): RuntimeConfig {
  if (!isObject(raw) || typeof raw.mode !== "string" || !MODES.includes(raw.mode)) {
    throw new Error("config.json: unknown mode");
  }
  const api = isObject(raw.api) ? raw.api : {};
  const features = isObject(raw.features) ? raw.features : {};
  const links = isObject(raw.links) ? raw.links : {};
  const config: RuntimeConfig = {
    mode: raw.mode as Mode,
    api: {},
    features: { table: features.table === true, shelve: features.shelve === true },
    links: {},
  };
  for (const key of ["run", "events", "runs", "circuit"] as const) {
    const value = base(api[key]);
    if (value) config.api[key] = value;
  }
  const runs = link(links.runs);
  if (runs) config.links.runs = runs;
  return config;
}

export async function loadConfig(): Promise<RuntimeConfig> {
  const response = await fetch("/config.json", { credentials: "same-origin", cache: "no-cache" });
  if (!response.ok) throw new Error(`config.json: ${response.status} ${response.statusText}`);
  return parseConfig(await response.json());
}

const ConfigContext = createContext<RuntimeConfig>(SANDBOX_CONFIG);
export const ConfigProvider = ConfigContext.Provider;
export const useConfig = () => useContext(ConfigContext);
