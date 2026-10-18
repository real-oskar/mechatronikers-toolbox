import { useState } from "react";

import { useRunApi } from "../api/client";
import type { Check, GateState, ValidationReport } from "../api/types";
import { MAX_NOTE } from "../api/types";
import { useI18n } from "../i18n";
import type { Key } from "../i18n";
import type { RunStatus } from "../lib/status";
import { failingChecks } from "../lib/status";
import { IsolationNotice } from "./common/IsolationNotice";
import { stopText } from "./TitleBlock";

const show = (v: unknown, num: (n: number) => string) =>
  v === null || v === undefined ? "–" : typeof v === "number" ? num(v) : String(v);

/** Checks of one build as gauges per category (V0–V3); failing checks spelled out. */
export function ChecksGauge({ report }: { report: ValidationReport }) {
  const { t, num } = useI18n();
  const groups = new Map<string, Check[]>();
  for (const c of report.checks) groups.set(c.category, [...(groups.get(c.category) ?? []), c]);
  const failing = failingChecks(report);
  return (
    <div>
      {[...groups.entries()].map(([category, checks]) => (
        <div className="gauge" key={category}>
          <span>{t(`cat_${category}` as Key) ?? category}</span>
          <span className="gauge-bar" aria-hidden>
            {checks.map((c, i) => (
              <span key={`${c.id}-${i}`} className={`gauge-tick${c.passed ? "" : " gauge-tick-fail"}`} title={c.id} />
            ))}
          </span>
          <span>
            {checks.filter((c) => c.passed).length} / {checks.length}
          </span>
        </div>
      ))}
      {failing.length > 0 && (
        <ul className="check-list">
          {failing.map((c, i) => (
            <li key={`${c.id}-${i}`} className="status-nok">
              ✕ {c.detail || c.id}
              {/* rule checks (ERC) have no nominal and measured value */}
              {(c.expected ?? c.measured ?? null) !== null &&
                `: ${t("expected", { v: show(c.expected, num) })}, ${t("measuredValue", { v: show(c.measured, num) })}`}
            </li>
          ))}
        </ul>
      )}
      {report.advisory.length > 0 && (
        <p className="fine muted" style={{ marginTop: 8 }}>
          {t("advisory")}: {report.advisory.filter((c) => c.passed).length} / {report.advisory.length}
        </p>
      )}
    </div>
  );
}

/** The validation gate (D58/D59): progress, a valid model, or a give-up with retry and abort. */
export function ValidationGate({
  status,
  gaveUpGate,
  reviewer,
  onDecided,
  untrusted = false,
}: {
  status: RunStatus;
  gaveUpGate?: GateState;
  reviewer: string;
  onDecided: () => void;
  untrusted?: boolean;
}) {
  const { t } = useI18n();
  const api = useRunApi();
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const decide = async (action: "retry" | "abort") => {
    if (!gaveUpGate) return;
    setBusy(true);
    setError(null);
    try {
      await api.decide({
        gate_id: gaveUpGate.request.id,
        action,
        reviewer,
        decided_at: new Date().toISOString(),
        hint: action === "retry" ? hint.trim() : null,
        reason: action === "abort" ? "aborted by the reviewer" : null,
      });
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const max = status.maxIterations ?? "?";
  return (
    <section className="panel" aria-label={t("checksTitle")}>
      <div className="panel-head">
        <h2>{t("checksTitle")}</h2>
      </div>
      {status.valid ? (
        <p className="notice notice-release">{t("valid")}</p>
      ) : status.gaveUp ? (
        <p className="notice notice-deviation">{t("gaveUp")}</p>
      ) : status.phase === "aborted" ? (
        <p className="notice notice-deviation">{t("aborted")}</p>
      ) : status.stopReason ? (
        <p className="notice notice-deviation">{t("stopped")}</p>
      ) : status.phase === "fixing" ? (
        <p className="notice notice-busy">{t("fixing", { n: status.fixRound, max })}</p>
      ) : (
        <p className="notice">{t("checksRun")}</p>
      )}
      {status.stopReason && (
        <p style={{ marginTop: 8 }}>
          {t("stopWhy", { reason: stopText(status.stopReason, status.maxRounds, t) })}
          {status.stopError && <span className="fine muted"> ({status.stopError})</span>}
        </p>
      )}
      {status.stopReason && status.failedFeatures.length > 0 && (
        <p className="status-nok">{t("failedFeatures", { ids: status.failedFeatures.join(", ") })}</p>
      )}
      {status.latestReport && (
        <div style={{ marginTop: 12 }}>
          <ChecksGauge report={status.latestReport} />
        </div>
      )}
      {status.gaveUp && status.lastFailures.length > 0 && (
        <>
          <h3 style={{ marginTop: 12 }}>{t("lastMessages")}</h3>
          <ul className="plain-list">
            {status.lastFailures.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </>
      )}
      {status.gaveUp &&
        (gaveUpGate?.open ? (
          <div style={{ marginTop: 12 }}>
            {(untrusted || (gaveUpGate.unisolated_builds ?? 0) > 0) && <IsolationNotice />}
            <label className="field">
              {t("hint")}
              <textarea className="input" rows={3} maxLength={MAX_NOTE} value={hint} onChange={(e) => setHint(e.target.value)} />
            </label>
            <div className="actions">
              <button type="button" className="button button-danger" onClick={() => decide("abort")} disabled={busy || !reviewer}>
                {t("abort")}
              </button>
              <button
                type="button"
                className="button button-primary"
                onClick={() => decide("retry")}
                disabled={busy || !reviewer || hint.trim() === ""}
              >
                {t("retry")}
              </button>
            </div>
          </div>
        ) : (
          <p className="fine muted" style={{ marginTop: 8 }}>{t("retryWait")}</p>
        ))}
      {error && <p className="notice notice-deviation" style={{ marginTop: 10 }}>{error}</p>}
    </section>
  );
}
