// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { _setSocket, _resetForTest as _resetRpcForTest } from "../../src/rpc/client";
import {
  _resetDialogForTest,
  clearPermissionRequests,
  pendingUiRequests,
  renderTextRequest,
  showPromptItemRequest,
  showRequest,
} from "../../src/ui/dialog";

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


describe("permission request lifecycle", () => {
  it("clears timed-out permission requests without dropping other queued prompts", () => {
    showRequest({
      kind: "permission",
      request_id: "permission-active",
      prompt: "Allow active tool?",
      choices: [["Allow", "y", "Allow once"]],
      response_method: "session.respond",
    });
    showRequest({
      kind: "permission",
      request_id: "permission-stale",
      prompt: "Allow stale tool?",
      choices: [["Allow", "y", "Allow once"]],
      response_method: "session.respond",
    });
    showRequest({
      kind: "choice",
      request_id: "choice-queued",
      prompt: "Continue?",
      choices: [["Yes", "y", "Continue"]],
      response_method: "session.respond",
    });

    expect(pendingUiRequests.map((request) => request.request_id)).toEqual([
      "permission-stale",
      "choice-queued",
    ]);

    clearPermissionRequests();

    expect(document.querySelector("#request-dialog").open).toBe(true);
    expect(document.querySelector("#request-title").textContent).toBe("Continue?");
    expect(pendingUiRequests).toHaveLength(0);
  });


  it("does not close a newer permission request when an older clear event arrives", () => {
    showRequest({
      kind: "permission",
      request_id: "permission-old",
      prompt: "Allow old tool?",
      choices: [["Allow", "y", "Allow once"]],
      response_method: "session.respond",
    });
    clearPermissionRequests("permission-old");
    showRequest({
      kind: "permission",
      request_id: "permission-new",
      prompt: "Allow new tool?",
      choices: [["Allow", "y", "Allow once"]],
      response_method: "session.respond",
    });

    clearPermissionRequests("permission-old");

    expect(document.querySelector("#request-dialog").open).toBe(true);
    expect(document.querySelector("#request-title").textContent).toBe("权限审批");
    expect(document.querySelector(".request-permission-question")?.textContent).toContain("Allow new tool?");
  });
});


describe("permission replay identity", () => {
    const request = {
        kind: "permission", request_id: "replayed", thread_id: "thread-a",
        response_method: "session.respond", prompt: "Allow?",
        choices: [["Allow", "y", "Allow once"]],
    };

    it("does not queue a replay of the open request", () => {
        showRequest(request);
        showRequest({ ...request });
        expect(document.querySelector("#request-dialog").open).toBe(true);
        expect(pendingUiRequests).toHaveLength(0);
    });

    it("deduplicates queued replays without changing FIFO order", () => {
        showRequest({ ...request, request_id: "first" });
        showRequest(request);
        showRequest({ ...request, request_id: "last" });
        showRequest({ ...request });
        expect(pendingUiRequests.map((r) => r.request_id)).toEqual(["replayed", "last"]);
        clearPermissionRequests("first");
        expect(document.querySelector("#request-dialog").dataset.requestId).toBe("replayed");
        clearPermissionRequests("replayed");
        expect(document.querySelector("#request-dialog").dataset.requestId).toBe("last");
    });

    it("does not merge identities from different threads or response routes", () => {
        showRequest(request);
        showRequest({ ...request, thread_id: "thread-b" });
        showRequest({ ...request, response_method: "" });
        expect(pendingUiRequests).toHaveLength(2);
    });

    it("merges business permission presentation with the equivalent ui.request", () => {
        showPromptItemRequest({ ...request, prompt_type: "permission", interactive: true });
        showRequest(request);
        expect(pendingUiRequests).toHaveLength(0);
        clearPermissionRequests(request.request_id);
        expect(document.querySelector("#request-dialog").open).toBe(false);
        expect(pendingUiRequests).toHaveLength(0);
    });
});

 describe("replace permission lifecycle", () => {
  const request = (id = "owned") => ({ kind: "permission", request_id: id,
    thread_id: "thread", prompt: "Approve?", response_method: "session.respond",
    reconnect_policy: "replace", choices: [["Allow", "allow", "Allow"]] });
  const dialog = () => document.querySelector("#request-dialog");
  const click = () => controlsEl.querySelector("button").click();
  it("clears open and queued owned permissions but preserves legacy", async () => {
    const { clearDisconnectedPermissionRequests } = await import("../../src/ui/dialog");
    showRequest(request()); showRequest(request("queued"));
    showRequest({ ...request("legacy"), reconnect_policy: undefined });
    clearDisconnectedPermissionRequests();
    expect(dialog().dataset.requestId).toBe("legacy");
    expect(pendingUiRequests).toEqual([]);
  });
  it("waits for ok:true and blocks double submission", async () => {
    const { _resolvePendingForTest } = await import("../../src/rpc/client");
    const socket = { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener() {} };
    _setSocket(socket); showRequest(request()); click(); click();
    expect(dialog().open).toBe(true);
    expect(controlsEl.querySelector("button").disabled).toBe(true);
    expect(socket.send).toHaveBeenCalledTimes(1);
    _resolvePendingForTest(1, { ok: true }); await Promise.resolve();
    expect(dialog().open).toBe(false);
  });
  it.each(["rejected", "error"])("keeps request and public error on %s", async (mode) => {
    const { _resolvePendingForTest, flushPendingRequests } = await import("../../src/rpc/client");
    showRequest(request()); click();
    if (mode === "error") flushPendingRequests(new Error("PRIVATE_SECRET"));
    else _resolvePendingForTest(1, { ok: false });
    await Promise.resolve(); await Promise.resolve();
    expect(dialog().open).toBe(true);
    expect(document.querySelector("#request-details").textContent).toContain("结果未确认或未接受");
    expect(document.querySelector("#request-details").textContent).not.toContain("PRIVATE_SECRET");
    expect(controlsEl.querySelector("button").disabled).toBe(false);
  });
  it.each([true, false, "error"])("ignores late completion %s after resolved", async (result) => {
    const { _resolvePendingForTest, flushPendingRequests } = await import("../../src/rpc/client");
    showRequest(request()); click(); clearPermissionRequests("owned"); showRequest(request("new"));
    if (result === "error") flushPendingRequests(new Error("PRIVATE_SECRET"));
    else _resolvePendingForTest(1, { ok: result });
    await Promise.resolve(); await Promise.resolve();
    expect(dialog().open).toBe(true); expect(dialog().dataset.requestId).toBe("new");
    expect(document.querySelector("#request-details").textContent).not.toContain("结果未确认");
  });
 });

describe("disconnected RPC ownership", () => {
  it.each([true, false, "error"])("cannot revive disconnected request on %s", async (result) => {
    const { clearDisconnectedPermissionRequests } = await import("../../src/ui/dialog");
    const { _resolvePendingForTest, flushPendingRequests } = await import("../../src/rpc/client");
    const req = { kind: "permission", request_id: "old", response_method: "session.respond",
      reconnect_policy: "replace", prompt: "Approve?", choices: [["Allow", "allow", "Allow"]] };
    showRequest(req); controlsEl.querySelector("button").click();
    clearDisconnectedPermissionRequests(); showRequest({ ...req, request_id: "new" });
    if (result === "error") flushPendingRequests(new Error("PRIVATE_SECRET"));
    else _resolvePendingForTest(1, { ok: result });
    await Promise.resolve(); await Promise.resolve();
    const dialog = document.querySelector("#request-dialog");
    expect(dialog.open).toBe(true); expect(dialog.dataset.requestId).toBe("new");
    expect(document.querySelector("#request-details [role=alert]")).toBeNull();
  });
});


describe("semantic clarify requests", () => {
    it("offers raw free text alongside choices and clears on disconnect", async () => {
        const { clearDisconnectedPermissionRequests } = await import("../../src/ui/dialog");
        const socket = { readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: () => { } };
        _setSocket(socket);
        showRequest({
            kind: "choice", request_id: "clarify-1", thread_id: "t",
            prompt: "Which environment?", choices: [["Staging", "Staging", ""]],
            response_method: "session.respond", reconnect_policy: "replace"
        });
        const field = controlsEl.querySelector("textarea");
        expect(field).not.toBeNull();
        field.value = "custom environment";
        [...controlsEl.querySelectorAll("button")].find(b => b.textContent === "Submit").click();
        const message = JSON.parse(socket.send.mock.calls[0][0]);
        expect(message.params.value).toBe("custom environment");
        expect(document.querySelector("#request-dialog").open).toBe(true);
        clearDisconnectedPermissionRequests();
        expect(document.querySelector("#request-dialog").open).toBe(false);
    });

    it("allows dismissing semantic open-ended clarification", () => {
        showRequest({
            kind: "text", request_id: "clarify-text", prompt: "Why?",
            response_method: "session.respond", reconnect_policy: "replace"
        });
        expect(controlsEl.querySelector(".request-choice-cancel")).not.toBeNull();
    });
});
