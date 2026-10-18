import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunSummary } from "../api/types";
import { ConfigProvider, parseConfig } from "../config";
import { HUB_RUNS } from "../test/hubRuns";
import { renderIn } from "../test/render";
import { RunTable, RunTableView, runEnded } from "./RunTable";

const row = (over: Partial<RunSummary>): RunSummary => ({
  session_id: "ses-fixture0001", drawing: "Fuss_dwg.pdf", part_name: "Fuss", state: "running",
  phase: "valid", checks_passed: 3, checks_total: 3, open_pois: 2, revision: 1, fix_iterations: 1,
  max_fix_iterations: 3, model: "vendor/vision-model", cost: 0.042,
  started_at: "2026-10-17T08:00:00.000000Z", last_activity: "2026-10-17T08:02:26.100000Z",
  link: "http://127.0.0.1:40000/?t=tok", ...over,
}); // fmt: skip

describe("RunTableView", () => {
  it("shows each session with its phase and a link only while it runs", () => {
    const rows = [
      row({}),
      row({ session_id: "ses-fixing00001", phase: "fixing", fix_iterations: 2 }),
      row({ session_id: "ses-shelved0001", state: "shelved", phase: "shelved", link: null }),
      row({ session_id: "ses-aborted0001", phase: "aborted" }),
      row({
        session_id: "ses-gaveup00001", phase: "gave_up", stop_reason: "cap",
        failing_checks: ["V2.bbox_z"], failed_features: ["bore"],
      }),
    ];
    renderIn(<RunTableView rows={rows} now={Date.parse("2026-10-17T08:03:00Z")} />);
    const [, valid, fixing, shelved, abortedRow, gaveUp] = screen.getAllByRole("row");
    expect(within(valid!).getByText("valid, ready for inspection")).toBeInTheDocument();
    expect(within(valid!).getByRole("link", { name: "Open" })).toHaveAttribute("href", rows[0]!.link!);
    expect(within(fixing!).getByText("reworking (2 of 3)")).toBeInTheDocument();
    expect(within(shelved!).queryByRole("link")).toBeNull();
    expect(within(abortedRow!).getByText("aborted by the reviewer")).toHaveClass("status-nok");
    // #92: why it gave up and what failed in its last build
    expect(within(gaveUp!).getByText("the rework limit of 3 rounds was reached")).toBeInTheDocument();
    expect(within(gaveUp!).getByText("Failing checks: V2.bbox_z")).toBeInTheDocument();
    expect(within(gaveUp!).getByText("Features not built: bore")).toBeInTheDocument();
  });

  it("says how many points a released run left open, and never a negative signal (#152)", () => {
    const rows = [
      row({ phase: "released", open_pois: 4, state: "shelved", link: null }),
      row({ session_id: "ses-clean000001", phase: "released", open_pois: 0 }),
      row({ session_id: "ses-circuit0001", run_kind: "circuit", phase: "valid", open_pois: 1, last_activity: "2026-10-17T08:03:02Z" }),
    ];
    renderIn(<RunTableView rows={rows} now={Date.parse("2026-10-17T08:03:00Z")} />);
    const [, open, clean, circuit] = screen.getAllByRole("row");
    expect(within(open!).getByText("released after inspection · 4 open points")).toHaveClass("status-ok");
    expect(within(clean!).getByText("released after inspection")).toBeInTheDocument();
    expect(within(circuit!).getByText("netlists ready · 1 open point")).toBeInTheDocument();
    expect(within(circuit!).queryByText(/-\d/)).toBeNull();
  });

  it("speaks German", () => {
    const rows = [row({}), row({ session_id: "ses-aborted0001", phase: "aborted", cost: 0 })];
    renderIn(<RunTableView rows={rows} now={Date.parse("2026-10-17T08:03:00Z")} />, "de");
    expect(screen.getByText("gültig, bereit zur Prüfung")).toBeInTheDocument();
    expect(screen.getByText("vom Prüfer abgebrochen")).toBeInTheDocument();
    renderIn(<RunTableView rows={[row({ phase: "failed", stop_reason: "budget", cost: 0 })]} now={0} />, "de");
    expect(screen.getByText("das Modellbudget ist aufgebraucht")).toBeInTheDocument();
    expect(screen.getByText("0,042")).toBeInTheDocument();
  });

  it("flags runs whose builds ran without sandbox isolation (D95)", () => {
    renderIn(<RunTableView rows={[row({}), row({ session_id: "ses-unisol0001", unisolated_builds: 2 })]} now={0} />);
    const [, isolated, flagged] = screen.getAllByRole("row");
    expect(within(isolated!).queryByText(/without sandbox isolation/)).toBeNull();
    expect(within(flagged!).getByText("2 builds without sandbox isolation")).toHaveClass("status-nok");
  });
});

describe("RunTable in the hub: the overview of all runs (#75)", () => {
  const HUB = parseConfig({ mode: "hub", api: { runs: "/api/runs" }, features: { table: true } });

  /** The hub: `GET /api/runs` filtered by kind and phase, in pages of 4. */
  function hub() {
    const fetch = vi.fn(async (url: string) => {
      const query = new URL(url, "http://hub").searchParams;
      const rows = HUB_RUNS.filter(
        (r) => (!query.get("kind") || r.run_kind === query.get("kind")) && (!query.get("phase") || r.phase === query.get("phase")),
      );
      const offset = Number(query.get("cursor") ?? 0);
      const next = offset + 4 < rows.length ? String(offset + 4) : null;
      return new Response(JSON.stringify({ runs: rows.slice(offset, offset + 4), next_cursor: next }));
    });
    vi.stubGlobal("fetch", fetch);
    return () => fetch.mock.calls.map(([url]) => url);
  }

  const show = (lang: "en" | "de" = "en") =>
    renderIn(
      <ConfigProvider value={HUB}>
        <RunTable runs="/api/runs" />
      </ConfigProvider>,
      lang,
    );
  const planCells = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[1]!.textContent);

  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("lists part and circuit runs from every page, newest first, with links into the hub", async () => {
    const urls = hub();
    show();
    expect(await screen.findByText("ecc83")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "All runs" })).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(HUB_RUNS.length + 1);
    expect(urls().slice(0, 3)).toEqual(["/api/runs", "/api/runs?cursor=4", "/api/runs?cursor=8"]);
    const lm317 = screen.getByText("lm317_supply").closest("tr")!;
    expect(within(lm317).getByText("Circuit")).toBeInTheDocument();
    expect(within(lm317).getByText("waiting for circuit approval")).toHaveClass("status-check"); // #110
    const fuss = screen.getByText("Fuss").closest("tr")!;
    expect(within(fuss).getByText("Anna")).toBeInTheDocument(); // reviewer
    const open = within(fuss).getByRole("link", { name: "Open" });
    expect(open).toHaveAttribute("href", "/runs/run-fuss0001");
    fireEvent.click(open);
    expect(window.location.pathname).toBe("/runs/run-fuss0001"); // in the app, no reload
    expect(planCells()[0]).toContain("Dreistern"); // started last
    // why a run ended without a model (#92), as in the host's table
    const gaveUp = screen.getByText("Maschinenteil").closest("tr")!;
    expect(within(gaveUp).getByText("the rework limit of 3 rounds was reached")).toBeInTheDocument();
    expect(within(gaveUp).getByText("Failing checks: V2.bbox_z, V2.hole_count")).toBeInTheDocument();
    expect(within(screen.getByText("Befestigung").closest("tr")!).getByText("the model budget is used up")).toBeInTheDocument();
  });

  it("filters by kind and phase on the server and sorts by column", async () => {
    const urls = hub();
    show();
    await screen.findByText("ecc83");
    fireEvent.click(screen.getByRole("button", { name: "Circuit" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(4));
    expect(urls()).toContain("/api/runs?kind=circuit");
    expect(screen.getByText("3 runs")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Phase"), { target: { value: "valid" } });
    await waitFor(() => expect(planCells()).toEqual([expect.stringContaining("sallen_key")]));
    expect(urls()).toContain("/api/runs?kind=circuit&phase=valid");
    fireEvent.change(screen.getByLabelText("Phase"), { target: { value: "gave_up" } });
    expect(await screen.findByText("No run matches the filter.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    fireEvent.change(screen.getByLabelText("Phase"), { target: { value: "all" } });
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(HUB_RUNS.length + 1));
    const cost = screen.getByRole("button", { name: /Model cost/ });
    fireEvent.click(cost);
    expect(cost.closest("th")).toHaveAttribute("aria-sort", "descending");
    expect(planCells()[0]).toContain("Befestigung"); // 1.0 credits
    fireEvent.click(cost);
    expect(cost.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(planCells()[0]).toContain("Dreistern"); // 0 credits; ties keep the server's order
  });

  it("speaks German", async () => {
    hub();
    show("de");
    expect(await screen.findByRole("heading", { name: "Alle Läufe" })).toBeInTheDocument();
    expect(await screen.findByText("ecc83")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Auftrag/ })).toBeInTheDocument();
    expect(screen.getAllByText("Schaltung").length).toBeGreaterThan(0);
    expect(screen.getByRole("option", { name: "Alle Phasen" })).toBeInTheDocument();
  });

  it("refreshes one at a time, not while the tab is hidden, and says when only the first runs came", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    const answers: ((r: Response) => void)[] = [];
    const fetch = vi.fn(() => new Promise<Response>((resolve) => answers.push(resolve)));
    vi.stubGlobal("fetch", fetch);
    let hidden = false;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    let n = 0;
    const page = (cursor: string | null) =>
      new Response(JSON.stringify({ runs: [{ ...HUB_RUNS[0]!, run_id: `run-${n++}` }], next_cursor: cursor }));
    try {
      show();
      await act(async () => vi.advanceTimersByTimeAsync(10_000)); // the first page is still pending
      expect(fetch).toHaveBeenCalledTimes(1); // no overlapping refresh
      await act(async () => answers.shift()!(page(null)));
      expect(screen.getByText("1 run")).toBeInTheDocument();
      await act(async () => vi.advanceTimersByTimeAsync(3_000));
      expect(fetch).toHaveBeenCalledTimes(2); // the next one, POLL_MS after the first settled
      hidden = true;
      await act(async () => answers.shift()!(page(null)));
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(fetch).toHaveBeenCalledTimes(2); // hidden tab: no polling
      hidden = false;
      await act(async () => document.dispatchEvent(new Event("visibilitychange")));
      expect(fetch).toHaveBeenCalledTimes(3); // back on the tab: at once
      for (let i = 0; i < 10; i += 1) await act(async () => answers.shift()!(page("more")));
      expect(screen.getByText("first 10 runs")).toBeInTheDocument();
      expect(screen.getByText(/counts and sorting cover these only/)).toBeInTheDocument();
    } finally {
      Reflect.deleteProperty(document, "hidden");
      vi.useRealTimers();
    }
  });

  it("does not repeat the session id for a host run without a drawing", () => {
    renderIn(<RunTableView rows={[{ ...HUB_RUNS[0]!, plan: null, part_name: null, drawing: null, run_id: undefined }]} now={0} />);
    const [cell] = within(screen.getAllByRole("row")[1]!).getAllByRole("cell").slice(1, 2);
    expect(cell!.textContent).toBe("–ses-fuss0001");
  });
});

describe("RunTable on the host: finish and shelve (#52)", () => {
  const HOST = parseConfig({ mode: "host", api: { runs: "/sessions" }, features: { table: true, shelve: true } });
  const EXPIRES = "2026-11-16T08:05:00Z";

  /** `session watch`: `GET /sessions`, and `POST /sessions/<id>/shelve` that shelves the row. */
  function host(rows: RunSummary[], shelve: () => Response = () => new Response(JSON.stringify({}))) {
    let current = rows;
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const answer = shelve();
        if (answer.ok) {
          const id = url.split("/")[2];
          current = current.map((r) =>
            r.session_id === id ? { ...r, state: "shelved", phase: "shelved", link: null, expires_at: EXPIRES } : r,
          );
        }
        return answer;
      }
      return new Response(JSON.stringify(current));
    });
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }

  const show = (lang: "en" | "de" = "en", config = HOST) =>
    renderIn(
      <ConfigProvider value={config}>
        <RunTable runs="/sessions" />
      </ConfigProvider>,
      lang,
    );
  const ready = (over: Partial<RunSummary> = {}) => row({ retention_class: "standard", retention_days: 30, ...over });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("asks for confirmation, names the session and what happens, then shelves it", async () => {
    const fetch = host([ready()]);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Finish and shelve ses-fixture0001" }));
    const dialog = screen.getByRole("dialog", { name: "Finish and shelve this session?" });
    expect(within(dialog).getByText("Fuss")).toBeInTheDocument();
    expect(within(dialog).getByText(/session ses-fixture0001/)).toBeInTheDocument();
    expect(within(dialog).getByText(/event log are saved and verified/)).toBeInTheDocument();
    expect(within(dialog).getByText(/the review link stops working/)).toBeInTheDocument();
    expect(within(dialog).getByText(/kept for 30 days \(standard\), until about/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/not finished yet/)).toBeNull(); // a valid run has ended
    expect(fetch).toHaveBeenCalledTimes(1); // nothing sent before the OK

    fireEvent.click(within(dialog).getByRole("button", { name: "Finish and shelve" }));
    await waitFor(() => expect(dialog).not.toHaveAttribute("open"));
    const [url, init] = fetch.mock.calls.find(([, i]) => i?.method === "POST")!;
    expect(url).toBe("/sessions/ses-fixture0001/shelve");
    expect(init!.body).toBe(JSON.stringify({ confirmed: true }));
    expect(init!.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(init!.credentials).toBe("same-origin");
    // the table refreshes at once: the row is shelved, with its expiry, and no action left
    const shelved = await screen.findByText(/snapshot kept until/);
    const tr = shelved.closest("tr")!;
    expect(within(tr).getByText("archived")).toBeInTheDocument();
    expect(within(tr).queryByRole("button", { name: /Finish and shelve/ })).toBeNull();
    expect(within(tr).queryByRole("link", { name: "Open" })).toBeNull();
  });

  it("does nothing on cancel and shows why shelving failed", async () => {
    const fetch = host([ready({ phase: "building" })], () =>
      // a failed snapshot: the dialog shows the server's reason (#141 review)
      new Response(JSON.stringify({ detail: "shelving failed: snapshot check failed for session.tar.zst" }), { status: 500 }),
    );
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Finish and shelve ses-fixture0001" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("This run is not finished yet (building the model). Shelving stops it where it is.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(fetch.mock.calls.some(([, i]) => i?.method === "POST")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Finish and shelve ses-fixture0001" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Finish and shelve" }));
    expect(await within(dialog).findByText("Could not shelve: shelving failed: snapshot check failed for session.tar.zst")).toBeInTheDocument();
    expect(dialog).toHaveAttribute("open");
  });

  it("warns only while the run still works, also for the released phase of #155", () => {
    // `released` (a finished inspection, #155) is not a `Phase` yet; the check takes it already
    expect(["valid", "released", "gave_up", "aborted", "failed"].every(runEnded)).toBe(true);
    expect(["building", "fixing", "waiting_gate_1", "analysing"].some(runEnded)).toBe(false);
  });

  it("offers it only for sessions the host can shelve, and highlights the one a run view linked to", async () => {
    window.history.replaceState(null, "", "/#ses-running0001");
    host([
      ready({ session_id: "ses-running0001" }),
      ready({ session_id: "ses-shelved0001", state: "shelved", phase: "shelved", link: null, expires_at: EXPIRES }),
      ready({ session_id: "ses-snapshot001", state: "snapshotting", phase: "shelved", link: null }),
      ready({ session_id: "ses-expired0001", state: "expired", phase: "shelved", link: null }),
    ]);
    show();
    const running = (await screen.findByRole("button", { name: "Finish and shelve ses-running0001" })).closest("tr")!;
    expect(running).toHaveAttribute("aria-current", "true");
    expect(screen.getAllByRole("button", { name: /^Finish and shelve/ })).toHaveLength(1);
    expect(screen.getByText(/snapshot kept until/).closest("tr")).toHaveAttribute("id", "ses-shelved0001");
    expect(screen.getByText("saving the snapshot…")).toBeInTheDocument();
    expect(screen.getByText("snapshot expired")).toBeInTheDocument();
  });

  it("is not offered without the host's shelve route or in the hub", async () => {
    host([ready()]);
    show("en", parseConfig({ mode: "host", api: { runs: "/sessions" }, features: { table: true } }));
    expect(await screen.findByText("Fuss")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Finish and shelve/ })).toBeNull();
    renderIn(<RunTableView rows={[ready()]} now={0} hub />);
    expect(screen.queryByRole("button", { name: /Finish and shelve/ })).toBeNull();
  });

  it("speaks German", async () => {
    host([ready()]);
    show("de");
    fireEvent.click(await screen.findByRole("button", { name: "ses-fixture0001 beenden und archivieren" }));
    const dialog = screen.getByRole("dialog", { name: "Diese Sitzung beenden und archivieren?" });
    expect(within(dialog).getByText(/30 Tage aufbewahrt \(standard\)/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Beenden und archivieren" })).toBeInTheDocument();
  });
});
