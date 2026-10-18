import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { LangProvider, useI18n } from "../i18n";
import type { Lang } from "../i18n";
import { phasesFor } from "./overview";
import { phaseClass, phaseText } from "./phases";

const translate = (lang: Lang) =>
  renderHook(() => useI18n(), { wrapper: ({ children }: { children: ReactNode }) => <LangProvider lang={lang}>{children}</LangProvider> })
    .result.current.t;

describe("phase texts per run kind (#110)", () => {
  it("names a circuit run's own steps and shares the common phases", () => {
    const t = translate("en");
    expect(phaseText("circuit", "checking", t)).toBe("checking the circuit (ERC)");
    expect(phaseText("circuit", "waiting_gate_1", t)).toBe("waiting for circuit approval");
    expect(phaseText("circuit", "exporting", t)).toBe("writing the netlists");
    expect(phaseText("circuit", "valid", t)).toBe("netlists ready");
    expect(phaseText("circuit", "failed", t)).toBe("failed");
    expect(phaseText("part", "valid", t)).toBe("valid, ready for inspection");
    expect(phaseText("part", "fixing", t, { n: 2, max: 3 })).toBe("reworking (2 of 3)");
    expect(phaseText("all", "fixing", t)).toBe("reworking"); // the filter: no round
    // the filter over both kinds: neutral where the kinds differ, else the kind's own text
    expect(phaseText("all", "valid", t)).toBe("result ready");
    expect(phaseText("all", "waiting_gate_1", t)).toBe("waiting for approval (gate 1)");
    expect(phaseText("all", "simulating", t)).toBe("computing the operating point (ngspice)");
    expect(phaseText("all", "queued", t)).toBe("queued");
  });

  it("speaks German", () => {
    const t = translate("de");
    expect(phaseText("circuit", "simulating", t)).toBe("Arbeitspunkt wird berechnet (ngspice)");
    expect(phaseText("circuit", "valid", t)).toBe("Netzlisten bereit");
    expect(phaseText("part", "valid", t)).toBe("gültig, bereit zur Prüfung");
  });

  it("marks the phase cell and lists the phases a kind can reach", () => {
    expect([phaseClass("released"), phaseClass("gave_up"), phaseClass("waiting_gate_1"), phaseClass("exporting")]).toEqual([
      "status-ok",
      "status-nok",
      "status-check",
      undefined,
    ]);
    expect(phasesFor("circuit")).toEqual(["queued", "provisioning", "analysing", "checking", "waiting_gate_1", "exporting", "simulating", "valid", "failed", "shelved"]);
    expect(phasesFor("part")).not.toContain("simulating");
    expect(phasesFor("part")).toContain("released");
    expect(phasesFor("all")).toHaveLength(16);
  });
});
