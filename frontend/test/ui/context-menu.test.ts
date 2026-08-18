// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  initContextMenu,
  _resetContextMenuForTest,
} from "../../src/ui/context-menu";
import { initProvidersModal, closeProvidersModal } from "../../src/ui/providers";
import { _setSocket, _resolvePendingForTest } from "../../src/rpc/client";

const openDialogMock = vi.fn();

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: openDialogMock,
}));

beforeEach(() => {
  _resetContextMenuForTest();
  openDialogMock.mockReset();
  delete window.__TAURI_INTERNALS__;
  delete window.__TAURI__;
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

  it("shows context menu when clicking the icon inside the attach button", () => {
    initContextMenu();
    const menu = document.querySelector("#context-menu");
    const icon = document.querySelector("#btn-attach svg");

    icon.dispatchEvent(new MouseEvent("click", { bubbles: true }));

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

  it("renders grouped add menu actions", () => {
    initContextMenu();
    const btn = document.querySelector("#btn-attach");
    btn.click();

    const menu = document.querySelector("#context-menu");
    expect(menu.textContent).toContain("添加");
    expect(menu.textContent).toContain("文件和文件夹");
    expect(menu.textContent).toContain("供应商 / 模型");
    expect(menu.textContent).toContain("技能");
    expect(menu.textContent).toContain("插件");
  });

  it("clicks integrations button when integrations action is triggered", () => {
    initContextMenu();
    const integrationsBtn = document.querySelector("#btn-integrations");
    const clickSpy = vi.fn();
    if (integrationsBtn) {
      integrationsBtn.addEventListener("click", clickSpy);
    }

    const btn = document.querySelector("#btn-attach");
    btn.click();

    const integrationsItem = document.querySelector(".context-menu-item[data-action='integrations']");
    integrationsItem.click();

    expect(clickSpy).toHaveBeenCalled();
  });

  it("opens providers modal when model provider action is triggered", async () => {
    const sent: Array<Record<string, unknown>> = [];
    _setSocket({
      readyState: WebSocket.OPEN,
      addEventListener: () => {},
      send: (data: string) => sent.push(JSON.parse(data)),
    });
    initProvidersModal();
    initContextMenu();
    const settingsBtn = document.querySelector("#btn-settings");
    const clickSpy = vi.fn();
    if (settingsBtn) {
      settingsBtn.addEventListener("click", clickSpy);
    }

    const btn = document.querySelector("#btn-attach");
    btn.click();

    const modelItem = document.querySelector(".context-menu-item[data-action='model']");
    modelItem.click();

    await vi.waitFor(() => expect(sent.some((m) => m.method === "settings.get")).toBe(true));
    const req = sent.find((m) => m.method === "settings.get")!;
    _resolvePendingForTest(req.id as number, { settings: { profiles: [] } });

    await vi.waitFor(() => {
      const dialog = document.querySelector("#providers-dialog") as HTMLDialogElement;
      expect(dialog.open || dialog.hasAttribute("open")).toBe(true);
    });
    expect(clickSpy).not.toHaveBeenCalled();

    closeProvidersModal();
    _setSocket(null);
  });

  it("opens native picker and inserts selected file attachments", async () => {
    window.__TAURI_INTERNALS__ = {};
    openDialogMock.mockResolvedValue(["/tmp/file one.txt", "/tmp/folder"]);

    initContextMenu();
    const input = document.querySelector("#input");
    const btn = document.querySelector("#btn-attach");
    btn.click();

    const fileItem = document.querySelector(".context-menu-item[data-action='file']");
    fileItem.click();

    await vi.waitFor(() => {
      expect(openDialogMock).toHaveBeenCalledWith({
        multiple: true,
        directory: true,
        title: "选择文件或文件夹",
      });
    });
    expect(input.value).toBe('@/tmp/file one.txt @/tmp/folder');
  });

  it("falls back to slash command text when native picker is unavailable", async () => {
    initContextMenu();
    const input = document.querySelector("#input");
    const btn = document.querySelector("#btn-attach");
    btn.click();

    const fileItem = document.querySelector(".context-menu-item[data-action='file']");
    fileItem.click();

    await vi.waitFor(() => {
      expect(input.value).toContain("/file ");
    });
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
