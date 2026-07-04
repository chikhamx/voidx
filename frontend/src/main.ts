/// <reference types="vite/client" />
import {
  renderTranscript,
  appendMessageItem,
  handleToolItem,
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
import { rpcCall, _setSocket } from "./rpc";
import {
  renderSidebar,
  addThread,
  updateThreadStatus,
  filterSessions,
  onThreadSelect,
  onNewThread,
  onThreadFork,
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
import type { SettingsSnapshot } from "./settings";
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
const statusPermissionEl = document.querySelector("#status-permission")!;
const statusRunningEl = document.querySelector("#status-running")!;
const stripWorkspaceEl = document.querySelector("#strip-workspace")!;
const stripPermissionEl = document.querySelector("#strip-permission")!;
const stripProviderModelEl = document.querySelector("#strip-provider-model")!;
const titlebarProjectEl = document.querySelector("#titlebar-project");
const contextWorkspaceEl = document.querySelector("#context-workspace")!;
const contextPermissionEl = document.querySelector("#context-permission")!;
const contextProviderModelEl = document.querySelector("#context-provider-model")!;
const emptyStateEl = document.querySelector<HTMLElement>("#empty-state")!;
const transcriptEl = document.querySelector<HTMLElement>("#transcript")!;
const composerEl = document.querySelector<HTMLFormElement>("#composer")!;
const inputEl = document.querySelector<HTMLTextAreaElement>("#input")!;
const btnSendEl = document.querySelector<HTMLButtonElement>("#btn-send")!;
const btnCancelEl = document.querySelector<HTMLButtonElement>("#btn-cancel")!;
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
  isSwitchingModel: boolean;
  slashCommands: SlashCommand[];
  slashSelectedIndex: number;
}

const uiState: UiState = {
  connection: "disconnected",
  provider: "openai",
  model: "",
  workspace: "",
  sessionId: "",
  isRunning: false,
  profileConfigured: null,
  isSwitchingModel: false,
  slashCommands: [],
  slashSelectedIndex: 0,
};

const MODEL_CATALOG: Record<string, string[]> = {
  openai: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
  anthropic: ["claude-sonnet-4-6", "claude-opus-4-1"],
  deepseek: ["deepseek-chat", "deepseek-reasoner"],
  gemini: ["gemini-3-pro", "gemini-2.5-pro"],
  custom: [],
};

const DEFAULT_WORKSPACE = "voidx";

let socket: WebSocket | null = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 10;

setTranscriptElement(transcriptEl);
initDock();
initTerminal();
initModelControls();
initIntegrationsPanel();
initSettingsModal({
  onSave: (patch: Record<string, unknown>) =>
    rpcCall("settings.update", { patch }),
});
initContextMenu();
syncEmptyState();

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
  rpcCall("session.create", { directory })
    .then((result: unknown) => {
      const r = result as Record<string, string>;
      uiState.sessionId = r.thread_id;
      addThread(
        {
          thread_id: r.thread_id,
          title: r.title,
          status: r.status,
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

onThreadFork((threadId: string) => {
  rpcCall("session.fork", { thread_id: threadId })
    .then((result: unknown) => {
      const r = result as Record<string, string>;
      addThread(
        {
          thread_id: r.thread_id,
          title: r.title,
          status: r.status,
        },
        null,
      );
    })
    .catch((err: Error) => {
      console.warn("voidx: session fork failed", err.message);
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

async function resolveWsUrl(): Promise<string | null> {
  const params = new URLSearchParams(window.location.search);
  const direct = params.get("ws");
  if (direct) {
    return direct;
  }
  if ((window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ || (window as unknown as Record<string, unknown>).__TAURI__) {
    const { invoke } = await import("@tauri-apps/api/core");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const url: unknown = await invoke("get_gateway_url");
      if (typeof url === "string" && url) {
        return url;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  return null;
}

function connect(url: string): void {
  setConnectionStatus("connecting");
  socket = new WebSocket(url);
  let reconnecting = false;
  const scheduleReconnect = () => {
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
    setConnectionStatus("disconnected");
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    setConnectionStatus("disconnected", "Connection error");
  });
  socket.addEventListener("message", (event: MessageEvent) => {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(event.data as string);
    } catch {
      console.warn("voidx: ignoring non-JSON websocket message");
      return;
    }
    if (msg.id != null && !msg.method) return;

    const method = msg.method as string;
    const params = (msg.params as Record<string, unknown>) || {};

    handleNotification(method, params);
  });
}

export function handleNotification(
  method: string,
  params: Record<string, unknown> = {},
): void {
  if (method === "workspace.snapshot") {
    const snapshot = params.active_snapshot || { nodes: [] };
    uiState.sessionId = (params.active_thread_id as string) || "";
    updateStatusBar();
    renderSidebar(
      (params.threads as unknown as ThreadInfo[]) || [],
      (params.active_thread_id as string) || "",
      workspaceBasename(uiState.workspace),
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
      }
    }
    return;
  }
  if (kind === "prompt") {
    if (method === "item.completed" && data.cleared) {
      requestDialogEl.close();
    }
    return;
  }
  if (kind === "status" || kind === "subagent") {
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
  btnCancelEl.disabled = !running;
  btnCancelEl.hidden = !running;
  btnSendEl.disabled = running;
  btnSendEl.hidden = running;
  inputEl.disabled = running || uiState.isSwitchingModel;
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
  const permissionLabel = profileConfiguredLabel();

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
  contextPermissionEl.textContent = permissionLabel;
  stripPermissionEl.textContent = permissionLabel;
  statusPermissionEl.textContent = permissionLabel;
  contextProviderModelEl.textContent = modelLabel;
  stripProviderModelEl.textContent = modelLabel;
  statusProviderModelEl.textContent = modelLabel;
  statusConnectionEl.textContent = uiState.connection;
  statusRunningEl.textContent = uiState.isRunning
    ? "running"
    : uiState.isSwitchingModel
      ? "switching"
      : "idle";
  renderProjectList(workspaceName);
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
    uiState.isRunning ||
    !socket ||
    socket.readyState !== WebSocket.OPEN
  ) {
    return;
  }
  rpcCall("session.submit", { text }).catch(() => {});
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
    uiState.provider = providerSelectEl.value || "custom";
    populateModelOptions(uiState.provider, "");
    updateStatusBar();
  });
  modelSelectEl.addEventListener("change", () => {
    const provider = providerSelectEl.value || uiState.provider || "custom";
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
      text: `/model switch ${provider}/${model}`,
    })
      .finally(() => {
        setTimeout(() => {
          uiState.isSwitchingModel = false;
          inputEl.disabled = uiState.isRunning;
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
  for (const provider of Object.keys(MODEL_CATALOG)) {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = provider;
    providerSelectEl.append(option);
  }
  providerSelectEl.value = uiState.provider || "openai";
  populateModelOptions(providerSelectEl.value, uiState.model);
}

function populateModelOptions(
  provider: string,
  selectedModel: string,
): void {
  if (!modelSelectEl) return;
  const models = [...(MODEL_CATALOG[provider] || [])];
  if (selectedModel && !models.includes(selectedModel)) {
    models.push(selectedModel);
  }
  if (models.length === 0 && uiState.model) {
    models.push(uiState.model);
  }
  modelSelectEl.replaceChildren();
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
  const parsed = parseProviderModel(
    params.provider as string,
    params.model as string,
  );
  uiState.provider = parsed.provider;
  uiState.model = parsed.model;
  uiState.workspace = (params.workspace as string) || "";
  uiState.profileConfigured =
    typeof params.profile_configured === "boolean"
      ? params.profile_configured
      : uiState.profileConfigured;
  populateModelControls();
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
  if (!uiState.model) return "";
  return `${uiState.provider || "custom"}/${uiState.model}`;
}

function profileConfiguredLabel(): string {
  if (uiState.profileConfigured === true) return "已配置";
  if (uiState.profileConfigured === false) return "未配置";
  return "完全访问";
}

function workspaceBasename(workspace: string): string {
  return workspace
    ? workspace.replace(/^.*[\\/]/, "")
    : DEFAULT_WORKSPACE;
}

function renderProjectList(activeName: string): void {
  const list = document.querySelector("#project-list");
  if (!list) return;
  const name = activeName || DEFAULT_WORKSPACE;
  let item = [
    ...list.querySelectorAll<HTMLElement>(".vx-project-item"),
  ].find((project) => project.dataset.projectName === name);
  if (!item) {
    item = document.createElement("button");
    (item as HTMLButtonElement).type = "button";
    item.className = "vx-project-item";
    item.dataset.projectName = name;
    item.textContent = name;
    list.append(item);
  }
  for (const project of list.querySelectorAll<HTMLElement>(".vx-project-item")) {
    project.classList.toggle(
      "active",
      project.dataset.projectName === name,
    );
  }
}

function syncEmptyState(): void {
  if (!emptyStateEl || !transcriptEl) return;
  emptyStateEl.hidden = transcriptEl.children.length > 0;
}

export function _resetWorkbenchForTest(): void {
  uiState.connection = "disconnected";
  uiState.provider = "openai";
  uiState.model = "";
  uiState.workspace = "";
  uiState.sessionId = "";
  uiState.isRunning = false;
  uiState.profileConfigured = null;
  uiState.isSwitchingModel = false;
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
  if (providerSelectEl) providerSelectEl.dataset.initialized = "";
  populateModelControls();
  updateStatusBar();
  syncEmptyState();
}

btnCancelEl.addEventListener("click", () => {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  rpcCall("session.cancel", {})
    .then(() => setRunning(false))
    .catch(() => setRunning(false));
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
  tools?: { name: string; pattern?: string; args?: Record<string, unknown> }[];
  choices?: [string, string, string][];
  default?: string;
  secret?: boolean;
}

function showRequest(request: Record<string, unknown>): void {
  const req = request as unknown as UiRequest;
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
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(
    JSON.stringify({
      jsonrpc: "2.0",
      id: requestId,
      result: { value },
    }),
  );
  requestDialogEl.close();
}
