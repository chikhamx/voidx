// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import { handleNotification } from "../../src/main";
import { appendNoticeItem } from "../../src/utils/render";

beforeEach(() => {
  document.querySelector(".notice-toast-region")?.remove();
});

describe("notice.set", () => {
  it("renders an info toast with the notice text", () => {
    handleNotification("notice.set", { text: "clangd ready" });
    const item = document.querySelector(".notice-item.notice-info");
    expect(item).not.toBeNull();
    expect(item.querySelector(".notice-text").textContent).toBe("clangd ready");
  });

  it("ignores empty notice text", () => {
    handleNotification("notice.set", { text: "" });
    expect(document.querySelector(".notice-item")).toBeNull();
  });
});

describe("appendNoticeItem info style", () => {
  it("uses an info icon instead of the error cross", () => {
    appendNoticeItem("n1", { style: "info", text: "fyi" });
    const item = document.querySelector(".notice-item.notice-info");
    expect(item).not.toBeNull();
    expect(item.querySelector(".notice-icon").textContent).toBe("i");
  });
});
