import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { fixtureEvents, unisolated, until } from "../test/fixture";
import { gate } from "../test/gates";
import { renderIn } from "../test/render";
import { RunViewContent } from "./RunView";

vi.mock("./ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

describe("RunViewContent", () => {
  it("shows the agent's work as a routing sheet with the plan release as a stamp", () => {
    renderIn(<RunViewContent events={fixtureEvents()} gates={[]} items={[]} connected reload={vi.fn()} />);
    const sheet = screen.getByRole("navigation", { name: "Routing sheet" });
    const ops = within(sheet).getAllByRole("button").map((b) => b.querySelector(".op-name")?.textContent);
    expect(ops).toEqual([
      "Prepare sandbox", "Analyse drawing", "Read dimensions", "Check against geometry", "Plan features",
      "Release plan", "Build model", "Rework #1", "Build model #2", "Export",
    ]); // fmt: skip
    expect(within(sheet).getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Valid")).toBeInTheDocument(); // the release stamp in the title block
  });

  it("keeps the first article inspection locked until the model is valid", () => {
    const { unmount } = renderIn(
      <RunViewContent events={until(fixtureEvents(), "review.gate_opened")} gates={[gate()]} items={[]} connected reload={vi.fn()} />,
    );
    expect(screen.getByRole("tab", { name: /First article inspection/ })).toBeDisabled();
    unmount();
    renderIn(<RunViewContent events={fixtureEvents()} gates={[gate()]} items={[]} connected reload={vi.fn()} />);
    expect(screen.getByRole("tab", { name: "First article inspection" })).toBeEnabled();
  });

  it("releases the plan from the sign-off: a rejection needs a reason, the release records the reviewer", async () => {
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ ...gate("spec_review"), open: false });
    const reload = vi.fn();
    renderIn(
      <RunViewContent events={until(fixtureEvents(), "review.gate_opened")} gates={[gate("spec_review")]} items={[]} connected reload={reload} />,
    );
    const user = userEvent.setup();
    const signOff = screen.getByRole("region", { name: "Release the plan?" });
    expect(within(signOff).getByText("Hole Ø6.6, through, counterbore Ø11 × 6.8 mm")).toBeInTheDocument();
    expect(within(signOff).getByRole("button", { name: "Release plan" })).toBeDisabled(); // no name yet
    await user.type(screen.getByPlaceholderText("Your name"), "Anna");
    await user.click(within(signOff).getByRole("button", { name: "Reject …" }));
    expect(within(signOff).getByRole("button", { name: "Reject plan" })).toBeDisabled();
    await user.click(within(signOff).getByRole("button", { name: "Cancel" }));
    await user.click(within(signOff).getByRole("button", { name: "Release plan" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ action: "approve", reviewer: "Anna" }));
    expect(reload).toHaveBeenCalled();
  });

  it("warns on the run view and on the gate card when a build ran without sandbox isolation (D95)", () => {
    const events = until(fixtureEvents(), "review.gate_opened");
    const { unmount } = renderIn(
      <RunViewContent events={events} gates={[gate("spec_review")]} items={[]} connected reload={vi.fn()} />,
    );
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();
    // the review API marks the gate state
    const flagged = { ...gate("spec_review"), unisolated_builds: 1 };
    const view = renderIn(<RunViewContent events={events} gates={[flagged]} items={[]} connected reload={vi.fn()} />);
    expect(screen.getAllByRole("alert")).toHaveLength(2); // the run view and the sign-off
    const signOff = screen.getByRole("region", { name: "Release the plan?" });
    expect(within(signOff).getByRole("alert")).toHaveTextContent(
      "Builds in this run ran without sandbox isolation; gate state may be untrusted.",
    );
    view.unmount();
    // the events say so, in German
    renderIn(<RunViewContent events={unisolated(fixtureEvents())} gates={[]} items={[]} connected reload={vi.fn()} />, "de");
    expect(screen.getByRole("alert")).toHaveTextContent("Aufbauten in diesem Lauf liefen ohne Sandbox-Isolation");
  });
});
