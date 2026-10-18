import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { SignOff } from "../components/SignOff";
import { TitleBlock } from "../components/TitleBlock";
import { RunViewContent } from "../components/RunView";
import { Panels as CircuitPanels } from "../kinds/circuit";
import { demoEvents, fixtureEvents, until } from "../test/fixture";
import { gate } from "../test/gates";
import { renderIn } from "../test/render";
import { runStatus } from "./status";

vi.mock("../components/ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

// The two runs recorded for the demo video, replayed (#152), as the reviewer saw them before the
// sessions were shelved.
const live = (name: "demo-fuss" | "demo-rectifier") =>
  demoEvents(name).filter((e) => !/^session\.(snapshot|shelved|stopped)|^sandbox\.stopping/.test(e.event.type));

describe("demo runs, replayed", () => {
  it("a part run is released once the inspection is finished", () => {
    const status = runStatus(live("demo-fuss").map((e) => e.event));
    expect([status.phase, status.valid]).toEqual(["released", true]);
    // only the run driver's resume from gate 2 with "finish" releases it, not `status: done`
    const noFinish = live("demo-fuss").filter((e) => !(e.event.type === "agent.resumed" && e.event.payload.action === "finish"));
    expect(runStatus(noFinish.map((e) => e.event)).phase).toBe("valid");
    const forged = live("demo-fuss").map((e) =>
      e.event.type === "agent.resumed" && e.event.payload.action === "finish" ? { ...e, event: { ...e.event, source: "container" as const } } : e,
    );
    expect(runStatus(forged.map((e) => e.event)).phase).toBe("valid"); // a container line is no proof
    renderIn(<RunViewContent events={live("demo-fuss")} gates={[]} items={[]} connected reload={vi.fn()} />);
    expect(screen.getByText("released after inspection · 4 open points")).toBeInTheDocument(); // D70
    expect(screen.getByRole("tab", { name: "First article inspection" })).toBeEnabled();
  });

  it("a circuit's failed operating point is a warning, not a tick", () => {
    vi.spyOn(api, "circuitSpec").mockRejectedValue(new Error("shelved"));
    renderIn(<CircuitPanels events={live("demo-rectifier")} gates={[]} items={[]} connected reload={vi.fn()} />);
    const sheet = screen.getByRole("navigation", { name: "Routing sheet" });
    const op = within(sheet).getByText("Operating point").closest("button")!;
    expect(op).toHaveClass("op-warned");
    expect(op.querySelector(".op-state")?.textContent).toMatch(/^⚠ /);
    const export_ = within(sheet).getByText("Export netlists").closest("button")!;
    expect(export_.querySelector(".op-state")?.textContent).toMatch(/^✓ /);
    // V1's "No value found" stayed open in the recording (before corrections resolved it, #129)
    expect(screen.getByText("netlists ready · 1 open point")).toBeInTheDocument();
    vi.restoreAllMocks();
  });
});

describe("UI states from the demo recording (#152)", () => {
  it("never says a signal came in the future", () => {
    const status = runStatus(demoEvents("demo-fuss").map((e) => e.event));
    const early = Date.parse(status.lastActivity!) - 2000; // the view's clock ticks every 5 s
    renderIn(<TitleBlock status={status} tab="routing" onTab={vi.fn()} reviewer="" onReviewer={vi.fn()} connected now={early} />);
    expect(screen.getByText(/last signal 0\s?s ago/)).toBeInTheDocument();
    expect(screen.queryByText(/-\d/)).toBeNull();
  });

  it("shows the overall size as printed values, the dimension id on hover", () => {
    renderIn(<SignOff gate={gate("spec_review")} reviewer="Anna" onDecided={vi.fn()} />);
    const size = screen.getByText(/^x 100 mm$/);
    expect(size).toHaveAttribute("title", "d-100");
    expect(size.parentElement).toHaveTextContent("x 100 mm, y not checked, z not checked");
  });

  it("does not count features while the plan is still being made", () => {
    renderIn(<RunViewContent events={until(fixtureEvents(), "agent.step_started", "plan")} gates={[]} items={[]} connected reload={vi.fn()} />);
    expect(screen.getByText("The features are being planned from the checked dimensions.")).toBeInTheDocument();
    expect(screen.queryByText(/\? features planned/)).toBeNull();
  });
});
