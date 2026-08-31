import { beforeEach, describe, expect, it } from "vitest";
import {
  parseUnifiedDiff,
  parseSessionChangeSummary,
  renderFileChanges,
  renderFileChangeSummary,
  resetFileChangeCards,
  peekFileChangeCard,
  reserveFileChangeCard,
  validateFileChangeCardReservations,
  commitFileChangeCardReservationsNoFail,
  releaseFileChangeCardReservations,
  quiesceFileToolCachesForBlockedInstallNoDom,
} from "../../src/utils/render-file-changes";
import {
  _resetForTest as resetStreams,
  setTranscriptElement,
} from "../../src/utils/stream";

const DIFF = [
  "--- /dev/null",
  "+++ b/src/new.ts",
  "@@ -0,0 +1,2 @@",
  "+const created = true;",
  "+export default created;",
  "--- a/src/changed.ts",
  "+++ b/src/changed.ts",
  "@@ -1,2 +1,3 @@",
  " const before = true;",
  "-const oldValue = 1;",
  "+const newValue = 2;",
  "+const after = true;",
  "--- a/src/removed.ts",
  "+++ /dev/null",
  "@@ -1,1 +0,0 @@",
  "-const removed = true;",
  "--- a/src/fourth.ts",
  "+++ b/src/fourth.ts",
  "@@ -1,1 +1,1 @@",
  "-const oldFourth = true;",
  "+const newFourth = true;",
].join("\n");

beforeEach(() => {
  const transcript = document.querySelector<HTMLElement>("#transcript");
  transcript?.replaceChildren();
  resetFileChangeCards();
  resetStreams();
  if (transcript) setTranscriptElement(transcript);
});

describe("parseUnifiedDiff", () => {
  it("extracts paths, operations, and line counts for multiple files", () => {
    const files = parseUnifiedDiff(DIFF);

    expect(files).toHaveLength(4);
    expect(files[0]).toMatchObject({
      path: "src/new.ts",
      operation: "created",
      added: 2,
      removed: 0,
    });
    expect(files[1]).toMatchObject({
      path: "src/changed.ts",
      operation: "modified",
      added: 2,
      removed: 1,
    });
    expect(files[2]).toMatchObject({
      path: "src/removed.ts",
      operation: "deleted",
      added: 0,
      removed: 1,
    });
  });

  it("returns no files for non-unified diff text", () => {
    expect(parseUnifiedDiff("plain tool output")).toEqual([]);
  });

  it("parses rich session change summary lines", () => {
    const files = parseSessionChangeSummary([
      "  [dim]Modified[/dim]  [cyan]README.md[/cyan]  [#A6E22E]+140[/#A6E22E] [#FF4689]−40[/#FF4689]",
      "  [dim]Created[/dim]  [cyan]src/new.ts[/cyan]  [#A6E22E]+12[/#A6E22E] [#FF4689]−0[/#FF4689]",
    ].join("\n"));

    expect(files).toEqual([
      expect.objectContaining({ path: "README.md", operation: "modified", added: 140, removed: 40 }),
      expect.objectContaining({ path: "src/new.ts", operation: "created", added: 12, removed: 0 }),
    ]);
  });
});

describe("renderFileChanges", () => {
  it("merges repeated paths within a turn", () => {
    renderFileChanges("turn-1", "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1,1 +1,1 @@\n-old\n+new");
    renderFileChanges("turn-1", "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -2,1 +2,2 @@\n-old2\n+new2\n+new3");

    const card = document.querySelector<HTMLElement>(".file-change-card");
    const row = card?.querySelector<HTMLElement>(".file-change-row");

    expect(card).not.toBeNull();
    expect(card?.querySelectorAll(".file-change-row")).toHaveLength(1);
    expect(row?.querySelector(".file-change-added")?.textContent).toBe("+3");
    expect(row?.querySelector(".file-change-removed")?.textContent).toBe("-2");
  });

  it("keeps separate cards for separate turns", () => {
    renderFileChanges("turn-a", "--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1,1 +1,1 @@\n-old\n+new");
    renderFileChanges("turn-b", "--- a/src/b.ts\n+++ b/src/b.ts\n@@ -1,1 +1,1 @@\n-old\n+new");

    expect(document.querySelectorAll(".file-change-card")).toHaveLength(2);
  });

  it("renders session change summaries as a compact file card", () => {
    const rendered = renderFileChangeSummary("summary-1", [
      "  [dim]Modified[/dim]  [cyan]README.md[/cyan]  [#A6E22E]+140[/#A6E22E] [#FF4689]−40[/#FF4689]",
      "  [dim]Modified[/dim]  [cyan]AGENTS.md[/cyan]  [#A6E22E]+122[/#A6E22E] [#FF4689]−10[/#FF4689]",
    ].join("\n"));

    const card = document.querySelector<HTMLElement>(".file-change-card");

    expect(rendered).toBe(true);
    expect(card).not.toBeNull();
    expect(card?.querySelectorAll(".file-change-row")).toHaveLength(2);
    expect(card?.textContent).toContain("README.md");
    expect(card?.textContent).toContain("AGENTS.md");
    expect(card?.textContent).not.toContain("[dim]");
    expect(card?.querySelector(".file-change-detail")).toBeNull();
  });


  it("renders a compact summary header and file-icon rows", () => {
    renderFileChanges("turn-reference-card", DIFF);

    const card = document.querySelector<HTMLElement>(".file-change-card");
    const header = card?.querySelector<HTMLElement>(".file-change-header");
    const firstRow = card?.querySelector<HTMLElement>(".file-change-row");

    expect(header?.querySelector(".file-change-summary-icon svg")).not.toBeNull();
    expect(header?.querySelector(".file-change-title")?.textContent).toBe("4 files changed");
    expect(header?.querySelector(".file-change-stats")?.textContent).toContain("+5");
    expect(header?.querySelector(".file-change-stats")?.textContent).toContain("-3");
    expect(card?.querySelector(".file-change-operation")).toBeNull();
    expect(card?.querySelectorAll(".file-change-file-icon svg")).toHaveLength(3);
    expect(firstRow?.dataset.operation).toBe("created");
    expect(firstRow?.querySelector(".file-change-row-stats .file-change-added")?.textContent).toBe("+2");
    expect(firstRow?.querySelector(".file-change-row-stats .file-change-removed")?.textContent).toBe("-0");
  });
  it("previews three files and expands to the complete list", () => {
    renderFileChanges("turn-2", DIFF);

    const card = document.querySelector<HTMLElement>(".file-change-card");
    const expand = card?.querySelector<HTMLButtonElement>(".file-change-expand");

    expect(card?.querySelectorAll(".file-change-row")).toHaveLength(3);
    expect(expand).not.toBeNull();
    expect(expand?.getAttribute("aria-expanded")).toBe("false");

    expand?.click();

    const expanded = card?.querySelector<HTMLButtonElement>(".file-change-expand");
    expect(card?.querySelectorAll(".file-change-row")).toHaveLength(4);
    expect(expanded?.getAttribute("aria-expanded")).toBe("true");
  });
});


describe("historical file change isolation", () => {
  it("merges cumulative source text only in the supplied local cache", async () => {
    const {
      createHistoricalFileChangeContext,
      renderHistoricalFileChanges,
    } = await import("../../src/utils/render-file-changes");
    const root = document.createDocumentFragment();
    const context = createHistoricalFileChangeContext(root);
    const first = "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1,1 +1,1 @@\n-old\n+new";
    const cumulative = `${first}\n@@ -3,1 +3,2 @@\n-old2\n+new2\n+new3`;

    expect(renderHistoricalFileChanges(context, "turn-1", first, "source-1"))
      .toBe(true);
    expect(renderHistoricalFileChanges(context, "turn-1", cumulative, "source-1"))
      .toBe(true);

    expect(root.querySelectorAll(".file-change-card")).toHaveLength(1);
    expect(root.querySelector(".file-change-added")?.textContent).toBe("+3");
    expect(root.querySelector(".file-change-removed")?.textContent).toBe("-2");
    expect(document.querySelector("#transcript .file-change-card")).toBeNull();

    renderFileChanges("turn-1", first, "source-1");
    expect(document.querySelectorAll("#transcript .file-change-card")).toHaveLength(1);
    expect(context.cards.get("turn-1")?.card.isConnected).toBe(false);
  });
});


describe("file change card reservations", () => {
  it("reserves without changing production state and commits a prebuilt next state", async () => {
    const first = "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new";
    renderFileChanges("turn-1", first, "source-1");
    const expected = peekFileChangeCard("turn-1");
    expect(expected?.card.isConnected).toBe(true);

    const { createHistoricalFileChangeContext, renderHistoricalFileChanges } = await import(
      "../../src/utils/render-file-changes"
    );
    const detachedRoot = document.createDocumentFragment();
    const context = createHistoricalFileChangeContext(detachedRoot);
    renderHistoricalFileChanges(
      context,
      "turn-1",
      "--- a/src/new.ts\n+++ b/src/new.ts\n@@ -1 +1 @@\n-before\n+after",
      "source-next",
    );
    const next = context.cards.get("turn-1")!;
    const reservation = reserveFileChangeCard("turn-1", expected!, next);

    expect(reservation).not.toBeNull();
    expect(peekFileChangeCard("turn-1")).toEqual(expected);
    expect(reserveFileChangeCard("turn-1", expected!, null)).toBeNull();
    expect(validateFileChangeCardReservations([reservation!])).toBe(true);

    commitFileChangeCardReservationsNoFail([reservation!]);

    const committed = peekFileChangeCard("turn-1");
    expect(committed?.card).toBe(next.card);
    expect(committed?.generation).toBeGreaterThan(expected!.generation);
  });

  it("releases locks and invalidates old reservations on reset", () => {
    const diff = "--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b";
    renderFileChanges("turn-1", diff);
    const expected = peekFileChangeCard("turn-1")!;
    const released = reserveFileChangeCard("turn-1", expected, null)!;
    releaseFileChangeCardReservations([released]);
    const reacquired = reserveFileChangeCard("turn-1", expected, null);
    expect(reacquired).not.toBeNull();
    releaseFileChangeCardReservations([reacquired!]);
    releaseFileChangeCardReservations([
      reserveFileChangeCard("missing", null, null)!,
    ]);

    const stale = reserveFileChangeCard("turn-1", expected, null)!;
    resetFileChangeCards();

    expect(validateFileChangeCardReservations([stale])).toBe(false);
    expect(peekFileChangeCard("turn-1")).toBeNull();
  });

});

describe("blocked file cache quiesce", () => {
  it("invalidates production ownership without removing attached cards", () => {
    const diff = "--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b";
    renderFileChanges("turn-blocked", diff);
    const card = peekFileChangeCard("turn-blocked")!.card;

    quiesceFileToolCachesForBlockedInstallNoDom();

    expect(card.isConnected).toBe(true);
    expect(peekFileChangeCard("turn-blocked")).toBeNull();
  });
});
