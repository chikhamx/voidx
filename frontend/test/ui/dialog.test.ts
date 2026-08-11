// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import { _resetDialogForTest, renderTextRequest, showPromptItemRequest } from "../../src/ui/dialog";

const controlsEl = document.querySelector("#request-controls");

beforeEach(() => {
  _resetRpcForTest();
  _resetDialogForTest();
  _setSocket({ readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => {} });
  document.querySelector("#request-details")?.replaceChildren();
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


describe("permission approval details", () => {
  it("renders tool name, monospace argument summary, and risk-level accent", () => {
    showPromptItemRequest({
      prompt_type: "permission",
      request_id: "permission-1",
      interactive: true,
      prompt: "Allow tool?",
      choices: [["Allow", "y", "Allow once"]],
      tools: [
        {
          name: "bash",
          pattern: "rm *",
          args: { command: "rm -rf build" },
          risk: { level: "high", tags: ["destructive"], reason: "Deletes files" },
        },
      ],
    });

    const card = document.querySelector(".request-tool-detail");
    expect(card).not.toBeNull();
    expect(card.classList.contains("request-tool-risk-high")).toBe(true);
    expect(card.querySelector(".request-tool-title").textContent).toBe("bash");
    expect(card.querySelector(".request-tool-pattern").textContent).toBe("rm *");
    expect(card.querySelector(".request-tool-args").textContent).toContain("rm -rf build");
    expect(card.querySelector(".request-tool-args").tagName).toBe("PRE");
  });

  it("uses a neutral accent when risk metadata is missing", () => {
    showPromptItemRequest({
      prompt_type: "permission",
      request_id: "permission-2",
      interactive: true,
      prompt: "Allow tool?",
      choices: [["Allow", "y", "Allow once"]],
      tools: [{ name: "read", args: { file_path: "README.md" } }],
    });

    expect(document.querySelector(".request-tool-detail")?.classList.contains("request-tool-risk-default")).toBe(true);
  });
});
