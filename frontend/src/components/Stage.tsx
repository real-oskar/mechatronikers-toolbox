import { useState } from "react";
import type { ReactNode } from "react";

import { useRunApi } from "../api/client";
import type { GateState, ValidationReport } from "../api/types";
import { useI18n } from "../i18n";
import type { I18n } from "../i18n";
import { describe } from "../lib/describe";
import type { Characteristic } from "../lib/inspection";
import type { RunStatus } from "../lib/status";
import type { Step } from "../lib/timeline";
import { ChecksGauge, ValidationGate } from "./Checks";
import { LazyModelViewer as ModelViewer } from "./LazyModelViewer";
import { opName } from "./RoutingSheet";
import { SheetView } from "./SheetView";
import { SignOff } from "./SignOff";

interface Figure {
  value: string;
  label: string;
}

const payload = (step: Step, type: string) =>
  [...step.events].reverse().find((e) => e.event.type === type)?.event.payload ?? null;

/** What an operation did, in plain words with its key figures. */
function summary(step: Step, gates: GateState[], { t, num }: I18n): { text: string; figures: Figure[]; report: ValidationReport | null } {
  const report = (payload(step, "validation.completed")?.report as ValidationReport | undefined) ?? null;
  switch (step.name) {
    case "provisioning":
      return { text: t("sum_provisioning"), figures: [], report };
    case "analyse": {
      const p = payload(step, "drawing.loaded");
      const views = Number(p?.views ?? 0);
      const labels = Number(p?.labels ?? 0);
      return {
        text: t("sum_analyse", { views, labels }),
        figures: [
          { value: String(views), label: t("viewDrawing") },
          { value: String(labels), label: "Labels" },
        ],
        report,
      };
    }
    case "read":
      return {
        text: t("sum_read", { calls: step.modelCalls, cost: num(step.cost, 3) }),
        figures: [{ value: String(step.modelCalls), label: "Model calls" }],
        report,
      };
    case "check": {
      const p = payload(step, "drawing.consistency_checked");
      return {
        text: t("sum_check", { dims: Number(p?.dimensions ?? 0), items: Number(p?.review_items ?? step.pois.length) }),
        figures: [],
        report,
      };
    }
    case "plan": {
      const gate = gates.find((g) => g.request.kind === "spec_review");
      return {
        // the plan reaches the UI with gate 1; until then there is nothing to count (#152)
        text: gate
          ? t("sum_plan", { features: gate.request.features.length, assumptions: gate.request.assumptions.length })
          : t("sum_plan_running"),
        figures: [],
        report,
      };
    }
    case "replan": {
      const p = payload(step, "agent.replanned") ?? payload(step, "agent.replan_discarded");
      return { text: t("sum_replan", { before: Number(p?.findings_before ?? 0), after: Number(p?.findings_after ?? 0) }), figures: [], report };
    }
    case "review_gate_1":
      return { text: t("sum_review_gate_1"), figures: [], report };
    case "build":
    case "fix": {
      const text = report
        ? t("sum_build", { passed: report.checks.filter((c) => c.passed).length, total: report.checks.length })
        : step.name === "fix"
          ? t("sum_fix", { n: step.label.match(/#(\d+)/)?.[1] ?? "1" })
          : t("sum_build_running");
      return { text, figures: [], report };
    }
    case "export":
      return { text: t("sum_export"), figures: [], report };
    case "revise":
      return { text: t("sum_revise", { n: step.revision }), figures: [], report };
    case "review_gate_2":
      return { text: t("sum_review_gate_2"), figures: [], report };
    case "give_up":
      return { text: t("sum_give_up"), figures: [], report };
    default:
      return { text: t("sum_other", { n: step.events.length }), figures: [], report };
  }
}

export function Log({ step }: { step: Step }) {
  const { t, time } = useI18n();
  const [filter, setFilter] = useState("");
  const shown = step.events.filter((e) => filter === "" || e.event.type.includes(filter));
  return (
    <div style={{ marginTop: 12 }}>
      <input className="input" placeholder={t("logFilter")} value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 280 }} />
      <div className="table-scroll" style={{ marginTop: 8 }}>
        <table className="log">
          <tbody>
            {shown.map((stored) => (
              <tr key={stored.id} className={`log-${stored.event.level}`}>
                <td className="fine" title={`emitter clock: ${stored.event.ts}`}>
                  {time(step.hostTs.get(stored.id) ?? stored.event.ts)}
                </td>
                <td className="fine muted">{stored.event.component}</td>
                <td>{stored.event.type}</td>
                <td className="fine">{describe(stored.event)}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td className="fine muted" colSpan={4}>
                  {t("noEvents")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** The right side of the routing sheet: what the selected operation produced. */
export function Stage({
  step,
  index,
  status,
  gates,
  characteristics,
  sheetSize,
  modelBuild,
  reviewer,
  onDecided,
  untrusted = false,
  footer,
}: {
  step: Step | undefined;
  index: number;
  status: RunStatus;
  gates: GateState[];
  characteristics: Characteristic[];
  sheetSize: [number, number] | null;
  modelBuild: string | null;
  reviewer: string;
  onDecided: () => void;
  untrusted?: boolean;
  /** Panels below the operation (downloads, finishing the session). */
  footer?: ReactNode;
}) {
  const i18n = useI18n();
  const { t, time, duration } = i18n;
  const api = useRunApi();
  const [view, setView] = useState<"model" | "drawing" | null>(null);
  const [log, setLog] = useState(false);
  const shown = view ?? (modelBuild ? "model" : "drawing");
  const specGate = gates.find((g) => g.open && g.request.kind === "spec_review");
  const gaveUpGate = gates.find((g) => g.open && g.request.kind === "gave_up");
  const s = step ? summary(step, gates, i18n) : null;

  return (
    <div className="stage">
      {specGate && <SignOff gate={specGate} reviewer={reviewer} onDecided={onDecided} untrusted={untrusted} />}
      <div className="viewport" style={shown === "drawing" ? { height: "auto", minHeight: 0, border: 0, background: "none" } : undefined}>
        {shown === "model" && (
          <>
            <div className="viewport-bar" style={{ justifyContent: "flex-end" }}>
              <ViewSwitch shown={shown} onChange={setView} />
            </div>
            {modelBuild ? (
              <ModelViewer url={api.modelUrl(modelBuild)} markers={characteristics} empty={t("noModel")} />
            ) : (
              <div className="viewport-empty">{t("noModel")}</div>
            )}
          </>
        )}
        {shown === "drawing" && (
          <div style={{ position: "relative" }}>
            <div style={{ position: "absolute", top: 10, right: 150, zIndex: 3 }}>
              <ViewSwitch shown={shown} onChange={setView} />
            </div>
            <SheetView url={api.sheetUrl()} size={sheetSize ?? [420, 297]} characteristics={sheetSize ? characteristics : []} />
          </div>
        )}
      </div>
      {step && s && (
        <section className="panel" aria-label={opName(step, t)}>
          <div className="panel-head">
            <h2>
              {(index + 1) * 10} {opName(step, t)}
            </h2>
            <span className="fine muted">
              {time(step.start)}, {duration(step.durationMs)}
            </span>
          </div>
          <p className="summary">{s.text}</p>
          {s.figures.length > 0 && (
            <div className="figures">
              {s.figures.map((f) => (
                <div key={f.label}>
                  <div className="figure-value">{f.value}</div>
                  <div className="figure-label">{f.label}</div>
                </div>
              ))}
            </div>
          )}
          {s.report && (step.name === "build" || step.name === "fix") && (
            <div style={{ marginTop: 12 }}>
              <ChecksGauge report={s.report} />
            </div>
          )}
          <div className="actions" style={{ justifyContent: "flex-start" }}>
            <button type="button" className="button button-quiet" onClick={() => setLog(!log)} aria-expanded={log}>
              {log ? t("hideLog") : t("showLog")}
            </button>
          </div>
          {log && <Log step={step} />}
        </section>
      )}
      <ValidationGate status={status} gaveUpGate={gaveUpGate} reviewer={reviewer} onDecided={onDecided} untrusted={untrusted} />
      {footer}
    </div>
  );
}

function ViewSwitch({ shown, onChange }: { shown: "model" | "drawing"; onChange: (v: "model" | "drawing") => void }) {
  const { t } = useI18n();
  return (
    <div className="segmented" role="group">
      <button type="button" aria-pressed={shown === "model"} onClick={() => onChange("model")}>
        {t("viewModel")}
      </button>
      <button type="button" aria-pressed={shown === "drawing"} onClick={() => onChange("drawing")}>
        {t("viewDrawing")}
      </button>
    </div>
  );
}
