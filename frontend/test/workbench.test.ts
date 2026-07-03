// @ts-nocheck
import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleNotification, initModelControls, _resetWorkbenchForTest } from "../src/main";
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

beforeEach(() => {
  resetRpc();
  resetDock();
  _resetWorkbenchForTest();
  initDock();
  initModelControls();
});

describe("workbench shell", () => {
  it("renders the fixed sidebar navigation and project sections", () => {
    const sidebar = document.querySelector("#sidebar");
    expect(sidebar.textContent).toContain("新对话");
    expect(sidebar.textContent).toContain("搜索");
    expect(sidebar.textContent).toContain("已安排");
    expect(sidebar.textContent).toContain("插件");
    expect(sidebar.textContent).toContain("项目");
    expect(sidebar.textContent).toContain("历史会话");
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
    expect(transcript.children).toHaveLength(0);
    expect(emptyState.hidden).toBe(false);
    expect(emptyState.textContent).toContain("我们应该在 voidx 中构建什么？");
  });

  it("hides empty-state prompt once a live conversation item starts", () => {
    const emptyState = document.querySelector("#empty-state");

    handleNotification("item.started", {
      kind: "assistant_stream",
      item_id: "stream-1",
      data: { phase: "thinking" },
    });

    expect(emptyState.hidden).toBe(true);
  });

  it("highlights the current workspace basename in the project list", () => {
    handleNotification("startup.shown", {
      workspace: "/Users/chikham/workspace/voidx",
      provider: "openai",
      model: "gpt-5.5",
      profile_configured: true,
    });

    const activeProject = document.querySelector(".vx-project-item.active");
    expect(activeProject).not.toBeNull();
    expect(activeProject.textContent).toContain("voidx");
  });
});

describe("provider and model controls", () => {
  it("renders provider and model selects with catalog options", () => {
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    expect(providerSelect).not.toBeNull();
    expect(modelSelect).not.toBeNull();
    expect([...providerSelect.options].map((option) => option.value)).toContain("openai");
    expect([...modelSelect.options].map((option) => option.value)).toContain("gpt-5.5");
  });

  it("changing provider refreshes model options without submitting", () => {
    const sentMessages = setupOpenSocket();
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    providerSelect.value = "anthropic";
    providerSelect.dispatchEvent(new Event("change", { bubbles: true }));

    expect([...modelSelect.options].map((option) => option.value)).toContain("claude-sonnet-4-6");
    expect(sentMessages).toHaveLength(0);
  });

  it("changing model submits exactly one slash command", async () => {
    const sentMessages = setupOpenSocket();
    const providerSelect = document.querySelector("#provider-select");
    const modelSelect = document.querySelector("#model-select");

    providerSelect.value = "anthropic";
    providerSelect.dispatchEvent(new Event("change", { bubbles: true }));
    modelSelect.value = "claude-opus-4-1";
    modelSelect.dispatchEvent(new Event("change", { bubbles: true }));

    await vi.waitFor(() => {
      expect(sentMessages).toHaveLength(1);
    });
    expect(sentPayloads(sentMessages)[0]).toMatchObject({
      method: "session.submit",
      params: { text: "/model switch anthropic/claude-opus-4-1" },
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
    expect(document.querySelector("#status-permission").textContent).toContain("未配置");
  });
});

describe("bottom panel", () => {
  it("contains Todo Terminal Diff and Status tabs", () => {
    const labels = [...document.querySelectorAll(".vx-dock-tab")].map((tab) => tab.textContent.trim());
    expect(labels).toEqual(["Todo", "Terminal", "Diff", "Status"]);
  });

  it("collapses to a status strip and preserves active tab", () => {
    const dock = document.querySelector("#dock");
    dock.classList.remove("collapsed");
    document.querySelector("#dock-strip").hidden = true;

    switchTab("status");
    toggleDock();

    expect(dock.classList.contains("collapsed")).toBe(true);
    expect(document.querySelector("#dock-strip").hidden).toBe(false);

    toggleDock();
    expect(dock.classList.contains("collapsed")).toBe(false);
    expect(getActiveTab()).toBe("status");
    expect(document.querySelector('.vx-dock-pane[data-pane="status"]').hidden).toBe(false);
  });
});
