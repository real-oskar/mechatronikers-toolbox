import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { gate, storedFor } from "../test/gates";
import { renderIn } from "../test/render";
import { InspectionView, parseValue } from "./InspectionView";

vi.mock("./ModelViewer", () => ({ ModelViewer: () => <div data-testid="model" /> }));

afterEach(() => vi.restoreAllMocks());

describe("InspectionView", () => {
  it("lists the characteristics with balloons and keeps the update run off without changes", () => {
    const g = gate();
    renderIn(<InspectionView gate={g} items={storedFor(g)} reviewer="Anna" onChanged={vi.fn()} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1]!.textContent)).toEqual([
      "100",
      "Ø57Printed Ø57, circle measures Ø56.4",
      "4 circles Ø5.5, plan has 2fixture",
    ]);
    expect(screen.getByRole("button", { name: "Start update run (0)" })).toBeDisabled();
  });

  it("enables the update run once the reviewer added a point and sends the stored points", async () => {
    const g = gate();
    const items = storedFor(g);
    items.push({ ...items[0]!, item: { ...items[0]!.item, id: "poi-1", kind: "user_note", message: "chamfer missing" }, origin: "reviewer", proposed_value: 2 });
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ ...g, open: false });
    renderIn(<InspectionView gate={g} items={items} reviewer="Anna" onChanged={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Start update run (1)" }));
    await user.click(screen.getByRole("button", { name: "Start update run" }));
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({ action: "update", update: expect.objectContaining({ revision: 1, items: [items[2]] }) }),
    );
  });

  it("records a confirmed finding and a changed printed value", async () => {
    const g = gate();
    const act = vi.spyOn(api, "act").mockResolvedValue(storedFor(g)[0]!);
    const add = vi.spyOn(api, "addItem").mockResolvedValue(storedFor(g)[0]!);
    const onChanged = vi.fn();
    renderIn(<InspectionView gate={g} items={storedFor(g)} reviewer="Anna" onChanged={onChanged} />, "de");
    const user = userEvent.setup();
    const [plain, finding] = screen.getAllByRole("row").slice(1);
    await user.click(within(finding!).getByRole("button", { name: "Bestätigen" }));
    expect(act).toHaveBeenCalledWith("rev1-r-1", expect.objectContaining({ action: "approved", reviewer: "Anna" }));
    await user.click(within(plain!).getByRole("button", { name: "Wert ändern" }));
    await user.type(within(plain!).getByLabelText("Neuer Wert"), "99,5");
    await user.click(within(plain!).getByRole("button", { name: "Speichern" }));
    expect(add).toHaveBeenCalledWith(
      expect.objectContaining({ proposed_value: 99.5, dimension_ids: ["d-100"], revision: 1 }),
    );
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("asks before finishing and names the points that stay open (D70, #152)", async () => {
    const g = gate();
    const items = storedFor(g);
    items[1] = { ...items[1]!, item: { ...items[1]!.item, severity: "blocking" } };
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ ...g, open: false });
    renderIn(<InspectionView gate={g} items={items} reviewer="Anna" onChanged={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Finish inspection" }));
    expect(decide).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Finish the inspection?" });
    expect(within(dialog).getByText("2 points stay open as review items.")).toBeInTheDocument();
    const open = within(dialog).getAllByRole("listitem").map((li) => li.textContent);
    expect(open).toEqual(["2 Ø57 (check)", "3 4 circles Ø5.5, plan has 2 (check, must fix)"]);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(decide).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Finish inspection" }));
    await user.click(within(screen.getByRole("dialog", { name: "Finish the inspection?" })).getByRole("button", { name: "Finish inspection" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ action: "finish", reviewer: "Anna" }));
  });

  it("finishes at once with nothing open, and not while changes are unsent", async () => {
    const g = gate();
    const decided = storedFor(g).map((s) => ({ ...s, item: { ...s.item, status: "approved" } }));
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ ...g, open: false });
    const { unmount } = renderIn(<InspectionView gate={g} items={decided} reviewer="Anna" onChanged={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Finish inspection" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ action: "finish" }));
    unmount();
    const items = storedFor(g);
    items.push({ ...items[0]!, item: { ...items[0]!.item, id: "poi-1", kind: "user_note", message: "chamfer missing" }, origin: "reviewer", proposed_value: 2 });
    renderIn(<InspectionView gate={g} items={items} reviewer="Anna" onChanged={vi.fn()} />, "de");
    expect(screen.getByRole("button", { name: "Prüfung abschließen" })).toBeDisabled();
    expect(screen.getByText(/Senden Sie Ihre Änderungen zuerst mit dem Änderungslauf/)).toBeInTheDocument();
  });

  it("keeps Finish after confirming a finding, but not after a changed value (#152)", async () => {
    const g = gate();
    const confirmed = storedFor(g);
    const approved = { at: "2026-10-17T08:05:00Z", by: "Anna", action: "approved" as const, old_value: null, new_value: null, comment: null };
    confirmed[0] = { ...confirmed[0]!, item: { ...confirmed[0]!.item, status: "approved", history: [approved] } };
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ ...g, open: false });
    const { unmount } = renderIn(<InspectionView gate={g} items={confirmed} reviewer="Anna" onChanged={vi.fn()} />);
    const user = userEvent.setup();
    const finish = screen.getByRole("button", { name: "Finish inspection" });
    expect(finish).toBeEnabled(); // confirming is recorded at once, it needs no update run
    expect(screen.getByRole("button", { name: "Start update run (1)" })).toBeEnabled();
    await user.click(finish);
    const dialog = screen.getByRole("dialog", { name: "Finish the inspection?" });
    expect(within(dialog).getByText("1 point stays open as a review item.")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["3 4 circles Ø5.5, plan has 2 (check)"]);
    await user.click(within(dialog).getByRole("button", { name: "Finish inspection" }));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ action: "finish" }));
    unmount();
    const edited = storedFor(g);
    const change = { ...approved, action: "edited" as const, new_value: 57.5 };
    edited[0] = { ...edited[0]!, item: { ...edited[0]!.item, status: "edited", history: [change] }, proposed_value: 57.5 };
    renderIn(<InspectionView gate={g} items={edited} reviewer="Anna" onChanged={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Finish inspection" })).toBeDisabled(); // needs a rebuild
    expect(screen.getByText(/Send your changes with the update run first/)).toBeInTheDocument();
  });

  it("reads German and English decimals", () => {
    expect([parseValue("6,5"), parseValue("6.5"), parseValue("M5")]).toEqual([6.5, 6.5, "M5"]);
  });
});
