// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import { showPromptItemRequest } from "../../src/ui/dialog";
import { renderTextRequest } from "../../src/ui/dialog";

const controlsEl = document.querySelector("#request-controls");

beforeEach(() => {
  _resetRpcForTest();
  _setSocket({ readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} });
  controlsEl.replaceChildren();
});

describe("renderTextRequest", () => {
  it("uses a textarea for plain text requests", () => {
    renderTextRequest({ request_id: "r1", prompt: "Name?", default: "x" });
    const field = controlsEl.querySelector("textarea");
    expect(field).not.toBeNull();
    expect(field.value).toBe("x");
    expect(controlsEl.querySelector("input[type=password]")).toBeNull();
  });

  it("masks secret requests with a password input", () => {
    renderTextRequest({ request_id: "r2", prompt: "API key?", secret: true });
    const field = controlsEl.querySelector("input[type=password]");
    expect(field).not.toBeNull();
    expect(controlsEl.querySelector("textarea")).toBeNull();
  });

  it("keeps the default value in the masked field", () => {
    renderTextRequest({ request_id: "r3", prompt: "Token?", secret: true, default: "sk-1" });
    const field = controlsEl.querySelector("input[type=password]");
    expect(field.value).toBe("sk-1");
  });
});

describe("goal spec prompts", () => {
  it("renders goal details and responds through session.respond", () => {
    const socket = { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} };
    _setSocket(socket);
    showPromptItemRequest({
      prompt_type: "goal_spec",
      prompt_id: "goal-1",
      thread_id: "thread-1",
      spec: {
        objective: "Ship it",
        acceptance_condition: "Tests pass",
        achievement_method: "Iterate",
        max_attempts: 3,
      },
      choices: [{ label: "Approve", value: "approve", description: "Start" }],
    });

    expect(document.querySelector("#request-title").textContent).toContain("Goal: Ship it");
    document.querySelector("#request-controls button").click();
    const request = JSON.parse(socket.send.mock.calls[0][0]);
    expect(request).toMatchObject({
      method: "session.respond",
      params: { request_id: "goal-1", thread_id: "thread-1", value: "approve" },
    });
  });
});
