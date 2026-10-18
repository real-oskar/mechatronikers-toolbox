import type { ReviewItem } from "../../api/types";
import { useRunApi } from "../../api/client";
import { useI18n } from "../../i18n";
import type { CircuitView, Selection } from "../../lib/circuit";
import { netLabel, selectionBox } from "../../lib/circuit";
import { SheetView } from "../SheetView";
import { kindName } from "./names";

const PAD = 0.8; // mm around a symbol body, so thin symbols are easy to hit

/** The schematic sheet with the circuit as read on top: part outlines (coloured by status,
 * dashed when corrected), the selected part's terminals, the selected net's wires and terminals,
 * the selected finding's box. Coordinates are sheet millimetres. */
export function CircuitSheet({
  sid,
  view,
  items,
  selection,
  corrected,
  onSelect,
  focus,
}: {
  sid: string;
  view: CircuitView;
  items: ReviewItem[];
  selection: Selection | null;
  corrected: Set<string>; // refdes with a pending correction (also of a terminal)
  onSelect: (sel: Selection) => void;
  focus: { box: [number, number, number, number]; zoom?: number } | null;
}) {
  const { t } = useI18n();
  const api = useRunApi();
  const part = selection?.type === "part" ? view.components.find((c) => c.refdes === selection.id) : undefined;
  const net = selection?.type === "net" ? view.nets.find((n) => n.id === selection.id) : undefined;
  const itemBox = selection?.type === "item" ? selectionBox(selection, view, items) : null;

  const overlay = (mmPerPx: number) => (
    <>
      {view.components.map((c) => {
        const [x0, y0, x1, y1] = c.bbox;
        const className = [
          "ov-part",
          `ov-part-${c.status}`,
          corrected.has(c.refdes) ? "ov-part-corrected" : "",
          part?.refdes === c.refdes ? "ov-part-selected" : "",
        ].join(" ");
        return (
          <rect
            key={c.refdes}
            className={className}
            x={x0 - PAD}
            y={y0 - PAD}
            width={x1 - x0 + 2 * PAD}
            height={y1 - y0 + 2 * PAD}
            rx={0.6}
            role="button"
            aria-label={`${c.refdes} · ${kindName(c.kind, t)} · ${c.value ?? t("noValue")}`}
            onClick={(e) => {
              e.stopPropagation();
              onSelect({ type: "part", id: c.refdes });
            }}
          />
        );
      })}
      {net?.segments.map(([[ax, ay], [bx, by]], i) => <line key={i} className="ov-net" x1={ax} y1={ay} x2={bx} y2={by} />)}
      {view.components.flatMap((c) =>
        c.terminals
          .filter((term) => part?.refdes === c.refdes || (net && term.net_id === net.id))
          .map((term) => (
            <circle
              key={term.id}
              className="ov-terminal"
              cx={term.at[0]}
              cy={term.at[1]}
              r={3.5 * mmPerPx}
              role="button"
              aria-label={`${term.id} → ${netLabel(view.nets.find((n) => n.id === term.net_id) ?? { id: "–", name: null })}`}
              onClick={(e) => {
                e.stopPropagation();
                if (term.net_id) onSelect({ type: "net", id: term.net_id });
              }}
            />
          )),
      )}
      {itemBox && (
        <rect
          className="ov-item"
          x={itemBox[0] - 1.5}
          y={itemBox[1] - 1.5}
          width={itemBox[2] - itemBox[0] + 3}
          height={itemBox[3] - itemBox[1] + 3}
        />
      )}
    </>
  );

  let hint = t("sheetHint");
  if (part) {
    const pins = part.terminals.map((term) => `${term.role ?? term.pin ?? term.id.split(".").pop()}→${netLabel(view.nets.find((n) => n.id === term.net_id) ?? { id: "–", name: null })}`);
    hint = `${part.refdes} · ${kindName(part.kind, t)} · ${part.value ?? t("noValue")} · ${pins.join(", ")}`;
  } else if (net) {
    hint = `${netLabel(net)}: ${net.terminals.join(", ")}`;
  }

  return (
    <div>
      <SheetView
        url={api.circuitSheetUrl(sid)}
        size={view.sheetSize}
        characteristics={[]}
        label={t("viewSchematic")}
        overlay={overlay}
        focus={focus}
      />
      <p className="fine muted" style={{ marginTop: 6 }} aria-live="polite">
        {hint}
      </p>
    </div>
  );
}
