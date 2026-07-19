/// <reference types="vite/client" />
import {
  renderTranscript,
  appendMessageItem,
  handleToolItem,
  handleStatusItem,
  appendThoughtItem,
  appendNoticeItem,
  appendDiffItem,
  setTranscriptElement,
  appendStreamText,
  commitStream,
} from "./utils";
import type { TranscriptSnapshot, SlashCommand } from "./utils";

import {
  rpcCall,
  rpcRespond,
  onNotification,
  _setSocket,
  isRpcConnected,
} from "./rpc";

import {
  isKnownSlashCommand,
  matchSlashCommands,
  renderSlashMenu,
  renderSidebar,
  addThread,
  removeThread,
  findReusableEmptyThread,
  onThreadSelect,
  onNewThread,
  onThreadDelete,
  onThreadRename,
  filterSessions,
  initDock,
  renderTodoInDock,
  switchTab,
  initTerminal,
  appendTerminalOutput,
  onTerminalInput,
  onTerminalStart,
  setActiveTerminal,
  renderDiffReview,
  showDiffEmpty,
  setHunkDecision,
  onHunkDecision,
  onApplyDiff,
  onGenerateDiff,
  initSettingsModal,
  openSettingsModal,
  _resetSettingsForTest,
  initIntegrationsPanel,
  openIntegrationsPanel,
  _resetIntegrationsForTest,
  initContextMenu,
  _resetContextMenuForTest,
  initWorkspaceControls,
  initSidebarResizer,
  openWorkspacePicker,
  showRequest,
  showPromptItemRequest,
  _resetDialogForTest,
  initModelControls,
  initPermissionControls,
  initReasoningControls,
  applySettingsRuntimeState,
  applyRuntimeState,
  initTheme,
} from "./ui";
import type { ThreadInfo, SettingsSnapshot, IntegrationsSnapshot } from "./ui";

import {
  uiState,
  composerEl,
  inputEl,
  btnSendEl,
  slashMenuEl,
  requestDialogEl,
  transcriptEl,
  providerSelectEl,
  modelSelectEl,
  setRunning,
  setConnectionStatus,
  syncEmptyState,
  updateStatusBar,
  workspaceBasename,
  _resetWorkbenchStateForTest,
  DEFAULT_SIDEBAR_WIDTH,
  bootstrap,
  connect,
  resolveWsUrl,
  requestStartupSettingsIfNeeded,
  _resetConnectionForTest,
  incrementConnectionGeneration,
  sendStopIcon,
} from "./services";

// Re-export functions required by test suites
export { initModelControls, resolveWsUrl };

if (typeof window !== "undefined" && ((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__)) {
  document.body.classList.add("is-desktop");
}
setTranscriptElement(transcriptEl);
initTheme();
initDock();
initTerminal();
initModelControls();
initPermissionControls();
initReasoningControls();
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
      removeThread(threadId, uiState.sessionId);
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
    requestStartupSettingsIfNeeded(applySettingsRuntimeState);
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
    applyRuntimeState(params);
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
      const result = commitStream(itemId);
      if (result && result.thinking) {
        const elapsed = typeof data.elapsed === "number" ? data.elapsed : null;
        appendThoughtItem(
          itemId + "-thought",
          {
            text: result.thinking,
            elapsed: elapsed,
          },
          result.el,
        );
      }
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
          status: "done",
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
          meta: (data.meta as string) || null,
          elapsed: (data.elapsed as number) || null,
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
  if (text.startsWith("/") && !isKnownSlashCommand(text)) {
    inputEl.value = "";
    hideSlashMenu();
    return;
  }
  if (uiState.isRunning) {
    btnSendEl.classList.add("guidance-pending");
    btnSendEl.innerHTML = sendStopIcon;
    rpcCall("session.submit", { text, thread_id: uiState.sessionId })
      .then(() => {
        inputEl.value = "";
        hideSlashMenu();
      })
      .catch(() => {})
      .finally(() => {
        btnSendEl.classList.remove("guidance-pending");
        btnSendEl.innerHTML = sendStopIcon;
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

export function _resetWorkbenchForTest(): void {
  _resetWorkbenchStateForTest();
  _resetConnectionForTest();
  _resetDialogForTest();

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
  initModelControls();
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
