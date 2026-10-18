import type { ReactNode } from "react";

import type { StoredEvent } from "../api/types";
import { useI18n } from "../i18n";
import { runStatus } from "../lib/status";
import { LangSwitch } from "./common/LangSwitch";

/** A run this build has no panels for: its drawing, what kind it is, and what to do. */
export function KindNotice({ events, title, children }: { events: StoredEvent[]; title: string; children: ReactNode }) {
  const { t } = useI18n();
  const drawing = runStatus(events.map((e) => e.event)).drawing;
  return (
    <main className="page">
      <header className="panel-head" style={{ marginBottom: 14 }}>
        <div>
          <span className="tb-label">{t("fieldDrawing")}: {drawing ?? "–"}</span>
          <h1 style={{ fontSize: "var(--fs-title)" }}>{title}</h1>
        </div>
        <LangSwitch />
      </header>
      <section className="panel">{children}</section>
    </main>
  );
}
