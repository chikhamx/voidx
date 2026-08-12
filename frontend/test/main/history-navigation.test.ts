// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import "../../src/main";
import {
  pushHistory,
  _resetHistoryForTest,
} from "../../src/ui/history";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";

const inputEl = document.querySelector("#input");
const composerEl = document.querySelector("#composer");

function keydown(key, init = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  inputEl.dispatchEvent(event);
  return event;
}

function fakeOpenSocket() {
  return {
    readyState: WebSocket.OPEN,
    send: vi.fn(),
    addEventListener: () => {},
  };
}

beforeEach(() => {
  _resetHistoryForTest();
  _resetRpcForTest();
  inputEl.value = "";
  document.querySelector("#transcript").innerHTML = "";
});

describe("composer history navigation", () => {
  it("ArrowUp in empty input recalls the latest entry", () => {
    pushHistory("older");
    pushHistory("latest");
    keydown("ArrowUp");
    expect(inputEl.value).toBe("latest");
  });

  it("ArrowUp twice walks back, ArrowDown restores the draft", () => {
    pushHistory("older");
    pushHistory("latest");
    keydown("ArrowUp");
    keydown("ArrowUp");
    expect(inputEl.value).toBe("older");
    keydown("ArrowDown");
    expect(inputEl.value).toBe("latest");
    keydown("ArrowDown");
    expect(inputEl.value).toBe("");
  });

  it("does not hijack ArrowUp when the input has typed text", () => {
    pushHistory("latest");
    inputEl.value = "typed";
    keydown("ArrowUp");
    expect(inputEl.value).toBe("typed");
  });

  it("submit pushes text into history", async () => {
    _setSocket(fakeOpenSocket());
    inputEl.value = "ship it";
    composerEl.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    inputEl.value = "";
    keydown("ArrowUp");
    expect(inputEl.value).toBe("ship it");
  });


  it("does not submit while an IME composition is being confirmed", () => {
    const socket = fakeOpenSocket();
    _setSocket(socket);
    inputEl.value = "你好";

    keydown("Enter", { isComposing: true });

    const methods = socket.send.mock.calls.map(([data]) => JSON.parse(data).method);
    expect(methods).not.toContain("session.submit");
    expect(document.querySelectorAll("#transcript .message-item")).toHaveLength(0);
    expect(inputEl.value).toBe("你好");
  });
  it("typing resets browsing so next ArrowUp starts from newest", () => {
    pushHistory("older");
    pushHistory("latest");
    keydown("ArrowUp");
    expect(inputEl.value).toBe("latest");
    inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    keydown("ArrowUp");
    expect(inputEl.value).toBe("latest");
  });
});
