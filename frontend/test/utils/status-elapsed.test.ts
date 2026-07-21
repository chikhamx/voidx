// @ts-nocheck
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { handleStatusItem } from "../../src/utils/render";
import { setTranscriptElement } from "../../src/utils/stream";

setTranscriptElement(document.querySelector("#transcript"));

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-07-20T10:00:00Z"));
  document.querySelector("#transcript").innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("status item elapsed time", () => {
  it("shows a ticking elapsed counter while running", () => {
    handleStatusItem("item.started", "s1", { status_id: "s1", label: "Analyzing" });
    const elapsed = document.querySelector("[data-status-item-id='s1'] .status-elapsed");
    expect(elapsed).not.toBeNull();
    expect(elapsed.textContent).toBe("0s");

    vi.advanceTimersByTime(12_000);
    expect(elapsed.textContent).toBe("12s");
  });

  it("freezes the elapsed time on completion", () => {
    handleStatusItem("item.started", "s2", { status_id: "s2", label: "Working" });
    vi.advanceTimersByTime(7_000);
    handleStatusItem("item.completed", "s2", { status_id: "s2", ok: true });
    const elapsed = document.querySelector("[data-status-item-id='s2'] .status-elapsed");
    expect(elapsed.textContent).toBe("7s");
    vi.advanceTimersByTime(5_000);
    expect(elapsed.textContent).toBe("7s");
  });

  it("formats minutes for long-running statuses", () => {
    handleStatusItem("item.started", "s3", { status_id: "s3", label: "Working" });
    vi.advanceTimersByTime(95_000);
    const elapsed = document.querySelector("[data-status-item-id='s3'] .status-elapsed");
    expect(elapsed.textContent).toBe("1m 35s");
  });

  it("still updates detail on completion", () => {
    handleStatusItem("item.started", "s4", { status_id: "s4", label: "Working", detail: "step 1" });
    handleStatusItem("item.completed", "s4", { status_id: "s4", ok: true, detail: "done" });
    const detail = document.querySelector("[data-status-item-id='s4'] .status-detail");
    expect(detail.textContent).toBe("done");
  });
});
