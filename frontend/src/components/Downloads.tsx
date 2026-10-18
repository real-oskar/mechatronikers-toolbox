import { useMemo, useState } from "react";

import { useRunApi } from "../api/client";
import type { StoredEvent } from "../api/types";
import { useConfig } from "../config";
import { useI18n } from "../i18n";
import type { Key } from "../i18n";
import { exportsByRevision, fileSize } from "../lib/downloads";

/** The exported files of a revision (#52): listed from cad-mcp's `artifact.created` events, served
 * read-only by the review API, which records each download as `artifact.downloaded`. */
export function Downloads({ events }: { events: StoredEvent[] }) {
  const { t, num, dateTime } = useI18n();
  const api = useRunApi();
  const revisions = useMemo(() => exportsByRevision(events.map((e) => e.event)), [events]);
  const [chosen, setChosen] = useState<number | null>(null);
  const shown = revisions.find((r) => r.revision === chosen) ?? revisions[revisions.length - 1];
  if (!shown) return null;
  const total = shown.files.reduce((sum, f) => sum + (f.bytes ?? 0), 0);
  return (
    <section className="panel" aria-label={t("downloadsTitle")}>
      <div className="panel-head">
        <h2>{t("downloadsTitle")}</h2>
        {revisions.length > 1 && (
          <div className="segmented" role="group" aria-label={t("fieldRevision")}>
            {revisions.map((r) => (
              <button key={r.revision} type="button" aria-pressed={r === shown} onClick={() => setChosen(r.revision)}>
                {t("revisionShort", { n: r.revision })}
              </button>
            ))}
          </div>
        )}
      </div>
      <p className="fine muted">
        {t("downloadsOf", { n: shown.revision, build: shown.buildId, at: dateTime(shown.at) })}
      </p>
      <table className="downloads">
        <tbody>
          {shown.files.map((f) => (
            <tr key={f.name}>
              <td>{t(`file_${f.format.replace("-", "_")}` as Key)}</td>
              <td>
                <a href={api.exportUrl(shown.buildId, f.name, shown.revision)} download={f.name}>
                  {f.name}
                </a>
              </td>
              <td className="num fine">{fileSize(f.bytes, num)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="actions" style={{ justifyContent: "flex-start" }}>
        <a className="button button-primary" href={api.exportsZipUrl(shown.buildId, shown.revision)} download>
          {t("downloadZip", { n: shown.files.length, size: fileSize(total, num) })}
        </a>
      </div>
    </section>
  );
}

/** The sandbox cannot shelve itself (no route out): the run view sends the reviewer to the host's
 * run table, where "finish and shelve" asks for confirmation (D22, #52). */
export function FinishSession({ events, shelved }: { events: StoredEvent[]; shelved: boolean }) {
  const { t } = useI18n();
  const { links } = useConfig();
  const sessionId = events.find((e) => e.event.session_id)?.event.session_id;
  if (!links.runs || !sessionId || shelved) return null;
  return (
    <section className="panel" aria-label={t("finishTitle")}>
      <h2>{t("finishTitle")}</h2>
      <p className="summary" style={{ marginTop: 6 }}>
        {t("finishBody")}
      </p>
      <div className="actions" style={{ justifyContent: "flex-start" }}>
        <a className="button" href={`${links.runs}#${encodeURIComponent(sessionId)}`} target="_blank" rel="noopener noreferrer">
          {t("finishOpen")}
        </a>
      </div>
    </section>
  );
}
