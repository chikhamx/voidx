// @ts-nocheck
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetWorkbenchForTest,
  handleItem,
  handleNotification,
} from "../../src/main";
import { _setSocket } from "../../src/rpc/client";
import { uiState } from "../../src/services/state";
import { getOrCreateStream } from "../../src/utils/stream";
import { renderMarkdown } from "../../src/utils/markdown";

function fakeSocket() {
  return {
    readyState: WebSocket.OPEN,
    send: vi.fn(),
    onmessage: null,
    addEventListener(type, handler) {
      if (type === "message") this.onmessage = handler;
    },
  };
}

function sent(socket, method) {
  return socket.send.mock.calls
    .map(([data]) => JSON.parse(data))
    .filter((message) => message.method === method);
}

function assistantItem(method, data, overrides = {}) {
  handleItem(method, {
    thread_id: "thread-1",
    turn_id: "turn-1",
    item_id: "item-1",
    kind: "assistant_stream",
    data,
    ...overrides,
  });
}

beforeEach(() => {
  _resetWorkbenchForTest();
  uiState.sessionId = "thread-1";
});

describe("workspace.patch consumer", () => {
  it("applies metadata only and ignores old or duplicate revisions", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const transcript = document.querySelector("#transcript");
    const existing = document.createElement("div");
    existing.textContent = "canonical transcript";
    transcript.append(existing);

    handleNotification("workspace.patch", {
      revision: 1,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1", status: "running" }],
      provider: "openai",
      model: "gpt-5",
      workspace: "/tmp/project",
      profile_configured: true,
      permission_mode: "ask",
      ai_approval_count: 2,
    });

    expect(uiState.provider).toBe("openai");
    expect(uiState.model).toBe("gpt-5");
    expect(uiState.workspace).toBe("/tmp/project");
    expect(uiState.permissionMode).toBe("ask");
    expect(uiState.aiApprovalCount).toBe(2);
    expect(uiState.isRunning).toBe(true);
    expect(transcript.firstChild).toBe(existing);

    handleNotification("workspace.patch", {
      revision: 1,
      provider: "stale-provider",
      model: "stale-model",
      ai_approval_count: 99,
    });
    handleNotification("workspace.patch", {
      revision: 0,
      provider: "older-provider",
      model: "older-model",
    });

    expect(uiState.provider).toBe("openai");
    expect(uiState.model).toBe("gpt-5");
    expect(uiState.aiApprovalCount).toBe(2);
    expect(sent(socket, "snapshot.requested")).toHaveLength(0);
  });

  it("requests one snapshot for a workspace revision gap and resumes after recovery", () => {
    const socket = fakeSocket();
    _setSocket(socket);

    handleNotification("workspace.patch", { revision: 1, active_thread_id: "thread-1" });
    handleNotification("workspace.patch", {
      revision: 3,
      active_thread_id: "thread-1",
      provider: "must-not-apply",
    });
    handleNotification("workspace.patch", {
      revision: 4,
      active_thread_id: "thread-1",
      provider: "also-must-not-apply",
    });

    expect(uiState.provider).not.toBe("must-not-apply");
    expect(sent(socket, "snapshot.requested")).toEqual([
      expect.objectContaining({ params: { thread_id: "thread-1" } }),
    ]);

    handleNotification("workspace.snapshot", {
      revision: 3,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: { thread_id: "thread-1", revision: 3, nodes: [] },
    });
    handleNotification("workspace.patch", {
      revision: 4,
      active_thread_id: "thread-1",
      provider: "recovered-provider",
    });

    expect(uiState.provider).toBe("recovered-provider");
    expect(sent(socket, "snapshot.requested")).toHaveLength(1);
  });
});

describe("assistant stream incremental consumer", () => {
  it("validates append cursors, supports replace, and makes duplicate revisions idempotent", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    assistantItem("item.started", {
      op: "replace",
      revision: 0,
      stream_id: "stream-1",
      text: "",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 0,
      revision: 1,
      stream_id: "stream-1",
      text: "hello",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 1,
      revision: 2,
      stream_id: "stream-1",
      text: " world",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 1,
      revision: 2,
      stream_id: "stream-1",
      text: " corrupted duplicate",
      phase: "text",
    });
    expect(getOrCreateStream("item-1", "text").text).toBe("hello world");

    assistantItem("item.delta", {
      op: "replace",
      base_revision: 2,
      revision: 3,
      stream_id: "stream-1",
      text: "reset",
      phase: "text",
    });
    expect(getOrCreateStream("item-1", "text").text).toBe("reset");

    assistantItem("item.delta", {
      op: "replace",
      base_revision: 2,
      revision: 3,
      stream_id: "stream-1",
      text: "corrupted replacement duplicate",
      phase: "text",
    });
    expect(getOrCreateStream("item-1", "text").text).toBe("reset");
    expect(sent(socket, "snapshot.requested")).toHaveLength(0);
  });

  it("does not apply an append gap and requests recovery once", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    assistantItem("item.started", {
      op: "replace",
      revision: 0,
      stream_id: "stream-1",
      text: "",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 0,
      revision: 1,
      stream_id: "stream-1",
      text: "hello",
      phase: "text",
    });
    const gap = {
      op: "append",
      base_revision: 1,
      revision: 3,
      stream_id: "stream-1",
      text: " skipped",
      phase: "text",
    };
    assistantItem("item.delta", gap);
    assistantItem("item.delta", gap);

    expect(getOrCreateStream("item-1", "text").text).toBe("hello");
    expect(sent(socket, "snapshot.requested")).toEqual([
      expect.objectContaining({ params: { thread_id: "thread-1" } }),
    ]);
  });

  it("keeps legacy full-text item.delta accumulation behavior", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    assistantItem("item.started", { phase: "text", text: "" });
    assistantItem("item.delta", { phase: "text", text: "hello" });
    assistantItem("item.delta", { phase: "text", text: "hello world" });

    expect(getOrCreateStream("item-1", "text").text).toBe("hello world");
    expect(sent(socket, "snapshot.requested")).toHaveLength(0);
  });

  it("commits through the canonical sanitized Markdown renderer", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const text = "safe **bold** <script>window.pwned = true</script>";
    assistantItem("item.started", { phase: "text", text: "" });
    assistantItem("item.delta", { phase: "text", text });
    assistantItem("item.completed", { phase: "text" });

    const body = document.querySelector("#transcript .stream-buffer .markdown-body");
    expect(body).not.toBeNull();
    expect(body.innerHTML).toBe(renderMarkdown(text).innerHTML);
    expect(body.querySelector("script")).toBeNull();
  });
  it("resets workspace cursors when a new socket connection starts", () => {
    const firstSocket = fakeSocket();
    _setSocket(firstSocket);
    handleNotification("workspace.patch", {
      revision: 5,
      active_thread_id: "thread-1",
      provider: "old-connection",
    });

    const secondSocket = fakeSocket();
    _setSocket(secondSocket);
    handleNotification("workspace.snapshot", {
      revision: 0,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: { thread_id: "thread-1", revision: 0, nodes: [] },
    });
    handleNotification("workspace.patch", {
      revision: 1,
      active_thread_id: "thread-1",
      provider: "new-connection",
    });

    expect(uiState.provider).toBe("new-connection");
  });

  it("clears stream and item cursors after snapshot recovery", () => {
    const socket = fakeSocket();
    _setSocket(socket);
    assistantItem("item.started", {
      op: "replace",
      revision: 0,
      stream_id: "stream-1",
      text: "",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 0,
      revision: 1,
      stream_id: "stream-1",
      text: "before-gap",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 1,
      revision: 3,
      stream_id: "stream-1",
      text: " skipped",
      phase: "text",
    });

    handleNotification("workspace.snapshot", {
      revision: 3,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: { thread_id: "thread-1", revision: 3, nodes: [] },
    });
    assistantItem("item.started", {
      op: "replace",
      revision: 0,
      stream_id: "stream-1",
      text: "after-recovery",
      phase: "text",
    });
    assistantItem("item.delta", {
      op: "append",
      base_revision: 0,
      revision: 1,
      stream_id: "stream-1",
      text: "-delta",
      phase: "text",
    });

    expect(getOrCreateStream("item-1", "text").text).toBe("after-recovery-delta");
  });
});
