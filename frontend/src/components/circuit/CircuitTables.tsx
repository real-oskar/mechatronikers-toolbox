import { Fragment, useState } from "react";

import type { CircuitComponent, CircuitTerminal, Correction, CorrectionTarget } from "../../api/types";
import { useI18n } from "../../i18n";
import type { Key } from "../../i18n";
import type { CircuitView, CorrectionProblem, Corrections, Selection } from "../../lib/circuit";
import { COMPONENT_KINDS, correctionKey, MAX_CORRECTIONS, MAX_NET_NAME, MAX_VALUE, netLabel, PIN_ROLES } from "../../lib/circuit";
import { kindName } from "./names";

export interface Editing {
  corrections: Corrections;
  onCorrect: (c: Correction) => CorrectionProblem | null;
  version: number; // bumped when corrections are removed elsewhere: text fields start over
}

const PROBLEMS: Record<CorrectionProblem, Key> = { tooLong: "corrTooLong", notAllowed: "corrNotAllowed", tooMany: "corrTooMany" };

/** Why a field's text is not recorded, next to the field. */
function Problem({ problem, limit }: { problem: CorrectionProblem | null; limit: number }) {
  const { t } = useI18n();
  if (!problem) return null;
  return (
    <span className="fine status-nok" role="alert" style={{ display: "block" }}>
      {t(PROBLEMS[problem], { n: problem === "tooMany" ? MAX_CORRECTIONS : limit })}
    </span>
  );
}

export const partRow = (refdes: string) => `cpart-${refdes}`;
export const netRow = (id: string) => `cnet-${id}`;

/** A text correction: keeps what the reviewer types (also an empty field) and records it as a
 * correction of `old`; an empty or unchanged text records none. */
function TextCorrection({
  target,
  id,
  field,
  old,
  editing,
  label,
  maxLength,
  placeholder,
}: {
  target: CorrectionTarget;
  id: string;
  field: string;
  old: string | null;
  editing: Editing;
  label: string;
  maxLength: number;
  placeholder?: string;
}) {
  const pending = editing.corrections.get(correctionKey(target, id, field));
  const [text, setText] = useState(pending?.new ?? old ?? "");
  const [problem, setProblem] = useState<CorrectionProblem | null>(null);
  return (
    <>
      <input
        className="input"
        aria-label={label}
        aria-invalid={problem !== null}
        value={text}
        maxLength={maxLength}
        placeholder={placeholder}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => {
          setText(e.target.value);
          setProblem(editing.onCorrect({ target, id, field, old, new: e.target.value }));
        }}
      />
      <Problem problem={problem} limit={maxLength} />
    </>
  );
}

/** A choice correction (kind, pin role): the value as read is the first option. */
function ChoiceCorrection({
  target,
  id,
  field,
  old,
  editing,
  label,
  options,
}: {
  target: CorrectionTarget;
  id: string;
  field: string;
  old: string | null;
  editing: Editing;
  label: string;
  options: { value: string; text: string }[];
}) {
  const pending = editing.corrections.get(correctionKey(target, id, field));
  const [problem, setProblem] = useState<CorrectionProblem | null>(null);
  return (
    <>
      <select
        className="input"
        aria-label={label}
        aria-invalid={problem !== null}
        value={pending?.new ?? old ?? ""}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setProblem(editing.onCorrect({ target, id, field, old, new: e.target.value }))}
      >
        {old === null && <option value="">–</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.text}
          </option>
        ))}
      </select>
      <Problem problem={problem} limit={0} />
    </>
  );
}

function TerminalRows({
  part,
  view,
  editing,
  onSelect,
}: {
  part: CircuitComponent;
  view: CircuitView;
  editing: Editing | null;
  onSelect: (sel: Selection) => void;
}) {
  const { t } = useI18n();
  const roles = PIN_ROLES.map((r) => ({ value: r, text: r }));
  const pinOf = (term: CircuitTerminal) => term.pin ?? null;
  return (
    <table className="terminal-table" aria-label={t("terminalsOf", { refdes: part.refdes })}>
      <thead>
        <tr>
          <th>{t("colTerminal")}</th>
          <th>{t("colPinName")}</th>
          <th>{t("colRole")}</th>
          <th>{t("colPin")}</th>
          <th>{t("colNet")}</th>
        </tr>
      </thead>
      <tbody>
        {part.terminals.map((term) => {
          const net = view.nets.find((n) => n.id === term.net_id);
          return (
            <tr key={term.id}>
              <td>{term.id}</td>
              <td className="muted">{term.name ?? "–"}</td>
              <td>
                {editing ? (
                  <ChoiceCorrection
                    target="terminal"
                    id={term.id}
                    field="role"
                    old={term.role}
                    editing={editing}
                    label={`${t("field_role")} ${term.id}`}
                    options={roles}
                  />
                ) : (
                  (term.role ?? "–")
                )}
              </td>
              <td>
                {editing ? (
                  <TextCorrection
                    key={`${term.id}-${editing.version}`}
                    target="terminal"
                    id={term.id}
                    field="pin"
                    old={pinOf(term)}
                    editing={editing}
                    label={`${t("field_pin")} ${term.id}`}
                    maxLength={MAX_VALUE}
                  />
                ) : (
                  (term.pin ?? "–")
                )}
              </td>
              <td>
                {net ? (
                  <button type="button" className="button button-quiet" onClick={() => onSelect({ type: "net", id: net.id })}>
                    {netLabel(net)}
                  </button>
                ) : (
                  "–"
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** The circuit as a parts table and a nets table. A row and its outline on the sheet select
 * each other; while gate 1 waits, values, kinds, pin roles and numbers and net names can be
 * corrected (recorded with the decision, applied by the run driver: D75). */
export function CircuitTables({
  view,
  selection,
  onSelect,
  editing,
}: {
  view: CircuitView;
  selection: Selection | null;
  onSelect: (sel: Selection) => void;
  editing: Editing | null;
}) {
  const { t } = useI18n();
  const kinds = COMPONENT_KINDS.map((k) => ({ value: k, text: kindName(k, t) }));
  const netName = (id: string | null) => {
    const net = view.nets.find((n) => n.id === id);
    return net ? netLabel(net) : "–";
  };
  const corrected = (refdes: string, terminals: string[]) =>
    [...(editing?.corrections.values() ?? [])].some((c) => c.id === refdes || terminals.includes(c.id));

  return (
    <div className="circuit-tables">
      {editing === null && <p className="fine muted">{t("editHint")}</p>}
      <section className="panel" style={{ padding: 0 }} aria-label={t("partsTitle", { n: view.components.length })}>
        <h2 style={{ padding: "10px 12px 0" }}>{t("partsTitle", { n: view.components.length })}</h2>
        <table className="char-table">
          <thead>
            <tr>
              <th>{t("colRef")}</th>
              <th>{t("colKind")}</th>
              <th>{t("colValue")}</th>
              <th>{t("colPins")}</th>
              <th>{t("colStatus")}</th>
            </tr>
          </thead>
          <tbody>
            {view.components.map((c) => {
              const selected = selection?.type === "part" && selection.id === c.refdes;
              const isCorrected = corrected(c.refdes, c.terminals.map((term) => term.id));
              return (
                <Fragment key={c.refdes}>
                  <tr id={partRow(c.refdes)} aria-selected={selected} data-clickable onClick={() => onSelect({ type: "part", id: c.refdes })}>
                    <td>
                      <button type="button" className="button button-quiet" style={{ fontWeight: 600 }} aria-pressed={selected}>
                        {c.refdes}
                      </button>
                    </td>
                    <td>
                      {editing ? (
                        <ChoiceCorrection
                          target="component"
                          id={c.refdes}
                          field="kind"
                          old={c.kind}
                          editing={editing}
                          label={`${t("field_kind")} ${c.refdes}`}
                          options={kinds}
                        />
                      ) : (
                        <>
                          {kindName(c.kind, t)}
                          {c.polarity && <span className="muted"> {c.polarity}</span>}
                        </>
                      )}
                    </td>
                    <td>
                      {editing ? (
                        <TextCorrection
                          key={`${c.refdes}-${editing.version}`}
                          target="component"
                          id={c.refdes}
                          field="value"
                          old={c.value}
                          editing={editing}
                          label={`${t("field_value")} ${c.refdes}`}
                          maxLength={MAX_VALUE}
                          placeholder={t("noValue")}
                        />
                      ) : (
                        (c.value ?? <span className="muted">{t("noValue")}</span>)
                      )}
                    </td>
                    <td className="fine">
                      {c.terminals.map((term) => `${term.role ?? term.pin ?? term.id.split(".").pop()}→${netName(term.net_id)}`).join(", ")}
                    </td>
                    <td className={isCorrected ? "status-edited" : c.status === "needs_review" ? "status-check" : `status-${c.status === "auto_ok" ? "ok" : c.status}`}>
                      {isCorrected ? t("corrected") : c.status === "needs_review" ? t("st_check") : c.status === "edited" ? t("st_edited") : t("st_ok")}
                    </td>
                  </tr>
                  {selected && c.terminals.length > 0 && (
                    <tr className="terminal-row">
                      <td colSpan={5}>
                        <TerminalRows part={c} view={view} editing={editing} onSelect={onSelect} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </section>
      <section className="panel" style={{ padding: 0 }} aria-label={t("netsTitle", { n: view.nets.length })}>
        <h2 style={{ padding: "10px 12px 0" }}>{t("netsTitle", { n: view.nets.length })}</h2>
        <table className="char-table">
          <thead>
            <tr>
              <th>{t("colNet")}</th>
              <th>{t("colConnections")}</th>
              <th className="num">{t("colWires")}</th>
            </tr>
          </thead>
          <tbody>
            {view.nets.map((n) => {
              const selected = selection?.type === "net" && selection.id === n.id;
              return (
                <tr key={n.id} id={netRow(n.id)} aria-selected={selected} data-clickable onClick={() => onSelect({ type: "net", id: n.id })}>
                  <td>
                    {editing ? (
                      <TextCorrection
                        key={`${n.id}-${editing.version}`}
                        target="net"
                        id={n.id}
                        field="name"
                        old={n.name}
                        editing={editing}
                        label={`${t("field_name")} ${n.id}`}
                        maxLength={MAX_NET_NAME}
                        placeholder={`${n.id} (${t("unnamed")})`}
                      />
                    ) : (
                      <button type="button" className="button button-quiet" aria-pressed={selected}>
                        {n.name ?? <span className="muted">{`${n.id} (${t("unnamed")})`}</span>}
                      </button>
                    )}
                  </td>
                  <td className="fine">{n.terminals.join(", ")}</td>
                  <td className="num">{n.segments.length || "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </div>
  );
}
