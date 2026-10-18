import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import type { GateState, StoredEvent } from "../api/types";
import { circuitEvents, circuitGate, circuitSpec, revisedGates, SID } from "../test/circuit";
import { renderIn } from "../test/render";
import { Panels } from "./circuit";

let decide: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  vi.spyOn(api, "circuitSpec").mockResolvedValue(circuitSpec());
  decide = vi.spyOn(api, "decide").mockResolvedValue({ ...circuitGate(), open: false });
});
afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

const run = (events: StoredEvent[], gates: GateState[], reload = vi.fn(), lang: "en" | "de" = "en") =>
  renderIn(<Panels events={events} gates={gates} items={[]} connected reload={reload} />, lang);

async function partsTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("tab", { name: "Parts and nets" }));
  return screen.findByRole("region", { name: "Parts (4)" });
}

describe("circuit panels", () => {
  it("shows a run waiting at gate 1: its operations, phase, rule check and findings", async () => {
    run(circuitEvents("gate"), [circuitGate()]);
    const sheet = screen.getByRole("navigation", { name: "Routing sheet" });
    const ops = within(sheet).getAllByRole("button").map((b) => b.querySelector(".op-name")?.textContent);
    expect(ops).toEqual(["Prepare sandbox", "Read schematic", "Check electrical rules", "Approve circuit"]);
    expect(within(sheet).getAllByText("waiting for you").length).toBeGreaterThan(0);
    expect(screen.getByText("waiting for circuit approval")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "divider" })).toBeInTheDocument();
    const checks = screen.getByRole("region", { name: "Electrical rule check" });
    expect(within(checks).getByText("Every electrical rule passes.")).toBeInTheDocument();
    expect(within(checks).getByText("Electrical rules")).toBeInTheDocument();
    expect(within(checks).getByText("A1.unnamed_nets: 1 net without a name")).toBeInTheDocument();
    const gate = screen.getByRole("region", { name: "Approve the circuit?" });
    expect(within(gate).getByText("Value of R2 is hard to read")).toBeInTheDocument();
    expect(within(gate).getByText("No corrections: you approve the circuit as read.")).toBeInTheDocument();
    expect(screen.getByText("The netlists are written once the circuit is approved.")).toBeInTheDocument();
    // the schematic comes from the circuit add-on, the sheet with the run's parts on top
    expect(await screen.findByRole("button", { name: "R2 · resistor · 4k7" })).toBeInTheDocument();
    expect(screen.getAllByAltText("Schematic")[0]).toHaveAttribute("src", `/circuit/api/schematics/${SID}/sheet.png`);
    expect(api.circuitSpec).toHaveBeenCalledWith(SID);
  });

  it("selects a part from the sheet with its row, and the net of one of its terminals", async () => {
    const user = userEvent.setup();
    run(circuitEvents("gate"), [circuitGate()]);
    const parts = await partsTab(user);
    await user.click(await screen.findByRole("button", { name: "R2 · resistor · 4k7" }));
    expect(document.getElementById("cpart-R2")).toHaveAttribute("aria-selected", "true");
    expect(within(parts).getByRole("table", { name: "Terminals of R2" })).toBeInTheDocument();
    expect(screen.getByText(/^R2 · resistor · 4k7 · 1→N2, 2→GND$/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "R2.1 → N2" }));
    expect(document.getElementById("cnet-N2")).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("cpart-R2")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("N2: R1.2, R2.1, D1.1")).toBeInTheDocument();
    // a row selects its part on the sheet
    await user.click(within(parts).getByRole("button", { name: "R1" }));
    expect(document.getElementById("cpart-R1")).toHaveAttribute("aria-selected", "true");
    expect(document.querySelector(".ov-part-selected")).toHaveAttribute("aria-label", "R1 · resistor · 10k");
  });

  it("records value, kind, pin role and number and net name corrections with the approval", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    run(circuitEvents("gate"), [circuitGate()], reload);
    const parts = await partsTab(user);
    const gate = screen.getByRole("region", { name: "Approve the circuit?" });
    expect(within(gate).getByRole("button", { name: "Approve circuit" })).toBeDisabled(); // no name yet
    await user.type(screen.getByPlaceholderText("Your name"), "Anna");

    const value = within(parts).getByRole("textbox", { name: "value R2" });
    await user.clear(value);
    await user.type(value, "47k");
    await user.selectOptions(within(parts).getByRole("combobox", { name: "kind V1" }), "current_source");
    await user.click(within(parts).getByRole("button", { name: "D1" }));
    await user.selectOptions(within(parts).getByRole("combobox", { name: "pin role D1.1" }), "K");
    await user.type(within(parts).getByRole("textbox", { name: "pin number D1.1" }), "1");
    await user.type(screen.getByRole("textbox", { name: "net name N2" }), "MID");
    expect(within(gate).getByRole("heading", { name: "Your corrections (5)" })).toBeInTheDocument();
    expect(within(gate).getByText("R2 value: 4k7 → 47k")).toBeInTheDocument();
    expect(within(gate).getByText("V1 kind: voltage source → current source")).toBeInTheDocument();

    // a correction removed from the list is gone from the table too
    await user.click(within(gate).getByRole("button", { name: "Remove the correction of V1 kind" }));
    expect(within(parts).getByRole("combobox", { name: "kind V1" })).toHaveValue("voltage_source");

    await user.click(within(gate).getByRole("button", { name: "Approve circuit" }));
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({
        gate_id: "gate-run-1-1",
        action: "approve",
        reviewer: "Anna",
        reason: null,
        corrections: [
          { target: "component", id: "R2", field: "value", old: "4k7", new: "47k" },
          { target: "terminal", id: "D1.1", field: "role", old: "A", new: "K" },
          { target: "terminal", id: "D1.1", field: "pin", old: null, new: "1" },
          { target: "net", id: "N2", field: "name", old: null, new: "MID" },
        ],
      }),
    );
    expect(reload).toHaveBeenCalled();
  });

  it("rejects the circuit only with a reason", async () => {
    const user = userEvent.setup();
    run(circuitEvents("gate"), [circuitGate()]);
    await user.type(screen.getByPlaceholderText("Your name"), "Anna");
    const gate = screen.getByRole("region", { name: "Approve the circuit?" });
    await user.click(within(gate).getByRole("button", { name: "Reject …" }));
    expect(within(gate).getByRole("button", { name: "Reject circuit" })).toBeDisabled();
    await user.type(within(gate).getByRole("textbox"), "R2 is 47k on paper");
    await user.click(within(gate).getByRole("button", { name: "Reject circuit" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ action: "reject", reason: "R2 is 47k on paper", corrections: [] }));
  });

  it("offers the netlists and ngspice's operating point once exported", async () => {
    run(circuitEvents("done"), [circuitGate(false)]);
    expect(screen.getByText("netlists ready · 1 open point")).toBeInTheDocument(); // R2's finding (D70)
    expect(screen.getByText("Netlists ready")).toBeInTheDocument(); // the stamp
    expect(screen.queryByRole("region", { name: "Approve the circuit?" })).toBeNull();
    const result = screen.getByRole("region", { name: "Netlists and operating point" });
    const spice = within(result).getByRole("link", { name: /SPICE netlist/ });
    expect(spice).toHaveAttribute("href", `/circuit/api/schematics/${SID}/files/divider.cir`);
    expect(spice).toHaveAttribute("download", "divider.cir");
    expect(within(result).getByRole("link", { name: /KiCad netlist/ })).toHaveAttribute("download", "divider.net");
    const rows = within(result).getAllByRole("row").map((r) => r.textContent);
    expect(rows).toEqual(["NodeVoltage", "gnd0 V", "n20.0003404 V", "vin5.000 V"]);
    expect(await screen.findByRole("button", { name: "R2 · resistor · 4k7" })).toBeInTheDocument();
  });

  it("shows the corrections recorded with the decision at the gate operation", async () => {
    const user = userEvent.setup();
    run(circuitEvents("done"), [circuitGate(false)]);
    await user.click(screen.getByRole("button", { name: /Approve circuit/ }));
    const op = screen.getByRole("region", { name: "Approve circuit" });
    expect(within(op).getByText("R2 value: 4k7 → 47k")).toBeInTheDocument();
  });

  it("lists an operation's findings and the corrections of its own gate across revisions", async () => {
    const user = userEvent.setup();
    run(circuitEvents("revised"), revisedGates());
    const sheet = screen.getByRole("navigation", { name: "Routing sheet" });
    const ops = within(sheet).getAllByRole("button").map((b) => b.querySelector(".op-name")?.textContent);
    expect(ops).toEqual([
      "Prepare sandbox", "Read schematic", "Check electrical rules", "Approve circuit", "Revise circuit",
      "Check electrical rules #2", "Approve circuit #2",
    ]); // fmt: skip
    expect(within(sheet).getByText("Revision 2")).toBeInTheDocument();
    expect(screen.getByText("waiting for circuit approval")).toBeInTheDocument();
    const gate = screen.getByRole("region", { name: "Approve the circuit?" });
    expect(within(gate).getByRole("heading", { name: /^Approve the circuit\?.*Revision 2$/ })).toBeInTheDocument();
    expect(within(gate).getByText("No corrections: you approve the circuit as read.")).toBeInTheDocument(); // a new gate starts empty

    await user.click(within(sheet).getByText("Read schematic"));
    const analyse = screen.getByRole("region", { name: "Read schematic" });
    expect(within(analyse).getByText("Value of R2 is hard to read")).toBeInTheDocument();
    expect(within(analyse).getByText("two texts near the symbol")).toBeInTheDocument();

    await user.click(within(sheet).getByText("Approve circuit"));
    const first = screen.getByRole("region", { name: "Approve circuit" });
    expect(within(first).getByText("Corrections recorded with the decision")).toBeInTheDocument();
    expect(within(first).getByText("R2 value: 4k7 → 47k")).toBeInTheDocument();
    await user.click(within(sheet).getByText("Revise circuit"));
    const revise = screen.getByRole("region", { name: "Revise circuit" });
    expect(within(revise).getByText("The corrections are in (count: 1); the next revision is checked again. Reason: R2 is 47k on paper")).toBeInTheDocument();
    expect(within(revise).getByText("Corrections applied to the circuit")).toBeInTheDocument();
    await user.click(within(sheet).getByText("Approve circuit #2"));
    expect(within(screen.getByRole("region", { name: "Approve circuit #2" })).queryByText("R2 value: 4k7 → 47k")).toBeNull();
  });

  it("sends pasted values cleaned and explains a refused decision", async () => {
    const user = userEvent.setup();
    decide.mockRejectedValueOnce(
      new ApiError(422, JSON.stringify([{ type: "value_error", loc: ["corrections", 0], msg: "Value error, a correction needs a printable new value" }])),
    );
    run(circuitEvents("gate"), [circuitGate()]);
    const parts = await partsTab(user);
    await user.type(screen.getByPlaceholderText("Your name"), "Anna");
    const value = within(parts).getByRole("textbox", { name: "value R2" });
    await user.clear(value);
    await user.type(value, "47\u00a0k\u03a9");
    const gate = screen.getByRole("region", { name: "Approve the circuit?" });
    expect(within(gate).getByText("R2 value: 4k7 → 47k")).toBeInTheDocument();
    await user.click(within(gate).getByRole("button", { name: "Approve circuit" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ corrections: [{ target: "component", id: "R2", field: "value", old: "4k7", new: "47k" }] }));
    expect(within(gate).getByText("The review API refused the decision: R2 value: 4k7 → 47k: a correction needs a printable new value")).toBeInTheDocument();
  });

  it("stamps an auto-approved gate 1 as automatic", () => {
    const events = circuitEvents("gate").filter((e) => e.event.type !== "agent.interrupt");
    const last = events[events.length - 1]!;
    const passed: StoredEvent = { id: last.id + 1, event: { ...last.event, seq: last.event.seq + 1, type: "review.gate_passed", payload: { gate: "spec_review", auto: true, run_kind: "circuit" } } };
    run([...events, passed], []);
    expect(screen.getByText("automatic")).toBeInTheDocument();
    expect(screen.queryByText("automatic (test bench)")).toBeNull(); // that is the part test bench
  });

  it("says why there are no netlists after a rejection, in German", () => {
    run(circuitEvents("rejected"), [], vi.fn(), "de");
    expect(screen.getByText("fehlgeschlagen")).toBeInTheDocument(); // the shared phase text (lib/phases)
    expect(screen.getByText("Die Schaltung wurde abgelehnt: R2 is 47k on paper")).toBeInTheDocument();
    const sheet = screen.getByRole("navigation", { name: "Arbeitsplan" });
    expect(within(sheet).getByText("Schaltplan lesen")).toBeInTheDocument();
    expect(within(sheet).getByText("Abgelehnt")).toBeInTheDocument();
  });
});
