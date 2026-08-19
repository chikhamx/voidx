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
    const parameters = document.querySelector(".request-parameters");
    expect(parameters?.hasAttribute("open")).toBe(false);
    expect(parameters?.querySelector(".request-tool-args")?.textContent).toContain("rm -rf build");
    expect(parameters?.querySelector(".request-tool-args")?.tagName).toBe("PRE");
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


describe("permission approval hierarchy", () => {
  it("shows the approval question before execution, risk, scope, and collapsed parameters", () => {
    showPromptItemRequest({
      prompt_type: "permission",
      request_id: "permission-hierarchy-1",
      interactive: true,
      prompt: "是否允许执行这个命令？",
      choices: [
        ["允许一次", "y", "仅本次执行"],
        ["拒绝", "n", "不要执行"],
      ],
      tools: [
        {
          name: "bash",
          pattern: "npm run build",
          args: { command: "npm run build", cwd: "/workspace" },
          risk: {
            level: "high",
            tags: ["执行命令"],
            reason: "命令会运行项目构建脚本",
          },
          allowed_scopes: ["once", "session"],
          default_scope: "once",
        },
      ],
    });

    expect(document.querySelector("#request-title").textContent).toBe("权限审批");
    expect(document.querySelector(".request-permission-question")?.textContent).toContain("是否允许执行这个命令？");

    const sections = [...document.querySelectorAll("[data-permission-section]")]
      .map((section) => section.getAttribute("data-permission-section"));
    expect(sections).toEqual(["question", "execution", "risk", "scope", "parameters"]);

    expect(document.querySelector(".request-execution")?.textContent).toContain("npm run build");
    expect(document.querySelector(".request-risk-reason")?.textContent).toContain("命令会运行项目构建脚本");
    expect(document.querySelector(".request-approval-scopes")?.textContent).toContain("once");
    expect(document.querySelector(".request-approval-scopes")?.textContent).toContain("session");

    const parameters = document.querySelector(".request-parameters");
    expect(parameters?.tagName).toBe("DETAILS");
    expect(parameters?.hasAttribute("open")).toBe(false);
    expect(parameters?.querySelector("summary")?.textContent).toContain("参数详情");
    expect(parameters?.querySelector(".request-tool-args")?.textContent).toContain("/workspace");

    const buttons = [...document.querySelectorAll("#request-controls .request-choice")];
    expect(buttons.map((button) => button.querySelector(".request-choice-label")?.textContent)).toEqual([
      "允许一次",
      "拒绝",
    ]);
    expect(buttons.map((button) => button.querySelector(".request-choice-description")?.textContent)).toEqual([
      "仅本次执行",
      "不要执行",
    ]);
  });

  it("falls back to the actual command and states when no extra scope is provided", () => {
    showPromptItemRequest({
      prompt_type: "permission",
      request_id: "permission-hierarchy-2",
      interactive: true,
      prompt: "是否允许运行构建命令？",
      choices: [["允许", "y", "本次运行"]],
      tools: [
        {
          name: "bash",
          args: { command: "npm run build" },
          risk: { level: "medium", reason: "会执行构建脚本" },
        },
      ],
    });

    expect(document.querySelector(".request-execution")?.textContent).toContain("npm run build");
    expect(document.querySelector(".request-scope-section")?.textContent).toContain("未提供额外授权范围");
  });
});
