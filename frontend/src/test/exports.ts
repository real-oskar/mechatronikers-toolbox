import type { AppEvent, StoredEvent } from "../api/types";

// cad-mcp's `artifact.created` events of an export (docs/05), for the downloads tests (#52).
export const SESSION = "ses-fixture0001";

let seq = 0;

export function event(component: string, type: string, payload: Record<string, unknown>, over: Partial<AppEvent> = {}): AppEvent {
  seq += 1;
  return {
    ts: `2026-10-17T08:0${Math.min(9, Math.floor(seq / 10))}:00.000000Z`,
    seq,
    session_id: SESSION,
    component,
    type,
    level: "info",
    payload,
    ...over,
  };
}

const FILES: [string, string, number][] = [
  ["step-ap242", "Fuss.ap242.step", 52_011],
  ["step-ap214", "Fuss.ap214.step", 48_800],
  ["stl", "Fuss.stl", 2_300_000],
  ["features-json", "Fuss.features.json", 4_200],
  ["review-json", "Fuss.review.json", 900],
];

/** One export step: every format of `build`, as cad-mcp emits them. */
export function exportOf(build: string, formats = FILES.map(([f]) => f)): AppEvent[] {
  return FILES.filter(([f]) => formats.includes(f)).map(([format, name, bytes]) =>
    event("cad-mcp", "artifact.created", { uri: `artifact://${SESSION}/${build}/${name}`, format, bytes, sha256: "0".repeat(64) }),
  );
}

export const revisionStarted = (revision: number) => event("agent", "agent.revision_started", { revision });

export const stored = (events: AppEvent[], first = 1): StoredEvent[] => events.map((e, i) => ({ id: first + i, event: e }));
