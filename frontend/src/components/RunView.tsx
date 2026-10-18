import { useEffect, useMemo, useState } from "react";

import type { GateState, StoredEvent, StoredItem } from "../api/types";
import { useI18n } from "../i18n";
import { exportedBuild } from "../lib/describe";
import { buildCharacteristics } from "../lib/inspection";
import { runStatus } from "../lib/status";
import { groupSteps } from "../lib/timeline";
import { IsolationNotice } from "./common/IsolationNotice";
import { Stamp } from "./common/Stamp";
import { Downloads, FinishSession } from "./Downloads";
import { InspectionView } from "./InspectionView";
import { useReviewer } from "./ReviewerName";
import { RoutingSheet } from "./RoutingSheet";
import { Stage } from "./Stage";
import type { Tab } from "./TitleBlock";
import { TitleBlock } from "./TitleBlock";

/** The panels of a part run (the `part` run kind): routing sheet, stage and inspection. */
export function RunViewContent({
  events,
  gates,
  items,
  connected,
  reload,
}: {
  events: StoredEvent[];
  gates: GateState[];
  items: StoredItem[];
  connected: boolean | null;
  reload: () => void;
}) {
  const { t, time } = useI18n();
  const [reviewer, setReviewer] = useReviewer();
  const [tab, setTab] = useState<Tab>("routing");
  const [chosen, setChosen] = useState<string>();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);

  const steps = useMemo(() => groupSteps(events), [events]);
  const status = useMemo(() => runStatus(events.map((e) => e.event)), [events]);
  const modelBuild = useMemo(() => exportedBuild(events.map((e) => e.event)), [events]);
  const resultGate = [...gates].reverse().find((g) => g.request.kind === "result_review");
  const sheetGate = [...gates].reverse().find((g) => g.request.dimensions?.length);
  const characteristics = useMemo(() => buildCharacteristics(sheetGate?.request, items), [sheetGate, items]);

  // The operation that needs attention comes first: a waiting gate, else the running one.
  const attention = steps.find((s) => s.status === "waiting") ?? steps.find((s) => s.status === "running");
  const selectedKey = chosen ?? attention?.key ?? steps[steps.length - 1]?.key;
  const index = steps.findIndex((s) => s.key === selectedKey);
  const validAt = [...events].reverse().find((e) => e.event.type === "agent.run_finished")?.event.ts;
  const untrusted = status.unisolatedBuilds > 0 || gates.some((g) => (g.unisolated_builds ?? 0) > 0);

  return (
    <main className="page">
      <TitleBlock
        status={status}
        tab={tab}
        onTab={setTab}
        reviewer={reviewer}
        onReviewer={setReviewer}
        connected={connected}
        now={now}
        stamp={status.valid ? <Stamp tone="release" title={t("stampValid")} by={time(validAt)} press /> : null}
      />
      {untrusted && <IsolationNotice />}
      {tab === "inspection" && status.valid && resultGate ? (
        <InspectionView gate={resultGate} items={items} reviewer={reviewer} onChanged={reload} untrusted={untrusted} />
      ) : tab === "inspection" ? (
        <p className="notice" style={{ marginTop: 18 }}>
          {t("locked")}
        </p>
      ) : (
        <div className="run-layout">
          <RoutingSheet steps={steps} selected={selectedKey} onSelect={setChosen} gates={gates} />
          <Stage
            step={steps[index]}
            index={index}
            status={status}
            gates={gates}
            characteristics={characteristics}
            sheetSize={sheetGate?.request.sheet_size ?? null}
            modelBuild={modelBuild}
            reviewer={reviewer}
            onDecided={reload}
            untrusted={untrusted}
            footer={
              <>
                <Downloads events={events} />
                <FinishSession events={events} shelved={status.phase === "shelved"} />
              </>
            }
          />
        </div>
      )}
    </main>
  );
}
