import { screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import type { StoredEvent } from "../api/types";
import { ConfigProvider, parseConfig } from "../config";
import { fixtureEvents } from "../test/fixture";
import { renderIn } from "../test/render";
import { KindPanels, kinds, runKind } from "./index";

vi.mock("../components/ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

const started = (payload: Record<string, unknown>): StoredEvent => ({
  id: 1,
  event: { ts: "2026-10-17T08:00:00.000000Z", seq: 1, component: "agent", type: "agent.run_started", level: "info", payload },
});

const panels = (kind: string, events = fixtureEvents()) => (
  <KindPanels kind={kind} events={events} gates={[]} items={[]} connected reload={vi.fn()} />
);

// Lazy chunks: import them up front so a slow machine does not hit findBy's timeout (App.test).
const LOADED = { timeout: 10_000 };
beforeAll(async () => {
  await Promise.all([kinds.part(), kinds.circuit()]);
}, 60_000);

describe("run-kind registry", () => {
  it("reads the kind from the first run start; runs without one are parts", () => {
    expect(runKind([])).toBe("part");
    expect(runKind(fixtureEvents())).toBe("part");
    expect(runKind([started({ drawing: "amp_sch.pdf", kind: "circuit" })])).toBe("circuit");
    expect(runKind([started({ run_kind: "circuit" })])).toBe("circuit");
    expect(runKind([started({ kind: 7 })])).toBe("part");
    // a later run start (e.g. printed by a build script) does not switch the panels
    expect(runKind([...fixtureEvents(), started({ kind: "circuit" })])).toBe("part");
  });

  it("loads today's panels for a part", async () => {
    renderIn(panels("part"));
    expect(await screen.findByRole("navigation", { name: "Routing sheet" }, LOADED)).toBeInTheDocument();
  });

  it("loads the circuit panels for a circuit run", async () => {
    renderIn(panels("circuit", [started({ drawing: "amp_sch.pdf", kind: "circuit" })]), "de");
    expect(await screen.findByRole("heading", { name: "amp_sch" }, LOADED)).toBeInTheDocument();
    expect(screen.getByText("Zeichnung: amp_sch.pdf")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Bauteile und Netze/ })).toBeDisabled(); // nothing read yet
    expect(screen.getByText("Die Regeln werden nach dem Lesen und noch einmal nach Gate 1 geprüft.")).toBeInTheDocument();
  });

  it("names a kind it does not know instead of guessing", () => {
    const hub = parseConfig({ mode: "hub", api: { runs: "/api/runs" }, features: { table: true } });
    const { unmount } = renderIn(<ConfigProvider value={hub}>{panels("pcb")}</ConfigProvider>);
    expect(screen.getByRole("heading", { name: "Unknown run kind" })).toBeInTheDocument();
    expect(screen.getByText(/of kind “pcb”, which this review UI cannot show/)).toBeInTheDocument();
    unmount();
    renderIn(panels("constructor")); // not a registry entry, even though every object has one
    expect(screen.getByRole("heading", { name: "Unknown run kind" })).toBeInTheDocument();
  });
});
