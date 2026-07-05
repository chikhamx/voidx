// @ts-nocheck
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tauriMocks = vi.hoisted(() => ({
  openDialog: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: tauriMocks.openDialog,
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: tauriMocks.invoke,
}));

import { handleNotification, initModelControls, resolveWsUrl, _resetWorkbenchForTest } from "../src/main";
import { _resetForTest as resetDock, initDock, switchTab, toggleDock, getActiveTab } from "../src/dock";
import { _setSocket, _resetForTest as resetRpc } from "../src/rpc";

function sentPayloads(sentMessages) {
  return sentMessages.map((raw) => JSON.parse(raw));
}

function setupOpenSocket() {
  const sentMessages = [];
  const socket = {
    readyState: WebSocket.OPEN,
    send: (message) => sentMessages.push(message),
  };
  _setSocket(socket);
  return sentMessages;
}

function setupOpenSocketWithHandle() {
  const sentMessages = [];
  const socket = {
    readyState: WebSocket.OPEN,
    onmessage: null,
    send: (message) => sentMessages.push(message),
  };
  _setSocket(socket);
  return { sentMessages, socket };
}

beforeEach(() => {
  resetRpc();
  resetDock();
  _resetWorkbenchForTest();
  tauriMocks.openDialog.mockReset();
  tauriMocks.invoke.mockReset();
  delete window.__TAURI_INTERNALS__;
  delete window.__TAURI__;
  initDock();
  initModelControls();
});

describe("workbench shell", () => {
  it("renders the fixed sidebar navigation and project sections", () => {
    const sidebar = document.querySelector("#sidebar");
    expect(sidebar.textContent).toContain("新对话");
    expect(sidebar.textContent).toContain("搜索");
    expect(sidebar.textContent).not.toContain("已安排");
    expect(sidebar.textContent).toContain("项目");
    expect(sidebar.textContent).not.toContain("历史会话");
    expect(document.querySelector(".vx-project-heading .vx-sidebar-row-icon svg")).not.toBeNull();
    expect(document.querySelector(".vx-project-heading-label").textContent).toBe("项目");
    expect(document.querySelector("#project-list")).toBeNull();
    expect(document.querySelector("#btn-integrations").hidden).toBe(true);
  });


  it("integrations button requests integration snapshot", () => {
    const sentMessages = setupOpenSocket();

    document.querySelector("#btn-integrations").click();

    expect(sentPayloads(sentMessages)[0]).toMatchObject({
      method: "integrations.get",
      params: {},
    });
  });

  it("shows empty-state prompt while transcript has no content", () => {
    const emptyState = document.querySelector("#empty-state");
    const transcript = document.querySelector("#transcript");
    const mainCanvas = document.querySelector(".vx-main-canvas");
    expect(transcript.children).toHaveLength(0);
    expect(emptyState.hidden).toBe(false);
    expect(mainCanvas.classList.contains("empty")).toBe(true);
    expect(emptyState.textContent).toContain("我们应该在 voidx 中构建什么？");
  });

  it("does not render the redundant configured permission pill in the composer", () => {
    expect(document.querySelector("#permission-pill")).toBeNull();
    expect(document.querySelector("#status-permission")).toBeNull();
    expect(document.querySelector("#context-permission")).toBeNull();
    expect(document.querySelector("#strip-permission")).toBeNull();
  });

  it("hides empty-state prompt once a live conversation item starts", () => {
    const emptyState = document.querySelector("#empty-state");
    const mainCanvas = document.querySelector(".vx-main-canvas");

    handleNotification("item.started", {
      kind: "assistant_stream",
      item_id: "stream-1",
      data: { phase: "thinking" },
    });

    expect(emptyState.hidden).toBe(true);
    expect(mainCanvas.classList.contains("empty")).toBe(false);
  });

  it("centers the composer in an empty new conversation", () => {
    const styles = readFileSync(join(process.cwd(), "styles.css"), "utf8");

    expect(styles).toMatch(/\.vx-main-canvas\.empty \{[^}]*justify-content: center;[^}]*\}/);
    expect(styles).toMatch(/\.vx-main-canvas\.empty \.vx-empty-state \{[^}]*margin: 0 auto 32px;[^}]*\}/);
    expect(styles).toMatch(/\.vx-main-canvas\.empty \.composer \{[^}]*margin-bottom: 0;[^}]*\}/);
  });

  it("renders current workspace in the workspace session tree", () => {
    handleNotification("startup.shown", {
      workspace: "/Users/chikham/workspace/voidx",
      provider: "openai",
      model: "gpt-5.5",
      profile_configured: true,
    });
    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "/Users/chikham/workspace/voidx" }],
      workspace: "/Users/chikham/workspace/voidx",
      provider: "openai",
      model: "gpt-5.5",
      profile_configured: true,
    });

    const activeWorkspace = document.querySelector(".vx-workspace-session-group.active .vx-workspace-session-name");
    expect(activeWorkspace).not.toBeNull();
    expect(activeWorkspace.textContent).toContain("voidx");
  });

  it("opens a workspace folder from the project add button", async () => {
    window.__TAURI_INTERNALS__ = {};
    tauriMocks.openDialog.mockResolvedValue("/Users/chikham/workspace/imcore-sdk");
    tauriMocks.invoke.mockImplementation((command: string) => {
      if (command === "restart_backend") {
        return Promise.resolve({ status: "restarting" });
      }
      if (command === "get_gateway_url") {
        return Promise.resolve("");
      }
      return Promise.resolve(null);
    });

    handleNotification("startup.shown", {
      workspace: "/Users/chikham/workspace/voidx",
      provider: "openai",
      model: "gpt-5.5",
      profile_configured: true,
    });

    document.querySelector("#btn-open-workspace").click();

    await vi.waitFor(() => {
      expect(tauriMocks.openDialog).toHaveBeenCalledWith({
        directory: true,
        multiple: false,
        title: "选择项目文件夹",
      });
    });
    await vi.waitFor(() => {
      expect(tauriMocks.invoke).toHaveBeenCalledWith("restart_backend", {
        workspace: "/Users/chikham/workspace/imcore-sdk",
      });
    });
    expect(document.querySelector("#project-list")).toBeNull();
    expect(document.querySelector("#status-workspace-detail").textContent).toBe("imcore-sdk");
  });

  it("resolves gateway url through tauri invoke without relying on tauri globals", async () => {
    tauriMocks.invoke.mockResolvedValue("ws://127.0.0.1:12345/?token=test");

    const url = await resolveWsUrl();

    expect(url).toBe("ws://127.0.0.1:12345/?token=test");
    expect(tauriMocks.invoke).toHaveBeenCalledWith("get_gateway_url");
  });

  it("keeps sidebar rows aligned in the workbench layout", () => {
    const styles = readFileSync(join(process.cwd(), "styles.css"), "utf8");

    expect(styles).toContain("--vx-sidebar-row-padding: 7px 9px;");
    expect(document.querySelector("#sidebar-resizer")).not.toBeNull();
    expect(styles).toContain(".vx-sidebar-nav,\n.vx-session-list,\n.vx-sidebar-footer");
    expect(styles).toContain(".vx-workbench-shell .vx-session-children {\n  display: grid;");
    expect(styles).toContain("padding-left: 28px;");
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-sidebar-section \{[^}]*flex: 1;[^}]*min-height: 0;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-session-item \{[^}]*font-size: 14px;[^}]*grid-template-columns: 16px minmax\(0, 1fr\) max-content;[^}]*\}/);
    expect(styles).toMatch(/\.vx-session-time \{[^}]*justify-self: end;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-directory-row \{[^}]*padding: var\(--vx-sidebar-row-padding\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-session-item \{[^}]*padding: var\(--vx-sidebar-row-padding\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-project-heading \{[^}]*color: var\(--vx-text-secondary\);[^}]*font-size: 14px;[^}]*min-height: 30px;[^}]*padding: var\(--vx-sidebar-row-padding\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-project-heading \.vx-sidebar-row-icon \{[^}]*color: var\(--vx-text-secondary\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-sidebar-resizer \{[^}]*cursor: col-resize;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-dock \{[^}]*margin-left: var\(--vx-sidebar-width\);[^}]*width: calc\(100% - var\(--vx-sidebar-width\)\);[^}]*\}/);
    expect(styles).toContain(".vx-sidebar-row-icon");
  });

  it("resizes the sidebar from the draggable boundary", () => {
    const resizer = document.querySelector("#sidebar-resizer");

    resizer.dispatchEvent(new PointerEvent("pointerdown", { clientX: 260, pointerId: 1 }));
    window.dispatchEvent(new PointerEvent("pointermove", { clientX: 340, pointerId: 1 }));
    window.dispatchEvent(new PointerEvent("pointerup", { clientX: 340, pointerId: 1 }));

    expect(document.querySelector(".vx-workbench-shell").style.getPropertyValue("--vx-sidebar-width")).toBe("340px");
  });

  it("reuses the existing empty session for a workspace instead of creating duplicates", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "filled",
      active_snapshot: { thread_id: "filled", nodes: [] },
      threads: [
        {
          thread_id: "empty-1",
          title: "New session",
          status: "idle",
          workspace: "/Users/chikham/workspace/voidx",
        },
        {
          thread_id: "filled",
          title: "Existing work",
          status: "idle",
          workspace: "/Users/chikham/workspace/voidx",
        },
      ],
      workspace: "/Users/chikham/workspace/voidx",
      provider: "openai",
      model: "gpt-5.5",
      profile_configured: true,
    });
    sentMessages.length = 0;

    document.querySelector("#btn-new-chat").click();

    await vi.waitFor(() => {
      expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.switch")).toBe(true);
    });
    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.switch")).toMatchObject({
      method: "session.switch",
      params: { thread_id: "empty-1" },
    });
    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.create")).toBe(false);

    const request = sentPayloads(sentMessages).find((payload) => payload.method === "session.switch");
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: { active_thread_id: "empty-1" },
      }),
    });

    await vi.waitFor(() => {
      expect(document.querySelector(".vx-session-item.active").dataset.threadId).toBe("empty-1");
    });
  });

  it("shows cancel state immediately after submitting a message and sends cancel", async () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    input.value = "你好";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(send.classList.contains("running")).toBe(true);
    expect(send.textContent).toBe("■");
    expect(input.disabled).toBe(true);
    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.submit")).toBe(true);

    send.click();

    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.cancel")).toBe(true);
  });

  it("clears running state when a turn end notification arrives", () => {
    const send = document.querySelector("#btn-send");
    handleNotification("turn.started", {});

    expect(send.classList.contains("running")).toBe(true);

    handleNotification("turn.completed", {});

    expect(send.classList.contains("running")).toBe(false);
    expect(send.textContent).toBe("↑");
  });

  it("opens request dialog for prompt item notifications", () => {
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-1",
      data: {
        prompt_type: "permission",
        prompt: "允许写文件？",
        choices: [["Yes", "allow", "允许一次"]],
        tools: [{ name: "write", pattern: "/tmp/a.txt", args: { path: "/tmp/a.txt" } }],
      },
    });

    expect(showModal).toHaveBeenCalled();
    expect(document.querySelector("#request-title").textContent).toContain("允许写文件");
    expect(document.querySelector("#request-controls").textContent).toContain("允许一次");
  });

  it("sends prompt item responses through session.respond", () => {
    const sentMessages = setupOpenSocket();
    const dialog = document.querySelector("#request-dialog");
    vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    vi.spyOn(dialog, "close").mockImplementation(() => {});

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-1",
      data: {
        prompt_type: "clarify",
        clarify_id: "cl_1",
        question: "选哪个方案？",
        options: ["直接实现", "先写文档"],
      },
    });
    document.querySelector("#request-controls button").click();

    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.respond")).toMatchObject({
      method: "session.respond",
      params: { request_id: "cl_1", value: "直接实现" },
    });
  });
});

describe("provider and model controls", () => {
  it("does not show fake provider model before startup state arrives", () => {
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    expect(providerSelect.value).toBe("");
    expect(modelSelect.value).toBe("");
    expect([...providerSelect.options].map((option) => option.value)).toContain("");
    expect([...modelSelect.options].map((option) => option.value)).toEqual([""]);
    expect(document.querySelector("#context-permission")).toBeNull();
    expect(document.querySelector("#status-permission")).toBeNull();
    expect(document.querySelector("#status-provider-model").textContent).toBe("等待模型状态");
  });

  it("renders provider and model selects from configured profiles", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages)[0];
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: { provider: "anthropic", model: "claude-sonnet-4-6" },
          profiles: [
            { name: "anthropic/claude-sonnet-4-6", provider: "anthropic", model: "claude-sonnet-4-6", configured: true },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(providerSelect.value).toBe("anthropic");
    });
    expect(providerSelect).not.toBeNull();
    expect(modelSelect).not.toBeNull();
    expect([...providerSelect.options].map((option) => option.value)).toEqual(["anthropic"]);
    expect([...modelSelect.options].map((option) => option.value)).toEqual(["claude-sonnet-4-6"]);
  });

  it("changing provider refreshes configured model options without submitting", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages)[0];
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: { provider: "deepseek", model: "deepseek-v4-flash" },
          profiles: [
            { name: "deepseek/deepseek-v4-flash", provider: "deepseek", model: "deepseek-v4-flash", configured: true },
            { name: "anthropic/claude-sonnet-4-6", provider: "anthropic", model: "claude-sonnet-4-6", configured: true },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(providerSelect.value).toBe("deepseek");
    });
    providerSelect.value = "anthropic";
    providerSelect.dispatchEvent(new Event("change", { bubbles: true }));

    expect([...modelSelect.options].map((option) => option.value)).toEqual(["claude-sonnet-4-6"]);
    expect(sentPayloads(sentMessages).filter((payload) => payload.method === "session.submit")).toHaveLength(0);
  });

  it("changing model submits exactly one slash command", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages)[0];
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: { provider: "anthropic", model: "claude-sonnet-4-6" },
          profiles: [
            { name: "anthropic/claude-sonnet-4-6", provider: "anthropic", model: "claude-sonnet-4-6", configured: true },
            { name: "anthropic/claude-opus-4-1", provider: "anthropic", model: "claude-opus-4-1", configured: true },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(modelSelect.value).toBe("claude-sonnet-4-6");
    });
    providerSelect.value = "anthropic";
    providerSelect.dispatchEvent(new Event("change", { bubbles: true }));
    modelSelect.value = "claude-opus-4-1";
    modelSelect.dispatchEvent(new Event("change", { bubbles: true }));

    await vi.waitFor(() => {
      expect(sentPayloads(sentMessages).filter((payload) => payload.method === "session.submit")).toHaveLength(1);
    });
    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.submit")).toMatchObject({
      method: "session.submit",
      params: { text: "/model switch anthropic/claude-opus-4-1 --local" },
    });
  });

  it("startup.shown syncs provider model workspace and status panel", () => {
    handleNotification("startup.shown", {
      workspace: "/Users/chikham/workspace/voidx",
      provider: "deepseek",
      model: "deepseek-reasoner",
      profile_configured: false,
    });

    expect(document.querySelector("#provider-select").value).toBe("deepseek");
    expect(document.querySelector("#model-select").value).toBe("deepseek-reasoner");
    expect(document.querySelector("#status-provider-model").textContent).toContain("deepseek/deepseek-reasoner");
    expect(document.querySelector("#status-permission")).toBeNull();
  });

  it("workspace.snapshot syncs startup default model status", () => {
    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/Users/chikham/workspace/voidx",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });

    expect(document.querySelector("#provider-select").value).toBe("deepseek");
    expect(document.querySelector("#model-select").value).toBe("deepseek-chat");
    expect(document.querySelector("#status-provider-model").textContent).toContain("deepseek/deepseek-chat");
    expect(document.querySelector("#status-permission")).toBeNull();
  });

  it("workspace.snapshot fetches settings when startup model is missing", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/Users/chikham/workspace/voidx",
    });

    const request = sentPayloads(sentMessages)[0];
    expect(request).toMatchObject({
      method: "settings.get",
      params: {},
    });

    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: {
            provider: "xunfei-coding-plan",
            model: "astron-code-latest",
          },
          profiles: [
            {
              provider: "xunfei-coding-plan",
              model: "astron-code-latest",
              configured: true,
            },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(document.querySelector("#status-provider-model").textContent).toContain("xunfei-coding-plan/astron-code-latest");
    });
    expect(document.querySelector("#provider-select").value).toBe("xunfei-coding-plan");
    expect(document.querySelector("#model-select").value).toBe("astron-code-latest");
    expect(document.querySelector("#status-permission")).toBeNull();
  });

  it("workspace.snapshot fetches settings when startup model fields are empty", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
      profile_configured: null,
    });

    const request = sentPayloads(sentMessages)[0];
    expect(request).toMatchObject({
      method: "settings.get",
      params: {},
    });

    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: {
            provider: "deepseek",
            model: "deepseek-v4-flash",
          },
          profiles: [
            {
              provider: "deepseek",
              model: "deepseek-v4-flash",
              configured: true,
            },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(document.querySelector("#status-provider-model").textContent).toContain("deepseek/deepseek-v4-flash");
    });
    expect(document.querySelector("#provider-select").value).toBe("deepseek");
    expect(document.querySelector("#model-select").value).toBe("deepseek-v4-flash");
    expect(document.querySelector("#status-permission")).toBeNull();
  });

  it("model selectors only list configured profiles returned by settings", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });

    const request = sentPayloads(sentMessages)[0];
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: {
            provider: "deepseek",
            model: "deepseek-v4-flash",
          },
          profiles: [
            {
              name: "deepseek/deepseek-v4-flash",
              provider: "deepseek",
              model: "deepseek-v4-flash",
              configured: true,
            },
            {
              name: "xunfei-coding-plan/astron-code-latest",
              provider: "xunfei-coding-plan",
              model: "astron-code-latest",
              configured: true,
            },
            {
              name: "openai/gpt-5.5",
              provider: "openai",
              model: "gpt-5.5",
              configured: false,
            },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      expect(document.querySelector("#provider-select").value).toBe("deepseek");
    });

    const providerOptions = [...document.querySelectorAll("#provider-select option")].map((option) => option.value);
    expect(providerOptions).toEqual(["deepseek", "xunfei-coding-plan"]);
    expect(providerOptions).not.toContain("openai");
    expect(providerOptions).not.toContain("anthropic");
    expect(providerOptions).not.toContain("gemini");
    expect(providerOptions).not.toContain("custom");
    expect(providerOptions).not.toContain("");

    const providerSelect = document.querySelector("#provider-select");
    providerSelect.value = "xunfei-coding-plan";
    providerSelect.dispatchEvent(new Event("change"));

    const modelOptions = [...document.querySelectorAll("#model-select option")].map((option) => option.value);
    expect(modelOptions).toEqual(["astron-code-latest"]);
  });

  it("workspace.snapshot with current model still fetches full configured profile list", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "deepseek",
      model: "deepseek-v4-flash",
      profile_configured: true,
    });

    const request = sentPayloads(sentMessages)[0];
    expect(request).toMatchObject({
      method: "settings.get",
      params: {},
    });

    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          model: {
            provider: "deepseek",
            model: "deepseek-v4-flash",
          },
          profiles: [
            {
              name: "xunfei-coding-plan/astron-code-latest",
              provider: "xunfei-coding-plan",
              model: "astron-code-latest",
              configured: true,
            },
            {
              name: "aixhan/gpt-5.5",
              provider: "aixhan",
              model: "gpt-5.5",
              configured: true,
            },
            {
              name: "deepseek/deepseek-v4-flash",
              provider: "deepseek",
              model: "deepseek-v4-flash",
              configured: true,
            },
          ],
        },
      }),
    });

    await vi.waitFor(() => {
      const providerOptions = [...document.querySelectorAll("#provider-select option")].map((option) => option.value);
      expect(providerOptions).toEqual(["xunfei-coding-plan", "aixhan", "deepseek"]);
    });
    expect(document.querySelector("#provider-select").value).toBe("deepseek");
    expect([...document.querySelectorAll("#provider-select option")].map((option) => option.value)).not.toContain("");
  });
});

describe("bottom panel", () => {
  it("contains Todo Terminal Diff and Status tabs", () => {
    const labels = [...document.querySelectorAll(".vx-dock-tab")].map((tab) => tab.textContent.trim());
    expect(labels).toEqual(["Todo", "Terminal", "Diff", "Status"]);
  });

  it("does not show duplicate status rows below the composer", () => {
    const contextRow = document.querySelector("#context-row");
    const dockStrip = document.querySelector("#dock-strip");

    expect(contextRow.hidden).toBe(true);
    expect(dockStrip.hidden).toBe(true);
  });

  it("collapses to a status strip and preserves active tab", () => {
    const dock = document.querySelector("#dock");
    dock.classList.remove("collapsed");
    document.querySelector("#dock-strip").hidden = true;

    switchTab("status");
    toggleDock();

    expect(dock.classList.contains("collapsed")).toBe(true);
    expect(document.querySelector("#dock-strip").hidden).toBe(true);

    toggleDock();
    expect(dock.classList.contains("collapsed")).toBe(false);
    expect(getActiveTab()).toBe("status");
    expect(document.querySelector('.vx-dock-pane[data-pane="status"]').hidden).toBe(false);
  });
});
