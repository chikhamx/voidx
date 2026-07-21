// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import {
  registerTextPaste,
  expandPasteTokens,
  clearPasteEntries,
  computeTextPasteDisplay,
  _pasteEntriesForTest,
} from "../../src/ui/paste";

beforeEach(() => {
  clearPasteEntries();
});

describe("computeTextPasteDisplay", () => {
  it("shows extra line count for multi-line text", () => {
    expect(computeTextPasteDisplay(1, "a\nb\nc")).toBe("[Pasted text #1 +2 lines]");
  });

  it("shows char count for single-line text", () => {
    expect(computeTextPasteDisplay(2, "hello")).toBe("[Pasted text #2 5 chars]");
  });
});

describe("registerTextPaste", () => {
  it("returns a placeholder token and records the expanded text", () => {
    const token = registerTextPaste("line1\nline2\nline3");
    expect(token).toBe("[Pasted text #1 +2 lines]");
    expect(_pasteEntriesForTest()).toHaveLength(1);
  });

  it("increments paste ids", () => {
    expect(registerTextPaste("a\nb")).toBe("[Pasted text #1 +1 lines]");
    expect(registerTextPaste("x\ny\nz")).toBe("[Pasted text #2 +2 lines]");
  });
});

describe("expandPasteTokens", () => {
  it("expands text tokens with <pasted> wrappers", () => {
    const token = registerTextPaste("alpha\nbeta");
    const expanded = expandPasteTokens(`请检查 ${token}`);
    expect(expanded).toBe("请检查 <pasted>\nalpha\nbeta\n</pasted>");
  });

  it("leaves unknown text untouched", () => {
    const t1 = registerTextPaste("one\ntwo");
    const input = `${t1} and [Pasted text #99 +5 lines]`;
    const out = expandPasteTokens(input);
    expect(out).toContain("<pasted>\none\ntwo\n</pasted>");
    expect(out).toContain("[Pasted text #99 +5 lines]");
  });

  it("returns the input unchanged when there are no entries", () => {
    expect(expandPasteTokens("plain text")).toBe("plain text");
  });
});

describe("clearPasteEntries", () => {
  it("drops all entries and resets the id counter", () => {
    registerTextPaste("a\nb");
    clearPasteEntries();
    expect(_pasteEntriesForTest()).toHaveLength(0);
    expect(registerTextPaste("c\nd")).toBe("[Pasted text #1 +1 lines]");
  });
});
