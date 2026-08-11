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
  uiState.aiApprovalCount = 0;
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



  it("shows AI approval count only when it is positive", () => {
    uiState.aiApprovalCount = 3;
    updateStatusBar();

    const stripCount = document.querySelector("#strip-ai-approval");
    const panelRow = document.querySelector("#status-ai-approval-row");
    expect(stripCount.hidden).toBe(false);
    expect(stripCount.textContent).toBe("AI 审批 3");
    expect(panelRow.hidden).toBe(false);
    expect(document.querySelector("#status-ai-approval").textContent).toBe("3");

    uiState.aiApprovalCount = 0;
    updateStatusBar();
    expect(stripCount.hidden).toBe(true);
    expect(panelRow.hidden).toBe(true);
  });

  it("applies AI approval count from runtime snapshots", () => {
    handleNotification("startup.shown", { ai_approval_count: 4 });

    expect(uiState.aiApprovalCount).toBe(4);
    expect(document.querySelector("#strip-ai-approval").textContent).toBe("AI 审批 4");
  });


  it("keeps the runtime status line hidden when no transient status is needed", () => {
    uiState.runtimeProfile = "goal";
    uiState.provider = "openai";
    uiState.model = "gpt-5.5";
    uiState.reasoningEffort = "high";
    uiState.permissionMode = "project_trusted";
    uiState.aiApprovalCount = 2;
    updateStatusBar();

    const runtimeStatus = document.querySelector("#runtime-status-line");
    expect(runtimeStatus.hidden).toBe(true);
    expect(runtimeStatus.textContent).toBe("");
  });

  it("disables sending while waiting for the write lock but keeps running cancellation available", () => {
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-waiting",
      active_snapshot: { thread_id: "thread-waiting", nodes: [] },
      threads: [],
      workspace_write_lock: {
        holder_thread_id: "thread-running",
        waiting_thread_ids: ["thread-waiting"],
      },
    });
    handleNotification("turn.started", {});

    expect(document.querySelector("#input").disabled).toBe(true);
    expect(document.querySelector("#btn-send").disabled).toBe(false);
    expect(document.querySelector("#runtime-status-line").textContent).toContain("等待另一个会话完成写入…");

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-waiting",
      active_snapshot: { thread_id: "thread-waiting", nodes: [] },
      threads: [],
      workspace_write_lock: {
        holder_thread_id: "",
        waiting_thread_ids: [],
      },
    });

    expect(document.querySelector("#input").disabled).toBe(false);
    expect(document.querySelector("#btn-send").disabled).toBe(false);
  });
});
