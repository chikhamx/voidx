// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";

const closeMock = vi.fn();
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ close: closeMock }),
}));

import "../../src/main";
import { uiState, updateStatusBar } from "../../src/services/state";
import { _resetHistoryForTest, historyPrev } from "../../src/ui/history";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";

const inputEl = document.querySelector("#input");

function keydown(key, init = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  inputEl.dispatchEvent(event);
  return event;
}

function fakeSocket() {
  return { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
}

function sentMethods(socket) {
  return socket.send.mock.calls.map((args) => JSON.parse(args[0]).method);
}

beforeEach(() => {
  _resetHistoryForTest();
  _resetRpcForTest();
  closeMock.mockReset();
  inputEl.value = "";
  uiState.isRunning = false;
  delete window.__TAURI_INTERNALS__;
  delete window.__TAURI__;
});

describe("cancel while running", () => {
  it("Ctrl+C sends session.cancel when running", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.isRunning = true;
    keydown("c", { ctrlKey: true });
    expect(sentMethods(socket)).toContain("session.cancel");
  });

  it("Escape sends session.cancel when running", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.isRunning = true;
    keydown("Escape");
    expect(sentMethods(socket)).toContain("session.cancel");
  });

  it("Escape does nothing when idle", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    keydown("Escape");
    expect(sentMethods(socket)).not.toContain("session.cancel");
  });
});
describe.each(["goal", "loop"])("%s Stop contract", (profile) => {
    it.each([true, false])("keeps Stop visible when running=%s", async (running) => {
        const socket = fakeSocket();
        _setSocket(socket);
        const { refreshModeMenu } = await import("../../src/ui/mode");
        const refreshed = refreshModeMenu();
        const request = socket.send.mock.calls.map(([text]) => JSON.parse(text))
            .find((entry) => entry.method === "list-agent-profiles");
        const { _resolvePendingForTest } = await import("../../src/rpc/client");
        _resolvePendingForTest(request.id, {
            profiles: [{
                name: profile, display_name: profile, revision: 1, content_hash: "hash",
                source: "builtin", run_mode: profile, hitl_mode: "autonomous",
                availability: "available", diagnostics: [],
            }]
        });
        await refreshed;
        uiState.sessionId = "owner";
        uiState.runtimeProfile = profile;
        uiState.isRunning = running;
        updateStatusBar();
        expect(document.querySelector("#mode-stop").hidden).toBe(false);
    });
    it("routes an active owner to cancel, never a legacy submission", () => {
        const socket = fakeSocket();
        _setSocket(socket);
        uiState.sessionId = "active-owner";
        uiState.runtimeProfile = profile;
        uiState.isRunning = true;
        document.querySelector("#mode-stop").click();
        expect(socket.send.mock.calls.map(([text]) => JSON.parse(text))).toEqual([
            expect.objectContaining({ method: "session.cancel", params: { thread_id: "active-owner" } }),
        ]);
    });

    it("preserves the inactive legacy stop command, never cancels", () => {
        const socket = fakeSocket();
        _setSocket(socket);
        uiState.sessionId = "inactive-owner";
        uiState.runtimeProfile = profile;
        document.querySelector("#mode-stop").click();
        expect(socket.send.mock.calls.map(([text]) => JSON.parse(text))).toEqual([
            expect.objectContaining({
                method: "session.submit", params: {
                    thread_id: "inactive-owner", text: `/${profile} stop`,
                }
            }),
        ]);
    });
});

describe("Ctrl+C on input text", () => {
  it("clears non-empty input and keeps it in history", () => {
    inputEl.value = "draft text";
    keydown("c", { ctrlKey: true });
    expect(inputEl.value).toBe("");
    expect(historyPrev("")).toBe("draft text");
  });

  it("does not clear when text is selected (native copy wins)", () => {
    inputEl.value = "draft text";
    inputEl.setSelectionRange(0, 5);
    keydown("c", { ctrlKey: true });
    expect(inputEl.value).toBe("draft text");
  });
});

describe("quit shortcuts", () => {
  it("double Ctrl+C on empty input closes the window in Tauri", async () => {
    window.__TAURI_INTERNALS__ = {};
    keydown("c", { ctrlKey: true });
    expect(closeMock).not.toHaveBeenCalled();
    keydown("c", { ctrlKey: true });
    await vi.waitFor(() => expect(closeMock).toHaveBeenCalledTimes(1));
  });

  it("single Ctrl+C on empty input does nothing without Tauri", () => {
    keydown("c", { ctrlKey: true });
    keydown("c", { ctrlKey: true });
    expect(closeMock).not.toHaveBeenCalled();
  });

  it("Ctrl+D on empty input closes the window in Tauri", async () => {
    window.__TAURI_INTERNALS__ = {};
    keydown("d", { ctrlKey: true });
    await vi.waitFor(() => expect(closeMock).toHaveBeenCalledTimes(1));
  });

  it("Ctrl+D with text does not close", () => {
    window.__TAURI_INTERNALS__ = {};
    inputEl.value = "text";
    keydown("d", { ctrlKey: true });
    expect(closeMock).not.toHaveBeenCalled();
  });
});
