import { beforeEach, describe, expect, it } from "vitest";
import {
  parseUnifiedDiff,
  renderFileChanges,
  resetFileChangeCards,
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
