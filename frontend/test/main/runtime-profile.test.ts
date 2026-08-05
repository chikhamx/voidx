// @ts-nocheck
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import { setConnectionStatus, uiState, _resetWorkbenchStateForTest } from "../../src/services/state";

function fakeSocket() {
  return { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
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


  it("creates a temporary session without a preset runtime profile", async () => {
    const socket = fakeSocket();
    _setSocket(socket);
    await import("../../src/main");

    document.querySelector("#btn-new-chat").click();

    const message = socket.send.mock.calls
      .map(([data]) => JSON.parse(data))
      .find((entry) => entry.method === "session.create");
    expect(message.params).toEqual({ directory: "" });
  });
});


describe("desktop runtime profile switching", () => {
  beforeEach(() => {});

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
