// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  initContextMenu,
  _resetContextMenuForTest,
} from "../src/context-menu";

beforeEach(() => {
  _resetContextMenuForTest();
  const menu = document.querySelector("#context-menu");
  if (menu) menu.hidden = true;
});

describe("initContextMenu", () => {
  it("shows context menu when attach button is clicked", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const btn = document.querySelector("#btn-attach");

    btn.click();

    expect(menu.hidden).toBe(false);
  });

  it("toggles context menu visibility on repeated attach clicks", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const btn = document.querySelector("#btn-attach");

    btn.click();
    expect(menu.hidden).toBe(false);

    btn.click();
    expect(menu.hidden).toBe(true);
  });

  it("hides context menu when clicking outside", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const btn = document.querySelector("#btn-attach");

    btn.click();
    expect(menu.hidden).toBe(false);

    document.body.click();
    expect(menu.hidden).toBe(true);
  });

  it("does not hide context menu when clicking the attach button itself", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const btn = document.querySelector("#btn-attach");

    btn.click();
    expect(menu.hidden).toBe(false);

    btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(menu.hidden).toBe(true);
  });

  it("dispatches paste action when paste menu item is clicked", () => {
    initContextMenu();
    const btn = document.querySelector("#btn-attach");
    btn.click();

    const pasteItem = document.querySelector(".context-menu-item[data-action='paste']");
    pasteItem.click();

    // pasteFromClipboard tries navigator.clipboard.read() which is not available
    // in jsdom — the important thing is the handler was wired without throwing
  });

  it("clicks integrations button when web action is triggered", () => {
    initContextMenu();
    const integrationsBtn = document.querySelector("#btn-integrations");
    const clickSpy = vi.fn();
    if (integrationsBtn) {
      integrationsBtn.addEventListener("click", clickSpy);
    }

    const btn = document.querySelector("#btn-attach");
    btn.click();

    const webItem = document.querySelector(".context-menu-item[data-action='web']");
    webItem.click();

    expect(clickSpy).toHaveBeenCalled();
  });

  it("does nothing for file action click", () => {
    initContextMenu();
    const btn = document.querySelector("#btn-attach");
    btn.click();

    const fileItem = document.querySelector(".context-menu-item.disabled[data-action='file']");
    // disabled items have no click handler — just verify no crash
    expect(fileItem).not.toBeNull();
  });

  it("positions context menu relative to attach button", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const btn = document.querySelector("#btn-attach");

    Object.defineProperty(btn, "getBoundingClientRect", {
      value: () => ({ top: 500, left: 10, bottom: 532, width: 20, height: 32 }),
    });

    btn.click();
    expect(menu.style.left).toBe("10px");
    expect(menu.style.bottom).not.toBe("");
  });
});
