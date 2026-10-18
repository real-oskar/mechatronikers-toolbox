import { lazy, Suspense } from "react";
import type { ComponentType } from "react";

import type { GateState, StoredEvent, StoredItem } from "../api/types";
import { KindNotice } from "../components/KindNotice";
import { useI18n } from "../i18n";

// Run-kind registry (docs/13, D74): the shell loads a run's events, gates and POIs; the panels
// come from the module of its kind, each in its own lazy chunk. The module grows towards the
// interface in docs/13 (steps, gate 1 body, result, overlay, downloads) with the circuit panels.
export type RunKind = "part" | "circuit";

export interface RunPanelsProps {
  events: StoredEvent[];
  gates: GateState[];
  items: StoredItem[];
  connected: boolean | null;
  reload: () => void;
}

export interface RunKindModule {
  kind: RunKind;
  Panels: ComponentType<RunPanelsProps>;
}

export const kinds: Record<RunKind, () => Promise<RunKindModule>> = {
  part: () => import("./part"),
  circuit: () => import("./circuit"),
};

// Created once per kind at load time (React.lazy caches the chunk), never during render.
const panels: Partial<Record<string, ComponentType<RunPanelsProps>>> = Object.fromEntries(
  Object.entries(kinds).map(([kind, load]) => [kind, lazy(async () => ({ default: (await load()).Panels }))]),
);

/** `agent.run_started.kind` (docs/13; `run_kind` accepted too); runs without one are parts.
 * The first run start counts: a session holds one plan, and container stdout is untrusted, so a
 * line printed later by a build script cannot switch the panels (docs/13, event provenance). */
export function runKind(events: StoredEvent[]): string {
  const started = events.find((e) => e.event.type === "agent.run_started")?.event.payload;
  const kind = started?.kind ?? started?.run_kind;
  return typeof kind === "string" && kind !== "" ? kind : "part";
}

export function KindPanels({ kind, ...props }: RunPanelsProps & { kind: string }) {
  const { t } = useI18n();
  const Panels = Object.hasOwn(panels, kind) ? panels[kind] : undefined;
  if (!Panels) {
    return (
      <KindNotice events={props.events} title={t("kindUnknown")}>
        <p className="notice notice-check">{t("kindUnknownBody", { kind })}</p>
      </KindNotice>
    );
  }
  return (
    <Suspense fallback={<p className="viewport-empty">…</p>}>
      <Panels {...props} />
    </Suspense>
  );
}
