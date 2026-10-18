// Circuit runs (docs/12, docs/13 step 3): the schematic with parts and nets, the electrical rule
// check, gate 1 with the reviewer's corrections, netlists and ngspice's operating point. Replaces
// the static `/circuit/` page; the circuit is read from the circuit add-on, decisions go to the
// review API (D75: the UI only records, the run driver applies corrections, #57).
import { useCallback, useEffect, useMemo, useState } from "react";

import { useRunApi } from "../api/client";
import type { BBox, CircuitSpec, Correction, ReviewItem } from "../api/types";
import { CircuitSheet } from "../components/circuit/CircuitSheet";
import { CircuitSignOff } from "../components/circuit/CircuitSignOff";
import { CircuitChecks, CircuitResult, CircuitStep } from "../components/circuit/CircuitStage";
import { CircuitTables, netRow, partRow } from "../components/circuit/CircuitTables";
import { Stamp } from "../components/common/Stamp";
import { useReviewer } from "../components/ReviewerName";
import { RoutingSheet } from "../components/RoutingSheet";
import type { Tab } from "../components/TitleBlock";
import { TitleBlock } from "../components/TitleBlock";
import { useI18n } from "../i18n";
import type { Key } from "../i18n";
import type { Corrections, Selection } from "../lib/circuit";
import {
  circuitFit,
  circuitView,
  ercReport,
  netlists,
  operatingPoint,
  schematicId,
  selectionBox,
  setCorrection,
  validCorrections,
} from "../lib/circuit";
import { runStatus } from "../lib/status";
import { groupSteps } from "../lib/timeline";
import type { RunKind, RunPanelsProps } from "./index";

export const kind: RunKind = "circuit";

const OPS = ["analyse", "check", "review_gate_1", "revise", "export", "simulate"] as const;
// Events after which circuit-mcp's state may have changed: read the circuit again.
const RELOAD = /^(circuit\.|agent\.step_completed|agent\.resumed)/;
const NONE: Corrections = new Map();

const rowId = (sel: Selection) => (sel.type === "part" ? partRow(sel.id) : sel.type === "net" ? netRow(sel.id) : null);

export function Panels({ events, gates, connected, reload }: RunPanelsProps) {
  const i18n = useI18n();
  const { t, time } = i18n;
  const api = useRunApi();
  const [reviewer, setReviewer] = useReviewer();
  const [tab, setTab] = useState<Tab>("routing");
  const [chosen, setChosen] = useState<string>();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [focus, setFocus] = useState<{ box: BBox; zoom?: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);

  const appEvents = useMemo(() => events.map((e) => e.event), [events]);
  const steps = useMemo(() => groupSteps(events), [events]);
  const status = useMemo(() => runStatus(appEvents), [appEvents]);
  // the shared phase rules and texts (lib/status, lib/phases; the run table says the same)
  const notApplied = appEvents.some((e) => e.component === "agent" && e.type === "agent.corrections_not_applied");
  const sid = useMemo(() => schematicId(appEvents, gates), [appEvents, gates]);
  const gate = gates.find((g) => g.open && g.request.kind === "spec_review");
  const payload = gate?.request.circuit ?? null;

  // The circuit as circuit-mcp holds it (wires for the overlay; values after corrections).
  const [loaded, setLoaded] = useState<{ sid: string; spec: CircuitSpec } | null>(null);
  const trigger = events.filter((e) => RELOAD.test(e.event.type)).length;
  useEffect(() => {
    if (!sid) return;
    let cancelled = false;
    api
      .circuitSpec(sid)
      .then((spec) => !cancelled && setLoaded({ sid, spec }))
      .catch(() => undefined); // shown from the gate until the circuit answers
    return () => {
      cancelled = true;
    };
  }, [api, sid, trigger]);
  const spec = loaded && loaded.sid === sid ? loaded.spec : null;
  const view = useMemo(() => circuitView(spec, payload), [spec, payload]);
  // First sight of a schematic: zoomed to the circuit, not the whole sheet.
  const [fitted, setFitted] = useState<string | null>(null);
  if (sid && view && fitted !== sid) {
    setFitted(sid);
    setFocus(circuitFit(view));
  }

  // Corrections belong to the open gate: a new gate starts with none.
  const [edits, setEdits] = useState<{ gate: string | null; map: Corrections; version: number }>({ gate: null, map: NONE, version: 0 });
  const gateId = gate?.request.id ?? null;
  const corrections = edits.gate === gateId ? edits.map : NONE;
  const pending = useMemo(() => validCorrections(corrections, payload), [corrections, payload]);
  const correct = useCallback(
    (c: Correction) => {
      const { corrections: map, problem } = setCorrection(corrections, c);
      setEdits((e) => ({ gate: gateId, map, version: e.version }));
      return problem;
    },
    [corrections, gateId],
  );
  const remove = (c: Correction) =>
    setEdits((e) => ({ gate: gateId, map: setCorrection(e.map, { ...c, new: "" }).corrections, version: e.version + 1 }));
  const corrected = useMemo(() => {
    const owner = new Map(view?.components.flatMap((c) => c.terminals.map((term) => [term.id, c.refdes] as const)) ?? []);
    return new Set(pending.map((c) => (c.target === "terminal" ? (owner.get(c.id) ?? c.id) : c.id)));
  }, [pending, view]);

  // Findings: the run's POIs (analysis and later), the gate's, circuit-mcp's.
  const findings = useMemo(() => {
    const byId = new Map<string, ReviewItem>();
    for (const item of [...(spec?.review_items ?? []), ...(gate?.request.review_items ?? []), ...steps.flatMap((s) => s.pois.map((p) => p.item))]) {
      byId.set(item.id, item);
    }
    return [...byId.values()];
  }, [spec, gate, steps]);

  const select = (sel: Selection, from: "sheet" | "list") => {
    if (from === "sheet") {
      const same = selection?.type === sel.type && selection.id === sel.id;
      setSelection(same ? null : sel);
      const row = same ? null : rowId(sel);
      if (row) document.getElementById(row)?.scrollIntoView?.({ block: "nearest" });
      return;
    }
    setSelection(sel);
    const box = selectionBox(sel, view, findings);
    if (box) setFocus({ box });
  };

  const names = useMemo(() => Object.fromEntries(OPS.map((op) => [op, t(`cop_${op}` as Key)])), [t]);
  const attention = steps.find((s) => s.status === "waiting") ?? steps.find((s) => s.status === "running");
  const selectedKey = chosen ?? attention?.key ?? steps[steps.length - 1]?.key;
  const index = steps.findIndex((s) => s.key === selectedKey);
  const finished = [...appEvents].reverse().find((e) => e.component === "agent" && e.type === "agent.run_finished");
  const drawing = status.drawing ?? gate?.request.drawing ?? null;
  const name = gate?.request.part_name ?? (drawing ? drawing.split("/").pop()!.replace(/\.pdf$/i, "") : t("kind_circuit"));

  const sheet =
    sid && view ? (
      <CircuitSheet
        sid={sid}
        view={view}
        items={findings}
        selection={selection}
        corrected={corrected}
        onSelect={(sel) => select(sel, "sheet")}
        focus={focus}
      />
    ) : (
      <div className="sheet-frame viewport-empty">{t("waitingFirst")}</div>
    );
  const signOff = (openTable: boolean) =>
    gate && (
      <CircuitSignOff
        key={gate.request.id}
        gate={gate}
        corrections={pending}
        onRemove={remove}
        reviewer={reviewer}
        onDecided={reload}
        onOpenTable={openTable ? () => setTab("inspection") : undefined}
        onSelect={(sel) => select(sel, "list")}
      />
    );

  return (
    <main className="page">
      <TitleBlock
        status={{ ...status, partName: name }}
        tab={tab}
        onTab={setTab}
        reviewer={reviewer}
        onReviewer={setReviewer}
        connected={connected}
        now={now}
        second={{ label: t("tabCircuit"), enabled: view !== null, note: t("tabCircuitLocked") }}
        stamp={status.valid && !notApplied ? <Stamp tone="release" title={t("stampNetlists")} by={time(finished?.ts)} press /> : null}
      />
      {tab === "inspection" && view ? (
        <>
          <div className="inspection-layout">
            {sheet}
            <CircuitTables
              view={view}
              selection={selection}
              onSelect={(sel) => select(sel, "list")}
              editing={payload ? { corrections, onCorrect: correct, version: edits.version } : null}
            />
          </div>
          <div style={{ marginTop: 18 }}>{signOff(false)}</div>
        </>
      ) : (
        <div className="run-layout">
          <RoutingSheet steps={steps} selected={selectedKey} onSelect={setChosen} gates={gates} names={names} />
          <div className="stage">
            {signOff(true)}
            {sheet}
            {steps[index] && <CircuitStep step={steps[index]} index={index} names={names} gates={gates} />}
            <CircuitChecks report={ercReport(appEvents)} />
            <CircuitResult
              sid={sid}
              rejectedReason={finished?.payload.status === "rejected" ? String(finished.payload.reason ?? "") : null}
              files={netlists(appEvents)}
              op={operatingPoint(appEvents)}
            />
          </div>
        </div>
      )}
    </main>
  );
}
