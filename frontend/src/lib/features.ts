import type { PlannedFeature } from "../api/types";
import type { I18n } from "../i18n";

/** One sentence per construction step, in the reviewer's language (like the review package). */
export function describeFeature(f: PlannedFeature, { t, num }: I18n): string {
  const mm = (v: number | null | undefined) => (v === null || v === undefined ? "?" : num(v));
  switch (f.type) {
    case "extrude_base": {
      const segments = f.profile?.segments ?? [];
      const arcs = segments.filter((s) => s.kind === "arc").length;
      const shape = segments.some((s) => s.kind === "circle")
        ? t("feat_shape_circle")
        : arcs === 1
          ? t("feat_shape_radius")
          : arcs
            ? t("feat_shape_radii", { n: arcs })
          : t("feat_shape_outline");
      return t("feat_extrude", { shape, d: mm(f.depth) });
    }
    case "revolve_base":
      return t("feat_revolve", { n: f.profile?.segments.length ?? 0 });
    case "hole": {
      let text = t("feat_hole", { d: mm(f.diameter) });
      text += f.depth ? t("feat_depth", { d: mm(f.depth) }) : t("feat_through");
      if (f.thread) text += t("feat_thread", { m: f.thread });
      if (f.hole_type === "counterbore") text += t("feat_counterbore", { d: mm(f.cb_diameter), h: mm(f.cb_depth) });
      if (f.hole_type === "countersink") text += t("feat_countersink", { d: mm(f.cs_diameter) });
      return text;
    }
    case "hole_pattern": {
      const p = f.pattern;
      const n = p?.count ?? (p?.count_1 ?? 1) * (p?.count_2 ?? 1);
      const seed = f.seed ? describeFeature(f.seed, { t, num } as I18n) : "";
      return t(p?.kind === "circular" ? "feat_circle_pattern" : "feat_grid_pattern", { n, hole: seed });
    }
    case "pocket":
      return f.depth ? t("feat_pocket", { d: mm(f.depth) }) : t("feat_cutout");
    case "boss":
      return t("feat_boss", { h: mm(f.height) });
    case "slot":
      return t("feat_slot", { l: mm(f.length), w: mm(f.width) }) + (f.depth ? t("feat_depth", { d: mm(f.depth) }) : t("feat_through"));
    case "fillet":
      return t("feat_fillet", { r: mm(f.radius) });
    case "chamfer":
      return t("feat_chamfer", { d: mm(f.distance) });
    case "script":
      return t("feat_script", { reason: f.reason ?? "" });
    default:
      return f.type;
  }
}
