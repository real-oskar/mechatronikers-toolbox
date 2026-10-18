import { describe, expect, it } from "vitest";

import type { StoredEvent } from "../api/types";
import { aborted, CAP, fixtureEvents, gaveUpRun, stoppedRun, unisolated, until } from "../test/fixture";
import { PHASE_LABELS, runStatus } from "./status";

const status = (events: StoredEvent[]) => runStatus(events.map((e) => e.event));

describe("runStatus", () => {
  it("is valid at the end of the run (D58); without gate 2 it is not released (#152)", () => {
    const s = status(fixtureEvents());
    expect(s.phase).toBe("valid");
    expect(s.valid).toBe(true);
    expect(s.latestReport?.build_id).toBe("build-fx2");
    expect(s.partName).toBe("Fuss");
    expect(s.cost).toBeCloseTo(0.042);
    expect(s.sessionState).toBe("running");
  });

  it("follows the phases like the run table does", () => {
    const events = fixtureEvents();
    expect(status(until(events, "session.started")).phase).toBe("provisioning");
    expect(status(until(events, "agent.step_started", "read")).phase).toBe("analysing");
    expect(status(until(events, "agent.run_finished")).phase).toBe("waiting_gate_1");
    const fixing = status(until(events, "agent.step_started", "fix"));
    expect([fixing.phase, fixing.fixRound, fixing.maxIterations]).toEqual(["fixing", 1, 3]);
    expect(fixing.valid).toBe(false);
    expect(fixing.latestReport?.passed).toBe(false);
  });

  it("reports a give-up with the agent's last messages", () => {
    const events = fixtureEvents();
    const last = events[events.length - 1]!;
    const gaveUp = [
      ...events.slice(0, -1),
      { ...last, event: { ...last.event, payload: { ...last.event.payload, status: "build_failed" } } },
    ];
    const s = status(gaveUp);
    expect([s.phase, s.valid, s.gaveUp]).toEqual(["gave_up", false, true]);
    expect(s.lastFailures).toEqual(["added h1 rolled back: removed no material"]);
  });

  it("says why a run gave up and what its last build could not add (#92)", () => {
    for (const end of ["awaiting_review", "build_failed", "aborted"]) {
      const s = status(gaveUpRun(end, CAP));
      expect([s.stopReason, s.maxRounds, s.failedFeatures]).toEqual(["cap", 3, ["h1"]]);
    }
    expect(status(gaveUpRun("aborted", { ...CAP, reason: "plateau" })).stopReason).toBe("plateau");
    const failed = status(stoppedRun("error", "fix: the model answered without JSON"));
    expect([failed.phase, failed.stopReason, failed.stopError]).toEqual(["failed", "error", "fix: the model answered without JSON"]);
    expect(status(stoppedRun("budget_exceeded", "budget")).stopReason).toBe("budget");
    // a valid run, and a retry after the give-up: nothing stopped
    expect(status(fixtureEvents()).stopReason).toBeNull();
    const retried = gaveUpRun("awaiting_review", CAP);
    const fix = { ...retried[0]!, id: 999, event: { ...until(fixtureEvents(), "agent.step_started", "fix").at(-1)!.event } };
    expect(status([...retried, fix]).stopReason).toBeNull();
  });

  it("follows a circuit run's own steps (#110)", () => {
    const events = fixtureEvents().map((e) =>
      e.event.type === "agent.run_started" ? { ...e, event: { ...e.event, payload: { ...e.event.payload, kind: "circuit" } } } : e,
    );
    const at = (step: string) => {
      const until = events.findIndex((e) => e.event.type === "agent.step_started");
      const template = events[until]!;
      return status([...events.slice(0, until), { ...template, event: { ...template.event, payload: { step } } }]);
    };
    expect([at("check").kind, at("check").phase]).toEqual(["circuit", "checking"]);
    expect(at("export").phase).toBe("exporting");
    expect(at("simulate").phase).toBe("simulating");
    expect(at("review_gate_1").phase).toBe("waiting_gate_1");
    expect(status(fixtureEvents()).kind).toBe("part");
  });

  it("ends an aborted run instead of showing it as building (#65)", () => {
    const s = status(aborted());
    expect([s.phase, s.valid, s.gaveUp]).toEqual(["aborted", false, false]);
    expect(PHASE_LABELS[s.phase]).toBe("aborted");
  });

  it("counts the builds that ran without sandbox isolation (D95)", () => {
    expect(status(fixtureEvents()).unisolatedBuilds).toBe(0);
    expect(status(unisolated(fixtureEvents())).unisolatedBuilds).toBe(1);
  });
});
