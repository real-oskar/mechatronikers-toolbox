import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConfigProvider, parseConfig } from "../config";
import { fixtureEvents } from "../test/fixture";
import { event, exportOf, revisionStarted, stored } from "../test/exports";
import { renderIn } from "../test/render";
import { Downloads, FinishSession } from "./Downloads";
import { RunViewContent } from "./RunView";

vi.mock("./ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

const twoRevisions = () =>
  stored([...exportOf("build-bbbbbb"), revisionStarted(2), ...exportOf("build-cccccc", ["step-ap242", "stl"])]);

describe("Downloads (#52)", () => {
  it("lists the exported files of the latest revision with sizes, and a ZIP of all", () => {
    renderIn(<Downloads events={stored(exportOf("build-bbbbbb"))} />);
    const panel = screen.getByRole("region", { name: "Downloads" });
    expect(within(panel).getByText(/Revision 1, exported from build-bbbbbb/)).toBeInTheDocument();
    const rows = within(panel).getAllByRole("row").map((r) => r.textContent);
    expect(rows).toEqual([
      "STEP AP242Fuss.ap242.step50.8 KB",
      "STEP AP214Fuss.ap214.step47.7 KB",
      "STLFuss.stl2.2 MB",
      "Features (JSON)Fuss.features.json4.1 KB",
      "Review list (JSON)Fuss.review.json900 B",
    ]);
    const step = within(panel).getByRole("link", { name: "Fuss.ap242.step" });
    // the review API records file, build and revision in `artifact.downloaded`
    expect(step).toHaveAttribute("href", "/api/review/artifacts/build-bbbbbb/Fuss.ap242.step?revision=1");
    expect(step).toHaveAttribute("download", "Fuss.ap242.step");
    const zip = within(panel).getByRole("link", { name: "Download all as ZIP (5 files, 2.3 MB)" });
    expect(zip).toHaveAttribute("href", "/api/review/artifacts/build-bbbbbb.zip?revision=1");
  });

  it("switches between revisions", () => {
    renderIn(<Downloads events={twoRevisions()} />);
    expect(screen.getByRole("button", { name: "Rev 2" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Fuss.stl" })).toHaveAttribute(
      "href",
      "/api/review/artifacts/build-cccccc/Fuss.stl?revision=2",
    );
    fireEvent.click(screen.getByRole("button", { name: "Rev 1" }));
    expect(screen.getAllByRole("row")).toHaveLength(5);
    expect(screen.getByRole("link", { name: "Fuss.stl" })).toHaveAttribute(
      "href",
      "/api/review/artifacts/build-bbbbbb/Fuss.stl?revision=1",
    );
    expect(screen.getByRole("link", { name: /ZIP/ })).toHaveAttribute("href", "/api/review/artifacts/build-bbbbbb.zip?revision=1");
  });

  it("speaks German and shows nothing before the first export", () => {
    const { unmount } = renderIn(<Downloads events={stored(exportOf("build-bbbbbb"))} />, "de");
    expect(screen.getByText("Prüfliste (JSON)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Alles als ZIP laden (5 Dateien, 2,3 MB)" })).toBeInTheDocument();
    expect(screen.getByText("50,8 KB")).toBeInTheDocument();
    unmount();
    const { container } = renderIn(<Downloads events={stored([event("agent", "agent.run_started", {})])} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is part of the run view once the run exported", () => {
    const events = fixtureEvents();
    renderIn(<RunViewContent events={[...events, ...stored(exportOf("build-bbbbbb"), events.length + 1)]} gates={[]} items={[]} connected reload={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Downloads" })).toBeInTheDocument();
  });
});

describe("FinishSession (#52)", () => {
  const sandbox = parseConfig({
    mode: "sandbox",
    api: { run: "/api", events: "/events" },
    links: { runs: "http://127.0.0.1:8765/" },
  });
  const events = stored(exportOf("build-bbbbbb"));

  it("sends the reviewer to the host's run table to finish and shelve", () => {
    renderIn(
      <ConfigProvider value={sandbox}>
        <FinishSession events={events} shelved={false} />
      </ConfigProvider>,
    );
    const link = screen.getByRole("link", { name: "Finish and shelve in the run table" });
    expect(link).toHaveAttribute("href", "http://127.0.0.1:8765/#ses-fixture0001");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("is hidden without a run table link and once shelved", () => {
    const { container, unmount } = renderIn(<FinishSession events={events} shelved={false} />); // no links
    expect(container).toBeEmptyDOMElement();
    unmount();
    const shelved = renderIn(
      <ConfigProvider value={sandbox}>
        <FinishSession events={events} shelved />
      </ConfigProvider>,
    );
    expect(shelved.container).toBeEmptyDOMElement();
  });

  it("speaks German", () => {
    renderIn(
      <ConfigProvider value={sandbox}>
        <FinishSession events={events} shelved={false} />
      </ConfigProvider>,
      "de",
    );
    expect(screen.getByRole("link", { name: "In der Laufübersicht beenden und archivieren" })).toBeInTheDocument();
  });
});
