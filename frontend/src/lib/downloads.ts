import type { AppEvent } from "../api/types";

// The files cad-mcp's `export` writes (docs/05) and the review API serves read-only
// (`R/review/artifacts/<build>/<file>`, #52, #119), in the order the downloads panel lists them.
export const EXPORT_FORMATS = ["step-ap242", "step-ap214", "stl", "obj", "features-json", "review-json"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

const SUFFIX: Record<ExportFormat, string> = {
  "step-ap242": ".ap242.step",
  "step-ap214": ".ap214.step",
  stl: ".stl",
  obj: ".obj",
  "features-json": ".features.json",
  "review-json": ".review.json",
};
// Same rules as the review API (`review_app/api.py`): a file it would refuse is not listed.
const BUILD = /^build-[0-9a-f]{6,32}$/;
// eslint-disable-next-line no-control-regex
const NAME = /^[^./\\\x00-\x1f][^/\\\x00-\x1f]{0,199}$/;

export interface ExportFile {
  name: string;
  format: ExportFormat;
  bytes: number | null;
}

/** The exported files of one revision: those of its last export, so of one build (#101). */
export interface RevisionExports {
  revision: number;
  buildId: string;
  /** When the export was written (emitter clock). */
  at: string;
  files: ExportFile[];
}

const isFormat = (value: unknown): value is ExportFormat => EXPORT_FORMATS.includes(value as ExportFormat);

/** An `artifact.created` of cad-mcp's export: `artifact://<session>/<build>/<file>` of this run's
 * own session. Container output is untrusted (docs/13): anything else is ignored. */
function exported(e: AppEvent): { buildId: string; file: ExportFile } | null {
  if (e.type !== "artifact.created" || e.component !== "cad-mcp") return null;
  const { format, uri, bytes } = e.payload;
  if (!isFormat(format) || typeof uri !== "string" || !uri.startsWith("artifact://")) return null;
  const parts = uri.slice("artifact://".length).split("/");
  const [session, buildId, name] = parts;
  if (parts.length !== 3 || !session || session !== e.session_id || !buildId || !name) return null;
  if (!BUILD.test(buildId) || !NAME.test(name) || !name.endsWith(SUFFIX[format])) return null;
  const size = typeof bytes === "number" && Number.isFinite(bytes) && bytes >= 0 ? bytes : null;
  return { buildId, file: { name, format, bytes: size } };
}

/** Exports per revision, oldest first. A revision starts with the agent's
 * `agent.revision_started` (host-only type, D70); a later export in the same revision replaces an
 * earlier one, so the files always belong to the revision's last exported build. */
export function exportsByRevision(events: AppEvent[]): RevisionExports[] {
  let revision = 1;
  const found = new Map<number, { buildId: string; at: string; files: Map<string, ExportFile> }>();
  for (const e of events) {
    if (e.type === "agent.revision_started" && e.component === "agent") {
      const next = Number(e.payload.revision);
      if (Number.isInteger(next) && next > revision) revision = next;
      continue;
    }
    const artifact = exported(e);
    if (!artifact) continue;
    let entry = found.get(revision);
    if (!entry || entry.buildId !== artifact.buildId) {
      entry = { buildId: artifact.buildId, at: e.ts, files: new Map() };
      found.set(revision, entry);
    }
    entry.files.set(artifact.file.name, artifact.file);
  }
  const order = (f: ExportFile) => EXPORT_FORMATS.indexOf(f.format);
  return [...found.entries()]
    .sort(([a], [b]) => a - b)
    .map(([rev, { buildId, at, files }]) => ({
      revision: rev,
      buildId,
      at,
      files: [...files.values()].sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name)),
    }));
}

/** A file size in B, KB or MB (1024 based), with the reviewer's decimal separator. */
export function fileSize(bytes: number | null, num: (value: number, digits?: number) => string): string {
  if (bytes === null) return "–";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${num(bytes / 1024, 1)} KB`;
  return `${num(bytes / (1024 * 1024), 1)} MB`;
}
