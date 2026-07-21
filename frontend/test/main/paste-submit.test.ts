// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import "../../src/main";
import { registerTextPaste, clearPasteEntries } from "../../src/ui/paste";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";

const inputEl = document.querySelector("#input");
const composerEl = document.querySelector("#composer");

function fakeSocket() {
  return { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
}

beforeEach(() => {
  _resetRpcForTest();
  clearPasteEntries();
  inputEl.value = "";
});

describe("submit expands paste tokens", () => {
  it("expands a collapsed text paste into <pasted> blocks and clears the registry", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const token = registerTextPaste("第一行\n第二行");
    inputEl.value = `分析下 ${token}`;

    composerEl.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    const call = socket.send.mock.calls
      .map((a) => JSON.parse(a[0]))
      .find((m) => m.method === "session.submit");
    expect(call).toBeTruthy();
    expect(call.params.text).toBe("分析下 <pasted>\n第一行\n第二行\n</pasted>");
  });

});

describe("submit with image attachments", () => {
  it("appends [image-] tokens for pending thumbnails and clears the strip", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { addImageAttachment, clearImageAttachments, _imageAttachmentsForTest } =
      await import("../../src/ui/image-attachments");
    clearImageAttachments();
    addImageAttachment("clip-7", "data:image/png;base64,AAAA");
    inputEl.value = "描述这张图";

    composerEl.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    const call = socket.send.mock.calls
      .map((a) => JSON.parse(a[0]))
      .find((m) => m.method === "session.submit");
    expect(call.params.text).toBe("描述这张图 [image-clip-7]");
    expect(_imageAttachmentsForTest()).toHaveLength(0);
    clearImageAttachments();
  });
});

describe("paste event collapses multi-line text", () => {
  it("inserts a placeholder for multi-line paste and records the entry", () => {
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: {
        files: [],
        items: [],
        getData: (type) => (type === "text/plain" ? "one\ntwo\nthree" : ""),
      },
    });
    inputEl.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(inputEl.value).toBe("[Pasted text #1 +2 lines]");
  });

  it("lets single-line paste fall through to the default handler", () => {
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: {
        files: [],
        items: [],
        getData: (type) => (type === "text/plain" ? "short" : ""),
      },
    });
    inputEl.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
  });
});
