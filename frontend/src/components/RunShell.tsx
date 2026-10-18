import { useCallback, useEffect, useMemo, useState } from "react";

import { useRunApi } from "../api/client";
import type { GateState, StoredItem } from "../api/types";
import { useI18n } from "../i18n";
import { KindPanels, runKind } from "../kinds";
import { useSessionEvents } from "../lib/useSessionEvents";

const REFRESH = new Set(["agent.interrupt", "agent.resumed", "review.gate_opened", "agent.run_finished"]);

/** One run (shared by every run kind): events, gates and POIs from the run's API bases; the
 * panels come from the run kind's module. */
export function RunShell() {
  const { t } = useI18n();
  const api = useRunApi();
  const { events, loaded, connected, error } = useSessionEvents();
  const [gates, setGates] = useState<GateState[]>([]);
  const [items, setItems] = useState<StoredItem[]>([]);
  const reload = useCallback(() => {
    api.gates().then(setGates).catch(() => undefined);
    api.items().then(setItems).catch(() => undefined);
  }, [api]);
  const trigger = events.filter((e) => REFRESH.has(e.event.type) || e.event.type.startsWith("review.")).length;
  useEffect(reload, [reload, trigger]);
  const kind = useMemo(() => runKind(events), [events]);
  if (error) return <p className="notice notice-deviation">{t("loadFailed", { error })}</p>;
  if (!loaded) return <p className="viewport-empty">…</p>;
  return <KindPanels kind={kind} events={events} gates={gates} items={items} connected={connected} reload={reload} />;
}
