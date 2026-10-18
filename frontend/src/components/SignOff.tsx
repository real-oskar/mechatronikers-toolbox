import { useState } from "react";

import { useRunApi } from "../api/client";
import type { GateState, ReviewItem } from "../api/types";
import { MAX_NOTE } from "../api/types";
import { useI18n } from "../i18n";
import { describeFeature } from "../lib/features";
import { Balloon } from "./common/Balloon";
import { IsolationNotice } from "./common/IsolationNotice";

function Findings({ title, items }: { title: string; items: ReviewItem[] }) {
  if (items.length === 0) return null;
  return (
    <section>
      <h3>{title}</h3>
      <ul className="plain-list">
        {items.map((item, i) => (
          <li key={item.id}>
            <Balloon n={i + 1} small status={item.severity === "blocking" ? "nok" : "check"} /> {item.message}
            <div className="fine muted">{item.reason}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Gate 1 (D57): the plan is released or rejected with a reason before anything is built. */
export function SignOff({
  gate,
  reviewer,
  onDecided,
  untrusted = false,
}: {
  gate: GateState;
  reviewer: string;
  onDecided: () => void;
  untrusted?: boolean;
}) {
  const i18n = useI18n();
  const { t, num } = i18n;
  const api = useRunApi();
  const r = gate.request;
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      });
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="signoff" aria-label={t("signOffTitle")}>
      <h2>{t("signOffTitle")}</h2>
      {(untrusted || (gate.unisolated_builds ?? 0) > 0) && <IsolationNotice />}
      <p className="muted">{t("signOffIntro")}</p>
      <div className="signoff-grid">
        <section>
          <h3>{t("plannedFeatures")}</h3>
          <ol className="feature-list">
            {r.plan?.length
              ? r.plan.map((f) => <li key={f.id}>{describeFeature(f, i18n)}</li>)
              : r.features.map((f) => <li key={f}>{f.replace(/_/g, " ")}</li>)}
          </ol>
          <h3 style={{ marginTop: 12 }}>{t("overallSize")}</h3>
          <p>
            {["x", "y", "z"].map((axis, i) => {
              // the plan names dimensions by id: show their printed values, the id on hover (#152)
              const id = r.overall_size_dims?.[i];
              const dim = id ? r.dimensions?.find((d) => d.id === id) : undefined;
              const value = dim ? `${num(dim.stated ?? dim.value)} mm` : (id ?? t("notChecked"));
              return (
                <span key={axis} title={id ?? undefined}>
                  {i > 0 && ", "}
                  {axis} {value}
                </span>
              );
            })}
          </p>
        </section>
        <section>
          {r.assumptions.length > 0 && (
            <>
              <h3>{t("assumptions")}</h3>
              <ul className="plain-list">
                {r.assumptions.map((a) =>
                  a.value === null || a.kind === "note" ? (
                    <li key={a.id}>{a.reason}</li>
                  ) : (
                    <li key={a.id}>
                      {a.kind} = {typeof a.value === "number" ? num(a.value) : a.value}
                      <div className="fine muted">{a.reason}</div>
                    </li>
                  ),
                )}
              </ul>
            </>
          )}
          <Findings title={t("foundOnDrawing")} items={r.review_items} />
          <Findings title={t("possiblyMissing")} items={r.possibly_missing} />
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
              {t("rejectPlan")}
            </button>
          </div>
        </div>
      ) : (
        <div className="actions">
          <button type="button" className="button" onClick={() => setRejecting(true)} disabled={busy || !reviewer}>
            {t("rejectEllipsis")}
          </button>
          <button type="button" className="button button-primary" onClick={() => decide("approve")} disabled={busy || !reviewer}>
            {t("releasePlan")}
          </button>
        </div>
      )}
      {error && <p className="notice notice-deviation" style={{ marginTop: 10 }}>{error}</p>}
    </section>
  );
}
