/// <reference types="vite/client" />
import {
  renderTranscript,
  appendMessageItem,
  handleToolItem,
  handleStatusItem,
  appendThoughtItem,
  appendNoticeItem,
  appendDiffItem,
} from "./render";
import type { TranscriptSnapshot } from "./render";
import { matchSlashCommands, renderSlashMenu } from "./slash";
import {
  setTranscriptElement,
  appendStreamText,
  commitStream,
  discardStream,
} from "./stream";
import { rpcCall, rpcRespond, onNotification, _setSocket, createWorkerSocket, isRpcConnected } from "./rpc";
import {
  renderSidebar,
  addThread,
  findReusableEmptyThread,
  updateThreadStatus,
  filterSessions,
  onThreadSelect,
  onNewThread,
  onThreadDelete,
  onThreadRename,
} from "./sidebar";
import type { ThreadInfo } from "./sidebar";
import { initDock, renderTodoInDock, switchTab } from "./dock";
import {
  initTerminal,
  appendTerminalOutput,
  showTerminalClosed,
  onTerminalInput,
  onTerminalStart,
  setActiveTerminal,
} from "./terminal";
import {
  renderDiffReview,
  setHunkDecision,
  onHunkDecision,
  onApplyDiff,
  onGenerateDiff,
  showDiffEmpty,
} from "./diff-review";
import { initSettingsModal, openSettingsModal, _resetSettingsForTest } from "./settings";
import type { ProfileSummary, SettingsSnapshot } from "./settings";
import {
  initIntegrationsPanel,
  openIntegrationsPanel,
  _resetIntegrationsForTest,
} from "./integrations";
import type { IntegrationsSnapshot } from "./integrations";
import { initContextMenu, _resetContextMenuForTest } from "./context-menu";
import type { SlashCommand } from "./types";

const statusDotEl = document.querySelector("#status-dot")!;
const statusModelEl = document.querySelector("#status-model");
const statusWorkspaceEl = document.querySelector("#status-workspace");
const statusSessionEl = document.querySelector("#status-session")!;
const statusConnectionEl = document.querySelector("#status-connection")!;
const statusSessionDetailEl = document.querySelector("#status-session-detail")!;
const statusWorkspaceDetailEl = document.querySelector("#status-workspace-detail")!;
const statusProviderModelEl = document.querySelector("#status-provider-model")!;
const statusPermissionEl = document.querySelector("#status-permission");
const statusRunningEl = document.querySelector("#status-running")!;
const stripWorkspaceEl = document.querySelector("#strip-workspace")!;
const stripPermissionEl = document.querySelector("#strip-permission");
const stripProviderModelEl = document.querySelector("#strip-provider-model")!;
const titlebarProjectEl = document.querySelector("#titlebar-project");
const contextWorkspaceEl = document.querySelector("#context-workspace")!;
const contextPermissionEl = document.querySelector("#context-permission");
const contextProviderModelEl = document.querySelector("#context-provider-model")!;
const emptyStateEl = document.querySelector<HTMLElement>("#empty-state")!;
const transcriptEl = document.querySelector<HTMLElement>("#transcript")!;
const mainCanvasEl = document.querySelector<HTMLElement>(".vx-main-canvas")!;
const composerEl = document.querySelector<HTMLFormElement>("#composer")!;
const inputEl = document.querySelector<HTMLTextAreaElement>("#input")!;
const btnSendEl = document.querySelector<HTMLButtonElement>("#btn-send")!;
const providerSelectEl = document.querySelector<HTMLSelectElement>("#provider-select")!;
const modelSelectEl = document.querySelector<HTMLSelectElement>("#model-select")!;
const slashMenuEl = document.querySelector<HTMLElement>("#slash-menu")!;
const requestDialogEl = document.querySelector<HTMLDialogElement>("#request-dialog")!;
const requestTitleEl = document.querySelector<HTMLElement>("#request-title")!;
const requestDetailsEl = document.querySelector<HTMLElement>("#request-details")!;
const requestControlsEl = document.querySelector<HTMLElement>("#request-controls")!;

interface UiState {
  connection: string;
  provider: string;
  model: string;
  workspace: string;
  sessionId: string;
  isRunning: boolean;
  profileConfigured: boolean | null;
  configuredProfiles: ProfileSummary[];
  isSwitchingModel: boolean;
  slashCommands: SlashCommand[];
  slashSelectedIndex: number;
}

const uiState: UiState = {
  connection: "disconnected",
  provider: "",
  model: "",
  workspace: "",
  sessionId: "",
  isRunning: false,
  profileConfigured: null,
  configuredProfiles: [],
  isSwitchingModel: false,
  slashCommands: [],
  slashSelectedIndex: 0,
};

const DEFAULT_WORKSPACE = "voidx";
const PENDING_MODEL_LABEL = "等待模型状态";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 210;
const MAX_SIDEBAR_WIDTH = 420;

let socket: ReturnType<typeof createWorkerSocket> | null = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 10;
let startupSettingsRequested = false;
let connectionGeneration = 0;

setTranscriptElement(transcriptEl);
initDock();
initTerminal();
initModelControls();
initIntegrationsPanel();
initSettingsModal({
  onSave: async (patch: Record<string, unknown>) => {
    const result = await rpcCall("settings.update", { patch });
    const settings = (result as { settings?: SettingsSnapshot } | undefined)?.settings;
    if (settings) {
      applySettingsRuntimeState(settings);
    }
    return result;
  },
});
initContextMenu();
registerNotificationHandlers();
syncEmptyState();
initWorkspaceControls();
initSidebarResizer();

onTerminalStart(() => {
  rpcCall("terminal.start", { command: ["bash"] })
    .then((result: unknown) => {
      setActiveTerminal(
        (result as Record<string, string>).terminal_id,
      );
    })
    .catch((err: Error) => {
      console.warn("voidx: terminal start failed", err.message);
    });
});

onTerminalInput((terminalId: string, data: string) => {
  rpcCall("terminal.input", {
    terminal_id: terminalId,
    data: data + "\n",
  }).catch(() => {});
});

onHunkDecision(
  (
    reviewId: string,
    filePath: string,
    hunkIndex: number,
    decision: string,
  ) => {
    rpcCall("diff.decide", {
      review_id: reviewId,
      file_path: filePath,
      hunk_index: hunkIndex,
      decision,
    })
      .then((result: unknown) => {
        setHunkDecision(
          filePath,
          hunkIndex,
          decision,
          (result as Record<string, unknown>).summary as never,
        );
      })
      .catch((err: Error) => {
        console.warn("voidx: diff decide failed", err.message);
      });
  },
);

onApplyDiff((reviewId: string) => {
  rpcCall("diff.apply", { review_id: reviewId })
    .then((result: unknown) => {
      console.log(
        "voidx: diff applied",
        (result as Record<string, unknown>).files_changed,
      );
    })
    .catch((err: Error) => {
      console.warn("voidx: diff apply failed", err.message);
    });
});

onGenerateDiff(() => {
  rpcCall("diff.generate", {})
    .then((genResult: unknown) => {
      const diffText =
        ((genResult as Record<string, unknown>).diff as string) || "";
      if (!diffText) {
        showDiffEmpty();
        return;
      }
      return rpcCall("diff.review", { diff: diffText }).then(
        (reviewResult: unknown) => {
          const rr = reviewResult as Record<string, string>;
          renderDiffReview(rr.review_id, rr.snapshot as never);
        },
      );
    })
    .catch((err: Error) => {
      console.warn("voidx: diff generate failed", err.message);
      showDiffEmpty();
    });
});

showDiffEmpty();

function openIntegrations(): void {
  void openIntegrationsPanel(
    rpcCall("integrations.get", {}) as Promise<IntegrationsSnapshot>,
  );
}

function initWorkspaceControls(): void {
  document
    .querySelector("#btn-open-workspace")
    ?.addEventListener("click", () => {
      void openWorkspacePicker();
    });
}

function setSidebarWidth(width: number): void {
  const clamped = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, Math.round(width)));
  const shell = document.querySelector<HTMLElement>(".vx-workbench-shell");
  (shell || document.documentElement).style.setProperty("--vx-sidebar-width", `${clamped}px`);
}

function initSidebarResizer(): void {
  const resizer = document.querySelector<HTMLElement>("#sidebar-resizer");
  if (!resizer || resizer.dataset.initialized === "true") return;
  resizer.dataset.initialized = "true";

  resizer.addEventListener("pointerdown", (event: PointerEvent) => {
    event.preventDefault();
    setSidebarWidth(event.clientX);
    resizer.classList.add("dragging");
    resizer.setPointerCapture?.(event.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      setSidebarWidth(moveEvent.clientX);
    };
    const onPointerUp = (upEvent: PointerEvent) => {
      resizer.classList.remove("dragging");
      resizer.releasePointerCapture?.(upEvent.pointerId);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  });
}

function isDesktopRuntime(): boolean {
  const win = window as unknown as Record<string, unknown>;
  return Boolean(win.__TAURI_INTERNALS__ || win.__TAURI__);
}

async function openWorkspacePicker(): Promise<void> {
  if (!isDesktopRuntime()) {
    return;
  }
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    title: "选择项目文件夹",
  });
  if (typeof selected !== "string" || !selected) {
    return;
  }
  await switchWorkspace(selected);
}

async function switchWorkspace(workspace: string): Promise<void> {
  connectionGeneration += 1;
  uiState.workspace = workspace;
  uiState.sessionId = "";
  uiState.isRunning = false;
  transcriptEl.replaceChildren();
  syncEmptyState();
  setConnectionStatus("connecting");
  updateStatusBar();

  if (socket) {
    socket.close();
    _setSocket(null);
  }

  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("restart_backend", { workspace });
  const url = await resolveWsUrl();
  if (url) {
    connect(url);
  }
}

onThreadSelect((threadId: string) => {
  rpcCall("session.switch", { thread_id: threadId })
    .then((result: unknown) => {
      uiState.sessionId =
        ((result as Record<string, string>).active_thread_id as string) ||
        threadId;
      updateStatusBar();
    })
    .catch((err: Error) => {
      console.warn("voidx: session switch failed", err.message);
    });
});

onNewThread((directory: string) => {
  const existing = findReusableEmptyThread(directory || uiState.workspace);
  if (existing) {
    rpcCall("session.switch", { thread_id: existing.thread_id })
      .then((result: unknown) => {
        uiState.sessionId =
          ((result as Record<string, string>).active_thread_id as string) ||
          existing.thread_id;
        addThread(existing, uiState.sessionId);
        updateStatusBar();
      })
      .catch((err: Error) => {
        console.warn("voidx: session switch failed", err.message);
      });
    return;
  }

  rpcCall("session.create", { directory })
    .then((result: unknown) => {
      const r = result as Record<string, string>;
      uiState.sessionId = r.thread_id;
      addThread(
        {
          thread_id: r.thread_id,
          title: r.title,
          status: r.status,
          workspace: r.workspace || r.directory || directory || uiState.workspace,
          directory: r.directory,
        },
        r.thread_id,
      );
      updateStatusBar();
    })
    .catch((err: Error) => {
      console.warn("voidx: session create failed", err.message);
    });
});

onThreadDelete((threadId: string) => {
  rpcCall("session.delete", { thread_id: threadId })
    .then(() => {
      const item = document.querySelector(
        `.vx-session-item[data-thread-id="${threadId}"]`,
      );
      if (item) item.remove();
    })
    .catch((err: Error) => {
      console.warn("voidx: session delete failed", err.message);
    });
});

onThreadRename((threadId: string) => {
  const item = document.querySelector(
    `.vx-session-item[data-thread-id="${threadId}"]`,
  );
  const titleEl = item?.querySelector(".vx-session-title");
  const oldTitle = titleEl?.textContent || "";
  const newTitle = window.prompt("Rename session:", oldTitle);
  if (!newTitle || newTitle === oldTitle) return;
  rpcCall("session.rename", { thread_id: threadId, title: newTitle })
    .then(() => {
      if (titleEl) titleEl.textContent = newTitle;
    })
    .catch((err: Error) => {
      console.warn("voidx: session rename failed", err.message);
    });
});

const searchEl = document.querySelector<HTMLInputElement>("#session-search");
if (searchEl) {
  searchEl.addEventListener("input", () => {
    filterSessions(searchEl.value);
  });
}

if (!import.meta.env.TEST) {
  bootstrap().catch((error: unknown) => {
    setConnectionStatus(
      "error",
      error instanceof Error ? error.message : String(error),
    );
  });
}

async function bootstrap(): Promise<void> {
  const wsUrl = await resolveWsUrl();
  if (!wsUrl) {
    setConnectionStatus(
      "disconnected",
      "Add ?ws=ws://127.0.0.1:<port>/?token=[redacted] to connect.",
    );
    return;
  }
  connect(wsUrl);
}

export async function resolveWsUrl(): Promise<string | null> {
  const params = new URLSearchParams(window.location.search);
  const direct = params.get("ws");
  if (direct) {
    return direct;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const url: unknown = await invoke("get_gateway_url");
      if (typeof url === "string" && url) {
        return url;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  } catch {
    return null;
  }
  return null;
}

function connect(url: string): void {
  const generation = connectionGeneration;
  setConnectionStatus("connecting");
  socket = createWorkerSocket(url);
  let reconnecting = false;
  const scheduleReconnect = () => {
    if (generation !== connectionGeneration) {
      return;
    }
    if (reconnecting) {
      return;
    }
    reconnecting = true;
    setRunning(false);
    if (reconnectAttempts < MAX_RECONNECT) {
      reconnectAttempts += 1;
      setTimeout(() => connect(url), 5000);
    }
  };
  socket.addEventListener("open", () => {
    reconnectAttempts = 0;
    setConnectionStatus("connected");
  });
  _setSocket(socket);
  socket.addEventListener("close", () => {
    if (generation !== connectionGeneration) {
      return;
    }
    setConnectionStatus("disconnected");
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    if (generation !== connectionGeneration) {
      return;
    }
    setConnectionStatus("disconnected", "Connection error");
  });
}

function registerNotificationHandlers(): void {
  for (const method of [
    "workspace.snapshot",
    "ui.request",
    "startup.shown",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "terminal.output",
    "capture.started",
    "capture.stopped",
    "refresh.requested",
    "reset.requested",
    "notice.set",
    "input.set",
    "item.started",
    "item.delta",
    "item.completed",
  ]) {
    onNotification(method, (params) => handleNotification(method, params));
  }
}

export function handleNotification(
  method: string,
  params: Record<string, unknown> = {},
): void {
  if (method === "workspace.snapshot") {
    const snapshot = params.active_snapshot || { nodes: [] };
    uiState.sessionId = (params.active_thread_id as string) || "";
    applyRuntimeState(params);
    requestStartupSettingsIfNeeded();
    updateStatusBar();
    renderSidebar(
      (params.threads as unknown as ThreadInfo[]) || [],
      (params.active_thread_id as string) || "",
      workspaceBasename(uiState.workspace),
      uiState.workspace,
    );
    renderTranscript(transcriptEl, snapshot as TranscriptSnapshot);
    syncEmptyState();
    scrollToBottom();
    return;
  }
  if (method === "ui.request") {
    showRequest(params);
    return;
  }
  if (method === "startup.shown") {
    applyStartupState(params);
    return;
  }
  if (method === "turn.started") {
    setRunning(true);
    return;
  }
  if (
    method === "turn.completed" ||
    method === "turn.failed" ||
    method === "turn.cancelled"
  ) {
    setRunning(false);
    if (method === "turn.failed") {
      const message = typeof params.message === "string" ? params.message : "";
      if (message) {
        appendMessageItem(`turn-error-${Date.now()}`, {
          style: "error",
          text: message,
        });
        syncEmptyState();
        scrollToBottom();
      }
    }
    return;
  }
  if (method === "terminal.output") {
    appendTerminalOutput(
      params.terminal_id as string,
      params.data as string,
    );
    const dock = document.querySelector("#dock");
    if (dock?.classList.contains("collapsed")) {
      dock.classList.remove("collapsed");
      const strip = document.querySelector<HTMLElement>("#dock-strip");
      if (strip) strip.setAttribute("hidden", "");
    }
    switchTab("terminal");
    return;
  }
  if (method === "capture.started" || method === "capture.stopped") {
    return;
  }
  if (method === "refresh.requested" || method === "reset.requested") {
    return;
  }
  if (method === "notice.set") {
    return;
  }
  if (method === "input.set") {
    if (params.text) {
      inputEl.value = params.text as string;
    }
    return;
  }
  if (
    method === "item.started" ||
    method === "item.delta" ||
    method === "item.completed"
  ) {
    handleItem(method, params);
    syncEmptyState();
  }
}

export function handleItem(
  method: string,
  params: Record<string, unknown>,
): void {
  const kind = params.kind as string;
  const itemId = params.item_id as string;
  const data = (params.data as Record<string, unknown>) || {};

  if (kind === "assistant_stream") {
    if (method === "item.started") {
      appendStreamText(itemId, "", (data.phase as string) || "text");
    } else if (method === "item.delta") {
      appendStreamText(
        itemId,
        (data.text as string) || "",
        (data.phase as string) || "text",
      );
    } else if (method === "item.completed") {
      commitStream(itemId);
      setRunning(false);
    }
    return;
  }
  if (kind === "tool") {
    if (method === "item.started") {
      setRunning(true);
    }
    handleToolItem(method, itemId, data);
    return;
  }
  if (kind === "todo") {
    if (method === "item.started") {
      renderTodoInDock(
        (data.items as Array<{ status: string; content: string }>) || [],
        data.summary as string,
      );
    } else if (method === "item.completed") {
      if (data.cleared) {
        renderTodoInDock([], "");
      } else {
        const todoPanel = document.querySelector<HTMLElement>("#todo-panel");
        const items = Array.from(
          todoPanel?.querySelectorAll<HTMLElement>(".todo-item") || [],
        ).map((item) => ({
          content: item.querySelector("span:last-child")?.textContent || "",
          status: item.classList.contains("done") ? "done" : "done",
        }));
        renderTodoInDock(
          items,
          typeof data.summary === "string" ? data.summary : "",
        );
      }
    }
    return;
  }
  if (kind === "prompt") {
    if (method === "item.started") {
      showPromptItemRequest(data);
    } else if (method === "item.completed" && data.cleared) {
      requestDialogEl.close();
    }
    return;
  }
  if (kind === "status") {
    handleStatusItem(method, itemId, data);
    return;
  }
  if (kind === "subagent") {
    return;
  }
  if (kind === "message") {
    if (method === "item.started") {
      const style = (data.style as string) || "text";
      if (style === "thought") {
        appendThoughtItem(itemId, {
          text: data.text as string,
          meta: data.elapsed
            ? `Thinking for ${data.elapsed}s`
            : "Thinking",
        });
      } else if (style === "error" || style === "warning") {
        appendNoticeItem(itemId, {
          style,
          text: data.text as string,
        });
      } else if (style === "diff") {
        appendDiffItem(itemId, {
          text: data.text as string,
          title: data.title as string,
        });
      } else {
        appendMessageItem(itemId, data);
      }
    }
    return;
  }
}

function setRunning(running: boolean): void {
  uiState.isRunning = running;
  btnSendEl.classList.toggle("running", running);
  btnSendEl.textContent = running ? "■" : "↑";
  btnSendEl.setAttribute("aria-label", running ? "Cancel" : "Send");
  inputEl.disabled = uiState.isSwitchingModel;
  updateStatusBar();
}

function setConnectionStatus(status: string, message?: string): void {
  uiState.connection = status;
  statusDotEl.className = `status-dot ${status}`;
  if (statusModelEl && message && status === "disconnected") {
    statusModelEl.textContent = message;
  } else if (statusModelEl && status === "connected") {
    statusModelEl.textContent = "";
  }
  updateStatusBar();
}

function updateStatusBar(): void {
  const workspaceName = workspaceBasename(uiState.workspace);
  const modelLabel = providerModelLabel();
  if (statusModelEl && uiState.model) {
    statusModelEl.textContent = modelLabel;
  }
  if (workspaceName) {
    if (statusWorkspaceEl) statusWorkspaceEl.textContent = workspaceName;
    if (titlebarProjectEl) titlebarProjectEl.textContent = workspaceName;
    contextWorkspaceEl.textContent = workspaceName;
    stripWorkspaceEl.textContent = workspaceName;
    statusWorkspaceDetailEl.textContent = workspaceName;
  }
  if (uiState.sessionId) {
    const sessionLabel = `session ${uiState.sessionId.slice(0, 8)}`;
    statusSessionEl.textContent = sessionLabel;
    statusSessionDetailEl.textContent = sessionLabel;
  }
  if (contextPermissionEl) contextPermissionEl.textContent = "";
  if (stripPermissionEl) stripPermissionEl.textContent = "";
  if (statusPermissionEl) statusPermissionEl.textContent = "";
  contextProviderModelEl.textContent = modelLabel;
  stripProviderModelEl.textContent = modelLabel;
  statusProviderModelEl.textContent = modelLabel;
  statusConnectionEl.textContent = uiState.connection;
  statusRunningEl.textContent = uiState.isRunning
    ? "running"
    : uiState.isSwitchingModel
      ? "switching"
      : "idle";
}

function scrollToBottom(): void {
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

composerEl.addEventListener("submit", (event: SubmitEvent) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (
    !text ||
    uiState.isSwitchingModel ||
    !isRpcConnected()
  ) {
    return;
  }
  if (uiState.isRunning) {
    btnSendEl.classList.add("guidance-pending");
    btnSendEl.textContent = "◌";
    rpcCall("session.submit", { text, thread_id: uiState.sessionId })
      .then(() => {
        inputEl.value = "";
        hideSlashMenu();
      })
      .catch(() => {})
      .finally(() => {
        btnSendEl.classList.remove("guidance-pending");
        btnSendEl.textContent = "■";
      });
    return;
  }
  setRunning(true);
  rpcCall("session.submit", { text, thread_id: uiState.sessionId }).catch(() => setRunning(false));
  appendMessageItem(`user-${Date.now()}`, { style: "text", text });
  syncEmptyState();
  inputEl.value = "";
  hideSlashMenu();
});

export function initModelControls(): void {
  if (
    !providerSelectEl ||
    !modelSelectEl ||
    providerSelectEl.dataset.initialized === "true"
  ) {
    populateModelControls();
    return;
  }
  providerSelectEl.dataset.initialized = "true";
  populateModelControls();
  providerSelectEl.addEventListener("change", () => {
    uiState.provider = providerSelectEl.value;
    populateModelOptions(uiState.provider, "");
    updateStatusBar();
  });
  modelSelectEl.addEventListener("change", () => {
    const provider = providerSelectEl.value || uiState.provider;
    const model = modelSelectEl.value;
    if (!model || uiState.isSwitchingModel) {
      populateModelOptions(uiState.provider, uiState.model);
      return;
    }
    uiState.isSwitchingModel = true;
    inputEl.disabled = true;
    btnSendEl.disabled = true;
    updateStatusBar();
    rpcCall("session.submit", {
      text: `/model switch ${provider}/${model} --local`,
      thread_id: uiState.sessionId,
    })
      .then(() => {
        uiState.provider = provider;
        uiState.model = model;
      })
      .finally(() => {
        setTimeout(() => {
          uiState.isSwitchingModel = false;
          inputEl.disabled = false;
          btnSendEl.disabled = uiState.isRunning;
          updateStatusBar();
        }, 500);
      })
      .catch(() => {
        populateModelOptions(uiState.provider, uiState.model);
      });
  });
}

function populateModelControls(): void {
  if (!providerSelectEl || !modelSelectEl) return;
  providerSelectEl.replaceChildren();
  const providers = new Set<string>(
    uiState.configuredProfiles
      .filter((profile) => profile.provider)
      .map((profile) => profile.provider),
  );
  if (uiState.provider && !providers.has(uiState.provider)) {
    providers.add(uiState.provider);
  }
  if (providers.size === 0) {
    const pendingOption = document.createElement("option");
    pendingOption.value = "";
    pendingOption.textContent = PENDING_MODEL_LABEL;
    providerSelectEl.append(pendingOption);
  }
  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = provider;
    providerSelectEl.append(option);
  }
  providerSelectEl.value = uiState.provider || "";
  populateModelOptions(providerSelectEl.value, uiState.model);
}

function populateModelOptions(
  provider: string,
  selectedModel: string,
): void {
  if (!modelSelectEl) return;
  const models = uiState.configuredProfiles
    .filter((profile) => profile.provider === provider && profile.model)
    .map((profile) => profile.model);
  if (selectedModel && !models.includes(selectedModel)) {
    models.push(selectedModel);
  }
  if (provider === uiState.provider && models.length === 0 && uiState.model) {
    models.push(uiState.model);
  }
  modelSelectEl.replaceChildren();
  if (!provider || models.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = PENDING_MODEL_LABEL;
    modelSelectEl.append(option);
    modelSelectEl.value = "";
    return;
  }
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    modelSelectEl.append(option);
  }
  if (selectedModel && models.includes(selectedModel)) {
    modelSelectEl.value = selectedModel;
  }
}

function applyStartupState(params: Record<string, unknown>): void {
  applyRuntimeState(params);
}

function requestStartupSettingsIfNeeded(): void {
  if (startupSettingsRequested || uiState.configuredProfiles.length > 0) {
    return;
  }
  startupSettingsRequested = true;
  rpcCall("settings.get", {})
    .then((snapshot) => {
      applySettingsRuntimeState(snapshot as SettingsSnapshot);
    })
    .catch((error: Error) => {
      console.warn("voidx: startup settings fallback failed", error.message);
    });
}

function applySettingsRuntimeState(snapshot: SettingsSnapshot): void {
  uiState.configuredProfiles = configuredProfilesFromSnapshot(snapshot);
  const model = (snapshot.model || {}) as Record<string, unknown>;
  const provider = typeof model.provider === "string" ? model.provider : "";
  const modelName = typeof model.model === "string" ? model.model : "";
  if (!provider && !modelName) {
    return;
  }

  applyRuntimeState({
    provider,
    model: modelName,
    profile_configured: resolveProfileConfigured(snapshot, provider, modelName),
  });
}

function configuredProfilesFromSnapshot(snapshot: SettingsSnapshot): ProfileSummary[] {
  return (snapshot.profiles || []).filter(
    (profile) => profile.configured === true && profile.provider && profile.model,
  );
}

function resolveProfileConfigured(
  snapshot: SettingsSnapshot,
  provider: string,
  model: string,
): boolean | undefined {
  const profiles = snapshot.profiles || [];
  const matchingProfile = profiles.find(
    (profile) => profile.provider === provider && profile.model === model,
  );
  if (matchingProfile) {
    return Boolean(matchingProfile.configured);
  }
  if (profiles.length > 0) {
    return profiles.some((profile) => Boolean(profile.configured));
  }
  return undefined;
}

function applyRuntimeState(params: Record<string, unknown>): void {
  const provider = typeof params.provider === "string" ? params.provider : "";
  const model = typeof params.model === "string" ? params.model : "";
  const hasProviderModel = Boolean(provider || model);
  if (hasProviderModel) {
    const parsed = parseProviderModel(provider, model);
    uiState.provider = parsed.provider;
    uiState.model = parsed.model;
  }
  if (typeof params.workspace === "string" && params.workspace) {
    uiState.workspace = params.workspace;
  }
  if (typeof params.profile_configured === "boolean") {
    uiState.profileConfigured = params.profile_configured;
  }
  if (hasProviderModel) {
    populateModelControls();
  }
  updateStatusBar();
}

function parseProviderModel(
  provider: string,
  model: string,
): { provider: string; model: string } {
  if (provider && model) {
    return { provider, model };
  }
  if (!provider && typeof model === "string" && model.includes("/")) {
    const [parsedProvider, ...rest] = model.split("/");
    return {
      provider: parsedProvider || "custom",
      model: rest.join("/") || model,
    };
  }
  return { provider: provider || "custom", model: model || "" };
}

function providerModelLabel(): string {
  if (!uiState.provider || !uiState.model) return PENDING_MODEL_LABEL;
  return `${uiState.provider || "custom"}/${uiState.model}`;
}

function workspaceBasename(workspace: string): string {
  return workspace
    ? workspace.replace(/^.*[\\/]/, "")
    : DEFAULT_WORKSPACE;
}

function syncEmptyState(): void {
  if (!emptyStateEl || !transcriptEl) return;
  const isEmpty = transcriptEl.children.length === 0;
  emptyStateEl.hidden = !isEmpty;
  mainCanvasEl?.classList.toggle("empty", isEmpty);
}

export function _resetWorkbenchForTest(): void {
  uiState.connection = "disconnected";
  uiState.provider = "";
  uiState.model = "";
  uiState.workspace = "";
  uiState.sessionId = "";
  uiState.isRunning = false;
  uiState.profileConfigured = null;
  uiState.configuredProfiles = [];
  uiState.isSwitchingModel = false;
  startupSettingsRequested = false;
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
  const shell = document.querySelector<HTMLElement>(".vx-workbench-shell");
  if (shell) {
    shell.style.setProperty("--vx-sidebar-width", `${DEFAULT_SIDEBAR_WIDTH}px`);
  } else {
    document.documentElement.style.setProperty("--vx-sidebar-width", `${DEFAULT_SIDEBAR_WIDTH}px`);
  }
  if (providerSelectEl) providerSelectEl.dataset.initialized = "";
  const resizer = document.querySelector<HTMLElement>("#sidebar-resizer");
  if (resizer) {
    resizer.classList.remove("dragging");
    resizer.dataset.initialized = "";
  }
  initSidebarResizer();
  populateModelControls();
  updateStatusBar();
  syncEmptyState();
}

btnSendEl.addEventListener("click", (e: MouseEvent) => {
  if (uiState.isRunning) {
    e.preventDefault();
    if (!isRpcConnected()) {
      return;
    }
    rpcCall("session.cancel", { thread_id: uiState.sessionId })
      .then(() => setRunning(false))
      .catch(() => setRunning(false));
  }
});

document
  .querySelector("#btn-integrations")
  ?.addEventListener("click", () => {
    openIntegrations();
  });

document.querySelector("#btn-settings")?.addEventListener("click", () => {
  void openSettingsModal(rpcCall("settings.get", {}) as Promise<SettingsSnapshot>);
});

// slash command open-ui dispatcher
window.addEventListener("voidx:open-ui", (event: Event) => {
  const detail = (event as CustomEvent<{ target: string }>).detail;
  const target = detail?.target || "";
  if (target.startsWith("settings:")) {
    void openSettingsModal(rpcCall("settings.get", {}) as Promise<SettingsSnapshot>);
  } else if (target.startsWith("integrations:")) {
    void openIntegrationsPanel(rpcCall("integrations.get", {}) as Promise<IntegrationsSnapshot>);
  }
});

inputEl.addEventListener("keydown", (event: KeyboardEvent) => {
  if (uiState.slashCommands.length > 0) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      uiState.slashSelectedIndex =
        (uiState.slashSelectedIndex + 1) % uiState.slashCommands.length;
      updateSlashMenu();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      uiState.slashSelectedIndex =
        (uiState.slashSelectedIndex - 1 + uiState.slashCommands.length) %
        uiState.slashCommands.length;
      updateSlashMenu();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const selected = uiState.slashCommands[uiState.slashSelectedIndex];
      if (selected) {
        runSlashCommand(selected);
      }
      hideSlashMenu();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      hideSlashMenu();
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

inputEl.addEventListener("input", () => {
  const value = inputEl.value;
  if (value.startsWith("/")) {
    const matched = matchSlashCommands(value);
    if (matched.length > 0) {
      uiState.slashCommands = matched;
      uiState.slashSelectedIndex = 0;
      showSlashMenu();
      return;
    }
  }
  hideSlashMenu();
});

function showSlashMenu(): void {
  updateSlashMenu();
  slashMenuEl.classList.add("visible");
}

function hideSlashMenu(): void {
  slashMenuEl.classList.remove("visible");
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
}

function updateSlashMenu(): void {
  const menu = renderSlashMenu(
    uiState.slashCommands,
    uiState.slashSelectedIndex,
    (command: SlashCommand) => {
      runSlashCommand(command);
      hideSlashMenu();
    },
  );
  slashMenuEl.replaceChildren(...menu.childNodes);
}

function runSlashCommand(command: SlashCommand): void {
  if (!command) return;
  if (command.execution === "open-ui") {
    inputEl.value = "";
    window.dispatchEvent(
      new CustomEvent("voidx:open-ui", {
        detail: { target: command.uiTarget, command },
      }),
    );
    return;
  }
  if (command.execution === "run" && !command.requiresArgs) {
    const confirmed =
      !command.dangerous ||
      window.confirm(`Run ${command.command}?`);
    if (!confirmed) return;
    rpcCall("commands.run", {
      text: command.command,
      confirmed,
    }).catch(() => {});
    inputEl.value = "";
    return;
  }
  inputEl.value = command.command + " ";
  inputEl.focus();
}

interface UiRequest {
  prompt: string;
  kind: string;
  request_id: string;
  thread_id?: string;
  tools?: { name: string; pattern?: string; args?: Record<string, unknown> }[];
  choices?: [string, string, string][];
  default?: string;
  secret?: boolean;
  response_method?: string;
}

function showRequest(request: Record<string, unknown>): void {
  const req = request as unknown as UiRequest;
  requestDialogEl.dataset.responseMethod = req.response_method || "";
  requestDialogEl.dataset.responseThreadId = req.thread_id || "";
  requestTitleEl.textContent = req.prompt;
  requestDetailsEl.replaceChildren();
  requestControlsEl.replaceChildren();

  if (req.kind === "permission") {
    renderPermissionDetails(req);
    renderChoiceButtons(req);
  } else if (req.kind === "choice") {
    requestDetailsEl.className = "";
    renderChoiceButtons(req);
  } else if (req.kind === "text") {
    requestDetailsEl.className = "";
    renderTextRequest(req);
  }

  requestDialogEl.showModal();
}

function showPromptItemRequest(data: Record<string, unknown>): void {
  const promptType = data.prompt_type as string;
  if (promptType === "permission") {
    if (!data.request_id || data.interactive === false) {
      return;
    }
    showRequest({
      kind: "permission",
      request_id: (data.request_id as string) || "permission",
      thread_id: (data.thread_id as string) || "",
      prompt: (data.prompt as string) || "Allow action?",
      choices: data.choices || [],
      tools: data.tools || [],
      response_method: "session.respond",
    });
    return;
  }
  if (promptType === "clarify") {
    const options = ((data.options as string[]) || []).map((option) => [
      option,
      option,
      option,
    ]);
    showRequest({
      kind: "choice",
      request_id: (data.clarify_id as string) || (data.request_id as string) || "clarify",
      thread_id: (data.thread_id as string) || "",
      prompt: (data.question as string) || "Clarify",
      choices: options,
      response_method: "session.respond",
    });
    return;
  }
  if (promptType === "checkpoint") {
    const plan = (data.plan as Record<string, unknown>) || {};
    const choices = ((data.choices as Array<Record<string, unknown>>) || []).map((choice) => [
      (choice.label as string) || (choice.value as string) || "",
      (choice.value as string) || (choice.label as string) || "",
      (choice.description as string) || (choice.label as string) || (choice.value as string) || "",
    ]);
    showRequest({
      kind: "choice",
      request_id: (data.checkpoint_id as string) || (data.request_id as string) || "checkpoint",
      thread_id: (data.thread_id as string) || "",
      prompt: (plan.plan_summary as string) || "Review plan",
      choices,
      response_method: "session.respond",
    });
  }
}

function renderPermissionDetails(request: UiRequest): void {
  requestDetailsEl.className = "request-details";
  if (!request.tools?.length) {
    requestDetailsEl.textContent = "";
    return;
  }
  requestDetailsEl.textContent = request.tools
    .map(
      (tool) =>
        `${tool.name} ${tool.pattern || ""}\n${JSON.stringify(tool.args || {}, null, 2)}`,
    )
    .join("\n\n");
}

function renderChoiceButtons(request: UiRequest): void {
  const actions = document.createElement("div");
  actions.className = "request-actions";
  for (const [label, value, desc] of request.choices || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = desc || label;
    button.addEventListener("click", () =>
      sendResponse(request.request_id, value),
    );
    actions.append(button);
  }
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () =>
    sendResponse(request.request_id, null),
  );
  actions.append(cancel);
  requestControlsEl.append(actions);
}

function renderTextRequest(request: UiRequest): void {
  const input = document.createElement("textarea");
  input.rows = 3;
  input.value = request.default || "";
  input.placeholder = request.secret ? "Input hidden in terminal UI" : "";
  const actions = document.createElement("div");
  actions.className = "request-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.textContent = "Submit";
  submit.addEventListener("click", () =>
    sendResponse(request.request_id, input.value),
  );
  actions.append(submit);
  requestControlsEl.append(input, actions);
  setTimeout(() => input.focus(), 0);
}

function sendResponse(requestId: string, value: unknown): void {
  if (!isRpcConnected()) {
    return;
  }
  const responseMethod = requestDialogEl.dataset.responseMethod || "";
  if (responseMethod) {
    const threadId = requestDialogEl.dataset.responseThreadId || "";
    const params: Record<string, unknown> = { request_id: requestId, value };
    if (threadId) {
      params.thread_id = threadId;
    }
    rpcCall(responseMethod, params).catch(() => {});
    requestDialogEl.dataset.responseMethod = "";
    requestDialogEl.dataset.responseThreadId = "";
    requestDialogEl.close();
    return;
  }
  rpcRespond(requestId, value);
  requestDialogEl.close();
}
