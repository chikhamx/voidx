// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  initContextMenu,
  _resetContextMenuForTest,
} from "../../src/ui/context-menu";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";

const inputEl = document.querySelector("#input");

function ensureClipboard() {
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, "clipboard", { value: {}, configurable: true });
  }
  return navigator.clipboard;
}

function fakeSocket() {
  return { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
}

function clickPasteAction() {
  document.querySelector("#context-menu .context-menu-item[data-action='paste']").click();
}

function mockClipboardImage(bytes = [1, 2, 3, 4]) {
  const blob = new Blob([new Uint8Array(bytes)], { type: "image/png" });
  const item = {
    types: ["image/png"],
    getType: () => Promise.resolve(blob),
  };
  ensureClipboard().read = vi.fn().mockResolvedValue([item]);
  return blob;
}

beforeEach(async () => {
  const { clearImageAttachments } = await import("../../src/ui/image-attachments");
  clearImageAttachments();
  _resetContextMenuForTest();
  _resetRpcForTest();
  inputEl.value = "";
  initContextMenu();
});

describe("paste image from clipboard", () => {
  it("uploads the clipboard image via attachments.saveImage and inserts the token", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    mockClipboardImage();

    clickPasteAction();
    await vi.waitFor(() => {
      const call = socket.send.mock.calls
        .map((a) => JSON.parse(a[0]))
        .find((m) => m.method === "attachments.saveImage");
      expect(call).toBeTruthy();
      expect(typeof call.params.data_base64).toBe("string");
    });

    const call = socket.send.mock.calls
      .map((a) => JSON.parse(a[0]))
      .find((m) => m.method === "attachments.saveImage");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(call.id, { ok: true, stem: "clip-1" });

    await vi.waitFor(() => {
      const chip = document.querySelector("#attachment-strip .attachment-chip");
      expect(chip).toBeTruthy();
      expect(chip.querySelector("img").src).toContain("data:image/png");
      expect(chip.dataset.stem).toBe("clip-1");
    });
    expect(inputEl.value).toBe("");
  });

  it("alerts when the clipboard has no image", async () => {
    _setSocket(fakeSocket());
    ensureClipboard().read = vi.fn().mockResolvedValue([{ types: ["text/plain"] }]);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    clickPasteAction();
    await vi.waitFor(() => expect(alertSpy).toHaveBeenCalled());
    expect(inputEl.value).toBe("");
    alertSpy.mockRestore();
  });

  it("shows the server error message when upload fails", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    mockClipboardImage();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    clickPasteAction();
    await vi.waitFor(() => {
      expect(
        socket.send.mock.calls.map((a) => JSON.parse(a[0])).find((m) => m.method === "attachments.saveImage"),
      ).toBeTruthy();
    });
    const call = socket.send.mock.calls
      .map((a) => JSON.parse(a[0]))
      .find((m) => m.method === "attachments.saveImage");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(call.id, { ok: false, message: "too large" });

    await vi.waitFor(() => expect(alertSpy).toHaveBeenCalledWith("too large"));
    expect(inputEl.value).toBe("");
    alertSpy.mockRestore();
  });

  it("handles image files in the native paste event", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const blob = new Blob([new Uint8Array([9, 9])], { type: "image/png" });
    const file = new File([blob], "pasted.png", { type: "image/png" });
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: { files: [file], items: [], getData: () => "" },
    });
    inputEl.dispatchEvent(event);

    await vi.waitFor(() => {
      const call = socket.send.mock.calls
        .map((a) => JSON.parse(a[0]))
        .find((m) => m.method === "attachments.saveImage");
      expect(call).toBeTruthy();
    });
  });
});
