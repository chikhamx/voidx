// @ts-nocheck
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tauriMocks = vi.hoisted(() => ({
  openDialog: vi.fn(),
  invoke: vi.fn(),
  listen: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: tauriMocks.openDialog,
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: tauriMocks.invoke,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: tauriMocks.listen,
}));

import { handleNotification, initModelControls, resolveWsUrl, _resetWorkbenchForTest } from "../../src/main";
import { isDesktopRuntime } from "../../src/services/connection";
import { initPermissionControls, populateCustomModelDropdown, populatePermissionDropdown } from "../../src/ui/model";
import { initStateDom, setConnectionStatus, uiState } from "../../src/services/state";
import { _resetForTest as resetDock, initDock, switchTab, toggleDock, getActiveTab } from "../../src/ui/dock";
import { _setSocket, _resetForTest as resetRpc } from "../../src/rpc";
import { addImageAttachment } from "../../src/ui";

function readCombinedStyles(filePath: string): string {
  let content = readFileSync(filePath, "utf8");
  const importRegex = /@import\s+url\(["']([^"']+)["']\);/g;
  content = content.replace(importRegex, (_, importPath) => {
    const absoluteImportPath = join(join(filePath, ".."), importPath);
    return readCombinedStyles(absoluteImportPath);
  });
  return content;
}

function readStylesCSS(): string {
  return readCombinedStyles(join(process.cwd(), "css/styles.css"));
}

function readIndexDOM(): Document {
  return new DOMParser().parseFromString(readFileSync(join(process.cwd(), "index.html"), "utf8"), "text/html");
}

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
  tauriMocks.listen.mockReset();
  delete window.__TAURI_INTERNALS__;
  delete window.__TAURI__;
  initDock();
  initModelControls();
  initPermissionControls();
});


describe("model dropdown safety", () => {
  it("renders configured provider and model names as text", () => {
    uiState.configuredProfiles = [{
      name: "unsafe",
      provider: "<svg data-provider-xss></svg>",
      model: "<img data-model-xss>",
      configured: true,
    }];

    populateCustomModelDropdown();

    const item = document.querySelector(".vx-model-dropdown-item");
    expect(item.querySelector("[data-provider-xss]")).toBeNull();
    expect(item.querySelector("[data-model-xss]")).toBeNull();
    expect(item.querySelector(".vx-model-item-title").textContent).toBe("<img data-model-xss>");
    expect(item.querySelector(".vx-model-item-subtitle").textContent).toBe("<svg data-provider-xss></svg>");
  });
});

describe("workbench shell", () => {
  it("renders the fixed sidebar navigation and project sections", () => {
    const sidebar = document.querySelector("#sidebar");
    expect(sidebar.textContent).toContain("新建会话");
    expect(sidebar.textContent).toContain("搜索");
    expect(sidebar.textContent).not.toContain("已安排");
    expect(sidebar.textContent).toContain("项目");
    expect(sidebar.textContent).not.toContain("历史会话");
    expect(readIndexDOM().querySelector(".vx-project-heading .vx-sidebar-row-icon")).toBeNull();
    expect(document.querySelector(".vx-project-heading-label").textContent).toBe("项目");
    expect(document.querySelector("#project-list")).toBeNull();
    expect(document.querySelector("#btn-integrations").hidden).toBe(true);
  });

  it("orders temporary sessions before projects and recent sessions", () => {
    for (const root of [document, readIndexDOM()]) {
      const sectionIds = [...root.querySelectorAll("#sidebar > .vx-sidebar-section")]
        .map((section) => section.id);
      expect(sectionIds).toEqual([
        "temporary-session-section",
        "project-session-section",
        "recent-session-section",
      ]);
    }
  });

  it("lays out three full-height columns with in-column headers", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-workbench-columns \{[^}]*display:\s*flex;[^}]*height:\s*100%;[^}]*min-height:\s*0;[^}]*\}/);
    expect(styles).toMatch(/\.vx-column-header \{[^}]*flex:\s*0 0 var\(--vx-titlebar-height\);[^}]*height:\s*var\(--vx-titlebar-height\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar \{[^}]*background:\s*var\(--vx-bg-app\);[^}]*flex:\s*0 0 var\(--vx-sidebar-width\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-main \{[^}]*background:\s*var\(--vx-bg-canvas\);[^}]*flex:\s*1 1 auto;[^}]*min-width:\s*0;[^}]*\}/);
    expect(styles).toMatch(/\.vx-dock \{[^}]*background-color:\s*var\(--vx-bg-subtle\);[^}]*flex:\s*0 0 var\(--vx-dock-width\);[^}]*\}/);
    expect(styles).not.toMatch(/\.vx-titlebar \{[^}]*position:\s*absolute;[^}]*\}/);
  });

  it("keeps the dock opaque and in its own flex column", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-main \{[^}]*flex: 1 1 auto;[^}]*min-width: 0;[^}]*width: 0;[^}]*\}/);
    expect(styles).toMatch(/\.vx-dock \{[^}]*background-color: var\(--vx-bg-subtle\);[^}]*flex: 0 0 var\(--vx-dock-width\);[^}]*position: relative;[^}]*z-index: 2;[^}]*\}/);
    expect(styles).toMatch(/\.vx-dock-content \{[^}]*background-color: var\(--vx-bg-subtle\);[^}]*\}/);
  });

  it("renders three independent workbench columns with their own headers", () => {
    const root = readIndexDOM();
    const workbench = root.querySelector(".vx-workbench-columns");
    const sidebar = workbench?.querySelector(":scope > #sidebar");
    const main = workbench?.querySelector(":scope > .vx-main");
    const dock = workbench?.querySelector(":scope > #dock");

    expect(workbench).not.toBeNull();
    expect(sidebar?.querySelector(":scope > .vx-column-header")).not.toBeNull();
    expect(main?.querySelector(":scope > .vx-column-header")).not.toBeNull();
    expect(dock?.querySelector(":scope > .vx-column-header")).not.toBeNull();
    expect(root.querySelector(".vx-workbench-shell > .vx-titlebar")).toBeNull();
  });

  it("places the terminal drawer at the bottom of the main conversation column", () => {
    const root = readIndexDOM();
    const main = root.querySelector(".vx-workbench-columns > .vx-main");
    const drawer = main?.querySelector(":scope > #terminal-drawer");

    expect(drawer).not.toBeNull();
    expect(drawer?.querySelector("#terminal-pane")).not.toBeNull();
    expect(root.querySelector("#dock #terminal-pane")).toBeNull();
    expect(root.querySelector("#dock [data-terminal-toggle]")).not.toBeNull();
  });


  it("keeps the terminal drawer open across streamed output notifications", () => {
    const drawer = document.querySelector("#terminal-drawer");
    drawer.hidden = true;

    handleNotification("terminal.output", { terminal_id: "term-stream", data: "first\n" });
    handleNotification("terminal.output", { terminal_id: "term-stream", data: "second\n" });

    expect(drawer.hidden).toBe(false);
    expect(document.querySelector("#terminal-pane").textContent).toContain("first");
    expect(document.querySelector("#terminal-pane").textContent).toContain("second");
  });
  it("separates workbench regions with backgrounds instead of structural borders", () => {
    const styles = readStylesCSS();

    expect(styles).not.toMatch(/\.vx-column-header \{[^}]*border-bottom:/);
    expect(styles).not.toMatch(/\.vx-sidebar \{[^}]*border-right:/);
    expect(styles).not.toMatch(/\.vx-dock \{[^}]*border-left:/);
    expect(styles).not.toMatch(/\.vx-dock-tabs \{[^}]*border-bottom:/);
    expect(styles).not.toMatch(/\.vx-terminal-drawer \{[^}]*border-top:/);
    expect(styles).not.toMatch(/\.vx-terminal-drawer-header \{[^}]*border-bottom:/);
    expect(styles).toMatch(/\.vx-dock-tabs \{[^}]*background:\s*var\(--vx-bg-subtle\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-terminal-drawer \{[^}]*background:\s*var\(--vx-bg-subtle\);[^}]*\}/);
  });

  it("renders right panel controls in the right column header", () => {
    const dockHeader = readIndexDOM().querySelector("#dock > .vx-column-header");
    expect(dockHeader?.querySelector('[data-tab="todo"]')).not.toBeNull();
    expect(dockHeader?.querySelector('[data-tab="diff"]')).not.toBeNull();
    expect(dockHeader?.querySelector('[data-tab="status"]')).not.toBeNull();
    expect(dockHeader?.querySelector("[data-terminal-toggle]")).not.toBeNull();
  });

  it("fully hides the collapsed dock and keeps its toggle in the main header", () => {
    const root = readIndexDOM();
    const dock = root.querySelector("#dock");
    const mainHeader = root.querySelector(".vx-main-header");
    const styles = readStylesCSS();

    expect(dock?.querySelector('[role="tablist"]')).not.toBeNull();
    expect(dock?.querySelectorAll('[role="tab"]')).toHaveLength(3);
    expect(mainHeader?.querySelector(":scope > #dock-toggle")).not.toBeNull();
    expect(dock?.querySelector("#dock-toggle")).toBeNull();
    expect(dock?.classList.contains("collapsed")).toBe(true);
    expect(mainHeader?.querySelector("#dock-toggle")?.getAttribute("aria-expanded")).toBe("false");
    expect(styles).toMatch(/\.vx-dock\.collapsed \{[^}]*flex-basis:\s*0;[^}]*width:\s*0;[^}]*\}/);
    expect(styles).toMatch(/@media \(max-width: 899px\) \{[\s\S]*?\.vx-dock \{[^}]*flex-basis:\s*min\(var\(--vx-dock-width\), 42vw\);[^}]*\}[\s\S]*?\.vx-dock\.collapsed \{[^}]*flex-basis:\s*0;[^}]*width:\s*0;[^}]*\}/);
  });

  it("keeps the recent heading close to the project section", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/#project-session-section\s*\+\s*#recent-session-section\s*\{[^}]*margin-top:\s*0;[^}]*\}/);
  });

  it("places the mode picker first in the sidebar above new session", () => {
    for (const root of [document, readIndexDOM()]) {
      const titlebarLeft = root.querySelector(".vx-titlebar-left");
      const sidebar = root.querySelector("#sidebar");
      const switcher = root.querySelector("#runtime-profile-switcher");
      const nav = root.querySelector(".vx-sidebar-nav");

      expect(sidebar?.firstElementChild).toBe(titlebarLeft);
      expect(titlebarLeft?.nextElementSibling).toBe(switcher);
      expect(switcher?.nextElementSibling).toBe(nav);
      expect(nav?.firstElementChild?.id).toBe("btn-new-chat");
      expect(switcher?.closest("#sidebar")).toBe(sidebar);
      expect(titlebarLeft?.contains(switcher)).toBe(false);
      expect(root.querySelector("#status-dot")).toBeNull();
      expect(titlebarLeft?.querySelector("#titlebar-sidebar-toggle")).not.toBeNull();
      expect(titlebarLeft?.querySelector("#titlebar-history-back")).not.toBeNull();
      expect(titlebarLeft?.querySelector("#titlebar-history-forward")).not.toBeNull();
      expect(switcher?.querySelector("#mode-trigger")).not.toBeNull();
      expect(
        [...switcher.querySelectorAll("[data-profile] .vx-mode-option-name")].map((el) => el.textContent.trim()),
      ).toEqual(["聊天", "编码", "目标", "循环"]);
      expect(root.querySelector("#composer #runtime-profile-switcher")).toBeNull();
    }
  });

  it("updates the status panel when the titlebar connection dot is absent", () => {
    const dot = document.querySelector("#status-dot");
    const parent = dot?.parentNode;
    const nextSibling = dot?.nextSibling;
    dot?.remove();

    try {
      expect(() => {
        initStateDom();
        setConnectionStatus("connected");
      }).not.toThrow();
      expect(document.querySelector("#status-connection").textContent).toBe("connected");
    } finally {
      if (dot && parent) parent.insertBefore(dot, nextSibling ?? null);
      initStateDom();
    }
  });

  it("accepts the backend initial snapshot when no thread is active", () => {
    handleNotification("workspace.snapshot", {
      threads: [],
      active_thread_id: "",
      active_snapshot: { thread_id: "", revision: 1, nodes: [] },
      provider: "openai",
      model: "gpt-5",
      workspace: "/Users/chikham/workspace/voidx",
    });

    expect(uiState.workspace).toBe("/Users/chikham/workspace/voidx");
    expect(uiState.provider).toBe("openai");
    expect(uiState.model).toBe("gpt-5");
    expect(document.querySelector(".vx-workspace-session-name").textContent).toBe("voidx");
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
    expect(emptyState.textContent).toContain("一起让世界变得更加美好！");
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
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-main-canvas\.empty \{[^}]*justify-content: center;[^}]*\}/);
    expect(styles).toMatch(/\.vx-main-canvas\.empty \.vx-empty-state \{[^}]*margin-bottom: var\(--vx-space-8\);[^}]*\}/);
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
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
    expect(tauriMocks.invoke).toHaveBeenCalledWith("wait_gateway_url");
  });

  it("recognizes the tauri protocol before desktop globals are injected", () => {
    expect(isDesktopRuntime({ location: { protocol: "tauri:" } })).toBe(true);
  });

  it("uses the desktop blocking gateway handshake when available", async () => {
    tauriMocks.invoke.mockImplementation((command: string) => {
      if (command === "wait_gateway_url") {
        return Promise.resolve("ws://127.0.0.1:54321/?token=wait");
      }
      return Promise.resolve(null);
    });

    await expect(resolveWsUrl()).resolves.toBe("ws://127.0.0.1:54321/?token=wait");
    expect(tauriMocks.invoke).toHaveBeenCalledWith("wait_gateway_url");
  });

  it("keeps waiting while the desktop backend is still starting", async () => {
    vi.useFakeTimers();
    let gatewayChecks = 0;
    tauriMocks.invoke.mockImplementation((command: string) => {
      if (command === "get_gateway_url") {
        gatewayChecks += 1;
        return Promise.resolve(gatewayChecks > 60 ? "ws://127.0.0.1:54321/?token=ready" : null);
      }
      if (command === "get_backend_status") {
        return Promise.resolve({ status: "starting" });
      }
      return Promise.resolve(null);
    });

    const pending = resolveWsUrl();
    await vi.advanceTimersByTimeAsync(31_000);

    await expect(pending).resolves.toBe("ws://127.0.0.1:54321/?token=ready");
    vi.useRealTimers();
  });

  it("keeps waiting when the optional backend status query is unavailable", async () => {
    vi.useFakeTimers();
    let gatewayChecks = 0;
    tauriMocks.invoke.mockImplementation((command: string) => {
      if (command === "get_gateway_url") {
        gatewayChecks += 1;
        return Promise.resolve(gatewayChecks > 2 ? "ws://127.0.0.1:54321/?token=ready" : null);
      }
      if (command === "get_backend_status") {
        return Promise.reject(new Error("status command unavailable"));
      }
      return Promise.resolve(null);
    });

    const pending = resolveWsUrl();
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(pending).resolves.toBe("ws://127.0.0.1:54321/?token=ready");
    vi.useRealTimers();
  });

  it("resolves from the desktop backend-ready event when polling has no url", async () => {
    tauriMocks.invoke.mockImplementation((command: string) => {
      if (command === "get_gateway_url") return Promise.resolve(null);
      if (command === "get_backend_status") return Promise.resolve({ status: "starting" });
      return Promise.resolve(null);
    });
    tauriMocks.listen.mockImplementation(async (_event: string, handler: Function) => {
      queueMicrotask(() => handler({ payload: { url: "ws://127.0.0.1:54321/?token=event" } }));
      return () => {};
    });

    await expect(resolveWsUrl()).resolves.toBe("ws://127.0.0.1:54321/?token=event");
    expect(tauriMocks.listen).toHaveBeenCalledWith("backend_ready", expect.any(Function));
  });

  it("keeps sidebar rows aligned in the workbench layout", () => {
    const styles = readStylesCSS();

    expect(styles).toContain(".vx-nav-item,\n.vx-directory-row,\n.vx-session-item");
    expect(document.querySelector("#sidebar-resizer")).not.toBeNull();
    expect(styles).toContain(".vx-sidebar-nav,\n.vx-sidebar-footer");
    expect(styles).toContain(".vx-session-children {\n  display: grid;");
    expect(styles).toContain("padding-left: var(--vx-space-4);");
    expect(styles).toMatch(/\.vx-sidebar-section \{[^}]*min-height: 0;[^}]*\}/);
    expect(styles).toContain(".vx-project-session-section { flex: 0 1 auto; }");
    expect(styles).toMatch(/\.vx-session-item \{[^}]*grid-template-columns: minmax\(0, 1fr\) max-content;[^}]*\}/);
    expect(styles).toMatch(/\.vx-session-time \{[^}]*justify-self: end;[^}]*\}/);
    expect(styles).toMatch(/\.vx-nav-item,[\s\S]*\.vx-directory-row,[\s\S]*\.vx-session-item \{[^}]*padding: 0 var\(--vx-space-2\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar-heading \{[^}]*color: var\(--vx-text-muted\);[^}]*font-size: var\(--vx-text-xs\);[^}]*min-height: 28px;[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar-row-icon \{[^}]*color: var\(--vx-text-muted\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar-resizer \{[^}]*cursor: col-resize;[^}]*\}/);
    expect(styles).toMatch(/\.vx-dock \{[^}]*background-color: var\(--vx-bg-subtle\);[^}]*width: var\(--vx-dock-width\);[^}]*\}/);
    expect(styles).toContain(".vx-sidebar-row-icon");
  });

  it("uses a desktop glass material across the left column", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-titlebar-left\s*\{[^}]*background:\s*var\(--vx-bg-app\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar\s*\{[^}]*background:\s*var\(--vx-bg-app\);[^}]*padding:\s*0 var\(--vx-space-2\) var\(--vx-space-2\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-sidebar > \.vx-mode-picker\s*\{[^}]*min-height:\s*32px;[^}]*\}/);
    expect(styles).toMatch(/body\.is-desktop\.is-mac \.vx-titlebar-left\s*\{[^}]*background:\s*transparent;[^}]*\}/);
    expect(styles).toMatch(/body\.is-desktop\.is-mac \.vx-sidebar\s*\{[^}]*background:\s*rgb\(255 255 255 \/ 0\.3\);[^}]*-webkit-backdrop-filter:\s*saturate\(180%\) blur\(28px\);[^}]*backdrop-filter:\s*saturate\(180%\) blur\(28px\);[^}]*\}/);
    expect(styles).toMatch(/:root\[data-theme=["']dark["']\] body\.is-desktop\.is-mac \.vx-sidebar\s*\{[^}]*background:\s*rgb\(28 28 30 \/ 0\.45\);[^}]*\}/);
    expect(styles).toMatch(/body\.is-desktop \.vx-titlebar-left\s*\{[^}]*padding-left:\s*80px;[^}]*\}/);
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
      active_snapshot: { thread_id: "filled", revision: 0, nodes: [] },
      threads: [
        {
          thread_id: "empty-1",
          title: "New session",
          status: "idle",
          workspace: "/Users/chikham/workspace/voidx",
          runtime_profile: "coding",
          temporary: true,
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
      expect(document.querySelector("#status-session-detail").textContent).toContain("empty-1");
    });
    document.querySelector(".vx-workspace-session-row")?.click();
    await vi.waitFor(() => {
      expect(document.querySelector(".vx-session-item.active").dataset.threadId).toBe("empty-1");
    });
  });

  it("keeps the draft and shows an error when send is clicked while disconnected", () => {
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    input.value = "你好";
    send.click();

    expect(input.value).toBe("你好");
    expect(uiState.isRunning).toBe(false);
    expect(document.querySelector(".message-error")?.textContent).toContain("发送失败");
    expect(document.querySelector(".message-error")?.textContent).toContain("未连接");
  });

  it("restores the draft and shows an error when idle submission fails", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "你好";
    send.click();

    const submitMsg = sentPayloads(sentMessages).find((payload) => payload.method === "session.submit");
    expect(submitMsg).toBeDefined();
    socket.onmessage({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: submitMsg.id,
        error: { code: -32603, message: "submit failed" },
      }),
    });

    await vi.waitFor(() => {
      expect(input.value).toBe("你好");
    });
    expect(uiState.isRunning).toBe(false);
    expect(document.querySelector(".message-text")).toBeNull();
    expect(document.querySelector(".message-error")?.textContent).toContain("submit failed");
  });

  it("restores the draft when the backend rejects an idle submission", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "你好";
    send.click();

    const submitMsg = sentPayloads(sentMessages).find((payload) => payload.method === "session.submit");
    socket.onmessage({
      data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, result: { ok: false } }),
    });

    await vi.waitFor(() => {
      expect(input.value).toBe("你好");
    });
    expect(uiState.isRunning).toBe(false);
    expect(document.querySelector(".message-error")?.textContent).toContain("未接受");
  });

  it("submits guidance when clicking send during a running turn", async () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    sentMessages.length = 0;

    input.value = "继续执行";
    send.click();

    expect(send.disabled).toBe(false);
    expect(send.getAttribute("aria-label")).toBe("Send guidance");
    expect(sentPayloads(sentMessages).filter((payload) => payload.method === "session.submit")).toEqual([
      expect.objectContaining({
        method: "session.submit",
        params: { text: "继续执行", thread_id: "t1" },
      }),
    ]);
    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.cancel")).toBe(false);
  });

  it("keeps input enabled while a turn is running", () => {
    const input = document.querySelector("#input");
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    expect(input.disabled).toBe(false);
  });

  it("clears input without sending when submitting an unknown slash command", () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "/zzz";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(input.value).toBe("");
    expect(sentPayloads(sentMessages).filter((p) => p.method === "session.submit")).toHaveLength(0);
  });

  it("sends known slash command without args instead of clearing input", () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "/help";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(sentPayloads(sentMessages).some((p) => p.method === "session.submit" && p.params.text === "/help")).toBe(true);
  });

  it("sends known slash command with args instead of clearing input", () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "/lang en";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(sentPayloads(sentMessages).some((p) => p.method === "session.submit" && p.params.text === "/lang en")).toBe(true);
  });


  it("sends /loop prompts with attachment tokens instead of clearing input", () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    sentMessages.length = 0;

    input.value = "/loop 5m @docs/review.md";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(sentPayloads(sentMessages).some((p) => p.method === "session.submit" && p.params.text === "/loop 5m @docs/review.md")).toBe(true);
  });

  it("shows guidance pending state when submitting during a running turn", async () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    sentMessages.length = 0;

    input.value = "keep going";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(send.classList.contains("guidance-pending")).toBe(true);
    expect(sentPayloads(sentMessages).find((p) => p.method === "session.submit")).toMatchObject({
      method: "session.submit",
      params: { text: "keep going", thread_id: "t1" },
    });
  });

  it("clears guidance pending state after RPC resolves", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    sentMessages.length = 0;

    input.value = "keep going";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(send.classList.contains("guidance-pending")).toBe(true);

    const submitMsg = sentPayloads(sentMessages).find((p) => p.method === "session.submit");
    expect(submitMsg).toBeDefined();
    socket.onmessage({ data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, result: { ok: true } }) } as MessageEvent);

    await vi.waitFor(() => {
      expect(send.classList.contains("guidance-pending")).toBe(false);
    });
  });

  it("preserves input text when guidance submission fails", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    sentMessages.length = 0;

    input.value = "keep going";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    const submitMsg = sentPayloads(sentMessages).find((p) => p.method === "session.submit");
    socket.onmessage({ data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, error: { code: -32603, message: "fail" } }) } as MessageEvent);

    await vi.waitFor(() => {
      expect(send.classList.contains("guidance-pending")).toBe(false);
    });
    expect(input.value).toBe("keep going");
  });

  it("clears input text when guidance submission succeeds", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
    });
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
    sentMessages.length = 0;

    input.value = "keep going";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    const submitMsg = sentPayloads(sentMessages).find((p) => p.method === "session.submit");
    socket.onmessage({ data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, result: { ok: true } }) } as MessageEvent);

    await vi.waitFor(() => {
      expect(input.value).toBe("");
    });
    expect(send.classList.contains("guidance-pending")).toBe(false);
  });

  it("restores the regular send label when a turn ends", () => {
    const send = document.querySelector("#btn-send");
    uiState.sessionId = "t1";
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });

    expect(send.getAttribute("aria-label")).toBe("Send guidance");

    handleNotification("turn.completed", { thread_id: "t1", turn_id: "test-turn" });

    expect(send.getAttribute("aria-label")).toBe("Send");
    expect(send.querySelector("svg.vx-icon")).not.toBeNull();
  });

  it("renders a visible error when a turn fails", () => {
    const send = document.querySelector("#btn-send");
    uiState.sessionId = "t1";
    handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });

    handleNotification("turn.failed", {
      thread_id: "t1",
      turn_id: "test-turn",
      message: "LLM call failed: invalid API key",
    });

    expect(send.classList.contains("running")).toBe(false);
    expect(send.querySelector("svg.vx-icon")).not.toBeNull();
    expect(document.querySelector("#transcript").textContent).toContain("LLM call failed: invalid API key");
    expect(document.querySelector(".message-error")).not.toBeNull();
  });

  it("renders Clarify in the conversation and posts a quick reply as a user message", () => {
    const sentMessages = setupOpenSocket();
    const dialog = document.querySelector("#request-dialog");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
    });
    sentMessages.length = 0;

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-clarify-1",
      thread_id: "t1",
      turn_id: "turn-1",
      data: {
        prompt_type: "clarify",
        clarify_id: "clarify-1",
        question: "你希望采用哪种实现方式？",
        options: ["直接实现", "先写设计文档"],
      },
    });

    const prompt = document.querySelector('[data-prompt-request-id="clarify-1"]');
    expect(dialog.open).toBe(false);
    expect(prompt?.classList.contains("prompt-message")).toBe(true);
    expect(prompt?.textContent).toContain("你希望采用哪种实现方式？");
    expect([...prompt.querySelectorAll(".prompt-reply")].map((button) => button.textContent)).toEqual([
      "直接实现",
      "先写设计文档",
    ]);

    prompt.querySelector(".prompt-reply").click();

    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.respond")).toMatchObject({
      method: "session.respond",
      params: { request_id: "clarify-1", thread_id: "t1", value: "直接实现" },
    });
    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.submit")).toBe(false);
    expect(document.querySelector(".message-text")?.textContent).toContain("直接实现");
    expect(prompt.querySelector(".prompt-replies")).toBeNull();
  });

  it("renders Checkpoint context as an assistant message", () => {
    setupOpenSocket();
    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
    });

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-checkpoint-1",
      thread_id: "t1",
      turn_id: "turn-1",
      data: {
        prompt_type: "checkpoint",
        checkpoint_id: "checkpoint-1",
        plan: {
          goal: "完成对话式审批",
          plan_summary: "先改交互，再验证",
          steps: ["增加测试", "实现交互"],
          affected_files: ["frontend/src/main.ts"],
          risks: ["回答不能误走 session.submit"],
        },
        choices: [{ label: "批准", value: "approved", description: "按计划实施" }],
      },
    });

    const prompt = document.querySelector('[data-prompt-request-id="checkpoint-1"]');
    expect(document.querySelector("#request-dialog").open).toBe(false);
    expect(prompt?.textContent).toContain("完成对话式审批");
    expect(prompt?.textContent).toContain("先改交互，再验证");
    expect(prompt?.textContent).toContain("增加测试");
    expect(prompt?.textContent).toContain("frontend/src/main.ts");
    expect(prompt?.textContent).toContain("回答不能误走 session.submit");
  });

  it("submits a Goal Spec answer from the composer through session.respond", () => {
    const sentMessages = setupOpenSocket();
    const input = document.querySelector("#input");

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
    });
    sentMessages.length = 0;

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-goal-1",
      thread_id: "t1",
      turn_id: "turn-1",
      data: {
        prompt_type: "goal_spec",
        prompt_id: "goal-1",
        spec: {
          objective: "发布桌面版",
          acceptance_condition: "全部前端测试通过",
          achievement_method: "先定向测试再完整验证",
          max_attempts: 3,
        },
        choices: [{ label: "批准", value: "approved", description: "开始执行" }],
      },
    });

    const prompt = document.querySelector('[data-prompt-request-id="goal-1"]');
    expect(document.querySelector("#request-dialog").open).toBe(false);
    expect(prompt?.textContent).toContain("发布桌面版");
    expect(prompt?.textContent).toContain("全部前端测试通过");

    input.value = "把最大尝试次数改成 5";
    document.querySelector("#composer").dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));

    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.respond")).toMatchObject({
      method: "session.respond",
      params: { request_id: "goal-1", thread_id: "t1", value: "把最大尝试次数改成 5" },
    });
    expect(sentPayloads(sentMessages).some((payload) => payload.method === "session.submit")).toBe(false);
    expect(document.querySelector(".message-text")?.textContent).toContain("把最大尝试次数改成 5");
    expect(input.value).toBe("");
  });

  it("does not open request dialog for status-only permission prompt notifications", () => {
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-1",
      data: {
        prompt_type: "permission",
        interactive: false,
        prompt: "允许写文件？",
        choices: [["Yes", "allow", "允许一次"]],
        tools: [{ name: "write", pattern: "/tmp/a.txt", args: { path: "/tmp/a.txt" } }],
      },
    });

    expect(showModal).not.toHaveBeenCalled();
    expect(document.querySelector("#request-controls").textContent).toBe("");
  });

  it("opens request dialog for real permission ui requests", () => {
    const dialog = document.querySelector("#request-dialog");
    const showModal = vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    uiState.sessionId = "t2";

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "perm_1",
      thread_id: "t2",
      prompt: "允许写文件？",
      choices: [["Yes", "y", "允许一次"]],
      tools: [{ name: "write", pattern: "/tmp/a.txt", args: { path: "/tmp/a.txt" } }],
    });

    expect(showModal).toHaveBeenCalled();
    expect(document.querySelector("#request-title").textContent).toBe("权限审批");
    expect(document.querySelector(".request-permission-question").textContent).toContain("允许写文件？");
    expect(document.querySelector("#request-controls").textContent).toContain("允许一次");
  });


  it("renders risk-aware permission request details", () => {
    const dialog = document.querySelector("#request-dialog");
    vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    uiState.sessionId = "t2";

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "perm_risk_1",
      thread_id: "t2",
      prompt: "Allow tool use?",
      choices: [["Do not run", "n", "This command is blocked"]],
      tools: [
        {
          name: "bash",
          pattern: "sudo true",
          args: { command: "sudo true" },
          risk: {
            level: "blocked",
            tags: ["privilege_escalation"],
            reason: "sudo is blocked",
            tool_name: "bash",
            pattern: "sudo true",
          },
          allowed_scopes: ["once"],
          default_scope: "once",
        },
      ],
    });

    const details = document.querySelector("#request-details");
    expect(details.textContent).toContain("bash");
    expect(details.textContent).toContain("sudo true");
    expect(details.textContent).toContain("blocked");
    expect(details.textContent).toContain("privilege_escalation");
    expect(details.textContent).toContain("sudo is blocked");
    expect(details.textContent).not.toContain("Allowed scopes: once");
    expect(details.textContent).not.toContain("Default scope: once");
    expect(document.querySelector("#request-controls").textContent).toContain("This command is blocked");
    expect(document.querySelectorAll("#request-controls button")).toHaveLength(1);
    expect(document.querySelector("#request-controls").textContent).not.toContain("Allow this command");
  });

  it("renders AI approval failure reason in permission details", () => {
    const dialog = document.querySelector("#request-dialog");
    vi.spyOn(dialog, "showModal").mockImplementation(() => {});
    uiState.sessionId = "t2";

    handleNotification("ui.request", {
      kind: "permission",
      request_id: "perm_ai_1",
      thread_id: "t2",
      prompt: "Allow tool use?",
      choices: [["Yes", "y", "Allow once"]],
      tools: [
        {
          name: "bash",
          pattern: "./build.sh",
          args: { command: "./build.sh" },
          ai_approval_failure: "AI approval failed: error",
        },
      ],
    });

    expect(document.querySelector("#request-details").textContent).toContain("AI approval failed: error");
  });

  it("queues overlapping ui requests instead of replacing the active dialog", () => {
    const sentMessages = setupOpenSocket();
    uiState.sessionId = "t2";
    const dialog = document.querySelector("#request-dialog");
    vi.spyOn(dialog, "showModal").mockImplementation(() => {
      dialog.setAttribute("open", "");
    });
    vi.spyOn(dialog, "close").mockImplementation(() => {
      dialog.removeAttribute("open");
    });

    handleNotification("ui.request", {
      kind: "choice",
      request_id: "read_1",
      thread_id: "t2",
      prompt: "Read file outside workspace? /tmp/a.txt",
      choices: [["Yes", "allow", "Allow this read once"]],
    });
    handleNotification("ui.request", {
      kind: "choice",
      request_id: "read_2",
      thread_id: "t2",
      prompt: "Read file outside workspace? /tmp/b.txt",
      choices: [["Yes", "allow", "Allow this read once"]],
    });

    expect(document.querySelector("#request-title").textContent).toContain("/tmp/a.txt");
    document.querySelector("#request-controls button").click();

    expect(sentPayloads(sentMessages).find((payload) => payload.id === "read_1")).toMatchObject({
      id: "read_1",
      result: { value: "allow" },
    });
    expect(document.querySelector("#request-title").textContent).toContain("/tmp/b.txt");
    document.querySelector("#request-controls button").click();

    expect(sentPayloads(sentMessages).find((payload) => payload.id === "read_2")).toMatchObject({
      id: "read_2",
      result: { value: "allow" },
    });
  });

  it("sends prompt item responses through session.respond", () => {
    const sentMessages = setupOpenSocket();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t2",
      active_snapshot: { thread_id: "t2", revision: 0, nodes: [] },
      threads: [{ thread_id: "t2", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
    });

    handleNotification("item.started", {
      kind: "prompt",
      item_id: "prompt-1",
      thread_id: "t2",
      turn_id: "turn-1",
      data: {
        prompt_type: "clarify",
        clarify_id: "cl_1",
        question: "选哪个方案？",
        options: ["直接实现", "先写文档"],
      },
    });
    document.querySelector('[data-prompt-request-id="cl_1"] .prompt-reply').click();

    expect(sentPayloads(sentMessages).find((payload) => payload.method === "session.respond")).toMatchObject({
      method: "session.respond",
      params: { request_id: "cl_1", value: "直接实现", thread_id: "t2" },
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });
    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      params: { text: "/model switch anthropic/claude-opus-4-1 --local", thread_id: "t1" },
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
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



  it("refreshes settings when profile_configured changes", () => {
    const sentMessages = setupOpenSocket();
    handleNotification("startup.shown", { profile_configured: false });
    sentMessages.length = 0;

    handleNotification("startup.shown", { profile_configured: true });

    expect(sentPayloads(sentMessages).some((payload) => payload.method === "settings.get")).toBe(true);
  });
  it("workspace.snapshot fetches settings when startup model is missing", async () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();

    handleNotification("workspace.snapshot", {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/Users/chikham/workspace/voidx",
    });

    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
      profile_configured: null,
    });

    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "",
      model: "",
    });

    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
      active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
      threads: [{ thread_id: "t1", title: "Default" }],
      workspace: "/",
      provider: "deepseek",
      model: "deepseek-v4-flash",
      profile_configured: true,
    });

    const request = sentPayloads(sentMessages).find((p) => p.method === "settings.get");
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
  it("contains Todo Diff Status tabs and a terminal drawer entry", () => {
    const dock = document.querySelector("#dock");
    const labels = [...dock.querySelectorAll(".vx-dock-tab[data-tab]")].map((tab) => tab.textContent.trim());
    expect(labels).toEqual(["Todo", "Diff", "Status"]);
    expect(dock.querySelector("[data-terminal-toggle]")?.textContent.trim()).toBe("Terminal");
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


it("renders running-turn guidance exactly once from the backend message event", async () => {
  const { sentMessages, socket } = setupOpenSocketWithHandle();
  const input = document.querySelector("#input");

  handleNotification("workspace.snapshot", {
    active_thread_id: "t1",
    active_snapshot: { thread_id: "t1", revision: 0, nodes: [] },
    threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
    workspace: "<workspace>",
    provider: "deepseek",
    model: "deepseek-chat",
    profile_configured: true,
  });
  handleNotification("turn.started", { thread_id: "t1", turn_id: "test-turn" });
  sentMessages.length = 0;

  input.value = "keep going";
  document.querySelector("#composer").dispatchEvent(
    new SubmitEvent("submit", { bubbles: true, cancelable: true }),
  );

  expect(document.querySelectorAll(".message-guidance")).toHaveLength(0);

  const submitMsg = sentPayloads(sentMessages).find((p) => p.method === "session.submit");
  socket.onmessage({
    data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, result: { ok: true } }),
  } as MessageEvent);
  await vi.waitFor(() => expect(input.value).toBe(""));
  expect(document.querySelectorAll(".message-guidance")).toHaveLength(0);

  handleNotification("item.started", {
    thread_id: "t1",
    turn_id: "test-turn",
    kind: "message",
    item_id: "guidance-1",
    data: { style: "guidance", text: "keep going" },
  });

  const messages = document.querySelectorAll(".message-guidance");
  expect(messages).toHaveLength(1);
  expect(messages[0].textContent).toContain("keep going");
});


describe("AI approval permission mode", () => {
  it("shows AI approval in the permission dropdown", async () => {
    const dropdown = document.createElement("div");
    dropdown.id = "permission-dropdown";
    document.body.append(dropdown);
    populatePermissionDropdown();
    expect(dropdown.textContent).toContain("AI 审批");
    expect(dropdown.textContent).toContain("受限工具参数");
  });
});


describe("turn-end snapshot reconciliation", () => {
  function snapshotEnvelope(nodes, revision = 2, extra = {}) {
    return {
      active_thread_id: "t1",
      active_snapshot: { thread_id: "t1", revision, nodes },
      threads: [{ thread_id: "t1", title: "Default", workspace: "<workspace>" }],
      workspace: "<workspace>",
      provider: "deepseek",
      model: "deepseek-chat",
      profile_configured: true,
      ...extra,
    };
  }

  function submitText(input, send, sentMessages, socket, text) {
    input.value = text;
    send.click();
    const submitMsg = sentPayloads(sentMessages).find((p) => p.method === "session.submit");
    socket.onmessage({
      data: JSON.stringify({ jsonrpc: "2.0", id: submitMsg.id, result: { ok: true } }),
    } as MessageEvent);
    return submitMsg;
  }

  it("absorbs the local echo when the snapshot turn carries raw_text with attachment tokens", () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", snapshotEnvelope([], 0));

    addImageAttachment("clipboard-test", "data:image/png;base64,xx");
    const submitMsg = submitText(input, send, sentMessages, socket, "看下这张图");
    expect(submitMsg.params.text).toBe("看下这张图 [image-clipboard-test]");
    expect(document.querySelectorAll(".message-text")).toHaveLength(1);

    handleNotification("workspace.snapshot", snapshotEnvelope([{
      id: "n1",
      node_type: "turn",
      status: "ok",
      header: "[bold white]❯[/] 看下这张图",
      body_lines: ["\\[attachments: .voidx/attachments/clipboard-test.png]"],
      payload: { raw_text: "看下这张图 [image-clipboard-test]" },
    }]));

    const messages = [...document.querySelectorAll(".message-item")]
      .filter((el) => el.textContent.includes("看下这张图"));
    expect(messages).toHaveLength(1);
    expect(messages[0].dataset.itemId).toBe("n1");
  });

  it("keeps streamed tool items in their group above the answer after the turn-end snapshot", () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");
    const transcript = document.querySelector("#transcript");

    handleNotification("workspace.snapshot", snapshotEnvelope([], 0));
    submitText(input, send, sentMessages, socket, "查一下");

    handleNotification("turn.started", { thread_id: "t1", turn_id: "turn-1" });
    handleNotification("item.started", {
      thread_id: "t1", turn_id: "turn-1", kind: "tool", item_id: "uid-tool-1",
      data: { tool_call_id: "tc1", tool_name: "bash", args: { command: "ls" } },
    });
    handleNotification("item.completed", {
      thread_id: "t1", turn_id: "turn-1", kind: "tool", item_id: "uid-tool-1",
      data: { tool_call_id: "tc1", ok: true, elapsed: 1, detail: "done" },
    });
    handleNotification("item.started", {
      thread_id: "t1", turn_id: "turn-1", kind: "assistant_stream", item_id: "s1",
      data: { phase: "text" },
    });
    handleNotification("item.delta", {
      thread_id: "t1", turn_id: "turn-1", kind: "assistant_stream", item_id: "s1",
      data: { text: "回答内容", phase: "text" },
    });
    handleNotification("item.completed", {
      thread_id: "t1", turn_id: "turn-1", kind: "assistant_stream", item_id: "s1", data: {},
    });
    handleNotification("turn.completed", { thread_id: "t1", turn_id: "turn-1" });

    handleNotification("workspace.snapshot", snapshotEnvelope([
      { id: "n1", node_type: "turn", status: "ok", header: "[bold white]❯[/] 查一下", payload: { raw_text: "查一下" } },
      { id: "n3", node_type: "tool_call", status: "ok", tool_call_id: "tc1", header: "bash", payload: { tool_name: "bash", args: { command: "ls" } } },
      { id: "n4", node_type: "tool_result", status: "ok", tool_call_id: "tc1", payload: { raw_text: "done" } },
      { id: "n2", node_type: "assistant", status: "ok", payload: { raw_text: "回答内容" } },
    ], 3));

    expect(document.querySelectorAll("[data-tool-id=\"tc1\"]")).toHaveLength(1);
    expect(document.querySelectorAll(".tool-group")).toHaveLength(1);

    const order = [...transcript.children].map((el) => (
      el.classList.contains("message-item") ? "message"
        : el.classList.contains("tool-group") ? "group"
          : el.classList.contains("stream-buffer") ? "stream"
            : "other"
    ));
    expect(order).toEqual(["message", "group", "stream"]);
  });

  it("absorbs a pending chat guidance echo when the snapshot turn carries guidance style", () => {
    const { sentMessages, socket } = setupOpenSocketWithHandle();
    const input = document.querySelector("#input");
    const send = document.querySelector("#btn-send");

    handleNotification("workspace.snapshot", snapshotEnvelope([], 0, { runtime_profile: "chat" }));
    handleNotification("turn.started", { thread_id: "t1", turn_id: "turn-1" });
    submitText(input, send, sentMessages, socket, "继续执行");
    expect(document.querySelectorAll(".message-guidance")).toHaveLength(1);

    handleNotification("workspace.snapshot", snapshotEnvelope([{
      id: "n1",
      node_type: "turn",
      status: "ok",
      header: "[bold white]❯[/] 继续执行",
      payload: { raw_text: "继续执行", style: "guidance" },
    }], 4, { runtime_profile: "chat" }));

    const messages = [...document.querySelectorAll(".message-item")]
      .filter((el) => el.textContent.includes("继续执行"));
    expect(messages).toHaveLength(1);
    expect(messages[0].dataset.itemId).toBe("n1");
  });
});


describe("collapsed sidebar titlebar", () => {
  it("keeps all three titlebar controls at their expanded positions", () => {
    const styles = readStylesCSS();
    const root = readIndexDOM();
    const collapsedSidebar = styles.match(/\.vx-workbench-shell\.sidebar-collapsed \.vx-sidebar \{([^}]*)\}/)?.[1] || "";
    const collapsedTitlebar = styles.match(/\.vx-workbench-shell\.sidebar-collapsed \.vx-titlebar-left \{([^}]*)\}/)?.[1] || "";

    expect(root.querySelectorAll(".vx-titlebar-left > .vx-titlebar-tool")).toHaveLength(3);
    expect(collapsedSidebar).toContain("flex-basis: 0");
    expect(collapsedSidebar).toContain("overflow: visible");
    expect(collapsedSidebar).toContain("width: 0");
    expect(collapsedTitlebar).toContain("left: 0");
    expect(collapsedTitlebar).toContain("position: absolute");
    expect(collapsedTitlebar).not.toContain("justify-content: center");
    expect(collapsedTitlebar).not.toContain("padding: 0");
    expect(styles).not.toContain(".vx-workbench-shell.sidebar-collapsed .vx-titlebar-left > :not(#titlebar-sidebar-toggle)");
    expect(styles).toMatch(/\.vx-workbench-shell\.sidebar-collapsed \.vx-main-header \{[^}]*padding-left:/);
  });
});
