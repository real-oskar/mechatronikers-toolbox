import { useState } from "react";

import { ApiError, useRunApi } from "../../api/client";
import type { Correction, GateState, ReviewItem } from "../../api/types";
import { MAX_NOTE } from "../../api/types";
import { useI18n } from "../../i18n";
import type { Translate } from "../../i18n";
import type { Selection } from "../../lib/circuit";
import { MAX_CORRECTIONS } from "../../lib/circuit";
import { Balloon } from "../common/Balloon";
import { fieldName, kindName } from "./names";

/** "R2 value: 4k7 → 47k" in the reviewer's language. */
export function correctionText(c: Correction, t: Translate): string {
  const show = (v: string | null | undefined) => (v === null || v === undefined || v === "" ? "–" : c.field === "kind" ? kindName(v, t) : v);
  return `${c.id} ${fieldName(c.field, t)}: ${show(c.old)} → ${show(c.new)}`;
}

/** A refused decision in words: the review API's field errors (pydantic) per correction, a
 * decided gate, or the error as it came. */
export function decisionError(e: unknown, corrections: Correction[], t: Translate): string {
  if (!(e instanceof ApiError)) return t("decisionFailed", { error: e instanceof Error ? e.message : String(e) });
  if (e.status === 409) return t("decisionConflict");
  if (e.status !== 422) return t("decisionFailed", { error: e.message });
  let details = e.detail;
  try {
    const errors = JSON.parse(e.detail) as { loc?: unknown[]; msg?: string }[];
    if (Array.isArray(errors)) {
      details = errors
        .map(({ loc = [], msg = "" }) => {
          const text = msg.replace(/^Value error, /, "");
          const c = loc[0] === "corrections" && typeof loc[1] === "number" ? corrections[loc[1]] : undefined;
          return c ? `${correctionText(c, t)}: ${text}` : text;
        })
        .join("; ");
    }
  } catch {
    // a plain message (e.g. unknown correction targets): shown as it is
  }
  return t("decisionInvalid", { details });
}

export function CorrectionList({ corrections, onRemove }: { corrections: Correction[]; onRemove?: (c: Correction) => void }) {
  const { t } = useI18n();
  return (
    <ul className="plain-list corrections">
      {corrections.map((c) => (
        <li key={`${c.target}:${c.id}:${c.field}`}>
          <span className="status-edited">{correctionText(c, t)}</span>
          {onRemove && (
            <button
              type="button"
              className="button button-quiet"
              aria-label={t("removeCorrection", { what: `${c.id} ${fieldName(c.field, t)}` })}
              onClick={() => onRemove(c)}
            >
              ✕
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Gate 1 of a circuit run (D57, D75): approve the circuit as read, with the reviewer's
 * corrections, or reject it with a reason. The UI only records the decision; the run driver
 * applies the corrections through circuit-mcp (#57). */
export function CircuitSignOff({
  gate,
  corrections,
  onRemove,
  reviewer,
  onDecided,
  onOpenTable,
  onSelect,
}: {
  gate: GateState;
  corrections: Correction[];
  onRemove: (c: Correction) => void;
  reviewer: string;
  onDecided: () => void;
  onOpenTable?: () => void;
  onSelect: (sel: Selection) => void;
}) {
  const { t } = useI18n();
  const api = useRunApi();
  const r = gate.request;
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editable = r.circuit !== null && r.circuit !== undefined;

  const decide = async (action: "approve" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await api.decide({
        gate_id: r.id,
        action,
        reviewer,
        decided_at: new Date().toISOString(),
        reason: action === "reject" ? reason.trim() : null,
        corrections: editable ? corrections : [],
      });
      onDecided();
    } catch (e) {
      setError(decisionError(e, corrections, t));
    } finally {
      setBusy(false);
    }
  };

  const findings: ReviewItem[] = r.review_items;
  return (
    <section className="signoff" aria-label={t("cgateTitle")}>
      <h2>
        {t("cgateTitle")}
        {r.revision > 1 && <span className="muted"> · {t("revisionBand", { n: r.revision })}</span>}
      </h2>
      <p className="muted">{t("cgateIntro")}</p>
      <div className="signoff-grid">
        <section>
          <h3>{t("correctionsTitle", { n: corrections.length })}</h3>
          {corrections.length > 0 ? (
            <CorrectionList corrections={corrections} onRemove={onRemove} />
          ) : (
            <p className="fine muted" style={{ marginTop: 4 }}>
              {t("correctionsNone")}
            </p>
          )}
          {corrections.length >= MAX_CORRECTIONS && (
            <p className="notice notice-check" style={{ marginTop: 8 }}>
              {t("corrTooMany", { n: MAX_CORRECTIONS })}
            </p>
          )}
          {editable && <p className="fine muted" style={{ marginTop: 8 }}>{t("correctionsHint")}</p>}
          {onOpenTable && (
            <div className="actions" style={{ justifyContent: "flex-start", marginTop: 8 }}>
              <button type="button" className="button" onClick={onOpenTable}>
                {t("openTable")}
              </button>
            </div>
          )}
        </section>
        <section>
          {findings.length > 0 && (
            <>
              <h3>{t("cfound")}</h3>
              <ul className="plain-list">
                {findings.map((item, i) => (
                  <li key={item.id}>
                    <Balloon
                      n={i + 1}
                      small
                      status={item.severity === "blocking" ? "nok" : "check"}
                      onClick={() => onSelect({ type: "item", id: item.id })}
                      label={item.message}
                    />{" "}
                    {item.message}
                    <div className="fine muted">{item.reason}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
      {!reviewer && <p className="notice notice-check" style={{ marginTop: 12 }}>{t("nameFirst")}</p>}
      {rejecting ? (
        <div style={{ marginTop: 12 }}>
          <label className="field">
            {t("reasonRequired")}
            <textarea className="input" rows={3} maxLength={MAX_NOTE} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="actions">
            <button type="button" className="button" onClick={() => setRejecting(false)} disabled={busy}>
              {t("cancel")}
            </button>
            <button
              type="button"
              className="button button-danger"
              onClick={() => decide("reject")}
              disabled={busy || !reviewer || reason.trim() === ""}
            >
              {t("rejectCircuit")}
            </button>
          </div>
        </div>
      ) : (
        <div className="actions">
          <button type="button" className="button" onClick={() => setRejecting(true)} disabled={busy || !reviewer}>
            {t("rejectEllipsis")}
          </button>
          <button type="button" className="button button-primary" onClick={() => decide("approve")} disabled={busy || !reviewer}>
            {t("approveCircuit")}
          </button>
        </div>
      )}
      {error && <p className="notice notice-deviation" style={{ marginTop: 10 }}>{error}</p>}
    </section>
  );
}
