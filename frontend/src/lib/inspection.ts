import type {
  BBox,
  Dimension,
  GateRequest,
  ReviewAction,
  ReviewItem,
  StoredItem,
} from "../api/types";

/** What an engineer sees in the inspection report for one numbered balloon. */
export type InspectionStatus = "ok" | "check" | "nok" | "approved" | "edited" | "rejected";
export type Source = "reading" | "check" | "plan" | "build" | "reviewer";

export interface Characteristic {
  key: string;
  balloon: number;
  kind: "dimension" | "finding" | "reviewer";
  label: string; // as printed, e.g. "Ø6,6", or the finding in words
  detail: string | null; // reason or message
  nominal: number | null;
  measured: number | null;
  status: InspectionStatus;
  source: Source;
  sheetBox: BBox | null;
  modelPoint: [number, number, number] | null;
  dimensionIds: string[];
  featureIds: string[];
  itemId: string | null; // review app item the reviewer's actions attach to
  proposedValue: number | string | null;
  history: ReviewAction[];
}

const ROW_MM = 25; // balloons are numbered row by row across the sheet, like a ballooned print

const fromSeverity = (item: ReviewItem): InspectionStatus =>
  item.severity === "blocking" ? "nok" : item.severity === "warning" ? "check" : "ok";

const reviewed = (status: string): InspectionStatus | null =>
  status === "approved" || status === "edited" || status === "rejected" ? status : null;

function byPosition(a: BBox | null, b: BBox | null): number {
  if (!a || !b) return a ? -1 : b ? 1 : 0;
  const rowA = Math.floor(a[1] / ROW_MM);
  const rowB = Math.floor(b[1] / ROW_MM);
  return rowA - rowB || a[0] - b[0];
}

function dimensionStatus(d: Dimension): InspectionStatus {
  if (d.status === "auto_ok") return "ok";
  if (d.status === "rejected") return "nok";
  return "check";
}

/** The inspection list of one revision: printed dimensions first (numbered across the sheet),
 * then the agent's findings, then the reviewer's own points in the order they were added. */
export function buildCharacteristics(gate: GateRequest | undefined, stored: StoredItem[]): Characteristic[] {
  if (!gate) return [];
  const revision = gate.revision;
  const storedById = new Map(stored.map((s) => [s.item.id, s]));
  const agentKey = (item: ReviewItem) => `rev${revision}-${item.id}`;
  const linked = new Map<string, ReviewItem>(); // dimension id → finding about it
  for (const item of gate.review_items) for (const id of item.dimension_ids) linked.set(id, item);

  const dims = [...(gate.dimensions ?? [])]
    .filter((d) => d.kind !== "other")
    .sort((a, b) => byPosition(a.source?.bbox ?? null, b.source?.bbox ?? null));
  const usedFindings = new Set<string>();
  const rows: Omit<Characteristic, "balloon">[] = dims.map((d) => {
    const finding = linked.get(d.id);
    if (finding) usedFindings.add(finding.id);
    const store = finding ? storedById.get(agentKey(finding)) : undefined;
    return {
      key: d.id,
      kind: "dimension",
      label: d.stated_text ?? String(d.value),
      detail: finding?.message ?? d.reason,
      nominal: d.stated,
      measured: d.measured,
      status: reviewed(store?.item.status ?? "") ?? (finding ? fromSeverity(finding) : dimensionStatus(d)),
      source: "reading",
      sheetBox: d.source?.bbox ?? null,
      modelPoint: null,
      dimensionIds: [d.id],
      featureIds: d.feature_ids,
      itemId: store ? store.item.id : null,
      proposedValue: store?.proposed_value ?? null,
      history: store?.item.history ?? [],
    };
  });

  const findings = [...gate.review_items.filter((i) => !usedFindings.has(i.id)), ...gate.possibly_missing];
  for (const item of findings.sort((a, b) => byPosition(a.sheet_ref?.bbox ?? null, b.sheet_ref?.bbox ?? null))) {
    const store = storedById.get(agentKey(item));
    rows.push({
      key: agentKey(item),
      kind: "finding",
      label: item.message,
      detail: item.reason,
      nominal: null,
      measured: null,
      status: reviewed(store?.item.status ?? "") ?? fromSeverity(item),
      source: item.kind === "possibly_missing" ? "plan" : "check",
      sheetBox: item.sheet_ref?.bbox ?? null,
      modelPoint: item.model_ref?.point ?? null,
      dimensionIds: item.dimension_ids,
      featureIds: item.feature_ids,
      itemId: store ? store.item.id : null,
      proposedValue: store?.proposed_value ?? null,
      history: store?.item.history ?? [],
    });
  }

  const own = stored
    .filter((s) => s.origin === "reviewer" && s.revision === revision)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const s of own) {
    rows.push({
      key: s.item.id,
      kind: "reviewer",
      label: s.item.message,
      detail: s.created_by,
      nominal: null,
      measured: null,
      status: reviewed(s.item.status) ?? fromSeverity(s.item),
      source: "reviewer",
      sheetBox: s.item.sheet_ref?.bbox ?? null,
      modelPoint: s.item.model_ref?.point ?? null,
      dimensionIds: s.item.dimension_ids,
      featureIds: s.item.feature_ids,
      itemId: s.item.id,
      proposedValue: s.proposed_value,
      history: s.item.history,
    });
  }
  return rows.map((row, i) => ({ ...row, balloon: i + 1 }));
}

export const isDeviation = (c: Characteristic) => c.status === "nok" || c.status === "check";

/** Changes the reviewer made in this revision: new points and decisions on the agent's. */
export function pendingChanges(stored: StoredItem[], revision: number): { items: StoredItem[]; actions: StoredItem[] } {
  const mine = stored.filter((s) => s.revision === revision);
  return {
    items: mine.filter((s) => s.origin === "reviewer" && s.item.status !== "rejected"),
    actions: mine.filter((s) => s.origin === "agent" && s.item.history.length > 0),
  };
}
