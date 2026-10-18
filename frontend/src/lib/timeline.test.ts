import { describe, expect, it } from "vitest";

import { fixtureEvents, until } from "../test/fixture";
import { groupSteps, matches } from "./timeline";

describe("groupSteps", () => {
  it("groups a run into steps from provisioning to export", () => {
    const steps = groupSteps(fixtureEvents());
    expect(steps.map((s) => s.label)).toEqual([
      "Provisioning",
      "Analyse",
      "Read",
      "Check",
      "Plan",
      "Gate 1 · spec review",
      "Build",
      "Fix #1",
      "Build #2",
      "Export",
    ]);
    expect(steps.every((s) => s.status === "done")).toBe(true);
    const [provisioning, analyse, read, check, plan, gate, build] = steps;
    expect(provisioning!.events.map((e) => e.event.type)).toEqual([
      "session.created",
      "session.input_added",
      "sandbox.started",
      "session.started",
      "agent.run_started",
    ]);
    // events of other components land in the step whose time window contains them
    expect(analyse!.events.map((e) => e.event.type)).toContain("drawing.loaded");
    expect(analyse!.toolCalls).toBe(1);
    expect(read!.modelCalls).toBe(1);
    expect(read!.cost).toBeCloseTo(0.012);
    expect(check!.pois.map((p) => p.item.kind)).toEqual(["mismatch"]);
    expect(plan!.pois.map((p) => p.item.kind)).toEqual(["assumption"]);
    expect(gate!.events.map((e) => e.event.type)).toEqual(
      expect.arrayContaining(["review.gate_opened", "review.gate_passed", "session.awaiting_review"]),
    );
    expect(build!.pois.map((p) => p.item.kind)).toEqual(["validation_failure"]);
    expect(build!.events.some((e) => e.event.type === "validation.completed")).toBe(true);
    expect(gate!.durationMs).toBeGreaterThan(50_000);
  });

  it("marks the gate step as waiting until the reviewer decided", () => {
    const steps = groupSteps(until(fixtureEvents(), "review.gate_opened"));
    const gate = steps[steps.length - 1]!;
    expect(gate.label).toBe("Gate 1 · spec review");
    expect(gate.status).toBe("waiting");
  });

  it("closes a step that never completed when the next one starts", () => {
    const stopped = fixtureEvents().filter(
      (e) => !(e.event.type === "agent.step_completed" && e.event.payload.step === "read"),
    );
    const steps = groupSteps(stopped);
    const read = steps.find((s) => s.name === "read")!;
    expect([read.status, read.errors]).toEqual(["failed", ["no completion event"]]);
    expect(steps.find((s) => s.name === "check")!.status).toBe("done");
    expect(steps.filter((s) => s.name === "review_gate_1")).toHaveLength(1); // resumed, not doubled
  });

  it("filters events by component, level and type", () => {
    const event = fixtureEvents()[0]!.event;
    const all = { components: new Set<string>(), levels: new Set<never>(), type: "" };
    expect(matches(event, all)).toBe(true);
    expect(matches(event, { ...all, components: new Set(["agent"]) })).toBe(false);
    expect(matches(event, { ...all, type: "session." })).toBe(true);
  });
});

describe("hostTimes", () => {
  it("corrects sandbox events whose clock drifted (Podman VM, T13)", () => {
    const sandbox = new Set(["sandbox", "drawing-mcp", "cad-mcp", "review-app"]);
    const drifted = fixtureEvents().map((s) => {
      const real = Date.parse(s.event.ts);
      const recv = new Date(real + 20).toISOString(); // log delivery takes a few ms
      const ts = sandbox.has(s.event.component) ? new Date(real - 600_000).toISOString() : s.event.ts;
      return { ...s, event: { ...s.event, ts, recv_ts: recv } };
    });
    expect(groupSteps(drifted).map((s) => [s.label, s.events.length])).toEqual(
      groupSteps(fixtureEvents()).map((s) => [s.label, s.events.length]),
    );
    const build = groupSteps(drifted).find((s) => s.name === "build")!;
    const validated = build.events.find((e) => e.event.type === "validation.completed")!;
    expect(build.hostTs.get(validated.id)).toBe("2026-10-17T08:01:39.020Z"); // + 20 ms delivery

  });
});
