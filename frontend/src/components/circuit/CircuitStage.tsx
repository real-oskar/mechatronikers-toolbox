import { useState } from "react";

import { useRunApi } from "../../api/client";
import type { Correction, GateState, ValidationReport } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18n, Key } from "../../i18n";
import type { Netlist, OperatingPoint } from "../../lib/circuit";
import type { Step } from "../../lib/timeline";
import { ChecksGauge } from "../Checks";
import { Balloon } from "../common/Balloon";
import { opName } from "../RoutingSheet";
import { Log } from "../Stage";
import { CorrectionList } from "./CircuitSignOff";

const payload = (step: Step, type: string) =>
  [...step.events].reverse().find((e) => e.event.type === type)?.event.payload ?? null;

interface Figure {
  value: string;
  label: string;
}

/** What a circuit run's operation did, in plain words with its key figures. */
function summary(step: Step, { t }: I18n): { text: string; figures: Figure[] } {
  switch (step.name) {
    case "provisioning":
      return { text: t("sum_provisioning"), figures: [] };
    case "analyse": {
      const p = payload(step, "circuit.loaded");
      const parts = Number(p?.components ?? 0);
      const nets = Number(p?.nets ?? 0);
      return {
        text: t("csum_analyse", { parts, nets, items: Number(p?.review_items ?? step.pois.length) }),
        figures: [
          { value: String(parts), label: t("fig_parts") },
          { value: String(nets), label: t("fig_nets") },
          { value: String(Number(p?.wires ?? 0)), label: t("fig_wires") },
          { value: String(Number(p?.junctions ?? 0)), label: t("fig_junctions") },
        ],
      };
    }
    case "check": {
      const report = payload(step, "validation.completed")?.report as ValidationReport | undefined;
      const checks = report?.checks ?? [];
      return { text: t("csum_check", { passed: checks.filter((c) => c.passed).length, total: checks.length }), figures: [] };
    }
    case "review_gate_1":
      return { text: t("csum_review_gate_1"), figures: [] };
    case "revise": {
      const p = payload(step, "agent.step_completed");
      return { text: t("csum_revise", { n: Number(p?.corrections ?? 0), reason: String(p?.reason || "–") }), figures: [] };
    }
    case "export":
      return { text: t("csum_export"), figures: [] };
    case "simulate": {
      const p = payload(step, "circuit.simulated");
      return { text: p?.ok ? t("csum_simulate") : t("csum_simulate_no", { reason: String(p?.reason ?? "–") }), figures: [] };
    }
    default:
      return { text: t("sum_other", { n: step.events.length }), figures: [] };
  }
}

/** The gate this operation waited at (gate 1 repeats per revision), from its events. */
function stepGate(step: Step, gates: GateState[]): GateState | undefined {
  const ids = new Set(step.events.map((e) => e.event.payload.gate_id).filter((id) => typeof id === "string"));
  return gates.find((g) => ids.has(g.request.id));
}

/** The corrections an operation dealt with: recorded with its gate's decision, or applied by the
 * run driver (`agent.corrections_applied`, #57). */
function stepCorrections(step: Step, gates: GateState[]): { title: Key; list: Correction[] } | null {
  if (step.name === "review_gate_1") {
    const list = stepGate(step, gates)?.decision?.corrections ?? [];
    return list.length ? { title: "correctionsDecided", list } : null;
  }
  const applied = step.events.find((e) => e.event.component === "agent" && e.event.type === "agent.corrections_applied");
  const list = applied?.event.payload.corrections;
  return Array.isArray(list) && list.length ? { title: "correctionsApplied", list: list as Correction[] } : null;
}

/** The selected operation of the routing sheet: summary, figures, findings, corrections, log. */
export function CircuitStep({
  step,
  index,
  names,
  gates,
}: {
  step: Step;
  index: number;
  names: Record<string, string>;
  gates: GateState[];
}) {
  const i18n = useI18n();
  const { t, time, duration } = i18n;
  const [log, setLog] = useState(false);
  const s = summary(step, i18n);
  const corrections = stepCorrections(step, gates);
  return (
    <section className="panel" aria-label={opName(step, t, names)}>
      <div className="panel-head">
        <h2>
          {(index + 1) * 10} {opName(step, t, names)}
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
      {step.pois.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h3>{step.pois.length === 1 ? t("findingsOne") : t("findings", { n: step.pois.length })}</h3>
          <ul className="plain-list">
            {step.pois.map((p, i) => (
              <li key={`${p.item.id}-${i}`}>
                <Balloon n={i + 1} small status={p.item.severity === "blocking" ? "nok" : "check"} /> {p.item.message}
                <div className="fine muted">{p.item.reason}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
      {corrections && (
        <div style={{ marginTop: 12 }}>
          <h3>{t(corrections.title)}</h3>
          <CorrectionList corrections={corrections.list} />
        </div>
      )}
      <div className="actions" style={{ justifyContent: "flex-start" }}>
        <button type="button" className="button button-quiet" onClick={() => setLog(!log)} aria-expanded={log}>
          {log ? t("hideLog") : t("showLog")}
        </button>
      </div>
      {log && <Log step={step} />}
    </section>
  );
}

/** The electrical rule check in the checks panel both run kinds share (category `ERC`). */
export function CircuitChecks({ report }: { report: ValidationReport | null }) {
  const { t } = useI18n();
  const failing = report?.checks.filter((c) => !c.passed) ?? [];
  const advisory = report?.advisory.filter((c) => !c.passed) ?? [];
  return (
    <section className="panel" aria-label={t("ercTitle")}>
      <div className="panel-head">
        <h2>{t("ercTitle")}</h2>
      </div>
      {!report ? (
        <p className="notice">{t("ercPending")}</p>
      ) : failing.length === 0 ? (
        <p className="notice notice-release">{t("ercPassed")}</p>
      ) : (
        <p className="notice notice-check">{t("ercFailing", { n: failing.length })}</p>
      )}
      {report && (
        <div style={{ marginTop: 12 }}>
          <ChecksGauge report={report} />
        </div>
      )}
      {advisory.length > 0 && (
        <>
          <h3 style={{ marginTop: 10 }}>{t("ercAdvisory")}</h3>
          <ul className="check-list">
            {advisory.map((c, i) => (
              <li key={`${c.id}-${i}`} className="status-check fine">
                {c.id}: {c.detail}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

const FORMAT_KEYS: Record<Netlist["format"], Key> = {
  spice: "fmt_spice",
  ltspice: "fmt_ltspice",
  "kicad-netlist": "fmt_kicad-netlist",
  "circuit-json": "fmt_circuit-json",
};

/** Four significant digits in the reviewer's locale, e.g. 1,600 V or 0,0003404 V. */
function volts(v: number, num: I18n["num"]): string {
  if (v === 0) return "0 V";
  const digits = Math.max(0, Math.min(9, 3 - Math.floor(Math.log10(Math.abs(v)))));
  return `${num(Number(v.toPrecision(4)), digits)} V`;
}

/** The circuit result: netlist downloads and ngspice's operating point (once exported). */
export function CircuitResult({
  sid,
  rejectedReason,
  files,
  op,
}: {
  sid: string | null;
  rejectedReason: string | null;
  files: Netlist[];
  op: OperatingPoint | null;
}) {
  const { t, num } = useI18n();
  const api = useRunApi();
  const exported = sid !== null && files.length > 0;
  return (
    <section className="panel" aria-label={t("resultTitle")}>
      <div className="panel-head">
        <h2>{t("resultTitle")}</h2>
      </div>
      {!exported ? (
        <p className={rejectedReason !== null ? "notice notice-deviation" : "notice"}>
          {rejectedReason !== null ? t("netlistsRejected", { reason: rejectedReason || "–" }) : t("netlistsLocked")}
        </p>
      ) : (
        <>
          <p className="fine muted">{t("netlistsNote")}</p>
          <div className="downloads">
            {files.map((f) => (
              <a key={f.name} className="button" href={api.circuitFileUrl(sid, f.name)} download={f.name}>
                {t(FORMAT_KEYS[f.format])} <span className="fine muted">{f.name}</span>
              </a>
            ))}
          </div>
          <h3 style={{ marginTop: 14 }}>{t("opTitle")}</h3>
          {!op ? (
            <p className="fine muted">{t("opNone")}</p>
          ) : !op.ok ? (
            <p className="notice" style={{ marginTop: 6 }}>
              {t("csum_simulate_no", { reason: op.reason })}
            </p>
          ) : (
            <table className="op-table">
              <thead>
                <tr>
                  <th>{t("colNode")}</th>
                  <th className="num">{t("colVoltage")}</th>
                </tr>
              </thead>
              <tbody>
                {op.voltages.map(([node, v]) => (
                  <tr key={node}>
                    <td>{node}</td>
                    <td className="num">{volts(v, num)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
