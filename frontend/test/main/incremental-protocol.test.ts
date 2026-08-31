// @ts-nocheck
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetWorkbenchForTest,
  handleItem,
  handleNotification,
} from "../../src/main";
import { _setSocket } from "../../src/rpc/client";
import { uiState } from "../../src/services/state";
import {
  _setCanonicalMarkdownCoordinatorForTest,
  getOrCreateStream,
} from "../../src/utils/stream";
import { renderMarkdown } from "../../src/utils/markdown";
import { createCanonicalMarkdownCoordinator } from "../../src/utils/markdown-worker-client";
import { handleCanonicalRenderRequest } from "../../src/utils/markdown.worker";

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


function eventSocket(initialReadyState = WebSocket.CONNECTING) {
  const listeners = new Map();
  return {
    readyState: initialReadyState,
    send: vi.fn(),
    onmessage: null,
    addEventListener(type, handler) {
      const handlers = listeners.get(type) || [];
      handlers.push(handler);
      listeners.set(type, handlers);
    },
    emit(type) {
      this.readyState = type === "open" ? WebSocket.OPEN : WebSocket.CLOSED;
      for (const handler of listeners.get(type) || []) handler(new Event(type));
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


function controlledCanonicalCoordinator() {
  const sent = [];
  const messageListeners = [];
  const worker = {
    postMessage(message) {
      sent.push(message);
    },
    addEventListener(type, listener) {
      if (type === "message") messageListeners.push(listener);
    },
  };
  return {
    coordinator: createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame(callback) {
        callback(0);
        return 1;
      },
      now: () => 0,
    }),
    settle() {
      const response = handleCanonicalRenderRequest(sent[0]);
      for (const listener of messageListeners) listener({ data: response });
    },
  };
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

  it("resends an ordinary gap recovery once when the replacement socket opens", () => {
    const first = eventSocket(WebSocket.OPEN);
    _setSocket(first);

    handleNotification("workspace.patch", { revision: 1, active_thread_id: "thread-1" });
    handleNotification("workspace.patch", { revision: 3, active_thread_id: "thread-1" });
    expect(sent(first, "snapshot.requested")).toHaveLength(1);

    first.emit("close");
    const replacement = eventSocket(WebSocket.CONNECTING);
    _setSocket(replacement);
    expect(sent(replacement, "snapshot.requested")).toHaveLength(0);

    replacement.emit("open");
    expect(sent(replacement, "snapshot.requested")).toHaveLength(1);
    replacement.emit("open");
    expect(sent(replacement, "snapshot.requested")).toHaveLength(1);
  });

  it("keeps ordinary recovery open until an authoritative full snapshot arrives", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);

    handleNotification("workspace.patch", { revision: 1, active_thread_id: "thread-1" });
    handleNotification("workspace.patch", { revision: 3, active_thread_id: "thread-1" });
    expect(sent(socket, "snapshot.requested")).toHaveLength(1);

    handleNotification("workspace.snapshot", {
      revision: 3,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 3,
        windowed: true,
        nodes: [],
      },
    });
    handleNotification("workspace.patch", {
      revision: 4,
      active_thread_id: "thread-1",
      provider: "must-remain-blocked",
    });

    expect(uiState.provider).not.toBe("must-remain-blocked");
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
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.isRunning = true;
    const text = "safe **bold** <script>window.pwned = true</script>";
    assistantItem("item.started", { phase: "text", text: "" });
    assistantItem("item.delta", { phase: "text", text });
    assistantItem("item.completed", { phase: "text" });

    const body = document.querySelector("#transcript .stream-buffer .markdown-body");
    expect(body).not.toBeNull();
    expect(uiState.isRunning).toBe(false);
    expect(body.dataset.renderPending).toBe("true");

    harness.settle();

    expect(body.dataset.renderPending).toBeUndefined();
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

  it("forwards validated append and replace payloads to the projection without rebuilding cumulative input", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const initial = "a".repeat(20_000);
    assistantItem("item.started", {
      op: "replace",
      revision: 0,
      stream_id: "stream-1",
      text: initial,
      phase: "text",
    });
    await new Promise((resolve) => setTimeout(resolve, 150));

    assistantItem("item.delta", {
      op: "append",
      base_revision: 0,
      revision: 1,
      stream_id: "stream-1",
      text: "!",
      phase: "text",
    });
    await new Promise((resolve) => setTimeout(resolve, 150));

    const stream = getOrCreateStream("item-1", "text");
    expect(stream.text).toBe(initial + "!");
    expect(stream.markdownProjection?._debugSnapshotForTest?.()).toEqual(
      expect.objectContaining({
        rawTextLength: initial.length + 1,
        lastUpdateOperation: "append",
        lastUpdateInputLength: 1,
      }),
    );

    assistantItem("item.delta", {
      op: "replace",
      base_revision: 1,
      revision: 2,
      stream_id: "stream-1",
      text: "replacement",
      phase: "text",
    });
    await new Promise((resolve) => setTimeout(resolve, 150));

    expect(stream.text).toBe("replacement");
    expect(stream.markdownProjection?._debugSnapshotForTest?.()).toEqual(
      expect.objectContaining({
        rawTextLength: "replacement".length,
        lastUpdateOperation: "replace",
        lastUpdateInputLength: "replacement".length,
      }),
    );
    expect(sent(socket, "snapshot.requested")).toHaveLength(0);
  });
});


describe("workspace snapshot keyed reconciliation", () => {
  function snapshot(revision, nodes, windowed = false, status = undefined) {
    handleNotification("workspace.snapshot", {
      revision,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1", status }],
      active_snapshot: {
        thread_id: "thread-1",
        revision,
        windowed,
        nodes,
      },
    });
  }

  it("keeps unchanged block identity and replaces only changed blocks", () => {
    snapshot(1, [
      { node_type: "message", id: "keep", payload: { style: "text", raw_text: "same" } },
      { node_type: "message", id: "change", payload: { style: "text", raw_text: "before" } },
    ]);
    const transcript = document.querySelector("#transcript");
    const keep = transcript.querySelector('[data-reconcile-key="node:keep"]');
    const change = transcript.querySelector('[data-reconcile-key="node:change"]');

    snapshot(2, [
      { node_type: "message", id: "keep", payload: { style: "text", raw_text: "same" } },
      { node_type: "message", id: "change", payload: { style: "text", raw_text: "after" } },
    ]);

    expect(transcript.querySelector('[data-reconcile-key="node:keep"]')).toBe(keep);
    expect(transcript.querySelector('[data-reconcile-key="node:change"]')).not.toBe(change);
    expect(transcript.textContent).toContain("after");
    expect(transcript.textContent).not.toContain("before");
  });

  it("does not delete absent canonical blocks from a windowed snapshot", () => {
    snapshot(1, [
      { node_type: "message", id: "outside", payload: { style: "text", raw_text: "outside" } },
      { node_type: "message", id: "page", payload: { style: "text", raw_text: "page" } },
    ]);
    const transcript = document.querySelector("#transcript");
    const outside = transcript.querySelector('[data-reconcile-key="node:outside"]');

    snapshot(2, [
      { node_type: "message", id: "page", payload: { style: "text", raw_text: "page updated" } },
    ], true);

    expect(transcript.querySelector('[data-reconcile-key="node:outside"]')).toBe(outside);
    expect(transcript.textContent).toContain("outside");
    expect(transcript.textContent).toContain("page updated");
  });

  it("preserves an unconfirmed pending local message identity across a same-thread snapshot", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const input = document.querySelector("#input");
    const composer = document.querySelector("#composer");
    input.value = "pending local";
    composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    const transcript = document.querySelector("#transcript");
    const pending = transcript.querySelector('[data-item-id^="user-"]');
    expect(pending).not.toBeNull();

    snapshot(1, [
      { node_type: "message", id: "history", payload: { style: "text", raw_text: "history" } },
    ]);

    expect(transcript.querySelector('[data-item-id^="user-"]')).toBe(pending);
    expect(transcript.textContent).toContain("pending local");
  });


  it("hands off duplicate pending local messages to fresh snapshot entries in FIFO order", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const input = document.querySelector("#input");
    const composer = document.querySelector("#composer");
    const transcript = document.querySelector("#transcript");

    input.value = "same text";
    composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    snapshot(1, [], false, "idle");
    input.value = "same text";
    composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    const pending = [...transcript.querySelectorAll('[data-item-id^="user-"]')];
    expect(pending).toHaveLength(2);

    snapshot(1, [
      { node_type: "turn", id: "server-user-1", header: "same text" },
    ]);

    expect(pending[0].isConnected).toBe(true);
    expect(pending[0].dataset.reconcileKey).toBe("node:server-user-1");
    expect(pending[1].isConnected).toBe(true);
    expect(transcript.querySelectorAll('[data-item-id^="user-"]:not([data-reconcile-key])')).toHaveLength(1);
  });


  it("does not consume a pending handoff when snapshot reconciliation is stale", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    snapshot(1, [
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
    ], false, "idle");
    const input = document.querySelector("#input");
    const composer = document.querySelector("#composer");
    input.value = "pending recovery";
    composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    const transcript = document.querySelector("#transcript");
    const pending = transcript.querySelector('[data-item-id^="user-"]:not([data-reconcile-key])');
    const boundary = document.createElement("div");
    boundary.dataset.pendingItemId = "unrelated-boundary";
    transcript.insertBefore(boundary, transcript.querySelector('[data-reconcile-key="node:segment-b"]'));

    snapshot(3, [
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
      { node_type: "turn", id: "server-pending", header: "pending recovery" },
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
    ], true);

    expect(pending.isConnected).toBe(true);
    expect(pending.dataset.reconcileKey).toBeUndefined();

    boundary.remove();
    snapshot(2, [
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
      { node_type: "turn", id: "server-pending", header: "pending recovery" },
    ]);

    expect(pending.isConnected).toBe(false);
    const recovered = transcript.querySelector('[data-reconcile-key="node:server-pending"]');
    expect(recovered).not.toBeNull();
    expect(recovered).not.toBe(pending);
  });


  it("retries blocked snapshot recovery after a failed reconciliation install", async () => {
    vi.useFakeTimers();
    try {
      const socket = eventSocket(WebSocket.OPEN);
      _setSocket(socket);
      snapshot(1, [
        { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
        { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
      ]);
      const transcript = document.querySelector("#transcript");
      const boundary = document.createElement("div");
      boundary.dataset.pendingItemId = "blocked-boundary";
      transcript.insertBefore(boundary, transcript.querySelector('[data-reconcile-key="node:segment-b"]'));

      snapshot(3, [
        { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
        { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      ], true);

      expect(sent(socket, "snapshot.requested")).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(249);
      expect(sent(socket, "snapshot.requested")).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sent(socket, "snapshot.requested")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("installs a blocked authoritative full snapshot through replacement and stops retry", async () => {
    vi.useFakeTimers();
    try {
      const socket = eventSocket(WebSocket.OPEN);
      _setSocket(socket);
      snapshot(1, [
        { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
        { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
      ]);
      const transcript = document.querySelector("#transcript");
      const boundary = document.createElement("div");
      boundary.dataset.pendingItemId = "damaged-boundary";
      transcript.insertBefore(boundary, transcript.querySelector('[data-reconcile-key="node:segment-b"]'));
      snapshot(3, [
        { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
        { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      ], true);
      expect(sent(socket, "snapshot.requested")).toHaveLength(1);

      snapshot(2, [
        { node_type: "message", id: "recovered", payload: { style: "text", raw_text: "recovered" } },
      ]);

      expect(boundary.isConnected).toBe(false);
      expect(transcript.querySelector('[data-reconcile-key="node:recovered"]')).not.toBeNull();
      await vi.advanceTimersByTimeAsync(1000);
      expect(sent(socket, "snapshot.requested")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps blocked commit slots unchanged when replacement throws and retries the same revision", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    snapshot(1, [
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
    ]);
    const transcript = document.querySelector("#transcript");
    const boundary = document.createElement("div");
    boundary.dataset.pendingItemId = "damaged-boundary";
    transcript.insertBefore(boundary, transcript.querySelector('[data-reconcile-key="node:segment-b"]'));
    snapshot(3, [
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
    ], true);

    const replaceChildren = transcript.replaceChildren.bind(transcript);
    transcript.replaceChildren = vi.fn(() => { throw new Error("replacement failed"); });
    snapshot(2, [
      { node_type: "message", id: "recovered", payload: { style: "text", raw_text: "recovered" } },
    ]);

    expect(boundary.isConnected).toBe(true);
    expect(transcript.querySelector('[data-reconcile-key="node:recovered"]')).toBeNull();

    transcript.replaceChildren = replaceChildren;
    snapshot(2, [
      { node_type: "message", id: "recovered", payload: { style: "text", raw_text: "recovered" } },
    ]);

    expect(boundary.isConnected).toBe(false);
    expect(transcript.querySelector('[data-reconcile-key="node:recovered"]')).not.toBeNull();
    expect(sent(socket, "snapshot.requested")).toHaveLength(1);
  });


  it("does not commit workspace revision when keyed snapshot reconciliation is stale", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    snapshot(1, [
      { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
    ]);
    const transcript = document.querySelector("#transcript");
    const second = transcript.querySelector('[data-reconcile-key="node:segment-b"]');
    const boundary = document.createElement("div");
    boundary.dataset.pendingItemId = "pending-boundary";
    transcript.insertBefore(boundary, second);

    handleNotification("workspace.snapshot", {
      revision: 3,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 2,
        windowed: true,
        nodes: [
          { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
          { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
        ],
      },
    });
    handleNotification("workspace.snapshot", {
      revision: 2,
      active_thread_id: "thread-1",
      provider: "applied-after-stale-snapshot",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 2,
        nodes: [
          { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
          { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
        ],
      },
    });

    expect(uiState.provider).toBe("applied-after-stale-snapshot");
    expect(sent(socket, "snapshot.requested")).toHaveLength(1);
  });
});
