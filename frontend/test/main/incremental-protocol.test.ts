// @ts-nocheck
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetWorkbenchForTest,
  handleItem,
  handleNotification,
  peekPendingLocalMessageTokens,
  validatePendingLocalMessageTokens,
  _peekTranscriptWindowSnapshotForTest,
  _peekTranscriptWindowHeightKeysForTest,
    _peekTranscriptWindowStateForTest,
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
import { sha256 } from "../../src/utils/sha256";

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
let restoreTranscriptGeometry: (() => void) | null = null;

function installTranscriptGeometry(transcript, blockHeight = 30, clientHeight = 50) {
  restoreTranscriptGeometry?.();
  const prototype = Element.prototype;
  const previousPrototype = Object.getOwnPropertyDescriptor(prototype, "getBoundingClientRect");
  const fallback = prototype.getBoundingClientRect;
  const previousGeometry = new Map(
    ["clientHeight", "scrollHeight", "scrollTop"]
      .map((name) => [name, Object.getOwnPropertyDescriptor(transcript, name)]),
  );
  Object.defineProperty(transcript, "clientHeight", {
    configurable: true,
    value: clientHeight,
  });
  const rect = (top, height) => ({
    top,
    bottom: top + height,
    left: 0,
    right: 0,
    width: 0,
    height,
    x: 0,
    y: top,
    toJSON: () => ({}),
  });
  Object.defineProperty(prototype, "getBoundingClientRect", {
    configurable: true,
    value: function getTranscriptTestRect() {
      const element = this;
      if (element === transcript) {
        return rect(0, Math.max(1, Number(transcript.clientHeight) || 0));
      }
      const directIndex = element.parentElement === transcript
        ? Array.from(transcript.children).indexOf(element)
        : -1;
      const detachedBlock = element.dataset?.reconcileKey !== undefined && !element.isConnected;
      if (directIndex < 0 && !detachedBlock) return fallback.call(this);
      if (directIndex < 0) return rect(0, blockHeight);
      let top = 0;
      for (const child of Array.from(transcript.children).slice(0, directIndex)) {
        const spacerHeight = child.dataset.transcriptSpacer !== undefined
          ? Number.parseFloat(child.style.height)
          : NaN;
        top += Number.isFinite(spacerHeight) && spacerHeight > 0 ? spacerHeight : blockHeight;
      }
      return rect(top - (Number(transcript.scrollTop) || 0), blockHeight);
    },
  });
  restoreTranscriptGeometry = () => {
    if (previousPrototype) Object.defineProperty(prototype, "getBoundingClientRect", previousPrototype);
    else Reflect.deleteProperty(prototype, "getBoundingClientRect");
    for (const [name, descriptor] of previousGeometry) {
      if (descriptor) Object.defineProperty(transcript, name, descriptor);
      else Reflect.deleteProperty(transcript, name);
    }
    restoreTranscriptGeometry = null;
  };
}

afterEach(() => {
  restoreTranscriptGeometry?.();
});



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

    it.each([
        [
            "text byte length",
            (text) => ({
                text_byte_length: new TextEncoder().encode(text).length + 1,
                content_hash: sha256(text),
            }),
        ],
        [
            "content hash",
            (text) => ({
                text_byte_length: new TextEncoder().encode(text).length,
                content_hash: "0".repeat(64),
            }),
        ],
    ])(
        "rejects a stream commit with mismatched %s and keeps recovery blocking repeated completion",
        (_label, mismatch) => {
            const harness = controlledCanonicalCoordinator();
            _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
            const socket = fakeSocket();
            _setSocket(socket);
            const text = "你好, stream 🌍";
            const validIntegrity = {
                revision: 1,
                stream_id: "stream-unicode",
                text_byte_length: new TextEncoder().encode(text).length,
                content_hash: sha256(text),
            };
            assistantItem("item.started", {
                op: "replace",
                revision: 0,
                stream_id: "stream-unicode",
                text: "",
                phase: "text",
            });
            assistantItem("item.delta", {
                op: "append",
                base_revision: 0,
                revision: 1,
                stream_id: "stream-unicode",
                text,
                phase: "text",
            });

            assistantItem("item.completed", {
                ...validIntegrity,
                ...mismatch(text),
                phase: "text",
            });
            assistantItem("item.completed", { ...validIntegrity, phase: "text" });

            const body = document.querySelector("#transcript .stream-buffer .markdown-body");
            expect(body).not.toBeNull();
            expect(body.dataset.renderPending).toBeUndefined();
            expect(body.textContent).toContain(text);
            expect(sent(socket, "snapshot.requested")).toEqual([
                expect.objectContaining({ params: { thread_id: "thread-1" } }),
            ]);
        },
    );

    it("commits an incremental stream with matching Unicode byte length and hash", () => {
        const harness = controlledCanonicalCoordinator();
        _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
        const socket = fakeSocket();
        _setSocket(socket);
        const text = "你好, stream 🌍";
        assistantItem("item.started", {
            op: "replace",
            revision: 0,
            stream_id: "stream-unicode",
            text: "",
            phase: "text",
        });
        assistantItem("item.delta", {
            op: "append",
            base_revision: 0,
            revision: 1,
            stream_id: "stream-unicode",
            text,
            phase: "text",
        });
        assistantItem("item.completed", {
            revision: 1,
            stream_id: "stream-unicode",
            text_byte_length: new TextEncoder().encode(text).length,
            content_hash: sha256(text),
            phase: "text",
        });

        const body = document.querySelector("#transcript .stream-buffer .markdown-body");
        expect(body).not.toBeNull();
        expect(body.dataset.renderPending).toBe("true");
        expect(sent(socket, "snapshot.requested")).toHaveLength(0);

        harness.settle();
        expect(body.dataset.renderPending).toBeUndefined();
        expect(body.innerHTML).toBe(renderMarkdown(text).innerHTML);
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

  it("preserves a historical scroll position when a subsequent ordinary snapshot arrives", async () => {
    const transcript = document.querySelector("#transcript");
    installTranscriptGeometry(transcript, 30, 50);
    Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 300 });

    snapshot(1, Array.from({ length: 10 }, (_, index) => ({
      node_type: "message",
      id: `scroll-${index}`,
      payload: { style: "text", raw_text: `scroll ${index}` },
    })));

    transcript.scrollTop = 90;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();
    const historicalScrollTop = transcript.scrollTop;

    snapshot(2, [{
      node_type: "message",
      id: "scroll-9",
      payload: { style: "text", raw_text: "scroll 9 updated" },
    }]);

    expect(transcript.scrollTop).toBe(historicalScrollTop);
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

  it("merges a windowed snapshot update into the complete canonical window model", () => {
    const nodes = Array.from({ length: 241 }, (_, index) => ({
      node_type: "message",
      id: `canonical-window-${index}`,
      payload: { style: "text", raw_text: `canonical ${index}` },
    }));
    snapshot(1, nodes, true);

    snapshot(2, [{
      node_type: "message",
      id: "canonical-window-240",
      payload: { style: "text", raw_text: "canonical updated" },
    }], true);

    const canonical = _peekTranscriptWindowSnapshotForTest("thread-1");
    expect(canonical?.nodes).toHaveLength(241);
    expect(canonical?.nodes[0].id).toBe("canonical-window-0");
    expect(canonical?.nodes[240].payload.raw_text).toBe("canonical updated");
  });

  it("drops cached heights for blocks removed by a full snapshot", () => {
    snapshot(1, Array.from({ length: 241 }, (_, index) => ({
      node_type: "turn",
      id: `height-old-${index}`,
      header: `height old ${index}`,
    })), true);
    expect(
      _peekTranscriptWindowHeightKeysForTest("thread-1")
        ?.some((key) => key.startsWith("node:height-old-")),
    ).toBe(true);

    snapshot(2, [
      { node_type: "turn", id: "height-new-0", header: "height new 0" },
      { node_type: "turn", id: "height-new-1", header: "height new 1" },
    ]);

    const keys = _peekTranscriptWindowHeightKeysForTest("thread-1") ?? [];
    expect(keys.some((key) => key.startsWith("node:height-old-"))).toBe(false);
    expect(keys).toContain("node:height-new-1");
    expect(keys).not.toContain("node:height-new-0");
  });
it("schedules a trim after a fallback full attach of a windowed snapshot", async () => {
    installTranscriptGeometry(document.querySelector("#transcript"));
    snapshot(1, [
      { node_type: "message", id: "preexisting", payload: { style: "text", raw_text: "pre" } },
    ]);

    snapshot(2, Array.from({ length: 241 }, (_, index) => ({
      node_type: "turn",
      id: `fallback-${index}`,
      header: `fallback ${index}`,
    })), true);

    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelectorAll("[data-reconcile-key]").length).toBeGreaterThan(240);

    await Promise.resolve();

    expect(transcript.querySelectorAll("[data-reconcile-key]").length).toBeLessThanOrEqual(240);
    expect(transcript.querySelector("[data-transcript-spacer]")).not.toBeNull();
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


  it("invalidates the complete pending-local token set when canonical handoff settles", () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const input = document.querySelector("#input");
    const composer = document.querySelector("#composer");
    input.value = "pending token";
    composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    const transcript = document.querySelector("#transcript");
    const pending = transcript.querySelector('[data-item-id^="user-"]');
    const tokens = peekPendingLocalMessageTokens("thread-1");
    expect(tokens).toHaveLength(1);
    expect(tokens[0].element).toBe(pending);
    expect(validatePendingLocalMessageTokens("thread-1", tokens)).toBe(true);

    snapshot(1, [
      { node_type: "turn", id: "server-pending-token", header: "pending token" },
    ]);

    expect(validatePendingLocalMessageTokens("thread-1", tokens)).toBe(false);
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

  it("escalates a malformed full snapshot over an existing window to blocked recovery", async () => {
    vi.useFakeTimers();
    try {
      const socket = eventSocket(WebSocket.OPEN);
      _setSocket(socket);
      snapshot(1, [
        { node_type: "message", id: "window-a", payload: { style: "text", raw_text: "A" } },
      ], true);

      snapshot(2, [
        { node_type: "message", id: "duplicate-node", payload: { style: "text", raw_text: "A" } },
        { node_type: "message", id: "duplicate-node", payload: { style: "text", raw_text: "B" } },
      ]);

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
      installTranscriptGeometry(transcript);
      const boundary = document.createElement("div");
      boundary.dataset.pendingItemId = "damaged-boundary";
      transcript.insertBefore(boundary, transcript.querySelector('[data-reconcile-key="node:segment-b"]'));
      snapshot(3, [
        { node_type: "message", id: "segment-b", payload: { style: "text", raw_text: "B" } },
        { node_type: "message", id: "segment-a", payload: { style: "text", raw_text: "A" } },
      ], true);
      expect(sent(socket, "snapshot.requested")).toHaveLength(1);

      snapshot(2, Array.from({ length: 241 }, (_, index) => ({
        node_type: "message",
        id: `recovered-${index}`,
        payload: { style: "text", raw_text: `recovered ${index}` },
      })));

      expect(boundary.isConnected).toBe(false);
      expect(transcript.querySelector('[data-reconcile-key="node:recovered-240"]')).not.toBeNull();
      expect(_peekTranscriptWindowSnapshotForTest("thread-1")?.nodes).toHaveLength(241);
      expect(transcript.querySelectorAll("[data-reconcile-key]")).toHaveLength(241);

      await Promise.resolve();

      expect(transcript.querySelectorAll("[data-reconcile-key]").length).toBeLessThanOrEqual(240);
      expect(transcript.querySelector("[data-transcript-spacer]")).not.toBeNull();
      await vi.advanceTimersByTimeAsync(1000);
      expect(sent(socket, "snapshot.requested")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });


  it("requests an earlier page when replan moves the scroll position after a top scroll", async () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const transcript = document.querySelector("#transcript");
    installTranscriptGeometry(transcript);

    handleNotification("workspace.snapshot", {
      revision: 1,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 1,
        windowed: true,
        before_turn_id: 100,
        has_earlier: true,
        nodes: Array.from({ length: 241 }, (_, index) => ({
          node_type: "turn",
          id: `replan-scroll-${index}`,
          header: `item ${index}`,
        })),
      },
    });
    expect(transcript.querySelectorAll("[data-reconcile-key]").length).toBeGreaterThan(0);

    let scrollTop = 0;
    let replanMovedScroll = false;
    Object.defineProperty(transcript, "scrollTop", {
      configurable: true,
      get: () => (replanMovedScroll ? 100 : scrollTop),
      set: (value) => {
        if (!replanMovedScroll) scrollTop = Number(value) || 0;
      },
    });
    const originalReplaceChildren = transcript.replaceChildren.bind(transcript);
    transcript.replaceChildren = (...children) => {
      originalReplaceChildren(...children);
      replanMovedScroll = true;
    };

    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();

    transcript.replaceChildren = originalReplaceChildren;
    expect(replanMovedScroll).toBe(true);
    expect(sent(socket, "transcript.page")).toHaveLength(1);
  });


    it("does not call replaceChildren when scrolling within the attached window budget", async () => {
        const socket = eventSocket(WebSocket.OPEN);
        _setSocket(socket);
        const transcript = document.querySelector("#transcript") as HTMLElement;
        installTranscriptGeometry(transcript, 100, 96);

        handleNotification("workspace.snapshot", {
            revision: 1,
            active_thread_id: "thread-1",
            threads: [{ thread_id: "thread-1" }],
            active_snapshot: {
                thread_id: "thread-1",
                revision: 1,
                windowed: true,
                before_turn_id: 100,
                has_earlier: false,
                nodes: Array.from({ length: 50 }, (_, index) => ({
                    node_type: "turn",
                    id: `no-flicker-${index}`,
                    header: `item ${index}`,
                })),
            },
        });

        await Promise.resolve();

        // Materialize and settle window around middle items
        transcript.scrollTop = 25 * 96;
        transcript.dispatchEvent(new Event("scroll"));
        await Promise.resolve();

        transcript.scrollTop = 25 * 96 + 2;
        transcript.dispatchEvent(new Event("scroll"));
        await Promise.resolve();

        let replaceChildrenCount = 0;
        const originalReplaceChildren = transcript.replaceChildren.bind(transcript);
        transcript.replaceChildren = (...children) => {
            replaceChildrenCount += 1;
            originalReplaceChildren(...children);
        };

        try {
            // Minor scroll within the already materialized overscan window
            transcript.scrollTop = 25 * 96 + 10;
            transcript.dispatchEvent(new Event("scroll"));
            await Promise.resolve();

            expect(replaceChildrenCount).toBe(0);
        } finally {
            transcript.replaceChildren = originalReplaceChildren;
        }
    });

  it("releases the pagination lock after a successful earlier page", async () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const transcript = document.querySelector("#transcript");
    const client = await import("../../src/rpc/client");

    handleNotification("workspace.snapshot", {
      revision: 1,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 1,
        windowed: true,
        before_turn_id: 100,
        has_earlier: true,
        nodes: [{ node_type: "turn", id: "page-lock-a", header: "A" }],
      },
    });

    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();
    const firstRequest = sent(socket, "transcript.page")[0];
    expect(firstRequest).toBeDefined();

    client._resolvePendingForTest(firstRequest.id, {
      thread_id: "thread-1",
      revision: 2,
      windowed: true,
      before_turn_id: 50,
      after_turn_id: 99,
      has_earlier: true,
      has_later: true,
      nodes: [
        { node_type: "turn", id: "page-lock-before", header: "Before" },
        { node_type: "turn", id: "page-lock-a", header: "A" },
      ],
    });
    await Promise.resolve();
    await Promise.resolve();

    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();

    expect(sent(socket, "transcript.page")).toHaveLength(2);
  });


  it("does not clear a newer window state's pagination lock when a stale page settles", async () => {
    const socket = eventSocket(WebSocket.OPEN);
    _setSocket(socket);
    const transcript = document.querySelector("#transcript");
    const client = await import("../../src/rpc/client");

    handleNotification("workspace.snapshot", {
      revision: 1,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 1,
        windowed: true,
        before_turn_id: 100,
        has_earlier: true,
        nodes: [{ node_type: "turn", id: "page-lock-a", header: "A" }],
      },
    });
    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();
    expect(sent(socket, "transcript.page")).toHaveLength(1);
    const staleRequest = sent(socket, "transcript.page")[0];

    snapshot(2, [
      { node_type: "message", id: "dup-node", payload: { style: "text", raw_text: "x" } },
      { node_type: "message", id: "dup-node", payload: { style: "text", raw_text: "y" } },
    ]);
    expect(sent(socket, "snapshot.requested")).toHaveLength(1);

    handleNotification("workspace.snapshot", {
      revision: 3,
      active_thread_id: "thread-1",
      threads: [{ thread_id: "thread-1" }],
      active_snapshot: {
        thread_id: "thread-1",
        revision: 3,
        before_turn_id: 50,
        has_earlier: true,
        nodes: [{ node_type: "turn", id: "page-lock-b", header: "B" }],
      },
    });
    await Promise.resolve();

    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();
    expect(sent(socket, "transcript.page")).toHaveLength(2);

    client._resolvePendingForTest(staleRequest.id, {
      thread_id: "thread-1",
      revision: 2,
      windowed: true,
      nodes: [],
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    transcript.dispatchEvent(new Event("scroll"));
    await Promise.resolve();
    expect(sent(socket, "transcript.page")).toHaveLength(2);
  });

    it("requests an earlier cursor page with the negotiated page size", async () => {
        const socket = eventSocket(WebSocket.OPEN);
        _setSocket(socket);
        const transcript = document.querySelector("#transcript");

        handleNotification("workspace.snapshot", {
            revision: 1,
            active_thread_id: "thread-1",
            threads: [{ thread_id: "thread-1" }],
            active_snapshot: {
                thread_id: "thread-1",
                revision: 1,
                windowed: true,
                before_cursor: "cursor-a",
                before_turn_id: 100,
                transcript_epoch: "epoch-a",
                has_earlier: true,
                nodes: [{ node_type: "turn", id: "cursor-current", header: "Current" }],
            },
        });

        transcript.scrollTop = 0;
        transcript.dispatchEvent(new Event("scroll"));
        await Promise.resolve();

        const request = sent(socket, "transcript.page")[0];
        expect(request?.params).toEqual({
            thread_id: "thread-1",
            before_cursor: "cursor-a",
            turn_limit: 40,
        });
        expect(request?.params).not.toHaveProperty("before_turn_id");
    });

    it("ignores an earlier page from a different transcript epoch", async () => {
        const socket = eventSocket(WebSocket.OPEN);
        _setSocket(socket);
        const transcript = document.querySelector("#transcript");
        const client = await import("../../src/rpc/client");

        handleNotification("workspace.snapshot", {
            revision: 1,
            active_thread_id: "thread-1",
            threads: [{ thread_id: "thread-1" }],
            active_snapshot: {
                thread_id: "thread-1",
                revision: 1,
                windowed: true,
                before_cursor: "cursor-a",
                before_turn_id: 100,
                transcript_epoch: "epoch-a",
                has_earlier: true,
                nodes: [{ node_type: "turn", id: "epoch-current", header: "Current" }],
            },
        });
        const beforeSnapshot = structuredClone(_peekTranscriptWindowSnapshotForTest("thread-1"));
        const beforeHtml = transcript.innerHTML;

        transcript.scrollTop = 0;
        transcript.dispatchEvent(new Event("scroll"));
        await Promise.resolve();
        const request = sent(socket, "transcript.page")[0];
        expect(request).toBeDefined();

        client._resolvePendingForTest(request.id, {
            thread_id: "thread-1",
            revision: 2,
            windowed: true,
            before_cursor: "cursor-b",
            before_turn_id: 50,
            after_turn_id: 99,
            transcript_epoch: "epoch-b",
            has_earlier: false,
            nodes: [
                { node_type: "turn", id: "wrong-epoch-new", header: "Must not merge" },
                { node_type: "turn", id: "epoch-current", header: "Current" },
            ],
        });
        await Promise.resolve();
        await Promise.resolve();

        expect(_peekTranscriptWindowSnapshotForTest("thread-1")).toEqual(beforeSnapshot);
        expect(transcript.innerHTML).toBe(beforeHtml);
        expect(transcript.querySelector('[data-reconcile-key="node:wrong-epoch-new"]')).toBeNull();
    });

    it("prepends an earlier page from the matching epoch and advances its cursor", async () => {
        const socket = eventSocket(WebSocket.OPEN);
        _setSocket(socket);
        const transcript = document.querySelector("#transcript");
        const client = await import("../../src/rpc/client");

        handleNotification("workspace.snapshot", {
            revision: 1,
            active_thread_id: "thread-1",
            threads: [{ thread_id: "thread-1" }],
            active_snapshot: {
                thread_id: "thread-1",
                revision: 1,
                windowed: true,
                before_cursor: "cursor-a",
                before_turn_id: 100,
                transcript_epoch: "epoch-a",
                has_earlier: true,
                nodes: [{ node_type: "turn", id: "matching-current", header: "Current" }],
            },
        });

        transcript.scrollTop = 0;
        transcript.dispatchEvent(new Event("scroll"));
        await Promise.resolve();
        const request = sent(socket, "transcript.page")[0];
        expect(request).toBeDefined();

        client._resolvePendingForTest(request.id, {
            thread_id: "thread-1",
            revision: 2,
            windowed: true,
            before_cursor: "cursor-b",
            before_turn_id: 50,
            after_turn_id: 99,
            transcript_epoch: "epoch-a",
            has_earlier: false,
            nodes: [
                { node_type: "turn", id: "matching-new", header: "Earlier" },
                { node_type: "turn", id: "matching-current", header: "Current" },
            ],
        });
        await Promise.resolve();
        await Promise.resolve();

        const snapshot = _peekTranscriptWindowSnapshotForTest("thread-1");
        expect(snapshot?.before_cursor).toBe("cursor-b");
        expect(snapshot?.transcript_epoch).toBe("epoch-a");
        expect(snapshot?.nodes.map((node) => node.id)).toEqual(["matching-new", "matching-current"]);
    });

    it("skips replanning when scrolling safely inside attached overscan cushion and replans near spacer", async () => {
        const socket = eventSocket(WebSocket.OPEN);
        _setSocket(socket);
        const transcript = document.querySelector("#transcript");
        const originalClientHeight = Object.getOwnPropertyDescriptor(transcript, "clientHeight");
        Object.defineProperty(transcript, "clientHeight", { configurable: true, value: 600 });
        try {
            const nodes = Array.from({ length: 300 }, (_, i) => ({
                node_type: "turn",
                id: `hysteresis-node-${i}`,
                header: `Node ${i}`,
            }));

            handleNotification("workspace.snapshot", {
                revision: 1,
                active_thread_id: "thread-hysteresis",
                threads: [{ thread_id: "thread-hysteresis" }],
                active_snapshot: {
                    thread_id: "thread-hysteresis",
                    revision: 1,
                    windowed: true,
                    has_earlier: false,
                    nodes,
                },
            });

            const stateBefore = _peekTranscriptWindowStateForTest("thread-hysteresis");
            expect(stateBefore).not.toBeNull();
            const initialGeneration = stateBefore.generation;

            // In initial install with clientHeight=600, tail nodes (~270..299) are attached.
            // topSpacer ends at ~26000.
            const topSpacer = stateBefore.spacerSegments[0];
            expect(topSpacer).toBeDefined();

            // Safe scroll: well below topSpacer.canonicalEndPx by > 1200px.
            transcript.scrollTop = topSpacer.canonicalEndPx + 1500;
            transcript.dispatchEvent(new Event("scroll"));
            await Promise.resolve();

            // Safe scroll (distance to spacer is > threshold): no replan!
            const stateAfterSafeScroll = _peekTranscriptWindowStateForTest("thread-hysteresis");
            expect(stateAfterSafeScroll.generation).toBe(initialGeneration);

            // Near-spacer scroll (moves close to canonicalEndPx, within threshold): triggers replan!
            transcript.scrollTop = topSpacer.canonicalEndPx + 100;
            transcript.dispatchEvent(new Event("scroll"));
            await Promise.resolve();

            const stateAfterNearSpacerScroll = _peekTranscriptWindowStateForTest("thread-hysteresis");
            expect(stateAfterNearSpacerScroll.generation).toBeGreaterThan(initialGeneration);
        } finally {
            if (originalClientHeight) Object.defineProperty(transcript, "clientHeight", originalClientHeight);
            else Reflect.deleteProperty(transcript, "clientHeight");
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
