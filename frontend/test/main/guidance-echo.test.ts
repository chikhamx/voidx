// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import { handleItem } from "../../src/main";

beforeEach(() => {
  document.querySelector("#transcript").innerHTML = "";
});

describe("guidance_preview items", () => {
  it("renders submitted guidance text as a guidance message", () => {
    handleItem("item.started", {
      kind: "guidance_preview",
      item_id: "g1",
      data: { text: "请加快速度", truncated: false },
    });
    const item = document.querySelector("#transcript .message-item.message-guidance");
    expect(item).not.toBeNull();
    expect(item.textContent).toContain("请加快速度");
  });

  it("ignores guidance_preview completion without duplicating content", () => {
    handleItem("item.started", {
      kind: "guidance_preview",
      item_id: "g2",
      data: { text: "注意边界条件" },
    });
    handleItem("item.completed", {
      kind: "guidance_preview",
      item_id: "g2-done",
      data: {},
    });
    const items = document.querySelectorAll("#transcript .message-guidance");
    expect(items).toHaveLength(1);
  });
});
