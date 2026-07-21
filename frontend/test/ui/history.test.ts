// @ts-nocheck
import { beforeEach, describe, it, expect } from "vitest";
import {
  pushHistory,
  historyPrev,
  historyNext,
  resetHistoryNavigation,
  _resetHistoryForTest,
  isHistoryBrowsing,
  HISTORY_LIMIT,
} from "../../src/ui/history";

beforeEach(() => {
  _resetHistoryForTest();
});

describe("pushHistory", () => {
  it("stores submitted entries newest-last", () => {
    pushHistory("first");
    pushHistory("second");
    expect(historyPrev("")).toBe("second");
    expect(historyPrev("second")).toBe("first");
  });

  it("ignores empty and whitespace-only entries", () => {
    pushHistory("");
    pushHistory("   ");
    expect(historyPrev("")).toBe(null);
  });

  it("dedupes adjacent duplicates", () => {
    pushHistory("same");
    pushHistory("same");
    expect(historyPrev("")).toBe("same");
    expect(historyPrev("same")).toBe(null);
  });

  it("keeps non-adjacent duplicates", () => {
    pushHistory("a");
    pushHistory("b");
    pushHistory("a");
    expect(historyPrev("")).toBe("a");
    expect(historyPrev("a")).toBe("b");
    expect(historyPrev("b")).toBe("a");
  });

  it("caps history at HISTORY_LIMIT", () => {
    for (let i = 0; i < HISTORY_LIMIT + 50; i += 1) {
      pushHistory(`entry-${i}`);
    }
    let count = 0;
    let current = "";
    let prev = historyPrev(current);
    while (prev !== null) {
      count += 1;
      current = prev;
      prev = historyPrev(current);
    }
    expect(count).toBe(HISTORY_LIMIT);
  });
});

describe("historyPrev/historyNext navigation", () => {
  beforeEach(() => {
    pushHistory("one");
    pushHistory("two");
    pushHistory("three");
  });

  it("walks back then forward, restoring the draft", () => {
    expect(historyPrev("my draft")).toBe("three");
    expect(historyPrev("three")).toBe("two");
    expect(historyNext("two")).toBe("three");
    expect(historyNext("three")).toBe("my draft");
  });

  it("returns null at the oldest entry and stays put", () => {
    expect(historyPrev("")).toBe("three");
    expect(historyPrev("three")).toBe("two");
    expect(historyPrev("two")).toBe("one");
    expect(historyPrev("one")).toBe(null);
    expect(historyPrev("one")).toBe(null);
  });

  it("returns null when moving next past the newest entry", () => {
    expect(historyNext("")).toBe(null);
  });

  it("resets navigation on new push", () => {
    expect(historyPrev("draft")).toBe("three");
    pushHistory("four");
    expect(historyPrev("new draft")).toBe("four");
    expect(historyNext("four")).toBe("new draft");
  });

  it("resetHistoryNavigation exits browsing so next prev starts newest", () => {
    expect(historyPrev("d1")).toBe("three");
    expect(historyPrev("three")).toBe("two");
    resetHistoryNavigation();
    expect(historyPrev("d2")).toBe("three");
  });
});

describe("isHistoryBrowsing", () => {
  it("is false initially, true while browsing, false after reset", () => {
    expect(isHistoryBrowsing()).toBe(false);
    pushHistory("x");
    expect(isHistoryBrowsing()).toBe(false);
    historyPrev("");
    expect(isHistoryBrowsing()).toBe(true);
    resetHistoryNavigation();
    expect(isHistoryBrowsing()).toBe(false);
  });

  it("becomes false after walking forward past newest", () => {
    pushHistory("x");
    historyPrev("");
    historyNext("x");
    expect(isHistoryBrowsing()).toBe(false);
  });
});
