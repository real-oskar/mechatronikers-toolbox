import type { AppEvent, ReviewItem } from "../api/types";

/** One line per event for the log; payloads are text, never HTML. */
export function describe(e: AppEvent): string {
  const p = e.payload;
  switch (e.type) {
    case "tool.called":
    case "tool.completed":
    case "tool.failed":
      return `${p.server ?? e.component} ${p.tool ?? ""}${p.error ? `: ${p.error}` : ""}`;
    case "model.request":
      return `${p.step ?? ""} ${p.model ?? ""}`;
    case "model.response":
      return `${p.step ?? ""} ${p.model ?? ""}, ${p.input_tokens ?? "?"} in / ${p.output_tokens ?? "?"} out, ${Number(p.cost ?? 0).toFixed(4)}`;
    case "validation.completed": {
      const checks = (p.report as { checks?: { id: string; passed: boolean }[] } | undefined)?.checks ?? [];
      const failing = checks.filter((c) => !c.passed).map((c) => c.id);
      return `${p.build_id}: ${failing.length ? `failing ${failing.join(", ")}` : `${checks.length} checks passed`}`;
    }
    case "review.item_created": {
      const item = p.item as ReviewItem | undefined;
      return item ? `${item.kind}: ${item.message}` : "";
    }
    case "agent.step_started":
    case "agent.step_completed":
      return String(p.step ?? "");
    default: {
      const text = JSON.stringify(p);
      return text === "{}" ? "" : text.length > 180 ? `${text.slice(0, 177)}…` : text;
    }
  }
}

/** Build id of the newest exported STL (artifact://<session>/<build>/<file>.stl). */
export function exportedBuild(events: AppEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]!;
    if (e.type === "artifact.created" && e.payload.format === "stl") {
      const uri = String(e.payload.uri ?? "");
      return uri.split("/")[3] ?? null;
    }
  }
  return null;
}
