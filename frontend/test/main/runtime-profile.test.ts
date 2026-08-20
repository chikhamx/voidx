// @ts-nocheck
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import { setConnectionStatus, uiState, _resetWorkbenchStateForTest } from "../../src/services/state";
import {
  _resetForTest as _resetStreamForTest,
  appendStreamText,
  commitStream,
  setTranscriptElement,
} from "../../src/utils/stream";

function fakeSocket() {
  const socket = {
    readyState: WebSocket.OPEN,
    send: vi.fn(),
    onmessage: null,
    addEventListener(type, handler) {
      if (type === "message") socket.onmessage = handler;
    },
  };
  return socket;
}

function sentCreateProfiles(socket) {
  return socket.send.mock.calls
    .map(([data]) => JSON.parse(data))
    .filter((message) => message.method === "session.create")
    .map((message) => message.params.profile);
}

describe("desktop runtime profile creation", () => {
  beforeEach(() => {
    for (const id of ["btn-new-chat", "btn-new-chat-restricted", "btn-new-loop", "btn-new-goal"]) {
      document.querySelector(`#${id}`)?.remove();
    }
    document.body.append(
      ...["btn-new-chat", "btn-new-chat-restricted", "btn-new-loop", "btn-new-goal"]
        .map((id) => {
          const button = document.createElement("button");
          button.id = id;
          return button;
        }),
    );
  });
  afterEach(() => {
    for (const id of ["btn-new-chat", "btn-new-chat-restricted", "btn-new-loop", "btn-new-goal"]) {
      document.querySelector(`#${id}`)?.remove();
    }
  });


  it("creates a temporary session with the active runtime profile", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    await import("../../src/main");
    uiState.workspace = "/tmp/imcore-sdk";
    uiState.runtimeProfile = "chat";

    document.querySelector("#btn-new-chat").click();

    const message = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.create");
    expect(message.params).toEqual({ directory: "/tmp/imcore-sdk", profile: "chat" });
  });
});


describe("desktop runtime profile switching", () => {
  beforeEach(() => {
    _resetRpcForTest();
    _resetWorkbenchStateForTest();
    _resetStreamForTest();
    setTranscriptElement(document.querySelector("#transcript"));
    document.querySelector("#transcript").replaceChildren();
  });

  it("sends session.switch and applies the returned runtime profile", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { switchThread } = await import("../../src/main");
    const { uiState } = await import("../../src/services/state");

    const pending = switchThread("thread-goal");
    const request = JSON.parse(socket.send.mock.calls[0][0]);
    expect(request).toMatchObject({
      method: "session.switch",
      params: { thread_id: "thread-goal" },
    });
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, {
      active_thread_id: "thread-goal",
      runtime_profile: "goal",
    });
    await pending;

    expect(uiState.sessionId).toBe("thread-goal");
    expect(uiState.runtimeProfile).toBe("goal");
  });

  it("blocks sending while an explicit thread switch is pending", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { switchThread } = await import("../../src/main");
    uiState.sessionId = "thread-old";
    const pending = switchThread("thread-new");
    const request = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");

    expect(uiState.isSwitchingThread).toBe(true);
    expect(document.querySelector("#input").disabled).toBe(true);
    expect(document.querySelector("#btn-send").disabled).toBe(true);

    document.querySelector("#input").value = "不要发到旧会话";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    expect(socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .filter((entry) => entry.method === "session.submit")).toHaveLength(0);

    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, { active_thread_id: "thread-new" });
    await pending;
    expect(uiState.isSwitchingThread).toBe(false);
    expect(document.querySelector("#input").disabled).toBe(false);
  });

  it("ignores a stale thread switch result after a newer selection", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { switchThread } = await import("../../src/main");
    uiState.sessionId = "thread-old";

    const stale = switchThread("thread-b");
    const staleRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "thread-b");
    const latest = switchThread("thread-c");
    const latestRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "thread-c");
    const client = await import("../../src/rpc/client");

    client._resolvePendingForTest(latestRequest.id, { active_thread_id: "thread-c" });
    await latest;
    client._resolvePendingForTest(staleRequest.id, { active_thread_id: "thread-b" });
    await stale;

    expect(uiState.sessionId).toBe("thread-c");
  });

  it("creates the selected profile instead of submitting an unsupported slash command", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    await import("../../src/main");
    socket.send.mockClear();
    uiState.sessionId = "";
    uiState.runtimeProfile = "coding";

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    const listRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "list-agent-profiles");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(listRequest.id, {
      profiles: [{
        name: "custom-goal",
        display_name: "Custom Goal",
        revision: 1,
        content_hash: "hash",
        source: "project",
        run_mode: "goal",
        hitl_mode: "autonomous",
        availability: "available",
        diagnostics: [],
      }],
    });
    await Promise.resolve();
    await Promise.resolve();
    document.querySelector<HTMLButtonElement>('[data-profile="custom-goal"]')!.click();
    const refreshRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .filter((entry) => entry.method === "list-agent-profiles")
      .at(-1);
    client._resolvePendingForTest(refreshRequest.id, {
      profiles: [{
        name: "custom-goal",
        display_name: "Custom Goal",
        revision: 1,
        content_hash: "hash",
        source: "project",
        run_mode: "goal",
        hitl_mode: "autonomous",
        availability: "available",
        diagnostics: [],
      }],
    });
    await Promise.resolve();
    await Promise.resolve();

    const messages = socket.send.mock.calls.map(([data]) => JSON.parse(data));
    expect(messages).toContainEqual(expect.objectContaining({
      method: "session.create",
      params: { directory: "", profile: "custom-goal" },
    }));
    expect(messages).not.toContainEqual(expect.objectContaining({
      method: "session.submit",
      params: expect.objectContaining({ text: "/goal" }),
    }));
  });

  it("does not restore committed coding output after switching to an empty chat thread", async () => {
    const { handleNotification } = await import("../../src/main");
    const transcript = document.querySelector("#transcript");
    uiState.sessionId = "old-coding-thread";
    uiState.runtimeProfile = "coding";
    appendStreamText("coding-reply", "我是编码助手", "text");
    commitStream("coding-reply");
    expect(transcript.textContent).toContain("编码助手");

    handleNotification("workspace.snapshot", {
      active_thread_id: "new-chat-thread",
      threads: [{ thread_id: "new-chat-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "new-chat-thread", revision: 0, nodes: [] },
    });

    expect(uiState.runtimeProfile).toBe("chat");
    expect(transcript.textContent).not.toContain("编码助手");
    expect(transcript.children).toHaveLength(0);
  });

  it("keeps committed output when refreshing the same thread snapshot", async () => {
    const { handleNotification } = await import("../../src/main");
    const transcript = document.querySelector("#transcript");
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    appendStreamText("chat-reply", "对话回复", "text");
    commitStream("chat-reply");

    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes: [] },
    });

    expect(transcript.textContent).toContain("对话回复");
  });

  it("keeps a locally echoed chat message while the snapshot is still empty", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { handleNotification } = await import("../../src/main");
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";

    document.querySelector("#input").value = "你好";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    const transcript = document.querySelector("#transcript");
    expect(transcript.textContent).toContain("你好");

    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes: [] },
    });

    expect(transcript.textContent).toContain("你好");
  });

  it("renders guidance only after the server preview arrives", async () => {
    const { _resetWorkbenchForTest, handleItem } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    uiState.isRunning = true;

    document.querySelector("#input").value = "继续这个方向";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    expect(document.querySelector("#transcript").textContent).not.toContain("继续这个方向");
    handleItem("item.started", {
      thread_id: "chat-thread",
      turn_id: "turn-guidance",
      kind: "message",
      item_id: "server-guidance",
      data: { style: "guidance", text: "继续这个方向" },
    });

    expect(document.querySelectorAll("#transcript .message-guidance")).toHaveLength(1);
    expect(document.querySelector("#transcript").textContent).toContain("继续这个方向");
  });

  it("confirms only stable user snapshot nodes and does not count arbitrary messages", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    const snapshot = (nodes) => ({
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes },
    });

    handleNotification("workspace.snapshot", snapshot([
      { id: "system-1", node_type: "message", payload: { raw_text: "重复文本", style: "error" } },
    ]));
    document.querySelector("#input").value = "重复文本";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    handleNotification("workspace.snapshot", snapshot([
      { id: "system-1", node_type: "message", payload: { raw_text: "重复文本", style: "error" } },
    ]));
    expect(document.querySelector(".notice-toast-region .notice-error")).not.toBeNull();
    expect(document.querySelectorAll("#transcript .message-text")).toHaveLength(1);

    const userSnapshot = snapshot([
      { id: "system-1", node_type: "message", payload: { raw_text: "重复文本", style: "error" } },
      { id: "user-1", node_type: "message", payload: { raw_text: "重复文本", style: "user" } },
    ]);
    handleNotification("workspace.snapshot", userSnapshot);
    handleNotification("workspace.snapshot", userSnapshot);

    expect(document.querySelectorAll("#transcript .message-user")).toHaveLength(1);
    expect(document.querySelectorAll("#transcript .message-text")).toHaveLength(0);
  });

  it("does not consume one stable user node twice for duplicate local messages", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    const snapshot = (nodes) => ({
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes },
    });
    handleNotification("workspace.snapshot", snapshot([]));

    for (let index = 0; index < 2; index += 1) {
      uiState.isRunning = false;
      document.querySelector("#input").value = "连续消息";
      document.querySelector("#composer").dispatchEvent(
        new SubmitEvent("submit", { bubbles: true, cancelable: true }),
      );
    }

    const oneUser = snapshot([
      { id: "user-1", node_type: "message", payload: { raw_text: "连续消息", style: "user" } },
    ]);
    handleNotification("workspace.snapshot", oneUser);
    handleNotification("workspace.snapshot", oneUser);
    expect(document.querySelectorAll("#transcript .message-user")).toHaveLength(1);
    expect(document.querySelectorAll("#transcript .message-text")).toHaveLength(1);

    handleNotification("workspace.snapshot", snapshot([
      { id: "user-1", node_type: "message", payload: { raw_text: "连续消息", style: "user" } },
      { id: "user-2", node_type: "message", payload: { raw_text: "连续消息", style: "user" } },
    ]));
    expect(document.querySelectorAll("#transcript .message-user")).toHaveLength(2);
    expect(document.querySelectorAll("#transcript .message-text")).toHaveLength(0);
  });

  it("uses unique local ids when messages are sent in the same millisecond", async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    const now = vi.spyOn(Date, "now").mockReturnValue(1234);
    try {
      for (let index = 0; index < 2; index += 1) {
        uiState.isRunning = false;
        document.querySelector("#input").value = `消息 ${index}`;
        document.querySelector("#composer").dispatchEvent(
          new SubmitEvent("submit", { bubbles: true, cancelable: true }),
        );
      }
      const ids = Array.from(document.querySelectorAll("#transcript .message-text"))
        .map((item) => item.dataset.itemId);
      expect(ids).toHaveLength(2);
      expect(new Set(ids).size).toBe(2);
    } finally {
      now.mockRestore();
    }
  });

  it("drops a pending local echo when an explicit thread switch is rendered", async () => {
    const { _resetWorkbenchForTest, switchThread, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "old-thread";
    uiState.runtimeProfile = "chat";

    document.querySelector("#input").value = "旧会话消息";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    const switchToNew = switchThread("new-thread");
    const newRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(newRequest.id, { active_thread_id: "new-thread" });
    await switchToNew;
    handleNotification("workspace.snapshot", {
      active_thread_id: "new-thread",
      threads: [{ thread_id: "new-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "new-thread", revision: 0, nodes: [] },
    });

    const switchToOld = switchThread("old-thread");
    const oldRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "old-thread");
    client._resolvePendingForTest(oldRequest.id, { active_thread_id: "old-thread" });
    await switchToOld;
    handleNotification("workspace.snapshot", {
      active_thread_id: "old-thread",
      threads: [{ thread_id: "old-thread", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "old-thread", revision: 0, nodes: [] },
    });

    expect(document.querySelector("#transcript").textContent).not.toContain("旧会话消息");
  });

  it("ignores an old snapshot and item after a completed thread switch", async () => {
    const { _resetWorkbenchForTest, switchThread, handleNotification, handleItem } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "thread-a";
    uiState.runtimeProfile = "chat";

    const switching = switchThread("thread-b");
    const request = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, {
      active_thread_id: "thread-b",
      runtime_profile: "chat",
    });
    await switching;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-b",
      threads: [{ thread_id: "thread-b", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "thread-b", revision: 0, nodes: [] },
    });

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "thread-a",
        revision: 0,
        nodes: [{ id: "old", node_type: "message", payload: { raw_text: "旧快照", style: "user" } }],
      },
    });
    handleItem("item.started", {
      thread_id: "thread-a",
      kind: "message",
      item_id: "old-item",
      data: { style: "text", text: "旧事件" },
    });

    expect(uiState.sessionId).toBe("thread-b");
    expect(document.querySelector("#transcript").textContent).not.toContain("旧快照");
    expect(document.querySelector("#transcript").textContent).not.toContain("旧事件");
  });

  it("does not restore a failed send from an old thread into the active thread", async () => {
    const { _resetWorkbenchForTest, switchThread, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "thread-a";
    uiState.runtimeProfile = "chat";
    document.querySelector("#input").value = "只属于旧会话";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    const submit = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.submit");

    const switching = switchThread("thread-b");
    const switchRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(switchRequest.id, {
      active_thread_id: "thread-b",
      runtime_profile: "chat",
    });
    await switching;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-b",
      threads: [{ thread_id: "thread-b", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "thread-b", revision: 0, nodes: [] },
    });

    socket.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: submit.id,
        error: { code: -32000, message: "旧会话发送失败" },
      }),
    }));
    await vi.waitFor(() => expect(document.querySelector("#input").value).toBe(""));
    expect(document.querySelector("#transcript").textContent).not.toContain("只属于旧会话");
    expect(document.querySelector("#transcript").textContent).not.toContain("旧会话发送失败");
  });

  it("clears the old running state when switching to an idle target thread", async () => {
    const { _resetWorkbenchForTest, switchThread, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "thread-a";
    uiState.isRunning = true;

    const switching = switchThread("thread-b");
    const request = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, {
      active_thread_id: "thread-b",
      runtime_profile: "chat",
      status: "idle",
    });
    await switching;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-b",
      threads: [{ thread_id: "thread-b", runtime_profile: "chat", status: "idle" }],
      active_snapshot: { thread_id: "thread-b", revision: 0, nodes: [] },
    });

    expect(uiState.isRunning).toBe(false);
  });

  it("does not consume a transient turn node as a duplicate local message", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    document.querySelector("#input").value = "暂态输入";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    const turnSnapshot = {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "chat-thread",
        revision: 0,
        nodes: [{ id: "turn-1", node_type: "turn", payload: { raw_text: "暂态输入" } }],
      },
    };
    handleNotification("workspace.snapshot", turnSnapshot);
    handleNotification("workspace.snapshot", turnSnapshot);

    expect(document.querySelectorAll("#transcript .message-user")).toHaveLength(1);
    expect(document.querySelectorAll("#transcript .message-text")).toHaveLength(0);
    expect(document.querySelector("#transcript").textContent).toContain("暂态输入");
  });

  it("keeps a persisted user turn before its assistant reply after snapshot refresh", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";

    document.querySelector("#input").value = "你好";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "chat-thread",
        revision: 1,
        nodes: [
          { id: "turn-1", node_type: "turn", header: "❯ 你好", body_lines: [] },
          {
            id: "assistant-1",
            node_type: "assistant",
            payload: { raw_text: "你好，我是 voidx。" },
            body_lines: ["你好，我是 voidx。"],
          },
        ],
      },
    });

    const items = Array.from(document.querySelector("#transcript").children);
    expect(items.map((item) => item.className)).toEqual([
      "message-item message-user",
      "stream-buffer",
    ]);
    expect(items[0].textContent).toContain("你好");
    expect(items[1].textContent).toContain("你好，我是 voidx。");
  });

  it("does not append a committed reply again after a snapshot includes it", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";

    appendStreamText("reply-1", "第一条回复", "text");
    commitStream("reply-1");
    document.querySelector("#input").value = "第二条消息";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );

    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "chat-thread",
        revision: 2,
        nodes: [
          { id: "turn-1", node_type: "turn", header: "❯ 第一条问题", body_lines: [] },
          {
            id: "assistant-1",
            node_type: "assistant",
            payload: { raw_text: "第一条回复" },
            body_lines: ["第一条回复"],
          },
          { id: "turn-2", node_type: "turn", header: "❯ 第二条消息", body_lines: [] },
        ],
      },
    });

    const items = Array.from(document.querySelector("#transcript").children);
    expect(items.map((item) => item.className)).toEqual([
      "message-item message-user",
      "stream-buffer",
      "message-item message-user",
    ]);
    expect(document.querySelectorAll("#transcript .stream-buffer")).toHaveLength(1);
    expect(document.querySelector("#transcript").textContent).toContain("第一条回复");
  });


  it("updates the active thread runtime state from a snapshot status", async () => {
    const { _resetWorkbenchForTest, handleNotification } = await import("../../src/main");
    _resetWorkbenchForTest();
    uiState.sessionId = "chat-thread";
    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat", status: "running" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes: [] },
    });
    expect(uiState.isRunning).toBe(true);

    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat", status: "idle" }],
      active_snapshot: { thread_id: "chat-thread", revision: 0, nodes: [] },
    });
    expect(uiState.isRunning).toBe(false);
  });

  it("resets transient state when the workbench test state is reset", async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    uiState.isRunning = true;
    uiState.isSwitchingThread = true;
    _resetWorkbenchForTest();
    expect(uiState.isRunning).toBe(false);
    expect(uiState.isSwitchingThread).toBe(false);
  });

  it("keeps the workspace group icon while removing the project heading icon", async () => {
    const { renderSidebar } = await import("../../src/ui/sidebar");
    renderSidebar([
      { thread_id: "thread-a", title: "A", workspace: "/tmp/voidx", runtime_profile: "chat" },
    ], "thread-a", "voidx", "/tmp/voidx");
    expect(document.querySelector(".vx-project-heading .vx-sidebar-row-icon")).toBeNull();
    expect(document.querySelector(".vx-workspace-session-row .vx-sidebar-row-icon")).not.toBeNull();
  });


  it("blocks submission to the old thread until the selected profile is active", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { openThreadForProfile } = await import("../../src/main");
    const client = await import("../../src/rpc/client");
    uiState.sessionId = "old-coding-thread";
    uiState.runtimeProfile = "coding";
    socket.send.mockClear();

    const pending = openThreadForProfile("chat");
    const create = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.create");

    expect(uiState.isSwitchingProfile).toBe(true);
    expect(document.querySelector("#input").disabled).toBe(true);
    expect(document.querySelector("#btn-send").disabled).toBe(true);

    document.querySelector("#input").value = "你好";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    expect(socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .filter((entry) => entry.method === "session.submit")).toHaveLength(0);

    client._resolvePendingForTest(create.id, {
      thread_id: "new-chat-thread",
      active_thread_id: "new-chat-thread",
      runtime_profile: "chat",
      status: "idle",
      temporary: true,
    });
    await pending;

    expect(uiState.sessionId).toBe("new-chat-thread");
    expect(uiState.runtimeProfile).toBe("chat");
    expect(uiState.isSwitchingProfile).toBe(false);
    expect(document.querySelector("#input").disabled).toBe(false);
  });

  it("restores the composer when profile creation fails", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { openThreadForProfile } = await import("../../src/main");
    uiState.sessionId = "old-coding-thread";
    uiState.runtimeProfile = "coding";
    socket.send.mockClear();

    const pending = openThreadForProfile("loop");
    const create = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.create");
    socket.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({ jsonrpc: "2.0", id: create.id, error: { code: -32000, message: "create failed" } }),
    }));
    await pending;

    expect(uiState.sessionId).toBe("old-coding-thread");
    expect(uiState.runtimeProfile).toBe("coding");
    expect(uiState.isSwitchingProfile).toBe(false);
    expect(document.querySelector("#input").disabled).toBe(false);
    expect(document.querySelector("#btn-send").disabled).toBe(false);
  });
});


describe("runtime profile snapshot and connection recovery", () => {
  let handleNotification;

  beforeEach(async () => {
    ({ handleNotification } = await import("../../src/main"));
    const state = await import("../../src/services/state");
    state._resetWorkbenchStateForTest();
  });

  it("restores the active thread profile from a workspace snapshot", () => {
    uiState.runtimeProfile = "goal";
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-loop",
      threads: [
        { thread_id: "thread-loop", runtime_profile: "loop" },
      ],
      active_snapshot: { thread_id: "thread-loop", revision: 0, nodes: [] },
      runtime_profile: "goal",
    });

    expect(uiState.sessionId).toBe("thread-loop");
    expect(uiState.runtimeProfile).toBe("loop");
  });

  it("does not replace the current profile when snapshot profile is blank or absent", () => {
    uiState.runtimeProfile = "goal";
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-goal",
      threads: [{ thread_id: "thread-goal", runtime_profile: "   " }],
      active_snapshot: { thread_id: "thread-goal", revision: 0, nodes: [] },
    });
    expect(uiState.runtimeProfile).toBe("goal");

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-goal",
      threads: [{ thread_id: "thread-goal" }],
      active_snapshot: { thread_id: "thread-goal", revision: 1, nodes: [] },
    });
    expect(uiState.runtimeProfile).toBe("goal");
  });

  it("keeps the current profile visible while the connection reports an error", () => {
    uiState.runtimeProfile = "loop";
    setConnectionStatus("disconnected", "Connection error");
    expect(uiState.runtimeProfile).toBe("loop");
    expect(uiState.connection).toBe("disconnected");
  });
});


describe("cross-thread activation boundaries", () => {
  beforeEach(async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    _resetWorkbenchForTest();
    _resetRpcForTest();
    _resetStreamForTest();
    setTranscriptElement(document.querySelector("#transcript"));
    document.querySelector("#transcript").replaceChildren();
  });

  it("rejects delayed snapshots and events from an earlier A activation after returning from B", async () => {
    const { switchThread, handleNotification, handleItem } = await import("../../src/main");
    const socket = fakeSocket();
    _setSocket(socket);
    const client = await import("../../src/rpc/client");
    uiState.sessionId = "thread-a";
    uiState.runtimeProfile = "chat";

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "thread-a",
        revision: 1,
        nodes: [],
      },
    });
    handleNotification("turn.started", {
      thread_id: "thread-a",
      turn_id: "old-turn",
    });

    const toB = switchThread("thread-b");
    const bRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "thread-b");
    client._resolvePendingForTest(bRequest.id, { active_thread_id: "thread-b", runtime_profile: "chat" });
    await toB;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-b",
      threads: [{ thread_id: "thread-b", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "thread-b", revision: 2, nodes: [] },
    });

    const toA = switchThread("thread-a");
    const aRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "thread-a");
    client._resolvePendingForTest(aRequest.id, { active_thread_id: "thread-a", runtime_profile: "chat" });
    await toA;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "thread-a",
        revision: 3,
        nodes: [{ id: "current-a", node_type: "message", payload: { raw_text: "当前 A", style: "user" } }],
      },
    });
    handleNotification("turn.started", {
      thread_id: "thread-a",
      turn_id: "current-turn",
    });

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "thread-a",
        revision: 1,
        nodes: [{ id: "old-a", node_type: "message", payload: { raw_text: "旧 A 快照", style: "user" } }],
      },
    });
    handleNotification("turn.completed", {
      thread_id: "thread-a",
      turn_id: "old-turn",
    });
    handleItem("item.started", {
      thread_id: "thread-a",
      turn_id: "old-turn",
      kind: "message",
      item_id: "old-item",
      data: { style: "text", text: "旧 A 事件" },
    });

    expect(uiState.sessionId).toBe("thread-a");
    expect(uiState.isRunning).toBe(true);
    expect(document.querySelector("#transcript").textContent).toContain("当前 A");
    expect(document.querySelector("#transcript").textContent).not.toContain("旧 A 快照");
    expect(document.querySelector("#transcript").textContent).not.toContain("旧 A 事件");
  });

  it("confirms guidance from a snapshot and ignores duplicate preview items", async () => {
    const { handleNotification, handleItem } = await import("../../src/main");
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "chat-thread";
    uiState.runtimeProfile = "chat";
    uiState.isRunning = true;

    document.querySelector("#input").value = "继续这个方向";
    document.querySelector("#composer").dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    handleNotification("workspace.snapshot", {
      active_thread_id: "chat-thread",
      threads: [{ thread_id: "chat-thread", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "chat-thread",
        revision: 1,
        nodes: [{
          id: "guidance-message",
          node_type: "message",
          payload: { raw_text: "继续这个方向", style: "guidance" },
        }],
      },
    });

    expect(document.querySelectorAll("#transcript .message-guidance")).toHaveLength(1);

    handleItem("item.started", {
      thread_id: "chat-thread",
      turn_id: "turn-1",
      kind: "guidance_preview",
      item_id: "guidance-item",
      data: { text: "重复预览" },
    });
    handleItem("item.started", {
      thread_id: "chat-thread",
      turn_id: "turn-1",
      kind: "guidance_preview",
      item_id: "guidance-item",
      data: { text: "重复预览" },
    });

    expect(document.querySelectorAll("#transcript .message-guidance")).toHaveLength(2);
    expect(document.querySelectorAll("#transcript .message-guidance")[1].textContent).toContain("重复预览");
  });

  it("does not show a mode command failure after switching away from its thread", async () => {
    const { switchThread, handleNotification } = await import("../../src/main");
    const socket = fakeSocket();
    _setSocket(socket);
    const client = await import("../../src/rpc/client");
    uiState.sessionId = "thread-a";
    uiState.runtimeProfile = "loop";

    document.querySelector("#mode-status").click();
    const submit = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.submit");
    expect(submit.params.thread_id).toBe("thread-a");

    const switching = switchThread("thread-b");
    const switchRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");
    client._resolvePendingForTest(switchRequest.id, { active_thread_id: "thread-b", runtime_profile: "chat" });
    await switching;
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-b",
      threads: [{ thread_id: "thread-b", runtime_profile: "chat" }],
      active_snapshot: { thread_id: "thread-b", revision: 1, nodes: [] },
    });

    socket.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: submit.id,
        error: { code: -32000, message: "旧模式命令失败" },
      }),
    }));
    await vi.waitFor(() => expect(uiState.sessionId).toBe("thread-b"));

    expect(document.querySelector("#transcript").textContent).not.toContain("旧模式命令失败");
  });

  it("clears active streams through the complete workbench test reset", async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    const { appendStreamText } = await import("../../src/utils/stream");
    setTranscriptElement(document.querySelector("#transcript"));
    appendStreamText("leaked-stream", "不应泄漏", "text");
    expect(document.querySelector("#transcript .stream-buffer")).not.toBeNull();

    _resetWorkbenchForTest();

    expect(document.querySelector("#transcript .stream-buffer")).toBeNull();
  });
});


describe("strict metadata boundaries and complete reset", () => {
  beforeEach(async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    _resetWorkbenchForTest();
    _resetRpcForTest();
    _resetStreamForTest();
    setTranscriptElement(document.querySelector("#transcript"));
    document.querySelector("#transcript").replaceChildren();
  });

  it("rejects unscoped stale snapshots and turn/item events after a scoped context exists", async () => {
    const { handleNotification, handleItem } = await import("../../src/main");
    const socket = fakeSocket();
    _setSocket(socket);
    uiState.sessionId = "thread-a";

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        thread_id: "thread-a",
        revision: 4,
        nodes: [{ id: "current", node_type: "message", payload: { raw_text: "当前内容", style: "user" } }],
      },
    });
    handleNotification("turn.started", {
      thread_id: "thread-a",
      turn_id: "turn-current",
    });

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-a",
      threads: [{ thread_id: "thread-a", runtime_profile: "chat" }],
      active_snapshot: {
        nodes: [{ id: "unscoped-old", node_type: "message", payload: { raw_text: "无标识旧快照", style: "user" } }],
      },
    });
    handleNotification("turn.completed", {
      thread_id: "thread-a",
    });
    handleNotification("turn.completed", {
      turn_id: "turn-current",
    });
    handleItem("item.started", {
      thread_id: "thread-a",
      kind: "message",
      item_id: "unscoped-old-item",
      data: { style: "text", text: "无标识旧事件" },
    });

    expect(uiState.isRunning).toBe(true);
    expect(document.querySelector("#transcript").textContent).toContain("当前内容");
    expect(document.querySelector("#transcript").textContent).not.toContain("无标识旧快照");
    expect(document.querySelector("#transcript").textContent).not.toContain("无标识旧事件");
  });

  it("rejects stale permission requests from another thread", async () => {
    const { handleNotification } = await import("../../src/main");
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    uiState.sessionId = "thread-a";

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "stale-permission",
      thread_id: "thread-b",
      prompt: "旧会话权限请求",
      choices: [["Yes", "y", "允许一次"]],
    });

    expect(showModal).not.toHaveBeenCalled();
  });

  it("rejects malformed empty-thread snapshots without replacing the current transcript", async () => {
    const { handleNotification } = await import("../../src/main");

    handleNotification("workspace.snapshot", {
      active_thread_id: "",
      threads: [],
      active_snapshot: {
        nodes: [{ id: "malformed-empty", node_type: "message", payload: { raw_text: "非法空会话", style: "user" } }],
      },
    });

    expect(document.querySelector("#transcript").textContent).not.toContain("非法空会话");
  });

  it("rejects scoped permission requests when no thread is active", async () => {
    const { handleNotification } = await import("../../src/main");
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "orphan-permission",
      thread_id: "thread-old",
      prompt: "无活动会话的旧权限请求",
      choices: [["Yes", "y", "允许一次"]],
    });

    expect(showModal).not.toHaveBeenCalled();
  });

  it("allows unscoped permission requests during a thread switch", async () => {
    const { handleNotification } = await import("../../src/main");
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    uiState.sessionId = "thread-a";
    uiState.isSwitchingThread = true;

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "switching-unscoped-permission",
      prompt: "切换期间的无标识权限请求",
      choices: [["Yes", "y", "允许一次"]],
    });

    expect(showModal).toHaveBeenCalled();
  });

  it("rejects scoped permission requests during a thread switch", async () => {
    const { handleNotification } = await import("../../src/main");
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    showModal.mockClear();
    uiState.sessionId = "thread-a";
    uiState.isSwitchingThread = true;

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "switching-scoped-permission",
      thread_id: "thread-a",
      prompt: "切换期间的旧线程权限请求",
      choices: [["Yes", "y", "允许一次"]],
    });

    expect(showModal).not.toHaveBeenCalled();
  });

  it("restores a failed mode command in the composer for retry", async () => {
    const { handleNotification } = await import("../../src/main");
    const socket = fakeSocket();
    _setSocket(socket);
    const input = document.querySelector("#input");
    uiState.sessionId = "thread-a";
    uiState.runtimeProfile = "loop";

    document.querySelector("#mode-status").click();
    const submit = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.submit");
    expect(submit.params).toEqual({ text: "/loop status", thread_id: "thread-a" });

    socket.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: submit.id,
        error: { code: -32000, message: "模式命令失败" },
      }),
    }));

    await vi.waitFor(() => expect(input.value).toBe("/loop status"));
    expect(document.querySelector("#transcript").textContent).toContain("模式命令失败");
  });

  it("clears composer, sidebar, and module state without duplicating bindings", async () => {
    const { _resetWorkbenchForTest } = await import("../../src/main");
    const { renderSidebar, onThreadSelect } = await import("../../src/ui/sidebar");
    const callback = vi.fn();
    onThreadSelect(callback);
    renderSidebar([
      { thread_id: "reset-thread", title: "Reset me", status: "idle", workspace: "/tmp/reset" },
    ], "reset-thread", "reset", "/tmp/reset");
    document.querySelector("#input").value = "泄漏草稿";
    uiState.sessionId = "reset-thread";
    uiState.isRunning = true;

    _resetWorkbenchForTest();
    renderSidebar([
      { thread_id: "reset-thread", title: "Reset me", status: "idle", workspace: "/tmp/reset" },
    ], "reset-thread", "reset", "/tmp/reset");
    document.querySelector('.vx-session-item[data-thread-id="reset-thread"]')?.click();

    expect(document.querySelector("#input").value).toBe("");
    expect(uiState.isRunning).toBe(false);
    expect(document.querySelector("#btn-send").getAttribute("aria-label")).toBe("Send");
    expect(callback).not.toHaveBeenCalled();
  });
});


  it("requests a bounded transcript window when switching threads", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { switchThread } = await import("../../src/main");

    const pending = switchThread("thread-windowed");
    const request = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch");

    expect(request.params).toEqual({
      thread_id: "thread-windowed",
      turn_limit: 20,
    });

    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, {
      active_thread_id: "thread-windowed",
    });
    await pending;
  });

  it("loads and prepends an earlier transcript page at the top of a windowed snapshot", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { handleNotification } = await import("../../src/main");
    const transcript = document.querySelector("#transcript");

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-windowed",
      threads: [{ thread_id: "thread-windowed", runtime_profile: "coding" }],
      active_snapshot: {
        thread_id: "thread-windowed",
        revision: 1,
        windowed: true,
        before_turn_id: 2,
        after_turn_id: 3,
        has_earlier: true,
        has_later: false,
        nodes: [{ node_type: "turn", id: "turn-2", header: "later page" }],
      },
    });

    Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 100 });
    Object.defineProperty(transcript, "clientHeight", { configurable: true, value: 50 });
    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));

    const request = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "transcript.page");
    expect(request.params).toEqual({
      thread_id: "thread-windowed",
      before_turn_id: 2,
      turn_limit: 20,
    });

    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(request.id, {
      thread_id: "thread-windowed",
      revision: 2,
      windowed: true,
      before_turn_id: 0,
      after_turn_id: 1,
      has_earlier: false,
      has_later: true,
      nodes: [{ node_type: "turn", id: "turn-1", header: "earlier page" }],
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(transcript.textContent).toContain("earlier page");
    expect(transcript.textContent).toContain("later page");
    expect(transcript.querySelectorAll(".message-item")).toHaveLength(2);
  });

  it("drops an earlier page response after the active thread changes", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    const { handleNotification, switchThread } = await import("../../src/main");
    const transcript = document.querySelector("#transcript");

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-windowed",
      threads: [{ thread_id: "thread-windowed", runtime_profile: "coding" }],
      active_snapshot: {
        thread_id: "thread-windowed",
        revision: 1,
        windowed: true,
        before_turn_id: 2,
        has_earlier: true,
        nodes: [{ node_type: "turn", id: "turn-2", header: "current page" }],
      },
    });
    Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 100 });
    transcript.scrollTop = 0;
    transcript.dispatchEvent(new Event("scroll"));
    const pageRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "transcript.page");

    const switchPending = switchThread("thread-other");
    const switchRequest = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.switch" && entry.params.thread_id === "thread-other");
    const client = await import("../../src/rpc/client");
    client._resolvePendingForTest(switchRequest.id, { active_thread_id: "thread-other" });
    await switchPending;
    client._resolvePendingForTest(pageRequest.id, {
      thread_id: "thread-windowed",
      revision: 2,
      windowed: true,
      before_turn_id: 0,
      nodes: [{ node_type: "turn", id: "turn-1", header: "stale page" }],
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(transcript.textContent).not.toContain("stale page");
  });


it("clears transcript window cursors when the workbench is reset", async () => {
  const { handleNotification, _resetWorkbenchForTest } = await import("../../src/main");
  _resetWorkbenchForTest();
  const socket = fakeSocket();
  _setSocket(socket);
  const transcript = document.querySelector("#transcript");

  handleNotification("workspace.snapshot", {
    active_thread_id: "thread-reset-window",
    threads: [{ thread_id: "thread-reset-window", runtime_profile: "coding" }],
    active_snapshot: {
      thread_id: "thread-reset-window",
      revision: 1,
      windowed: true,
      before_turn_id: 2,
      has_earlier: true,
      nodes: [{ node_type: "turn", id: "turn-reset", header: "window before reset" }],
    },
  });

  _resetWorkbenchForTest();
  _setSocket(socket);
  socket.send.mockClear();
  uiState.sessionId = "thread-reset-window";
  Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 100 });
  transcript.scrollTop = 0;
  transcript.dispatchEvent(new Event("scroll"));

  expect(socket.send.mock.calls
    .map(([data]) => JSON.parse(data))
    .filter((entry) => entry.method === "transcript.page")).toHaveLength(0);
});

it("drops a page response after a newer snapshot for the same thread", async () => {
  const { handleNotification, _resetWorkbenchForTest } = await import("../../src/main");
  _resetWorkbenchForTest();
  const socket = fakeSocket();
  _setSocket(socket);
  const transcript = document.querySelector("#transcript");

  handleNotification("workspace.snapshot", {
    active_thread_id: "thread-same",
    threads: [{ thread_id: "thread-same", runtime_profile: "coding" }],
    active_snapshot: {
      thread_id: "thread-same",
      revision: 1,
      windowed: true,
      before_turn_id: 2,
      has_earlier: true,
      nodes: [{ node_type: "turn", id: "turn-current", header: "current window" }],
    },
  });
  Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 100 });
  transcript.scrollTop = 0;
  transcript.dispatchEvent(new Event("scroll"));
  const pageRequest = socket.send.mock.calls
    .map(([data]) => JSON.parse(data))
    .find((entry) => entry.method === "transcript.page");

  handleNotification("workspace.snapshot", {
    active_thread_id: "thread-same",
    threads: [{ thread_id: "thread-same", runtime_profile: "coding" }],
    active_snapshot: {
      thread_id: "thread-same",
      revision: 2,
      windowed: true,
      before_turn_id: 4,
      has_earlier: true,
      nodes: [{ node_type: "turn", id: "turn-fresh", header: "fresh window" }],
    },
  });

  const client = await import("../../src/rpc/client");
  client._resolvePendingForTest(pageRequest.id, {
    thread_id: "thread-same",
    revision: 3,
    windowed: true,
    before_turn_id: 0,
    has_earlier: false,
    nodes: [{ node_type: "turn", id: "turn-stale", header: "stale page" }],
  });
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(transcript.textContent).toContain("fresh window");
  expect(transcript.textContent).not.toContain("stale page");
});


describe("opaque runtime profile snapshots", () => {
  it("accepts a custom profile id from runtime state metadata", async () => {
    const { applyRuntimeState } = await import("../../src/ui/model");
    uiState.runtimeProfile = "coding";

    applyRuntimeState({ runtime_profile: "reviewer-v2" });

    expect(uiState.runtimeProfile).toBe("reviewer-v2");
  });
});
