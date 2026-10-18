import { useMemo, useRef, useState } from "react";

import { useRunApi } from "../api/client";
import type { BBox, GateState, ModelRef, Severity, SourceRef, StoredItem } from "../api/types";
import { MAX_NOTE } from "../api/types";
import { useI18n } from "../i18n";
import type { Key } from "../i18n";
import { buildCharacteristics, isDeviation, pendingChanges } from "../lib/inspection";
import type { Characteristic } from "../lib/inspection";
import { Balloon } from "./common/Balloon";
import { IsolationNotice } from "./common/IsolationNotice";
import { LazyModelViewer as ModelViewer } from "./LazyModelViewer";
import { SheetView } from "./SheetView";

type Filter = "all" | "open" | "deviations";
type Mark = { sheet: BBox } | { model: [number, number, number] };

/** "6,5" (German) and "6.5" are numbers; anything else stays text. */
export function parseValue(raw: string): number | string {
  const n = Number(raw.trim().replace(",", "."));
  return raw.trim() !== "" && Number.isFinite(n) ? n : raw.trim();
}

function RowActions({
  c,
  reviewer,
  revision,
  onChanged,
}: {
  c: Characteristic;
  reviewer: string;
  revision: number;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const api = useRunApi();
  const [mode, setMode] = useState<"edit" | "note" | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const sheetRef: SourceRef | null = c.sheetBox ? { sheet: 0, view_id: null, bbox: c.sheetBox } : null;

  const run = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      setMode(null);
      setText("");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const act = (action: "approved" | "rejected" | "edited" | "note") =>
    run(async () => {
      if (c.itemId) {
        await api.act(c.itemId, {
          action,
          reviewer,
          new_value: action === "edited" ? parseValue(text) : null,
          comment: action === "note" ? text : null,
        });
        return;
      }
      // A printed dimension without a finding: the reviewer's change becomes their own point.
      await api.addItem({
        message: action === "edited" ? `${c.label} → ${text}` : text,
        severity: action === "edited" ? "warning" : "info",
        reviewer,
        revision,
        sheet_ref: sheetRef,
        dimension_ids: c.dimensionIds,
        feature_ids: c.featureIds,
        proposed_value: action === "edited" ? parseValue(text) : null,
      });
    });

  const decided = c.status === "approved" || c.status === "rejected";
  return (
    <div>
      <div className="row-actions">
        {c.itemId && !decided && (
          <button type="button" className="button button-quiet" disabled={!reviewer} onClick={() => act("approved")}>
            {t("actConfirm")}
          </button>
        )}
        <button type="button" className="button button-quiet" disabled={!reviewer} onClick={() => setMode(mode === "edit" ? null : "edit")}>
          {t("actEdit")}
        </button>
        {c.itemId && !decided && (
          <button type="button" className="button button-quiet" disabled={!reviewer} onClick={() => act("rejected")}>
            {t("actReject")}
          </button>
        )}
        <button type="button" className="button button-quiet" disabled={!reviewer} onClick={() => setMode(mode === "note" ? null : "note")}>
          {t("actNote")}
        </button>
      </div>
      {mode && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) act(mode === "edit" ? "edited" : "note");
          }}
        >
          <label className="field" style={{ flex: 1 }}>
            {mode === "edit" ? t("newValue") : t("noteText")}
            <input className="input" autoFocus value={text} maxLength={mode === "edit" ? 80 : MAX_NOTE} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" className="button" disabled={!text.trim()}>
            {t("save")}
          </button>
        </form>
      )}
      {error && <p className="fine status-nok">{error}</p>}
    </div>
  );
}

function NewPoint({
  mark,
  characteristics,
  reviewer,
  revision,
  onDone,
}: {
  mark: Mark;
  characteristics: Characteristic[];
  reviewer: string;
  revision: number;
  onDone: (created: boolean) => void;
}) {
  const { t } = useI18n();
  const api = useRunApi();
  const [message, setMessage] = useState("");
  const [severity, setSeverity] = useState<Severity>("warning");
  const [value, setValue] = useState("");
  const [dimension, setDimension] = useState("");
  const [error, setError] = useState<string | null>(null);
  const dims = characteristics.filter((c) => c.kind === "dimension");

  const save = async () => {
    setError(null);
    try {
      const modelRef: ModelRef | null = "model" in mark ? { face_ids: [], point: mark.model } : null;
      await api.addItem({
        message: message.trim(),
        severity,
        reviewer,
        revision,
        sheet_ref: "sheet" in mark ? { sheet: 0, view_id: null, bbox: mark.sheet } : null,
        model_ref: modelRef,
        dimension_ids: dimension ? [dimension] : [],
        proposed_value: value.trim() ? parseValue(value) : null,
      });
      onDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <form
      className="panel"
      aria-label={t("addPoint")}
      onSubmit={(e) => {
        e.preventDefault();
        if (message.trim()) save();
      }}
    >
      <div className="signoff-grid" style={{ marginTop: 0 }}>
        <label className="field">
          {t("poiMessage")}
          <textarea className="input" rows={3} autoFocus maxLength={MAX_NOTE} value={message} onChange={(e) => setMessage(e.target.value)} />
        </label>
        <div style={{ display: "grid", gap: 8 }}>
          <label className="field">
            {t("severity")}
            <select className="input" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
              {(["info", "warning", "blocking"] as const).map((s) => (
                <option key={s} value={s}>
                  {t(`sev_${s}` as Key)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            {t("proposedValue")}
            <input className="input" maxLength={80} value={value} onChange={(e) => setValue(e.target.value)} />
          </label>
          <label className="field">
            {t("linkedDimension")}
            <select className="input" value={dimension} onChange={(e) => setDimension(e.target.value)}>
              <option value="">–</option>
              {dims.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.balloon}: {d.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <div className="actions">
        <button type="button" className="button" onClick={() => onDone(false)}>
          {t("cancel")}
        </button>
        <button type="submit" className="button button-primary" disabled={!message.trim() || !reviewer}>
          {t("addPoint")}
        </button>
      </div>
      {error && <p className="notice notice-deviation">{error}</p>}
    </form>
  );
}

/** First article inspection of a valid model (D58): ballooned drawing, model, characteristics. */
export function InspectionView({
  gate,
  items,
  reviewer,
  onChanged,
  untrusted = false,
}: {
  gate: GateState;
  items: StoredItem[];
  reviewer: string;
  onChanged: () => void;
  untrusted?: boolean;
}) {
  const { t, num } = useI18n();
  const api = useRunApi();
  const r = gate.request;
  const characteristics = useMemo(() => buildCharacteristics(r, items), [r, items]);
  const [selected, setSelected] = useState<string>();
  const [filter, setFilter] = useState<Filter>("all");
  const [marking, setMarking] = useState<"sheet" | "model" | null>(null);
  const [mark, setMark] = useState<Mark | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const finishDialog = useRef<HTMLDialogElement>(null);
  const pending = pendingChanges(items, r.revision);
  const changes = pending.items.length + pending.actions.length;
  // Only changes that need a rebuild block "Finish inspection": the reviewer's own points and
  // changed values. Confirm, reject and note are recorded at once and stay with the finish (#152).
  const unbuilt = pending.items.length + pending.actions.filter((s) => s.item.status === "edited").length;
  // Points that stay open when the inspection is finished: they remain review items (D70, #152).
  const open = useMemo(() => {
    const openIds = new Set(
      items
        .filter((s) => s.revision === r.revision && s.item.status === "open" && s.item.kind !== "validation_failure")
        .map((s) => s.item.id),
    );
    const severity = new Map(items.map((s) => [s.item.id, s.item.severity]));
    return characteristics
      .filter((c) => c.itemId !== null && openIds.has(c.itemId))
      .map((c) => ({ c, blocking: severity.get(c.itemId!) === "blocking" }));
  }, [characteristics, items, r.revision]);

  const shown = characteristics.filter((c) =>
    filter === "all"
      ? true
      : filter === "deviations"
        ? c.status === "nok"
        : isDeviation(c) || c.kind === "reviewer",
  );
  const counts = {
    ok: characteristics.filter((c) => c.status === "ok" || c.status === "approved" || c.status === "edited").length,
    check: characteristics.filter((c) => c.status === "check").length,
    nok: characteristics.filter((c) => c.status === "nok").length,
  };

  const decide = async (action: "update" | "finish") => {
    setError(null);
    try {
      await api.decide({
        gate_id: r.id,
        action,
        reviewer,
        decided_at: new Date().toISOString(),
        update:
          action === "update"
            ? {
                revision: r.revision,
                items: pending.items,
                actions: pending.actions.map((s) => ({ item_id: s.item.id, action: s.item.history[s.item.history.length - 1]! })),
                note: note.trim() || null,
              }
            : null,
      });
      dialog.current?.close();
      finishDialog.current?.close();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const select = (key: string) => {
    setSelected(key);
    document.getElementById(`char-${key}`)?.scrollIntoView({ block: "nearest" });
  };

  return (
    <>
      {(untrusted || (gate.unisolated_builds ?? 0) > 0) && <IsolationNotice />}
      <div className="inspection-layout">
        <SheetView
          url={api.sheetUrl()}
          size={r.sheet_size ?? [420, 297]}
          characteristics={characteristics}
          selected={selected}
          onSelect={select}
          marking={marking === "sheet"}
          onMark={(box) => {
            setMark({ sheet: box });
            setMarking(null);
          }}
        />
        <div className="viewport" style={{ height: "min(62vh, 660px)" }}>
          <div className="viewport-bar">
            <span className="view-name">{t("viewModel")}</span>
          </div>
          {r.build_id ? (
            <ModelViewer
              url={api.modelUrl(r.build_id)}
              markers={characteristics}
              selected={selected}
              onSelect={select}
              onPick={
                marking === "model"
                  ? (point) => {
                      setMark({ model: point });
                      setMarking(null);
                    }
                  : undefined
              }
              empty={t("noModel")}
            />
          ) : (
            <div className="viewport-empty">{t("noModel")}</div>
          )}
        </div>
      </div>

      <div className="inspection-bar">
        <div style={{ display: "flex", gap: 18, alignItems: "baseline", flexWrap: "wrap" }}>
          <h2>{t("characteristics")}</h2>
          <span className="status-ok">
            {counts.ok} {t("st_ok")}
          </span>
          <span className="status-check">
            {counts.check} {t("st_check")}
          </span>
          <span className="status-nok">
            {counts.nok} {t("st_nok")}
          </span>
          <div className="segmented" role="group">
            {(["all", "open", "deviations"] as const).map((f) => (
              <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {t(f === "all" ? "filterAll" : f === "open" ? "filterOpen" : "filterDeviations")}
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="button" aria-pressed={marking === "sheet"} disabled={!reviewer} onClick={() => setMarking(marking === "sheet" ? null : "sheet")}>
            {t("markDrawing")}
          </button>
          <button type="button" className="button" aria-pressed={marking === "model"} disabled={!reviewer || !r.build_id} onClick={() => setMarking(marking === "model" ? null : "model")}>
            {t("markModel")}
          </button>
          {gate.open && (
            <>
              <button
                type="button"
                className="button"
                disabled={!reviewer || unbuilt > 0}
                title={unbuilt > 0 ? t("inspectFinishPending") : undefined}
                onClick={() => (open.length === 0 ? decide("finish") : finishDialog.current?.showModal())}
              >
                {t("finishReview")}
              </button>
              <button type="button" className="button button-primary" disabled={!reviewer || changes === 0} onClick={() => dialog.current?.showModal()}>
                {t("updateRun", { n: changes })}
              </button>
            </>
          )}
        </div>
      </div>
      {marking && (
        <p className="notice notice-busy" style={{ marginTop: 10 }}>
          {marking === "sheet" ? t("markDrawingHint") : t("markModelHint")}
        </p>
      )}
      {!reviewer && <p className="notice notice-check" style={{ marginTop: 10 }}>{t("nameFirst")}</p>}
      {gate.open && reviewer && unbuilt > 0 && <p className="fine muted" style={{ marginTop: 6 }}>{t("inspectFinishPending")}</p>}
      {error && <p className="notice notice-deviation" style={{ marginTop: 10 }}>{error}</p>}
      {mark && (
        <div style={{ marginTop: 12 }}>
          <NewPoint
            mark={mark}
            characteristics={characteristics}
            reviewer={reviewer}
            revision={r.revision}
            onDone={(created) => {
              setMark(null);
              if (created) onChanged();
            }}
          />
        </div>
      )}

      <div className="panel char-scroll" style={{ marginTop: 12, padding: 0 }}>
        <table className="char-table">
          <thead>
            <tr>
              <th>{t("colNo")}</th>
              <th>{t("colCharacteristic")}</th>
              <th className="num">{t("colNominal")}</th>
              <th className="num">{t("colMeasured")}</th>
              <th>{t("colStatus")}</th>
              <th>{t("colSource")}</th>
              <th>{t("colAction")}</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => (
              <tr key={c.key} id={`char-${c.key}`} aria-selected={selected === c.key}>
                <td>
                  <Balloon n={c.balloon} status={c.status} reviewer={c.kind === "reviewer"} pressed={selected === c.key} onClick={() => setSelected(c.key)} />
                </td>
                <td>
                  <span className={c.kind === "dimension" ? "char-label" : undefined}>{c.label}</span>
                  {c.detail && c.kind !== "dimension" && <div className="fine muted">{c.detail}</div>}
                  {c.detail && c.kind === "dimension" && c.status !== "ok" && <div className="fine muted">{c.detail}</div>}
                  {c.proposedValue !== null && (
                    <div className="fine status-edited">
                      → {typeof c.proposedValue === "number" ? num(c.proposedValue) : c.proposedValue}
                    </div>
                  )}
                </td>
                <td className="num">{c.nominal === null ? "–" : num(c.nominal)}</td>
                <td className="num">{c.measured === null ? "–" : num(c.measured, 2)}</td>
                <td className={`status-${c.status}`}>{t(`st_${c.status}` as Key)}</td>
                <td className="fine muted">{t(`src_${c.source}` as Key)}</td>
                <td>
                  <RowActions c={c} reviewer={reviewer} revision={r.revision} onChanged={onChanged} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <dialog ref={finishDialog} className="dialog" aria-label={t("inspectFinishTitle")}>
        <h2>{t("inspectFinishTitle")}</h2>
        <p style={{ marginTop: 8 }}>{open.length === 1 ? t("inspectFinishBodyOne") : t("inspectFinishBody", { n: open.length })}</p>
        <ul className="plain-list" style={{ marginTop: 10 }}>
          {open.map(({ c, blocking }) => (
            <li key={c.key}>
              <Balloon n={c.balloon} small status={c.status} reviewer={c.kind === "reviewer"} /> {c.label}
              <span className={`fine ${blocking ? "status-nok" : "muted"}`}>
                {" "}
                ({t(`st_${c.status}` as Key)}
                {blocking && `, ${t("sev_blocking")}`})
              </span>
            </li>
          ))}
        </ul>
        <div className="actions">
          <button type="button" className="button" onClick={() => finishDialog.current?.close()}>
            {t("cancel")}
          </button>
          <button type="button" className="button button-primary" onClick={() => decide("finish")}>
            {t("finishReview")}
          </button>
        </div>
      </dialog>

      <dialog ref={dialog} className="dialog" aria-label={t("updateTitle")}>
        <h2>{t("updateTitle")}</h2>
        <p style={{ marginTop: 8 }}>{t("updateBody")}</p>
        <ul className="plain-list" style={{ marginTop: 10 }}>
          {[...pending.items, ...pending.actions].map((s) => (
            <li key={s.item.id}>
              {s.item.message}
              {s.origin === "agent" && <span className="fine muted"> ({t(`st_${s.item.status}` as Key)})</span>}
            </li>
          ))}
        </ul>
        <label className="field" style={{ marginTop: 12 }}>
          {t("updateNote")}
          <textarea className="input" rows={2} maxLength={MAX_NOTE} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <div className="actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            {t("cancel")}
          </button>
          <button type="button" className="button button-primary" onClick={() => decide("update")}>
            {t("startUpdate")}
          </button>
        </div>
      </dialog>
    </>
  );
}
