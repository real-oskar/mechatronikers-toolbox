import { describe, expect, it } from "vitest";

import type { StoredEvent } from "../api/types";
import { circuitEvents, circuitGate, circuitPayload, circuitSpec, SID } from "../test/circuit";
import {
  cleanText,
  cleanValue,
  MAX_CORRECTIONS,
  circuitView,
  correctionKey,
  ercReport,
  netlists,
  operatingPoint,
  schematicId,
  selectionBox,
  setCorrection,
  validCorrections,
} from "./circuit";
import type { Corrections } from "./circuit";
import { runStatus } from "./status";

const status = (events: StoredEvent[]) => runStatus(events.map((e) => e.event));
const upTo = (events: StoredEvent[], n: number) => events.slice(0, n);

describe("circuit run status", () => {
  it("uses the shared circuit phases (lib/status, lib/phases) through revise rounds", () => {
    const done = circuitEvents("done");
    expect(status(upTo(done, 3))).toMatchObject({ kind: "circuit", phase: "analysing" });
    expect(status(circuitEvents("gate")).phase).toBe("waiting_gate_1");
    expect(status(upTo(done, 18)).phase).toBe("exporting");
    expect(status(done)).toMatchObject({ phase: "valid", valid: true });
    expect(status(circuitEvents("done", { passed: false }))).toMatchObject({ phase: "valid", valid: false });
    const revised = circuitEvents("revised");
    expect(status(upTo(revised, 20))).toMatchObject({ phase: "checking", revision: 2 }); // ERC of revision 2
    expect(status(revised)).toMatchObject({ phase: "waiting_gate_1", revision: 2 });
  });

  it("reads the schematic, checks, netlists and operating point from the events", () => {
    const events = circuitEvents("done").map((e) => e.event);
    expect(schematicId(events, [])).toBe(SID);
    expect(schematicId([], [circuitGate()])).toBe(SID);
    expect(schematicId([], [])).toBeNull();
    expect(ercReport(events)?.checks.map((c) => c.category)).toEqual(["ERC", "ERC"]);
    expect(netlists(events)).toEqual([
      { format: "spice", name: "divider.cir" },
      { format: "ltspice", name: "divider.asc" },
      { format: "kicad-netlist", name: "divider.net" },
    ]);
    expect(netlists(circuitEvents("gate").map((e) => e.event))).toEqual([]);
    expect(operatingPoint(events)).toEqual({ ok: true, reason: "", voltages: [["gnd", 0], ["n2", 0.000340426], ["vin", 5]] });
    expect(operatingPoint([])).toBeNull();
  });
});

describe("circuit view and selection", () => {
  it("shows what the open gate showed, with the wires from the spec", () => {
    const fromGate = circuitView(circuitSpec(), circuitPayload())!;
    expect(fromGate.nets.find((n) => n.id === "N2")?.segments).toHaveLength(3);
    const gateOnly = circuitView(null, circuitPayload())!;
    expect(gateOnly.components.map((c) => c.refdes)).toEqual(["V1", "R1", "R2", "D1"]);
    expect(gateOnly.nets.every((n) => n.segments.length === 0)).toBe(true);
    expect(circuitView(null, null)).toBeNull();
  });

  it("finds the sheet box of a part, a net and a finding", () => {
    const view = circuitView(circuitSpec(), null);
    const items = circuitSpec().review_items;
    expect(selectionBox({ type: "part", id: "R1" }, view, items)).toEqual([108, 92, 116, 95]);
    expect(selectionBox({ type: "net", id: "N1" }, view, items)).toEqual([102, 93.5, 108, 98]);
    expect(selectionBox({ type: "item", id: "ri-1" }, view, items)).toEqual([118, 96, 124, 104]);
    expect(selectionBox({ type: "part", id: "X9" }, view, items)).toBeNull();
  });
});

describe("corrections", () => {
  const set = (c: Corrections, x: Parameters<typeof setCorrection>[1]) => setCorrection(c, x).corrections;

  it("keeps one correction per field and drops one that changes nothing", () => {
    let c: Corrections = new Map();
    c = set(c, { target: "component", id: "R2", field: "value", old: "4k7", new: "4" });
    c = set(c, { target: "component", id: "R2", field: "value", old: "4k7", new: " 47k " });
    c = set(c, { target: "net", id: "N2", field: "name", old: null, new: "MID" });
    expect([...c.values()]).toEqual([
      { target: "component", id: "R2", field: "value", old: "4k7", new: "47k" },
      { target: "net", id: "N2", field: "name", old: null, new: "MID" },
    ]);
    c = set(c, { target: "component", id: "R2", field: "value", old: "4k7", new: "4k7" });
    c = set(c, { target: "net", id: "N2", field: "name", old: null, new: "\u00a0 " });
    expect(c.size).toBe(0);
  });

  it("cleans pasted text and units like the netlist writer reads them", () => {
    expect(cleanText(" signal\u00a0in\u202f2\u200b ")).toBe("signal in 2");
    expect(cleanText("a\tb\nc")).toBe("a b c");
    expect(cleanValue("100\u00a0nF")).toBe("100nF");
    expect(cleanValue("4,7 k\u03a9")).toBe("4.7k");
    expect(cleanValue("2.2 \u03bcF")).toBe("2.2uF");
    expect(cleanValue("10 \u2126")).toBe("10");
    expect(cleanValue("1N4148")).toBe("1N4148");
    expect(cleanValue("4k7")).toBe("4k7");
    expect(cleanValue("dc=0  ampl=1")).toBe("dc=0 ampl=1");
    const pasted = setCorrection(new Map(), { target: "component", id: "C1", field: "value", old: "100n", new: "220\u202fnF" });
    expect([...pasted.corrections.values()][0]?.new).toBe("220nF");
  });

  it("refuses what the review API would refuse, with the reason", () => {
    const long = setCorrection(new Map(), { target: "net", id: "N2", field: "name", old: null, new: "x".repeat(65) });
    expect(long).toEqual({ corrections: new Map(), problem: "tooLong" });
    const role = setCorrection(new Map(), { target: "terminal", id: "D1.1", field: "role", old: "A", new: "anode" });
    expect(role.problem).toBe("notAllowed");
    let full: Corrections = new Map();
    for (let i = 0; i < MAX_CORRECTIONS; i += 1) full = set(full, { target: "component", id: `R${i}`, field: "value", old: "1k", new: "2k" });
    const more = setCorrection(full, { target: "component", id: "R999", field: "value", old: "1k", new: "2k" });
    expect(more.problem).toBe("tooMany");
    expect(more.corrections.size).toBe(MAX_CORRECTIONS);
    // changing one that is already recorded is still fine
    expect(setCorrection(full, { target: "component", id: "R1", field: "value", old: "1k", new: "3k" }).problem).toBeNull();
  });

  it("sends only corrections of what the gate shows", () => {
    const c: Corrections = new Map([
      [correctionKey("terminal", "D1.1", "role"), { target: "terminal", id: "D1.1", field: "role", old: "A", new: "K" }],
      [correctionKey("component", "R9", "value"), { target: "component", id: "R9", field: "value", old: null, new: "1k" }],
    ]);
    expect(validCorrections(c, circuitPayload()).map((x) => x.id)).toEqual(["D1.1"]);
    expect(validCorrections(c, null)).toEqual([]);
  });
});
