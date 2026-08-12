// @ts-nocheck
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import { setConnectionStatus, uiState, _resetWorkbenchStateForTest } from "../../src/services/state";

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
    uiState.runtimeProfile = "chat";

    document.querySelector("#btn-new-chat").click();

    const message = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.create");
    expect(message.params).toEqual({ directory: "", profile: "chat" });
  });
});


describe("desktop runtime profile switching", () => {
  beforeEach(() => {
    _resetRpcForTest();
    _resetWorkbenchStateForTest();
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

  it("creates the selected profile instead of submitting an unsupported slash command", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    await import("../../src/main");
    socket.send.mockClear();
    uiState.sessionId = "";
    uiState.runtimeProfile = "coding";

    document.querySelector('[data-profile="goal"]').click();

    const messages = socket.send.mock.calls.map(([data]) => JSON.parse(data));
    expect(messages).toContainEqual(expect.objectContaining({
      method: "session.create",
      params: { directory: "", profile: "goal" },
    }));
    expect(messages).not.toContainEqual(expect.objectContaining({
      method: "session.submit",
      params: expect.objectContaining({ text: "/goal" }),
    }));
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
      active_snapshot: { nodes: [] },
      runtime_profile: "goal",
    });

    expect(uiState.sessionId).toBe("thread-loop");
    expect(uiState.runtimeProfile).toBe("loop");
  });

  it("does not replace the current profile when snapshot profile is invalid or absent", () => {
    uiState.runtimeProfile = "goal";
    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-goal",
      threads: [{ thread_id: "thread-goal", runtime_profile: "invalid" }],
      active_snapshot: { nodes: [] },
    });
    expect(uiState.runtimeProfile).toBe("goal");

    handleNotification("workspace.snapshot", {
      active_thread_id: "thread-goal",
      threads: [{ thread_id: "thread-goal" }],
      active_snapshot: { nodes: [] },
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
