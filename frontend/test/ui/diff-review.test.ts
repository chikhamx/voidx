// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  renderDiffReview,
  setHunkDecision,
  onHunkDecision,
  onApplyDiff,
  onGenerateDiff,
  showDiffEmpty,
  _resetForTest,
} from "../../src/ui/diff-review";

beforeEach(() => {
  _resetForTest();
  const pane = document.querySelector("#diff-pane");
  if (pane) pane.innerHTML = "";
});

const sampleSnapshot = {
  files: [
    {
      path: "src/main.js",
      old_path: "",
      new_path: "src/main.js",
      operation: "Update",
      added: 2,
      removed: 1,
      hunks: [
        {
          index: 0,
          old_start: 1,
          old_count: 2,
          new_start: 1,
          new_count: 3,
          section: "",
          lines: [
            { kind: "context", text: "line 1" },
            { kind: "add", text: "new line" },
            { kind: "remove", text: "old line" },
          ],
          decision: "pending",
        },
      ],
    },
  ],
};

describe("renderDiffReview", () => {
  it("renders file header with path and stats", () => {
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    expect(pane.textContent).toContain("src/main.js");
    expect(pane.textContent).toContain("+2");
    expect(pane.textContent).toContain("-1");
  });

  it("renders hunk lines with add/del/context classes", () => {
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const addLines = pane.querySelectorAll(".diff-line-add");
    const delLines = pane.querySelectorAll(".diff-line-del");
    const ctxLines = pane.querySelectorAll(".diff-line-context");

    expect(addLines).toHaveLength(1);
    expect(delLines).toHaveLength(1);
    expect(ctxLines).toHaveLength(1);
  });

  it("renders decision buttons for each hunk", () => {
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const approveBtn = pane.querySelector('[data-decision="approved"]');
    const rejectBtn = pane.querySelector('[data-decision="rejected"]');

    expect(approveBtn).not.toBeNull();
    expect(rejectBtn).not.toBeNull();
  });

  it("renders apply button", () => {
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const applyBtn = pane.querySelector(".vx-diff-apply");
    expect(applyBtn).not.toBeNull();
  });

  it("handles empty snapshot", () => {
    renderDiffReview("r1", { files: [] });
    const pane = document.querySelector("#diff-pane");
    expect(pane.textContent).toContain("No changes");
  });
});

describe("setHunkDecision", () => {
  it("updates hunk decision state in UI", () => {
    renderDiffReview("r1", sampleSnapshot);

    setHunkDecision("src/main.js", 0, "approved", { total_hunks: 1, approved: 1, rejected: 0, pending: 0 });

    const pane = document.querySelector("#diff-pane");
    const hunk = pane.querySelector('[data-hunk-index="0"]');
    expect(hunk.classList.contains("decision-approved")).toBe(true);
  });

  it("updates summary display", () => {
    renderDiffReview("r1", sampleSnapshot);

    setHunkDecision("src/main.js", 0, "rejected", { total_hunks: 1, approved: 0, rejected: 1, pending: 0 });

    const pane = document.querySelector("#diff-pane");
    expect(pane.textContent).toContain("1/1 rejected");
  });
});

describe("onHunkDecision", () => {
  it("calls callback when approve button clicked", () => {
    const cb = vi.fn();
    onHunkDecision(cb);
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const approveBtn = pane.querySelector('[data-decision="approved"]');
    approveBtn.click();

    expect(cb).toHaveBeenCalledWith("r1", "src/main.js", 0, "approved");
  });

  it("calls callback when reject button clicked", () => {
    const cb = vi.fn();
    onHunkDecision(cb);
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const rejectBtn = pane.querySelector('[data-decision="rejected"]');
    rejectBtn.click();

    expect(cb).toHaveBeenCalledWith("r1", "src/main.js", 0, "rejected");
  });
});

describe("onApplyDiff", () => {
  it("calls callback when apply button clicked", () => {
    const cb = vi.fn();
    onApplyDiff(cb);
    renderDiffReview("r1", sampleSnapshot);

    const pane = document.querySelector("#diff-pane");
    const applyBtn = pane.querySelector(".vx-diff-apply");
    applyBtn.click();

    expect(cb).toHaveBeenCalledWith("r1");
  });
});

describe("showDiffEmpty", () => {
  it("renders generate button when diff pane is empty", () => {
    showDiffEmpty();

    const pane = document.querySelector("#diff-pane");
    const btn = pane.querySelector(".vx-diff-generate");
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("Generate");
  });

  it("calls onGenerateDiff callback when generate button clicked", () => {
    const cb = vi.fn();
    onGenerateDiff(cb);
    showDiffEmpty();

    const pane = document.querySelector("#diff-pane");
    const btn = pane.querySelector(".vx-diff-generate");
    btn.click();

    expect(cb).toHaveBeenCalled();
  });
});