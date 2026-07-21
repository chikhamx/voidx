// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { handleNotification } from "../../src/main";
import { setCommandCatalog, _resetCommandCatalogForTest } from "../../src/ui/slash";
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
  _resetRpcForTest();
  _resetCommandCatalogForTest();
  inputEl.value = "";
});

describe("remote command catalog", () => {
  it("requests commands.list on workspace.snapshot", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    handleNotification("workspace.snapshot", { threads: [], active_snapshot: { nodes: [] } });
    expect(sentMethods(socket)).toContain("commands.list");
  });
});

describe("Tab completion", () => {
  it("Tab completes a unique slash prefix", () => {
    setCommandCatalog([
      { command: "/usage", description: "Show token usage", category: "maintenance", execution: "run", dangerous: false, requiresArgs: false },
    ]);
    inputEl.value = "/usa";
    inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    const event = keydown("Tab");
    expect(event.defaultPrevented).toBe(true);
    expect(inputEl.value).toBe("/usage ");
  });

  it("Tab extends to the shared prefix when several commands match", () => {
    setCommandCatalog([
      { command: "/mcp", description: "MCP", category: "integrations", execution: "fill", dangerous: false, requiresArgs: false },
      { command: "/mcp new", description: "New", category: "integrations", execution: "fill", dangerous: false, requiresArgs: true },
      { command: "/mcp del", description: "Del", category: "integrations", execution: "fill", dangerous: false, requiresArgs: false },
    ]);
    inputEl.value = "/mcp ";
    inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    keydown("Tab");
    expect(inputEl.value).toBe("/mcp ");
    inputEl.value = "/mcp n";
    inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    keydown("Tab");
    expect(inputEl.value).toBe("/mcp new ");
  });

  it("Tab does not hijack non-slash input", () => {
    inputEl.value = "hello";
    const event = keydown("Tab");
    expect(event.defaultPrevented).toBe(false);
  });
});
