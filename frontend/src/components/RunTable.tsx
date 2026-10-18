import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";

import { ApiError, listOverview, shelveRun } from "../api/client";
import type { OverviewRow, Phase, RunKind } from "../api/types";
import { useConfig } from "../config";
import { useI18n } from "../i18n";
import { DEFAULT_SORT, kindOf, matches, NO_FILTER, nextSort, phasesFor, planOf, runHref, sortRows } from "../lib/overview";
import type { Filter, Sort, SortKey } from "../lib/overview";
import { phaseClass, phaseText, withOpenPoints } from "../lib/phases";
import { navigate } from "../router";
import { LangSwitch } from "./common/LangSwitch";
import { stopText } from "./TitleBlock";

const POLL_MS = 3000; // docs/06: the table polls; the hub has no overview stream (docs/13)

/** Plain clicks on a hub link stay in this app; modified clicks open a new tab as usual. */
function inApp(e: MouseEvent<HTMLAnchorElement>) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(e.currentTarget.getAttribute("href") ?? "/");
}

function SortHeader({ label, column, sort, onSort }: { label: string; column: SortKey; sort: Sort; onSort?: (s: Sort) => void }) {
  const active = sort.key === column;
  if (!onSort) return <th>{label}</th>;
  return (
    <th aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" className="sort-button" onClick={() => onSort(nextSort(sort, column))}>
        {label}
        <span aria-hidden className="sort-mark">
          {active ? (sort.dir === "asc" ? "▲" : "▼") : ""}
        </span>
      </button>
    </th>
  );
}

// Session states the session manager can shelve (`SessionManager.shelve`, D22).
const SHELVABLE = new Set(["running", "awaiting_review", "failed"]);
// The run itself has ended: shelving then stops nothing that still works. Plain strings, so the
// phase `released` (#155, a finished inspection) counts before and after it joins `Phase`.
const ENDED: ReadonlySet<string> = new Set(["valid", "released", "gave_up", "aborted", "failed"]);
/** Shelving a run in this phase stops nothing that still works (no warning in the dialog). */
export const runEnded = (phase: string) => ENDED.has(phase);

/** Where a shelved session stands: the snapshot's expiry, or why it has none (#52). */
function SessionState({ row }: { row: OverviewRow }) {
  const { t, dateTime } = useI18n();
  if (row.state === "shelved" && row.expires_at)
    return <div className="fine muted">{t("state_shelved", { date: dateTime(row.expires_at) })}</div>;
  if (row.state === "snapshotting" || row.state === "expired" || row.state === "deleted")
    return <div className="fine muted">{t(`state_${row.state}`)}</div>;
  return null;
}

/** The run table of both modes: the host's runs (`/sessions`) and the hub's overview (docs/13).
 * `onShelve`: the host's "finish and shelve" for sessions that can be shelved (#52). `target`: the
 * session a run view linked to (`/#<session_id>`), highlighted. */
export function RunTableView({
  rows,
  now,
  hub = false,
  sort = DEFAULT_SORT,
  onSort,
  onShelve,
  target,
}: {
  rows: OverviewRow[];
  now: number;
  hub?: boolean;
  sort?: Sort;
  onSort?: (sort: Sort) => void;
  onShelve?: (row: OverviewRow) => void;
  target?: string;
}) {
  const { t, num, dateTime, duration } = useI18n();
  const sorted = useMemo(() => sortRows(rows, sort), [rows, sort]);
  const reviewers = hub || rows.some((r) => r.reviewer);
  const head = (label: string, column: SortKey) => <SortHeader label={label} column={column} sort={sort} onSort={onSort} />;
  return (
    <div className="table-scroll">
      <table className="runs">
        <thead>
          <tr>
            <th>{t("colKind")}</th>
            {head(hub ? t("colPlan") : t("colPart"), "plan")}
            {head(t("colPhase"), "phase")}
            {head(t("colChecks"), "checks")}
            {head(t("colPois"), "pois")}
            {reviewers && <th>{t("colReviewer")}</th>}
            {head(t("fieldRevision"), "revision")}
            <th>{t("colFixes")}</th>
            {head(t("fieldCost"), "cost")}
            {head(t("colStarted"), "started")}
            {head(t("colActivity"), "activity")}
            <th />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => {
            const href = runHref(row, hub);
            const kind = kindOf(row);
            return (
              <tr
                key={row.run_id ?? row.session_id ?? planOf(row)}
                id={row.session_id ?? undefined}
                className={target && row.session_id === target ? "run-target" : undefined}
                aria-current={target && row.session_id === target ? "true" : undefined}
              >
                <td>
                  <span className={`kind-tag kind-${kind}`}>{t(`kindShort_${kind}`)}</span>
                </td>
                <td>
                  <div className="runs-part">{planOf(row) ?? "–"}</div>
                  <div className="fine muted">
                    {row.drawing ?? row.session_id}
                    {row.batch ? ` · ${t("batch", { id: row.batch })}` : ""}
                  </div>
                  {(row.unisolated_builds ?? 0) > 0 && (
                    <div className="fine status-nok" title={t("unisolated")}>
                      {row.unisolated_builds} {t("unisolatedFlag")}
                    </div>
                  )}
                </td>
                <td className={phaseClass(row.phase)}>
                  {withOpenPoints(phaseText(kind, row.phase, t, { n: row.fix_iterations, max: row.max_fix_iterations }), row.phase, row.open_pois, t, kind)}
                  {row.stop_reason && <div className="fine">{stopText(row.stop_reason, row.max_fix_iterations, t)}</div>}
                  {(row.failing_checks ?? []).length > 0 && (
                    <div className="fine muted">{t("failingCheckIds", { ids: row.failing_checks!.join(", ") })}</div>
                  )}
                  {(row.failed_features ?? []).length > 0 && (
                    <div className="fine muted">{t("failedFeatures", { ids: row.failed_features!.join(", ") })}</div>
                  )}
                  <SessionState row={row} />
                </td>
                <td>{row.checks_total === null ? "–" : `${row.checks_passed} / ${row.checks_total}`}</td>
                <td>{row.open_pois}</td>
                {reviewers && <td>{row.reviewer ?? "–"}</td>}
                <td>{row.revision}</td>
                <td>
                  {row.fix_iterations}
                  {row.max_fix_iterations ? ` / ${row.max_fix_iterations}` : ""}
                </td>
                <td>{num(row.cost, 3)}</td>
                <td className="fine">{dateTime(row.started_at)}</td>
                <td className="fine">{row.last_activity ? t("ago", { t: duration(Math.max(0, now - Date.parse(row.last_activity))) }) : "–"}</td>
                <td>
                  <div className="run-actions">
                    {href &&
                      (hub ? (
                        <a className="button" href={href} onClick={inApp}>
                          {t("open")}
                        </a>
                      ) : (
                        <a className="button" href={href} target="_blank" rel="noreferrer noopener">
                          {t("open")}
                        </a>
                      ))}
                    {onShelve && row.session_id && SHELVABLE.has(row.state) && (
                      <button
                        type="button"
                        className="button button-quiet"
                        aria-label={t("shelveFor", { session: row.session_id })}
                        onClick={() => onShelve(row)}
                      >
                        {t("shelveAction")}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** "Finish and shelve" (host, D22, #52): names the session and what happens, then shelves it
 * through `SessionManager.shelve(confirmed=True)`. Esc or Cancel leaves everything as it is. */
export function ShelveDialog({
  row,
  runs,
  now,
  onClose,
  onShelved,
}: {
  row: OverviewRow | null;
  runs: string;
  now: number;
  onClose: () => void;
  onShelved: () => void;
}) {
  const { t, dateTime } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (row && !dialog.current?.open) dialog.current?.showModal();
    else if (!row) dialog.current?.close();
  }, [row]);
  const close = () => {
    setError(null);
    onClose();
  };
  const kind = row ? kindOf(row) : "part";
  const days = row?.retention_days ?? null;
  const confirm = async () => {
    if (!row?.session_id) return;
    setBusy(true);
    setError(null);
    try {
      await shelveRun(runs, row.session_id);
      onShelved();
      close();
    } catch (e) {
      // the server's reason (e.g. why the snapshot failed), not only its status code
      setError(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-labelledby="shelve-title"
      onCancel={(e) => {
        e.preventDefault(); // Esc: close through React state, never while the snapshot runs
        if (!busy) close();
      }}
    >
      {row && (
        <>
          <h2 id="shelve-title">{t("shelveTitle")}</h2>
          <p style={{ marginTop: 8 }}>
            <strong>{planOf(row) ?? row.drawing ?? "–"}</strong>
            <span className="fine muted"> · {t("shelveSession", { session: row.session_id ?? "–" })}</span>
          </p>
          {!runEnded(row.phase) && (
            <p className="notice notice-check" style={{ marginTop: 10 }}>
              {t("shelveRunning", { phase: phaseText(kind, row.phase, t, { n: row.fix_iterations, max: row.max_fix_iterations }) })}
            </p>
          )}
          <ol className="shelve-steps">
            <li>
              <strong>{t("shelveSnapshotHead")}</strong> {t("shelveSnapshot")}
            </li>
            <li>
              <strong>{t("shelveStopHead")}</strong> {t("shelveStop")}
            </li>
            <li>
              <strong>{t("shelveRetentionHead")}</strong>{" "}
              {days === null
                ? t("shelveRetentionUnknown")
                : t("shelveRetention", {
                    days,
                    class: row.retention_class ?? "–",
                    date: dateTime(now + days * 86_400_000),
                  })}
            </li>
          </ol>
          {busy && (
            <p className="notice notice-busy" style={{ marginTop: 10 }} role="status">
              {t("shelving")}
            </p>
          )}
          {error && (
            <p className="notice notice-deviation" style={{ marginTop: 10 }}>
              {t("shelveFailed", { error })}
            </p>
          )}
          <div className="actions">
            <button type="button" className="button" disabled={busy} onClick={close}>
              {t("cancel")}
            </button>
            <button type="button" className="button button-danger" disabled={busy} onClick={confirm}>
              {t("shelveConfirm")}
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}

const KINDS: readonly (RunKind | "all")[] = ["all", "part", "circuit"];

/** Filters by run kind and phase (docs/13 overview). */
export function RunFilters({ filter, onFilter }: { filter: Filter; onFilter: (filter: Filter) => void }) {
  const { t } = useI18n();
  return (
    <div className="run-filters">
      <div className="segmented" role="group" aria-label={t("filterKind")}>
        {KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            aria-pressed={filter.kind === kind}
            // a phase the new kind never reaches would hide every row
            onClick={() => onFilter({ kind, phase: phasesFor(kind).includes(filter.phase as Phase) ? filter.phase : "all" })}
          >
            {kind === "all" ? t("filterAll") : t(`kindShort_${kind}`)}
          </button>
        ))}
      </div>
      <label className="field run-filter-phase">
        {t("filterPhase")}
        <select
          className="input"
          value={filter.phase}
          onChange={(e) => onFilter({ ...filter, phase: e.target.value as Phase | "all" })}
        >
          <option value="all">{t("allPhases")}</option>
          {phasesFor(filter.kind).map((phase) => (
            <option key={phase} value={phase}>
              {phaseText(filter.kind, phase, t)}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** The run table at `/`: the host's runs (`/sessions`) or, in the hub, the overview of all runs
 * (`/api/runs`, filtered on the server). Polls for live updates. */
export function RunTable({ runs }: { runs: string }) {
  const { t } = useI18n();
  const config = useConfig();
  const hub = config.mode === "hub";
  const [shelving, setShelving] = useState<OverviewRow | null>(null);
  const [refresh, setRefresh] = useState(0);
  // A run view's "finish and shelve" opens the table at `/#<session_id>` (#52).
  const [target] = useState(() => {
    try {
      return decodeURIComponent(window.location.hash.slice(1));
    } catch {
      return "";
    }
  });
  const [rows, setRows] = useState<OverviewRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [filter, setFilter] = useState<Filter>(NO_FILTER);
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT);
  const { kind, phase } = filter;
  useEffect(() => {
    // One refresh at a time (a hub refresh can be several pages), the next one POLL_MS after it
    // settles, none while the tab is hidden; a stale answer never overwrites a newer one.
    let active = true;
    let inFlight = false;
    let seq = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const server = { kind: kind === "all" ? undefined : kind, phase: phase === "all" ? undefined : phase };
    const schedule = () => {
      if (active && !inFlight && timer === undefined && !document.hidden) timer = setTimeout(load, POLL_MS);
    };
    async function load() {
      timer = undefined;
      if (!active || inFlight || document.hidden) return;
      inFlight = true;
      const mine = ++seq;
      try {
        const data = await listOverview(runs, hub, server);
        if (active && mine === seq) {
          setRows(data.rows);
          setTruncated(data.truncated);
          setLoaded(true);
          setError(null);
          setNow(Date.now());
        }
      } catch (e) {
        if (active && mine === seq) setError(e instanceof Error ? e.message : String(e));
      } finally {
        inFlight = false;
        schedule();
      }
    }
    const onVisibility = () => {
      if (document.hidden) return;
      clearTimeout(timer);
      timer = undefined;
      void load(); // back on the tab: refresh at once
    };
    document.addEventListener("visibilitychange", onVisibility);
    void load();
    return () => {
      active = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [runs, hub, kind, phase, refresh]);
  const found = loaded && target !== "" && rows.some((r) => r.session_id === target);
  useEffect(() => {
    if (found) document.getElementById(target)?.scrollIntoView?.({ block: "center" });
  }, [found, target]);
  // the host filters here; the hub already did, this only keeps a stale page consistent
  const shown = rows.filter((row) => matches(row, filter));
  const filtered = filter.kind !== "all" || filter.phase !== "all";
  return (
    <main className="page">
      <header className="panel-head" style={{ marginBottom: 14 }}>
        <h1 style={{ fontSize: "var(--fs-title)" }}>{hub ? t("hubTitle") : t("runsTitle")}</h1>
        <LangSwitch />
      </header>
      {error && <p className="notice notice-deviation">{t("loadFailed", { error })}</p>}
      <section className="panel">
        <div className="panel-head">
          <RunFilters filter={filter} onFilter={setFilter} />
          <p className="fine muted" aria-live="polite">
            {!loaded ? "" : truncated ? t("runsFirst", { n: shown.length }) : t(shown.length === 1 ? "runShown" : "runsShown", { n: shown.length })}
          </p>
        </div>
        {truncated && <p className="notice notice-check">{t("runsTruncated", { n: rows.length })}</p>}
        {shown.length === 0 && loaded ? (
          <p className="viewport-empty">{filtered ? t("runsNoMatch") : hub ? t("hubEmpty") : t("runsEmpty")}</p>
        ) : (
          <RunTableView
            rows={shown}
            now={now}
            hub={hub}
            sort={sort}
            onSort={setSort}
            onShelve={config.features.shelve && !hub ? setShelving : undefined}
            target={target}
          />
        )}
      </section>
      {config.features.shelve && !hub && (
        <ShelveDialog
          row={shelving}
          runs={runs}
          now={now}
          onClose={() => setShelving(null)}
          onShelved={() => setRefresh((n) => n + 1)}
        />
      )}
    </main>
  );
}
