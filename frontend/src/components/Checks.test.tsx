import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { StoredEvent } from "../api/types";
import { runStatus } from "../lib/status";
import { aborted, CAP, fixtureEvents, gaveUpRun, stoppedRun, until } from "../test/fixture";
import { gate } from "../test/gates";
import { renderIn } from "../test/render";
import { ValidationGate } from "./Checks";

const status = (events: StoredEvent[]) => runStatus(events.map((e) => e.event));

function gaveUp(): StoredEvent[] {
  const events = fixtureEvents();
  const last = events[events.length - 1]!;
  return [...events.slice(0, -1), { ...last, event: { ...last.event, payload: { ...last.event.payload, status: "build_failed" } } }];
}

describe("ValidationGate", () => {
  it("shows a valid model with its checks by category", () => {
    renderIn(<ValidationGate status={status(fixtureEvents())} reviewer="Anna" onDecided={vi.fn()} />);
    expect(screen.getByText("Model valid: every check passed.")).toBeInTheDocument();
    expect(screen.getByText("Dimensions")).toBeInTheDocument();
  });

  it("shows the rework round while the agent fixes", () => {
    renderIn(<ValidationGate status={status(until(fixtureEvents(), "agent.step_started", "fix"))} reviewer="Anna" onDecided={vi.fn()} />);
    expect(screen.getByText("The agent is reworking the model (round 1 of 3).")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("offers retry and abort only after a give-up, when the agent waits", () => {
    const { rerender } = renderIn(<ValidationGate status={status(gaveUp())} reviewer="Anna" onDecided={vi.fn()} />);
    expect(screen.getByText("The agent stopped without a valid model.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    const waiting = gate("gave_up");
    rerender(<ValidationGate status={status(gaveUp())} gaveUpGate={waiting} reviewer="Anna" onDecided={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Retry with hint" })).toBeDisabled(); // needs a hint
    expect(screen.getByRole("button", { name: "Abort run" })).toBeEnabled();
  });

  it("says why the agent gave up and which features its last build could not add (#92)", () => {
    const waiting = gate("gave_up");
    renderIn(<ValidationGate status={status(gaveUpRun("awaiting_review", CAP))} gaveUpGate={waiting} reviewer="Anna" onDecided={vi.fn()} />);
    expect(screen.getByText("The agent stopped without a valid model.")).toBeInTheDocument();
    expect(screen.getByText("Why: the rework limit of 3 rounds was reached.")).toBeInTheDocument();
    expect(screen.getByText("Features not built: h1")).toBeInTheDocument();
  });

  it("names a plateau and a failed step in German (#92)", () => {
    const plateau = gaveUpRun("awaiting_review", { ...CAP, reason: "plateau" });
    const { unmount } = renderIn(<ValidationGate status={status(plateau)} reviewer="Anna" onDecided={vi.fn()} />, "de");
    expect(screen.getByText("Grund: zwei Nacharbeitsrunden hintereinander brachten keine Verbesserung.")).toBeInTheDocument();
    unmount();
    renderIn(<ValidationGate status={status(stoppedRun("error", "fix: no JSON"))} reviewer="Anna" onDecided={vi.fn()} />, "de");
    expect(screen.getByText("Der Lauf endete ohne Modell.")).toBeInTheDocument();
    expect(screen.getByText(/Grund: ein Schritt ist fehlgeschlagen\./)).toBeInTheDocument();
    expect(screen.getByText("(fix: no JSON)")).toBeInTheDocument();
  });

  it("says the run was aborted, not that checks still run (#65)", () => {
    renderIn(<ValidationGate status={status(aborted())} reviewer="Anna" onDecided={vi.fn()} />, "de");
    expect(screen.getByText("Der Prüfer hat den Lauf abgebrochen.")).toBeInTheDocument();
    expect(screen.queryByText("Nach jedem Aufbauschritt wird geprüft.")).toBeNull();
  });
});
