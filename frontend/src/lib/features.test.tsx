import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { LangProvider, useI18n } from "../i18n";
import type { Lang } from "../i18n";
import { gate } from "../test/gates";
import { describeFeature } from "./features";

function i18n(lang: Lang) {
  const wrapper = ({ children }: { children: ReactNode }) => <LangProvider lang={lang}>{children}</LangProvider>;
  return renderHook(() => useI18n(), { wrapper }).result.current;
}

describe("describeFeature", () => {
  it("describes the plan in the reviewer's language, with local number format", () => {
    const [base, hole] = gate().request.plan!;
    expect(describeFeature(base!, i18n("en"))).toBe("Base body extruded (outline with one radius), thickness 10 mm");
    expect(describeFeature(hole!, i18n("de"))).toBe("Bohrung Ø6,6, durchgehend, Senkung Ø11 × 6,8 mm");
    expect(describeFeature({ type: "hole_pattern", id: "p", seed: hole, pattern: { kind: "circular", count: 4 } }, i18n("en"))).toBe(
      "Bolt circle, 4 times: Hole Ø6.6, through, counterbore Ø11 × 6.8 mm",
    );
  });
});
