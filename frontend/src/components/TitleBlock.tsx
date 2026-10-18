import type { ReactNode } from "react";

import type { StopReason } from "../api/types";
import { useI18n } from "../i18n";
import { phaseText as kindPhaseText, withOpenPoints } from "../lib/phases";
import type { RunStatus } from "../lib/status";
import { LangSwitch } from "./common/LangSwitch";

export type Tab = "routing" | "inspection";

export function phaseText(status: RunStatus, t: ReturnType<typeof useI18n>["t"]): string {
  const text = kindPhaseText(status.kind, status.phase, t, { n: status.fixRound, max: status.maxIterations });
  return withOpenPoints(text, status.phase, status.openPois, t, status.kind);
}

/** Why a run ended without a model, in words (#92). */
export function stopText(
  reason: StopReason,
  maxRounds: number | null,
  t: ReturnType<typeof useI18n>["t"],
): string {
  return t(`stop_${reason}`, { max: maxRounds ?? "?" });
}

/** The second tab of a run kind: the first article inspection of a part (default), the parts
 * and nets of a circuit. `note` says why it is locked. */
export interface SecondTab {
  label: string;
  enabled: boolean;
  note?: string;
}

/** The header as a drawing title block: part, drawing, revision, status, cost, reviewer. */
export function TitleBlock({
  status,
  tab,
  onTab,
  reviewer,
  onReviewer,
  connected,
  now,
  stamp,
  phase,
  second,
}: {
  status: RunStatus;
  tab: Tab;
  onTab: (tab: Tab) => void;
  reviewer: string;
  onReviewer: (name: string) => void;
  connected: boolean | null;
  now: number;
  stamp?: ReactNode;
  phase?: string; // a run kind's own phase text
  second?: SecondTab;
}) {
  const { t, num, duration } = useI18n();
  const inspection = second ?? { label: t("tabInspection"), enabled: status.valid, note: t("inspectionLocked") };
  // `now` ticks every few seconds, so an event can be newer than it: never "-2 s ago" (#152)
  const since = (ts: string) => duration(Math.max(0, now - Date.parse(ts)));
  const runtime = status.startedAt ? since(status.startedAt) : "–";
  const signal = status.lastActivity ? t("ago", { t: since(status.lastActivity) }) : "–";
  return (
    <header className="title-block">
      <div className="tb-cell tb-part">
        <span className="tb-label">{t("fieldDrawing")}: {status.drawing ?? "–"}</span>
        <div className="tb-part-row">
          <h1 className="tb-part-name">{status.partName ?? status.drawing ?? "Drawing2CAD"}</h1>
          {stamp}
        </div>
        <span />
      </div>
      <div className="tb-cell">
        <span className="tb-label">{t("fieldRevision")}</span>
        <span className="tb-value">{status.revision}</span>
      </div>
      <div className="tb-cell">
        <span className="tb-label">{t("fieldStatus")}</span>
        <span className="tb-value">{phase ?? phaseText(status, t)}</span>
      </div>
      <div className="tb-cell">
        <span className="tb-label">{t("fieldCost")}</span>
        <span className="tb-value">{t("credits", { n: num(status.cost, 3) })}</span>
      </div>
      <div className="tb-cell">
        <span className="tb-label">{t("fieldRuntime")}</span>
        <span className="tb-value">{runtime}</span>
        <span className={`signal ${connected ? "signal-live" : connected === false ? "signal-off" : ""}`}>
          {t("signalLine", {
            state: connected === null ? t("connecting") : connected ? t("live") : t("reconnecting"),
            signal,
          })}
        </span>
      </div>
      <div className="tb-cell tb-controls">
        <label className="field" style={{ flex: 1 }}>
          {t("fieldReviewer")}
          <input
            className="input"
            value={reviewer}
            maxLength={80}
            placeholder={t("reviewerPlaceholder")}
            onChange={(e) => onReviewer(e.target.value)}
          />
        </label>
        <LangSwitch />
      </div>
      <div className="tb-cell tb-tabs" role="tablist">
        <button type="button" role="tab" className="tb-tab" aria-selected={tab === "routing"} onClick={() => onTab("routing")}>
          {t("tabRouting")}
        </button>
        <button
          type="button"
          role="tab"
          className="tb-tab"
          aria-selected={tab === "inspection"}
          disabled={!inspection.enabled}
          onClick={() => onTab("inspection")}
        >
          {inspection.label}
          {!inspection.enabled && inspection.note && <span className="fine">{inspection.note}</span>}
        </button>
      </div>
    </header>
  );
}
