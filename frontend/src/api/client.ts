import { createContext, useContext } from "react";

import type {
  CircuitSpec,
  GateDecision,
  GateState,
  ItemActionRequest,
  NewItem,
  OverviewRow,
  Phase,
  RunKind,
  RunPage,
  RunSummary,
  ShelveResult,
  StoredEvent,
  StoredItem,
} from "./types";

const PAGE = 1000; // the collector returns at most this many events per request

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  // Same origin: the gateway's HttpOnly cookie authenticates (no token in scripts).
  const response = await fetch(url, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

/** The bases of one run (docs/13): `run` for review and files, `events` for its event log.
 * Mode 1: `/api` and `/events`; hub: `/api/runs/<id>` and `/api/runs/<id>/events`. A circuit
 * run also reads its circuit from `circuit`: the sandbox's circuit add-on (`/circuit/api`, from
 * the runtime config) in mode 1, `<run>/circuit` otherwise. */
export interface RunBases {
  run: string;
  events: string;
  circuit?: string;
}

export function createRunApi({ run, events, circuit = `${run}/circuit` }: RunBases) {
  const review = `${run}/review`;
  const schematic = (sid: string) => `${circuit}/schematics/${encodeURIComponent(sid)}`;
  return {
    gates: () => request<GateState[]>(`${review}/gates`),
    decide: (decision: GateDecision) =>
      request<GateState>(`${review}/gates/${encodeURIComponent(decision.gate_id)}/decision`, {
        method: "POST",
        body: JSON.stringify(decision),
      }),
    items: (revision?: number) =>
      request<StoredItem[]>(revision ? `${review}/items?revision=${revision}` : `${review}/items`),
    addItem: (item: NewItem) =>
      request<StoredItem>(`${review}/items`, { method: "POST", body: JSON.stringify(item) }),
    act: (itemId: string, action: ItemActionRequest) =>
      request<StoredItem>(`${review}/items/${encodeURIComponent(itemId)}/actions`, {
        method: "POST",
        body: JSON.stringify(action),
      }),
    /** All stored events of this run after `after`, page by page. */
    async history(after = 0): Promise<StoredEvent[]> {
      const all: StoredEvent[] = [];
      let cursor = after;
      for (;;) {
        const page = await request<StoredEvent[]>(`${events}/history?after=${cursor}`);
        all.push(...page);
        const last = page[page.length - 1];
        if (page.length < PAGE || last === undefined) return all;
        cursor = last.id;
      }
    },
    /** Follow the live stream after `after`; returns a function that closes it. The browser
     * reconnects by itself and sends Last-Event-ID, so nothing is delivered twice. */
    follow(after: number, onEvent: (event: StoredEvent) => void, onStatus: (connected: boolean) => void): () => void {
      const source = new EventSource(`${events}/stream?after=${after}`);
      source.onopen = () => onStatus(true);
      source.onerror = () => onStatus(false);
      source.onmessage = (message: MessageEvent<string>) => {
        const id = Number(message.lastEventId);
        if (Number.isFinite(id)) onEvent({ id, event: JSON.parse(message.data) });
      };
      return () => source.close();
    },
    sheetUrl: (dpi = 150) => `${review}/sheet.png?dpi=${dpi}`,
    modelUrl: (buildId: string) => `${review}/model/${encodeURIComponent(buildId)}.stl`,
    /** One exported file of a build (#52); the revision goes into `artifact.downloaded`. */
    exportUrl: (buildId: string, file: string, revision: number) =>
      `${review}/artifacts/${encodeURIComponent(buildId)}/${encodeURIComponent(file)}?revision=${revision}`,
    /** All exports of a build as one ZIP (#52). */
    exportsZipUrl: (buildId: string, revision: number) =>
      `${review}/artifacts/${encodeURIComponent(buildId)}.zip?revision=${revision}`,
    /** The circuit of a circuit run as circuit-mcp holds it (read-only; corrections travel in
     * the gate decision, D75). */
    circuitSpec: (sid: string) => request<CircuitSpec>(schematic(sid)),
    circuitSheetUrl: (sid: string) => `${schematic(sid)}/sheet.png`,
    circuitFileUrl: (sid: string, name: string) => `${schematic(sid)}/files/${encodeURIComponent(name)}`,
  };
}

export type RunApi = ReturnType<typeof createRunApi>;

/** Mode 1 (sandbox) bases: the default for components rendered without a provider (tests). */
export const api = createRunApi({ run: "/api", events: "/events", circuit: "/circuit/api" });

const RunApiContext = createContext<RunApi>(api);
export const RunApiProvider = RunApiContext.Provider;
/** Components call the run they are shown for, never a hard-coded path. */
export const useRunApi = () => useContext(RunApiContext);

/** The run table: `GET <runs>` lists one `RunSummary` per run (`/sessions` on the host). */
export const listRuns = (runs: string) => request<RunSummary[]>(runs);

/** "Finish and shelve" after the user's OK in the dialog (host, D22, #52): snapshot, verify, stop.
 * Same origin and a JSON body, which the host checks against cross-site requests. */
export const shelveRun = (runs: string, sessionId: string) =>
  request<ShelveResult>(`${runs}/${encodeURIComponent(sessionId)}/shelve`, {
    method: "POST",
    body: JSON.stringify({ confirmed: true }),
  });

/** What the overview asks the hub to filter on the server (docs/13: `?kind=&phase=`). */
export interface RunFilter {
  kind?: RunKind;
  phase?: Phase;
}

export const MAX_PAGES = 10; // the overview follows at most this many hub pages per refresh

/** The overview's rows: the host answers `GET /sessions` with one list; the hub answers
 * `GET /api/runs?kind=&phase=&cursor=` page by page (docs/13). `truncated`: more pages exist, so
 * the rows are only the first ones. A run seen on two pages (the list moved between requests)
 * counts once. */
export async function listOverview(
  runs: string,
  hub: boolean,
  filter: RunFilter = {},
): Promise<{ rows: OverviewRow[]; truncated: boolean }> {
  if (!hub) return { rows: await listRuns(runs), truncated: false };
  const query = new URLSearchParams();
  if (filter.kind) query.set("kind", filter.kind);
  if (filter.phase) query.set("phase", filter.phase);
  const rows = new Map<string, OverviewRow>();
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    if (cursor) query.set("cursor", cursor);
    const params = query.toString();
    const body = await request<RunPage>(params ? `${runs}?${params}` : runs);
    for (const row of body.runs) if (!rows.has(row.run_id)) rows.set(row.run_id, row);
    cursor = body.next_cursor;
    if (!cursor) return { rows: [...rows.values()], truncated: false };
  }
  return { rows: [...rows.values()], truncated: true };
}
