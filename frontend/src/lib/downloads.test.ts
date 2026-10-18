import { describe, expect, it } from "vitest";

import { event, exportOf, revisionStarted, SESSION } from "../test/exports";
import { exportsByRevision, fileSize } from "./downloads";

describe("exportsByRevision (#52)", () => {
  it("lists the files of each revision's last export, in a fixed order", () => {
    const revisions = exportsByRevision([
      ...exportOf("build-aaaaaa", ["stl"]), // an earlier export of revision 1 ...
      ...exportOf("build-bbbbbb").reverse(), // ... replaced by the last one (#101)
      event("agent", "agent.run_finished", { status: "done" }),
      revisionStarted(2),
      ...exportOf("build-cccccc", ["step-ap242", "stl"]),
    ]);
    expect(revisions.map((r) => [r.revision, r.buildId])).toEqual([
      [1, "build-bbbbbb"],
      [2, "build-cccccc"],
    ]);
    expect(revisions[0]!.files.map((f) => f.name)).toEqual([
      "Fuss.ap242.step",
      "Fuss.ap214.step",
      "Fuss.stl",
      "Fuss.features.json",
      "Fuss.review.json",
    ]);
    expect(revisions[0]!.files[0]).toEqual({ name: "Fuss.ap242.step", format: "step-ap242", bytes: 52_011 });
    expect(revisions[1]!.files.map((f) => f.format)).toEqual(["step-ap242", "stl"]);
  });

  it("ignores what the review API would not serve and lines that only look like exports", () => {
    const created = (uri: string, format = "stl", over = {}) => event("cad-mcp", "artifact.created", { uri, format, bytes: 1 }, over);
    expect(
      exportsByRevision([
        created(`artifact://ses-other000001/build-aaaaaa/x.stl`), // another session's file
        created(`artifact://${SESSION}/build-fx2/x.stl`), // not a build id
        created(`artifact://${SESSION}/build-aaaaaa/../x.stl`),
        created(`artifact://${SESSION}/build-aaaaaa/.x.stl`),
        created(`artifact://${SESSION}/build-aaaaaa/x.step`), // suffix of another format
        created(`artifact://${SESSION}/build-aaaaaa/front.svg`, "svg"), // render_views, not an export
        created(`artifact://${SESSION}/build-aaaaaa/x.stl`, "stl", { component: "build-runner" }),
        created(`file:///session/artifacts/build-aaaaaa/x.stl`),
      ]),
    ).toEqual([]);
    // a revision mark only counts from the agent (a host-only type, docs/08)
    const [only] = exportsByRevision([
      event("cad-mcp", "agent.revision_started", { revision: 5 }),
      revisionStarted(Number.NaN),
      ...exportOf("build-dddddd", ["stl"]),
    ]);
    expect(only!.revision).toBe(1);
    const [sized] = exportsByRevision([created(`artifact://${SESSION}/build-aaaaaa/x.stl`, "stl", { payload: { uri: `artifact://${SESSION}/build-aaaaaa/x.stl`, format: "stl", bytes: "big" } })]);
    expect(sized!.files[0]!.bytes).toBeNull();
  });

  it("writes sizes in B, KB and MB", () => {
    const num = (value: number, digits?: number) => value.toFixed(digits ?? 0);
    expect([fileSize(null, num), fileSize(900, num), fileSize(52_011, num), fileSize(2_300_000, num)]).toEqual([
      "–",
      "900 B",
      "50.8 KB",
      "2.2 MB",
    ]);
  });
});
