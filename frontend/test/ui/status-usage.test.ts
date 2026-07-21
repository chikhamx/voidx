// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { uiState, updateStatusBar, formatUsageLabel } from "../../src/services/state";
import { handleNotification } from "../../src/main";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";

function fakeSocket() {
  return { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
}

function sentMethods(socket) {
  return socket.send.mock.calls.map((args) => JSON.parse(args[0]).method);
}

beforeEach(() => {
  _resetRpcForTest();
  uiState.usage = null;
});

describe("formatUsageLabel", () => {
  it("formats context, cache rate, and total tokens", () => {
    expect(
      formatUsageLabel({
        context_tokens: 42_000,
        context_limit: 200_000,
        total_tokens: 105_000,
        cache_hit_rate: 0.57,
        cache_hit_rate_estimated: false,
      }),
    ).toBe("42.0k/200k ctx · cache 57% · 105k total");
  });

  it("marks estimated cache rates", () => {
    expect(
      formatUsageLabel({
        context_tokens: 1_000,
        context_limit: 128_000,
        total_tokens: 2_500,
        cache_hit_rate: 0.25,
        cache_hit_rate_estimated: true,
      }),
    ).toBe("1.0k/128k ctx · cache ~25% · 2.5k total");
  });

  it("omits cache segment when rate is null", () => {
    expect(
      formatUsageLabel({
        context_tokens: 0,
        context_limit: 128_000,
        total_tokens: 0,
        cache_hit_rate: null,
      }),
    ).toBe("0/128k ctx · 0 total");
  });

  it("returns em dash for missing usage", () => {
    expect(formatUsageLabel(null)).toBe("—");
  });
});

describe("status bar usage", () => {
  it("renders usage into #status-usage", () => {
    uiState.usage = {
      context_tokens: 42_000,
      context_limit: 200_000,
      total_tokens: 105_000,
      cache_hit_rate: 0.57,
      cache_hit_rate_estimated: false,
    };
    updateStatusBar();
    expect(document.querySelector("#status-usage").textContent).toContain("42.0k/200k ctx");
  });

  it("requests usage.get on workspace.snapshot", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    handleNotification("workspace.snapshot", { threads: [], active_snapshot: { nodes: [] } });
    expect(sentMethods(socket)).toContain("usage.get");
  });

  it("requests usage.get on turn.completed", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    handleNotification("turn.completed", {});
    expect(sentMethods(socket)).toContain("usage.get");
  });

  it("applies the usage.get response to uiState", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    handleNotification("turn.completed", {});
    const call = socket.send.mock.calls
      .map((a) => JSON.parse(a[0]))
      .find((m) => m.method === "usage.get");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(call.id, {
      usage: { context_tokens: 10, context_limit: 100, total_tokens: 10, cache_hit_rate: null },
    });
    await vi.waitFor(() => expect(uiState.usage?.context_tokens).toBe(10));
  });

});
