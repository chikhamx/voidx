/// <reference types="vite/client" />
import "../css/tokens.css";
import "../css/base.css";
import "../css/layout.css";
import "../css/chat.css";
import "../css/composer.css";
import "../css/components.css";
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
  clearCommittedStreams,
  clearActiveStreams,
  stripRichMarkup,
  snapshotTurnText,
} from "./utils";
import { resetFileChangeCards } from "./utils/render-file-changes";
import type {
  IncrementalStreamState,
  TranscriptSnapshot,
  SlashCommand,
} from "./utils";

import {
  rpcCall,
  rpcNotify,
  onNotification,
  _setSocket,
  onSocketChange,
  _resetForTest as _resetRpcForTest,
  isRpcConnected,
} from "./rpc";

import {
  isKnownSlashCommand,
  matchSlashCommands,
  completeSlashInput,
  setCommandCatalog,
  expandPasteTokens,
  clearPasteEntries,
  registerTextPaste,
  imageAttachmentTokens,
  clearImageAttachments,
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
  openTerminalDrawer,
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
  initProvidersModal,
  initIntegrationsPanel,
  openIntegrationsPanel,
  initContextMenu,
  initWorkspaceControls,
  initSidebarResizer,
  initSidebarToggle,
  _resetWorkspaceForTest,
  _resetNavigationForTest,
  initThreadNavigation,
  recordThreadVisit,
  clearPermissionRequests,
  showRequest,
  showPromptItemRequest,
  showConversationPrompt,
  pendingConversationPrompt,
  beginConversationPromptResponse,
  failConversationPromptResponse,
  resolveConversationPrompt,
  completeConversationPrompt,
  resetConversationPrompts,
  initModelControls,
  initPermissionControls,
  initReasoningControls,
  applySettingsRuntimeState,
  applyRuntimeState,
  initTheme,
  initModeControls,
  refreshModeMenu,
  renderRuntimeProfile,
  openAgentStudio,
  type AgentCatalog,
  type AgentStudioRpc,
  type AgentProfileDiagnostic,
} from "./ui";
import {
  pushHistory,
  historyPrev,
  historyNext,
  resetHistoryNavigation,
  isHistoryBrowsing,
} from "./ui/history";
import type {
  ConversationPrompt,
  ThreadInfo,
  SettingsSnapshot,
  IntegrationsSnapshot,
  RuntimeProfile,
} from "./ui";
import {
  showSlashMenu, hideSlashMenu, updateSlashMenu, runSlashCommand,
  refMenuVisible, hideRefMenu, updateRefMenu,
  scheduleRefUpdate, acceptRefCandidate,
} from "./ui/menus";
import { _resetSettingsForTest } from "./ui/settings";
import { _resetIntegrationsForTest } from "./ui/integrations";
import { _resetContextMenuForTest } from "./ui/context-menu";
import { _resetDialogForTest } from "./ui/dialog";
import { _resetForTest as _resetSidebarForTest } from "./ui/sidebar";
import { _resetModeControlsForTest } from "./ui/mode";
import { _resetHistoryForTest } from "./ui/history";
import { _resetCommandCatalogForTest } from "./ui/slash";

import {
  type UsageSnapshot,
  uiState,
  initStateDom,
  composerEl,
  inputEl,
  btnSendEl,
  requestDialogEl,
  transcriptEl,
  providerSelectEl,
  setRunning,
  setConnectionStatus,
  syncEmptyState,
  updateStatusBar,
  workspaceBasename,
  _resetWorkbenchStateForTest,
  DEFAULT_SIDEBAR_WIDTH,
  bootstrap,
  resolveWsUrl,
  requestStartupSettingsIfNeeded,
  _resetConnectionForTest,
} from "./services";

// Re-export functions required by test suites
export { initModelControls, resolveWsUrl };

if (typeof window !== "undefined" && ((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__)) {
  document.body.classList.add("is-desktop");
  if (/macintosh|mac os x/i.test(navigator.userAgent)) {
    document.body.classList.add("is-mac");
  }
}
initStateDom();
setTranscriptElement(transcriptEl);
initTheme();
initDock();
initTerminal();
interface PendingLocalMessage {
  threadId: string;
  itemId: string;
  text: string;
  style: "text" | "guidance";
}

interface SnapshotUserEntry {
  id: string;
  text: string;
  style: "user" | "guidance";
  rendered: boolean;
}

let pendingLocalMessages: PendingLocalMessage[] = [];
const knownSnapshotUserNodeIds = new Map<string, Set<string>>();
let localItemSequence = 0;

function snapshotUserEntries(snapshot: TranscriptSnapshot): SnapshotUserEntry[] {
  const entries: SnapshotUserEntry[] = [];
  for (const node of snapshot.nodes || []) {
    const payload = node.payload as Record<string, unknown> | undefined;
    if (node.node_type === "turn") {
      const text = snapshotTurnText(node);
      if (text.trim()) {
        const turnStyle = String(payload?.style || "").toLowerCase();
        entries.push({
          id: node.id,
          text: text.trim(),
          style: turnStyle === "guidance" ? "guidance" : "user",
          rendered: true,
        });
      }
      continue;
    }
    if (node.node_type !== "message") continue;
    const style = String(payload?.style || payload?.role || "").toLowerCase();
    if (style !== "user" && style !== "guidance") continue;
    const text = typeof payload?.raw_text === "string"
      ? payload.raw_text
      : stripRichMarkup([node.header || node.title || "", ...(node.body_lines || [])].join("\n"));
    if (text.trim()) {
      entries.push({
        id: node.id,
        text: text.trim(),
        style: style as "user" | "guidance",
        rendered: true,
      });
    }
  }
  return entries;
}

function rememberPendingLocalMessage(
  threadId: string,
  itemId: string,
  text: string,
  style: "text" | "guidance",
): void {
  pendingLocalMessages.push({
    threadId,
    itemId,
    text,
    style,
  });
}

function appendPendingLocalMessage(pending: PendingLocalMessage): void {
  appendMessageItem(pending.itemId, { style: pending.style, text: pending.text });
}

function restorePendingLocalMessages(
  threadId: string,
  snapshot: TranscriptSnapshot,
): void {
  const entries = snapshotUserEntries(snapshot);
  const knownIds = knownSnapshotUserNodeIds.get(threadId) || new Set<string>();
  const freshByText = new Map<string, SnapshotUserEntry[]>();
  for (const entry of entries) {
    if (!knownIds.has(entry.id)) {
      const fresh = freshByText.get(entry.text) || [];
      fresh.push(entry);
      freshByText.set(entry.text, fresh);
    }
    knownIds.add(entry.id);
  }
  knownSnapshotUserNodeIds.set(threadId, knownIds);

  const takeFreshEntry = (
    text: string,
    predicate: (entry: SnapshotUserEntry) => boolean,
  ): SnapshotUserEntry | undefined => {
    const fresh = freshByText.get(text) || [];
    const index = fresh.findIndex(predicate);
    if (index < 0) return undefined;
    const [entry] = fresh.splice(index, 1);
    if (fresh.length === 0) freshByText.delete(text);
    return entry;
  };

  const retained: PendingLocalMessage[] = [];
  for (const pending of pendingLocalMessages) {
    if (pending.threadId !== threadId) {
      retained.push(pending);
      continue;
    }
    const expectedStyle = pending.style === "guidance" ? "guidance" : "user";
    const renderedEntry = takeFreshEntry(
      pending.text,
      (entry) => entry.rendered && entry.style === expectedStyle,
    );
    if (renderedEntry) continue;
    retained.push(pending);
    appendPendingLocalMessage(pending);
  }

  pendingLocalMessages = retained;
}

function forgetPendingLocalMessage(itemId: string): void {
  pendingLocalMessages = pendingLocalMessages.filter((pending) => pending.itemId !== itemId);
}

function removePendingLocalMessage(itemId: string): void {
  forgetPendingLocalMessage(itemId);
  transcriptEl.querySelector<HTMLElement>(`[data-item-id="${itemId}"]`)?.remove();
}

function removePendingLocalMessageElements(): void {
  for (const pending of pendingLocalMessages) {
    transcriptEl.querySelector<HTMLElement>(`[data-item-id="${pending.itemId}"]`)?.remove();
  }
}

function forgetPendingLocalMessages(): void {
  pendingLocalMessages = [];
  knownSnapshotUserNodeIds.clear();
}

function createLocalItemId(prefix: string): string {
  localItemSequence += 1;
  return `${prefix}-${localItemSequence}`;
}

function replacePendingGuidanceWithServerItem(
  threadId: string,
  text: string,
): void {
  const pending = pendingLocalMessages.find(
    (candidate) =>
      candidate.threadId === threadId &&
      candidate.style === "guidance" &&
      candidate.text === text,
  );
  if (pending) {
    removePendingLocalMessage(pending.itemId);
  }
}

function submitModeCommand(command: string): void {
  if (!isRpcConnected() || !uiState.sessionId) return;
  const threadId = uiState.sessionId;
  const contextGeneration = threadContextGeneration;
  rpcCall("session.submit", { text: command, thread_id: threadId })
    .catch((error: Error) => {
      if (!isCurrentSendContext(threadId, contextGeneration)) return;
      if (!inputEl.value) inputEl.value = command;
      appendMessageItem(`mode-error-${Date.now()}`, {
        style: "error",
        text: error.message || `命令失败: ${command}`,
      });
      syncEmptyState();
      scrollToBottom();
    });
}

function handleRuntimeProfileSwitch(profile: RuntimeProfile): void {
  if (uiState.isSwitchingProfile) return;
  if (uiState.sessionId && uiState.runtimeProfile === profile) return;
  void openThreadForProfile(profile);
}

const agentStudioRpc: AgentStudioRpc = {
  getCatalog: () => rpcCall("agent-catalog", {}) as Promise<AgentCatalog>,
  validate: (params) => rpcCall("validate-agent-profile", params) as Promise<{ valid: boolean; diagnostics: AgentProfileDiagnostic[] }>,
  save: (params) => rpcCall("save-agent-profile", params) as Promise<{ diagnostics?: AgentProfileDiagnostic[] }>,
};

function initializeModeControls(): void {
  initModeControls(handleRuntimeProfileSwitch, {
    listProfiles: () => rpcCall("list-agent-profiles", {}) as Promise<{ profiles: import("./ui").AgentProfileInfo[] }>,
    onCreateAgent: () => {
      void openAgentStudio({
        rpc: agentStudioRpc,
        onSaved: (profileName) => {
          void refreshModeMenu().then(() => handleRuntimeProfileSwitch(profileName));
        },
      });
    },
  });
  renderRuntimeProfile(uiState.runtimeProfile);
}

initializeModeControls();

for (const [id, command] of [["mode-status", "status"], ["mode-stop", "stop"]] as const) {
  document.querySelector<HTMLElement>(`#${id}`)?.addEventListener("click", () => {
    submitModeCommand(`/${uiState.runtimeProfile} ${command}`);
  });
}
initModelControls();
initPermissionControls();
initReasoningControls();
initIntegrationsPanel();

async function saveSettings(patch: Record<string, unknown>): Promise<unknown> {
  const result = await rpcCall("settings.update", { patch });
  const settings = (result as { settings?: SettingsSnapshot } | undefined)?.settings;
  if (settings) {
    applySettingsRuntimeState(settings);
  }
  return result;
}

function initializeSettingsModal(): void {
  initSettingsModal({ onSave: saveSettings });
  initProvidersModal({ onSave: saveSettings });
}

initializeSettingsModal();
initContextMenu();
registerNotificationHandlers();
syncEmptyState();
initWorkspaceControls();
initSidebarToggle();
initSidebarResizer();
initThreadNavigation(switchThread);

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

function showSessionError(action: string, error: unknown): void {
  const message = error instanceof Error ? error.message : String(error || "未知错误");
  appendMessageItem(`session-error-${Date.now()}`, {
    style: "error",
    text: `${action}失败：${message}`,
  });
  syncEmptyState();
  scrollToBottom();
}

function submitConversationPromptResponse(
  prompt: ConversationPrompt,
  value: string,
  displayValue: string,
): void {
  const answer = value.trim();
  const visibleAnswer = displayValue.trim();
  if (!answer || !visibleAnswer || !isRpcConnected()) return;
  if (uiState.sessionId !== prompt.threadId) return;
  if (!beginConversationPromptResponse(prompt.requestId)) return;

  const contextGeneration = threadContextGeneration;
  const itemId = createLocalItemId("prompt-answer");
  rememberPendingLocalMessage(prompt.threadId, itemId, visibleAnswer, "text");
  appendMessageItem(itemId, { style: "text", text: visibleAnswer });
  if (inputEl.value.trim() === visibleAnswer) inputEl.value = "";
  syncEmptyState();
  scrollToBottom();

  rpcCall("session.respond", {
    request_id: prompt.requestId,
    thread_id: prompt.threadId,
    value: answer,
  })
    .then((result: unknown) => {
      if ((result as { ok?: boolean } | null)?.ok === false) {
        throw new Error("后端未接受回答");
      }
      if (!isCurrentSendContext(prompt.threadId, contextGeneration)) return;
      resolveConversationPrompt(prompt.requestId);
    })
    .catch((error: Error) => {
      if (!isCurrentSendContext(prompt.threadId, contextGeneration)) return;
      removePendingLocalMessage(itemId);
      failConversationPromptResponse(prompt.requestId);
      if (!inputEl.value) inputEl.value = visibleAnswer;
      showSessionError("回答", error);
    });
}

function openIntegrations(): void {
  void openIntegrationsPanel(
    rpcCall("integrations.get", {}) as Promise<IntegrationsSnapshot>,
  );
}

let threadActivationGeneration = 0;
let threadContextGeneration = 0;
let pendingActivationTarget: string | null = null;
let pendingActivationSnapshot: Record<string, unknown> | null = null;
const staleSnapshotThreadIds = new Set<string>();
const lastSnapshotRevisionByThread = new Map<string, number>();
let lastWorkspaceRevision = 0;
const pendingSnapshotRequests = new Set<string>();
const incrementalStreamStates = new Map<string, IncrementalStreamState>();
const renderedItemIdsByThread = new Map<string, Set<string>>();

onSocketChange(() => {
  lastWorkspaceRevision = 0;
  pendingSnapshotRequests.clear();
  lastSnapshotRevisionByThread.clear();
  incrementalStreamStates.clear();
  renderedItemIdsByThread.clear();
});
interface ThreadTurnContext {
  currentTurnId: string | null;
  retiredTurnIds: Set<string>;
}
const threadTurnContexts = new Map<string, ThreadTurnContext>();

const TRANSCRIPT_PAGE_SIZE = 20;
interface TranscriptWindowState {
  snapshot: TranscriptSnapshot;
  loading: boolean;
}
const transcriptWindows = new Map<string, TranscriptWindowState>();

function snapshotForRendering(threadId: string, snapshot: TranscriptSnapshot): TranscriptSnapshot {
  if (!snapshot.windowed) {
    transcriptWindows.delete(threadId);
    return snapshot;
  }
  transcriptWindows.set(threadId, { snapshot, loading: false });
  return snapshot;
}

function loadEarlierTranscriptPage(): void {
  const threadId = uiState.sessionId;
  if (!threadId || uiState.isSwitchingThread) return;
  const state = transcriptWindows.get(threadId);
  if (
    !state ||
    state.loading ||
    !state.snapshot.has_earlier ||
    typeof state.snapshot.before_turn_id !== "number"
  ) return;

  state.loading = true;
  const contextGeneration = threadContextGeneration;
  const previousHeight = transcriptEl.scrollHeight;
  const previousTop = transcriptEl.scrollTop;
  void rpcCall("transcript.page", {
    thread_id: threadId,
    before_turn_id: state.snapshot.before_turn_id,
    turn_limit: TRANSCRIPT_PAGE_SIZE,
  })
    .then((result: unknown) => {
      if (
        uiState.sessionId !== threadId ||
        uiState.isSwitchingThread ||
        threadContextGeneration !== contextGeneration
      ) return;
      if (!result || typeof result !== "object") return;
      const page = result as TranscriptSnapshot;
      if (!Array.isArray(page.nodes) || (page.thread_id && page.thread_id !== threadId)) return;
      const current = transcriptWindows.get(threadId);
      if (!current || current !== state) return;
      const existingIds = new Set(current.snapshot.nodes.map((node) => node.id));
      const mergedSnapshot: TranscriptSnapshot = {
        ...current.snapshot,
        nodes: [
          ...page.nodes.filter((node) => !existingIds.has(node.id)),
          ...current.snapshot.nodes,
        ],
        revision: page.revision ?? current.snapshot.revision,
        before_turn_id: page.before_turn_id ?? null,
        has_earlier: Boolean(page.has_earlier),
      };
      current.snapshot = mergedSnapshot;
      removePendingLocalMessageElements();
      renderTranscript(transcriptEl, mergedSnapshot);
      restorePendingLocalMessages(threadId, mergedSnapshot);
      syncEmptyState();
      transcriptEl.scrollTop = previousTop + (transcriptEl.scrollHeight - previousHeight);
    })
    .catch((error: unknown) => {
      console.warn("voidx: transcript page failed", error);
    })
    .finally(() => {
      state.loading = false;
    });
}

function handleTranscriptScroll(): void {
  if (transcriptEl.scrollTop <= 24) loadEarlierTranscriptPage();
}

transcriptEl.addEventListener("scroll", handleTranscriptScroll);

function threadTurnContext(threadId: string): ThreadTurnContext {
  let context = threadTurnContexts.get(threadId);
  if (!context) {
    context = { currentTurnId: null, retiredTurnIds: new Set<string>() };
    threadTurnContexts.set(threadId, context);
  }
  return context;
}

function isCurrentThreadIdentity(params: Record<string, unknown>): boolean {
  if (uiState.isSwitchingThread) return false;
  const threadId = params.thread_id;
  if (!uiState.sessionId) {
    return typeof threadId !== "string" || !threadId;
  }
  return typeof threadId === "string" && threadId === uiState.sessionId;
}

function isCurrentUiRequest(params: Record<string, unknown>): boolean {
  const threadId = params.thread_id;
  const isUnscoped = threadId === undefined || threadId === null || threadId === "";
  if (uiState.isSwitchingThread) return isUnscoped;
  if (!uiState.sessionId) return isUnscoped;
  return typeof threadId === "string" && threadId === uiState.sessionId;
}

function retireThreadTurn(threadId: string): void {
  if (!threadId) return;
  const context = threadTurnContext(threadId);
  if (context.currentTurnId) {
    context.retiredTurnIds.add(context.currentTurnId);
    context.currentTurnId = null;
  }
}

function isCurrentThreadEvent(params: Record<string, unknown>): boolean {
  if (!isCurrentThreadIdentity(params)) return false;
  if (!uiState.sessionId) return true;
  const threadId = params.thread_id;
  const turnId = params.turn_id;
  if (typeof threadId !== "string" || !threadId || typeof turnId !== "string" || !turnId) {
    return false;
  }
  const context = threadTurnContext(threadId);
  if (context.retiredTurnIds.has(turnId)) return false;
  return context.currentTurnId === null || context.currentTurnId === turnId;
}

function registerTurnStarted(params: Record<string, unknown>): boolean {
  if (!isCurrentThreadIdentity(params)) return false;
  if (!uiState.sessionId) return true;
  const threadId = params.thread_id;
  const turnId = params.turn_id;
  if (typeof threadId !== "string" || !threadId || typeof turnId !== "string" || !turnId) {
    return false;
  }
  const context = threadTurnContext(threadId);
  if (context.retiredTurnIds.has(turnId)) return false;
  if (context.currentTurnId && context.currentTurnId !== turnId) {
    context.retiredTurnIds.add(context.currentTurnId);
  }
  context.currentTurnId = turnId;
  return true;
}

function retireCompletedTurn(params: Record<string, unknown>): void {
  const threadId = params.thread_id;
  const turnId = params.turn_id;
  if (typeof threadId !== "string" || !threadId || typeof turnId !== "string" || !turnId) return;
  const context = threadTurnContext(threadId);
  if (context.currentTurnId === turnId) {
    context.retiredTurnIds.add(turnId);
    context.currentTurnId = null;
  }
}

function isCurrentSendContext(threadId: string, contextGeneration: number): boolean {
  return !uiState.isSwitchingThread &&
    uiState.sessionId === threadId &&
    threadContextGeneration === contextGeneration;
}

function snapshotRevision(params: Record<string, unknown>): number | null {
  const snapshot = params.active_snapshot;
  if (!snapshot || typeof snapshot !== "object") return null;
  const revision = (snapshot as Record<string, unknown>).revision;
  return typeof revision === "number" && Number.isFinite(revision) ? revision : null;
}

function workspaceRevision(params: Record<string, unknown>): number | null {
  const revision = params.revision;
  return typeof revision === "number" && Number.isInteger(revision) && revision >= 0
    ? revision
    : null;
}

function requestSnapshotRecovery(threadId?: unknown): void {
  const targetThreadId = typeof threadId === "string" && threadId
    ? threadId
    : uiState.sessionId;
  if (!targetThreadId || pendingSnapshotRequests.has(targetThreadId)) return;
  pendingSnapshotRequests.add(targetThreadId);
  rpcNotify("snapshot.requested", { thread_id: targetThreadId });
}

function applyWorkspacePatch(params: Record<string, unknown>): void {
  const revision = workspaceRevision(params);
  const activeThreadId = typeof params.active_thread_id === "string"
    ? params.active_thread_id
    : uiState.sessionId;
  const recoveryThreadId = uiState.sessionId || activeThreadId;
  if (revision === null) {
    requestSnapshotRecovery(recoveryThreadId);
    return;
  }
  if (revision <= lastWorkspaceRevision) return;
  if (revision !== lastWorkspaceRevision + 1) {
    requestSnapshotRecovery(recoveryThreadId);
    return;
  }
  if (
    pendingSnapshotRequests.has(recoveryThreadId) ||
    (activeThreadId && uiState.sessionId && activeThreadId !== uiState.sessionId)
  ) {
    requestSnapshotRecovery(recoveryThreadId);
    return;
  }

  applyRuntimeState(params);
  const threads = Array.isArray(params.threads)
    ? params.threads as Array<Record<string, unknown>>
    : null;
  const currentThreadId = uiState.sessionId || activeThreadId;
  const activeThread = threads?.find(
    (thread) => thread.thread_id === currentThreadId,
  );
  applyThreadStatus(
    activeThread?.status ??
    (activeThreadId === currentThreadId ? params.status : undefined),
  );
  if (threads) {
    renderSidebar(
      threads as unknown as ThreadInfo[],
      currentThreadId || null,
      workspaceBasename(uiState.workspace),
      uiState.workspace,
    );
  }
  lastWorkspaceRevision = revision;
  updateStatusBar();
}

function snapshotStreamIdentity(
  params: Record<string, unknown>,
  data: Record<string, unknown>,
): {
  key: string;
  threadId: string;
  turnId: string;
  itemId: string;
  streamId: string;
} | null {
  const threadId = typeof params.thread_id === "string" ? params.thread_id : "";
  const turnId = typeof params.turn_id === "string" ? params.turn_id : "";
  const itemId = typeof params.item_id === "string" ? params.item_id : "";
  const streamId = typeof data.stream_id === "string" ? data.stream_id : "";
  if (!threadId || !turnId || !itemId || !streamId) return null;
  return {
    key: JSON.stringify([threadId, turnId, itemId, streamId]),
    threadId,
    turnId,
    itemId,
    streamId,
  };
}

function isRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isIncrementalStreamData(data: Record<string, unknown>): boolean {
  return "op" in data || "base_revision" in data || "revision" in data || "stream_id" in data;
}

function startIncrementalStream(
  params: Record<string, unknown>,
  data: Record<string, unknown>,
  itemId: string,
): boolean {
  const identity = snapshotStreamIdentity(params, data);
  const revision = data.revision;
  const text = data.text;
  const phase = typeof data.phase === "string" ? data.phase : "text";
  if (
    !identity ||
    data.op !== "replace" ||
    !isRevision(revision) ||
    typeof text !== "string"
  ) {
    requestSnapshotRecovery(params.thread_id);
    return false;
  }
  const state: IncrementalStreamState = {
    ...identity,
    revision,
    phase,
    text,
  };
  incrementalStreamStates.set(identity.key, state);
  appendStreamText(itemId, text, phase);
  return true;
}

function consumeIncrementalStreamDelta(
  params: Record<string, unknown>,
  data: Record<string, unknown>,
  itemId: string,
): boolean {
  const identity = snapshotStreamIdentity(params, data);
  const op = data.op;
  const baseRevision = data.base_revision;
  const revision = data.revision;
  const text = data.text;
  if (
    !identity ||
    (op !== "append" && op !== "replace") ||
    !isRevision(baseRevision) ||
    !isRevision(revision) ||
    typeof text !== "string"
  ) {
    requestSnapshotRecovery(params.thread_id);
    return false;
  }
  if (pendingSnapshotRequests.has(identity.threadId)) return false;

  const state = incrementalStreamStates.get(identity.key);
  if (!state) {
    requestSnapshotRecovery(identity.threadId);
    return false;
  }
  if (revision <= state.revision) return true;
  if (
    baseRevision !== state.revision ||
    revision !== state.revision + 1
  ) {
    requestSnapshotRecovery(identity.threadId);
    return false;
  }
  const phase = typeof data.phase === "string" ? data.phase : state.phase;
  if (op === "append" && phase !== state.phase) {
    requestSnapshotRecovery(identity.threadId);
    return false;
  }

  const nextText = op === "append" ? state.text + text : text;
  state.revision = revision;
  state.phase = phase;
  state.text = nextText;
  appendStreamText(itemId, nextText, phase);
  return true;
}

function clearIncrementalStreamStatesForThread(threadId: string): void {
  if (!threadId) return;
  for (const [key, state] of incrementalStreamStates) {
    if (state.threadId === threadId) incrementalStreamStates.delete(key);
  }
}

function clearIncrementalStreamItem(params: Record<string, unknown>): void {
  const threadId = typeof params.thread_id === "string" ? params.thread_id : "";
  const turnId = typeof params.turn_id === "string" ? params.turn_id : "";
  const itemId = typeof params.item_id === "string" ? params.item_id : "";
  const data = (params.data as Record<string, unknown>) || {};
  const streamId = typeof data.stream_id === "string" ? data.stream_id : "";
  for (const [key, state] of incrementalStreamStates) {
    if (
      state.threadId === threadId &&
      state.turnId === turnId &&
      state.itemId === itemId &&
      (!streamId || state.streamId === streamId)
    ) {
      incrementalStreamStates.delete(key);
    }
  }
}

function snapshotThreadMatchesActive(params: Record<string, unknown>, activeThreadId: string): boolean {
  if (!activeThreadId) {
    if (uiState.sessionId || pendingActivationTarget !== null) return false;
    const snapshot = params.active_snapshot;
    if (snapshot === undefined || snapshot === null) return true;
    if (typeof snapshot !== "object") return false;
    const snapshotRecord = snapshot as Record<string, unknown>;
    return snapshotRecord.thread_id === "" &&
      typeof snapshotRecord.revision === "number" &&
      Number.isInteger(snapshotRecord.revision) &&
      snapshotRecord.revision >= 0 &&
      Array.isArray(snapshotRecord.nodes);
  }
  const snapshot = params.active_snapshot;
  if (!snapshot || typeof snapshot !== "object") return false;
  const snapshotRecord = snapshot as Record<string, unknown>;
  return snapshotRecord.thread_id === activeThreadId &&
    typeof snapshotRecord.revision === "number" &&
    Number.isInteger(snapshotRecord.revision) &&
    snapshotRecord.revision >= 0;
}

function isSnapshotRevisionFresh(threadId: string, params: Record<string, unknown>): boolean {
  const revision = snapshotRevision(params);
  if (revision === null) return true;
  const lastRevision = lastSnapshotRevisionByThread.get(threadId);
  return lastRevision === undefined || revision >= lastRevision;
}

function rememberSnapshotRevision(threadId: string, params: Record<string, unknown>): void {
  const revision = snapshotRevision(params);
  if (revision === null) return;
  const lastRevision = lastSnapshotRevisionByThread.get(threadId);
  if (lastRevision === undefined || revision > lastRevision) {
    lastSnapshotRevisionByThread.set(threadId, revision);
  }
}

function renderedItemIds(threadId: string): Set<string> {
  let ids = renderedItemIdsByThread.get(threadId);
  if (!ids) {
    ids = new Set<string>();
    renderedItemIdsByThread.set(threadId, ids);
  }
  return ids;
}

function isDuplicateItemStart(method: string, params: Record<string, unknown>): boolean {
  if (method !== "item.started") return false;
  const itemId = params.item_id;
  if (typeof itemId !== "string" || !itemId) return false;
  const threadId = typeof params.thread_id === "string" && params.thread_id
    ? params.thread_id
    : uiState.sessionId;
  if (!threadId) return false;
  const ids = renderedItemIds(threadId);
  if (ids.has(itemId)) return true;
  ids.add(itemId);
  return false;
}

function applyThreadStatus(status: unknown): void {
  if (typeof status === "string") {
    setRunning(status === "running");
  }
}

function beginThreadActivation(targetThreadId: string | null): number {
  btnSendEl.classList.remove("guidance-pending");
  if (uiState.sessionId && uiState.sessionId !== targetThreadId) {
    staleSnapshotThreadIds.add(uiState.sessionId);
  }
  if (pendingActivationTarget && pendingActivationTarget !== targetThreadId) {
    staleSnapshotThreadIds.add(pendingActivationTarget);
  }
  if (targetThreadId) staleSnapshotThreadIds.delete(targetThreadId);
  const generation = ++threadActivationGeneration;
  pendingActivationTarget = targetThreadId;
  pendingActivationSnapshot = null;
  uiState.isSwitchingThread = true;
  updateStatusBar();
  return generation;
}

function isCurrentThreadActivation(generation: number): boolean {
  return generation === threadActivationGeneration;
}

function activateThread(threadId: string): void {
  if (threadId !== uiState.sessionId) {
    if (uiState.sessionId) {
      retireThreadTurn(uiState.sessionId);
      clearIncrementalStreamStatesForThread(uiState.sessionId);
      pendingSnapshotRequests.delete(uiState.sessionId);
    }
    threadContextGeneration += 1;
    clearCommittedStreams();
    clearActiveStreams();
    resetFileChangeCards();
    forgetPendingLocalMessages();
    resetConversationPrompts();
    transcriptEl.replaceChildren();
    btnSendEl.classList.remove("guidance-pending");
    setRunning(false);
    syncEmptyState();
  }
  uiState.sessionId = threadId;
  recordThreadVisit(threadId);
}

function finishThreadActivation(generation: number, threadId: string): void {
  if (!isCurrentThreadActivation(generation)) return;
  const bufferedSnapshot = pendingActivationSnapshot;
  pendingActivationTarget = null;
  pendingActivationSnapshot = null;
  activateThread(threadId);
  uiState.isSwitchingThread = false;
  updateStatusBar();
  if (bufferedSnapshot?.active_thread_id === threadId) {
    renderWorkspaceSnapshot(bufferedSnapshot);
  }
}

function failThreadActivation(generation: number): void {
  if (!isCurrentThreadActivation(generation)) return;
  if (uiState.sessionId) staleSnapshotThreadIds.delete(uiState.sessionId);
  pendingActivationTarget = null;
  pendingActivationSnapshot = null;
  uiState.isSwitchingThread = false;
  updateStatusBar();
}

export function switchThread(threadId: string): Promise<void> {
  const generation = beginThreadActivation(threadId);
  return rpcCall("session.switch", {
    thread_id: threadId,
    turn_limit: TRANSCRIPT_PAGE_SIZE,
  })
    .then((result: unknown) => {
      if (!isCurrentThreadActivation(generation)) return;
      const selected = result as Record<string, unknown>;
      const activeThreadId = (selected.active_thread_id as string) || threadId;
      finishThreadActivation(generation, activeThreadId);
      if (typeof selected.runtime_profile === "string") {
        applyRuntimeState({ runtime_profile: selected.runtime_profile });
      }
      applyThreadStatus(selected.status);
      updateStatusBar();
    })
    .catch((error: unknown) => {
      if (!isCurrentThreadActivation(generation)) return;
      failThreadActivation(generation);
      throw error;
    });
}

function openThread(directory: string, profile?: string): Promise<void> {
  if (!isRpcConnected()) return Promise.resolve();

  const targetDirectory = directory || uiState.workspace;
  const existing = findReusableEmptyThread(targetDirectory, profile);
  const generation = beginThreadActivation(existing?.thread_id || null);
  if (existing) {
    return rpcCall("session.switch", {
      thread_id: existing.thread_id,
      turn_limit: TRANSCRIPT_PAGE_SIZE,
    })
      .then((result: unknown) => {
        if (!isCurrentThreadActivation(generation)) return;
        const selected = result as Record<string, unknown>;
        const activeThreadId =
          (selected.active_thread_id as string) || existing.thread_id;
        finishThreadActivation(generation, activeThreadId);
        if (typeof selected.runtime_profile === "string") {
          existing.runtime_profile = selected.runtime_profile;
          applyRuntimeState({ runtime_profile: selected.runtime_profile });
        }
        applyThreadStatus(selected.status ?? existing.status);
        addThread(existing, uiState.sessionId);
        updateStatusBar();
      })
      .catch((error: unknown) => {
        if (!isCurrentThreadActivation(generation)) return;
        failThreadActivation(generation);
        throw error;
      });
  }

  const params: { directory: string; profile?: string } = { directory: targetDirectory };
  if (profile) params.profile = profile;
  return rpcCall("session.create", params)
    .then((result: unknown) => {
      if (!isCurrentThreadActivation(generation)) return;
      const r = result as {
        thread_id: string;
        title?: string;
        status?: string;
        workspace?: string;
        directory?: string;
        runtime_profile?: string;
        temporary?: boolean;
      };
      finishThreadActivation(generation, r.thread_id);
      const runtimeProfile =
        typeof r.runtime_profile === "string" ? r.runtime_profile : profile;
      if (runtimeProfile) {
        applyRuntimeState({ runtime_profile: runtimeProfile });
      }
      applyThreadStatus(r.status);
      addThread(
        {
          thread_id: r.thread_id,
          title: r.title,
          status: r.status,
          workspace: r.workspace || r.directory || directory || uiState.workspace,
          runtime_profile: runtimeProfile,
          temporary: r.temporary === true,
        },
        uiState.sessionId,
      );
      updateStatusBar();
    })
    .catch((error: unknown) => {
      if (!isCurrentThreadActivation(generation)) return;
      failThreadActivation(generation);
      throw error;
    });
}

export function openThreadForProfile(profile: RuntimeProfile): Promise<void> {
  if (uiState.isSwitchingProfile) return Promise.resolve();
  uiState.isSwitchingProfile = true;
  updateStatusBar();
  return openThread("", profile)
    .catch((error: unknown) => {
      showSessionError("模式切换", error);
    })
    .finally(() => {
      uiState.isSwitchingProfile = false;
      updateStatusBar();
    });
}

function initializeSidebarCallbacks(): void {
  onThreadSelect((threadId: string) => {
    switchThread(threadId).catch((err: Error) => {
      showSessionError("会话切换", err);
    });
  });

  onNewThread((directory: string, profile?: string) => {
    void openThread(directory, profile || uiState.runtimeProfile).catch((err: Error) => {
      showSessionError("会话创建", err);
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
}

initializeSidebarCallbacks();

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

let catalogRequested = false;

function refreshUsageSnapshot(): void {
  if (!isRpcConnected()) return;
  rpcCall("usage.get", {})
    .then((result: unknown) => {
      const usage = (result as { usage?: UsageSnapshot } | undefined)?.usage;
      if (usage && typeof usage.context_tokens === "number") {
        uiState.usage = usage;
      } else {
        uiState.usage = null;
      }
      updateStatusBar();
    })
    .catch(() => {});
}

function requestCommandCatalogIfNeeded(): void {
  if (catalogRequested || !isRpcConnected()) return;
  catalogRequested = true;
  rpcCall("commands.list", {})
    .then((result: unknown) => {
      const commands = (result as { commands?: SlashCommand[] } | undefined)?.commands;
      if (Array.isArray(commands) && commands.length > 0) {
        setCommandCatalog(commands);
      }
    })
    .catch(() => {
      catalogRequested = false;
    });
}

function registerNotificationHandlers(): void {
  for (const method of [
    "workspace.snapshot",
    "workspace.patch",
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

function renderWorkspaceSnapshot(params: Record<string, unknown>): void {
  const activeThreadId = (params.active_thread_id as string) || "";
  if (!snapshotThreadMatchesActive(params, activeThreadId)) return;
  if (activeThreadId && !isSnapshotRevisionFresh(activeThreadId, params)) return;
  if (uiState.isSwitchingThread) {
    if (pendingActivationTarget !== null) {
      if (activeThreadId !== pendingActivationTarget) return;
      pendingActivationSnapshot = params;
      return;
    }
    if (!activeThreadId || activeThreadId === uiState.sessionId) return;
    pendingActivationSnapshot = params;
    return;
  }
  if (activeThreadId && staleSnapshotThreadIds.has(activeThreadId)) return;

  const revision = workspaceRevision(params);
  const recoveryThreadId = activeThreadId || uiState.sessionId;
  const recovering = Boolean(
    recoveryThreadId && pendingSnapshotRequests.has(recoveryThreadId),
  );
  if (recovering) {
    clearIncrementalStreamStatesForThread(recoveryThreadId);
    renderedItemIdsByThread.delete(recoveryThreadId);
  }
  if (revision !== null && revision < lastWorkspaceRevision) return;
  if (revision !== null) {
    lastWorkspaceRevision = revision;
    pendingSnapshotRequests.delete(activeThreadId);
    pendingSnapshotRequests.delete(uiState.sessionId);
  }

  const snapshot = params.active_snapshot || { nodes: [] };
  if (activeThreadId) rememberSnapshotRevision(activeThreadId, params);
  requestCommandCatalogIfNeeded();
  const activeThread = ((params.threads as Array<Record<string, unknown>> | undefined) || []).find(
    (thread) => thread.thread_id === activeThreadId,
  );
  const threadChanged = uiState.sessionId !== activeThreadId;
  if (threadChanged) {
    if (uiState.sessionId) staleSnapshotThreadIds.add(uiState.sessionId);
    activateThread(activeThreadId);
  }
  uiState.sessionId = activeThreadId;
  applyRuntimeState(params);
  if (typeof activeThread?.runtime_profile === "string") {
    applyRuntimeState({ runtime_profile: activeThread.runtime_profile });
  }
  applyThreadStatus(activeThread?.status ?? params.status);
  refreshUsageSnapshot();
  requestStartupSettingsIfNeeded(applySettingsRuntimeState);
  updateStatusBar();
  renderSidebar(
    (params.threads as unknown as ThreadInfo[]) || [],
    activeThreadId,
    workspaceBasename(uiState.workspace),
    uiState.workspace,
  );
  const typedSnapshot = snapshotForRendering(activeThreadId, snapshot as TranscriptSnapshot);
  removePendingLocalMessageElements();
  renderTranscript(transcriptEl, typedSnapshot);
  restorePendingLocalMessages(activeThreadId, typedSnapshot);
  syncEmptyState();
  scrollToBottom();
}

export function handleNotification(
  method: string,
  params: Record<string, unknown> = {},
): void {
  if (method === "workspace.snapshot") {
    renderWorkspaceSnapshot(params);
    return;
  }
  if (method === "workspace.patch") {
    applyWorkspacePatch(params);
    return;
  }
  if (method === "ui.request") {
    if (!isCurrentUiRequest(params)) return;
    showRequest(params);
    return;
  }
  if (method === "startup.shown") {
    applyRuntimeState(params);
    return;
  }
  if (method === "turn.started") {
    if (!registerTurnStarted(params)) return;
    setRunning(true);
    return;
  }
  if (
    method === "turn.completed" ||
    method === "turn.failed" ||
    method === "turn.cancelled"
  ) {
    if (!isCurrentThreadEvent(params)) return;
    retireCompletedTurn(params);
    if (method === "turn.completed") {
      refreshUsageSnapshot();
    }
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
    openTerminalDrawer();
    return;
  }
  if (method === "capture.started" || method === "capture.stopped") {
    return;
  }
  if (method === "refresh.requested" || method === "reset.requested") {
    return;
  }
  if (method === "notice.set") {
    const text = typeof params.text === "string" ? params.text.trim() : "";
    if (text) {
      appendNoticeItem(`notice-${Date.now()}`, { style: "info", text });
    }
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
  if (!isCurrentThreadEvent(params)) return;
  const kind = params.kind as string;
  const itemId = params.item_id as string;
  const data = (params.data as Record<string, unknown>) || {};
  if (isDuplicateItemStart(method, params)) return;

  if (kind === "assistant_stream") {
    if (method === "item.started") {
      if (isIncrementalStreamData(data)) {
        startIncrementalStream(params, data, itemId);
      } else {
        appendStreamText(itemId, "", (data.phase as string) || "text");
      }
    } else if (method === "item.delta") {
      if (isIncrementalStreamData(data)) {
        consumeIncrementalStreamDelta(params, data, itemId);
      } else {
        appendStreamText(
          itemId,
          (data.text as string) || "",
          (data.phase as string) || "text",
        );
      }
    } else if (method === "item.completed") {
      const result = commitStream(itemId);
      clearIncrementalStreamItem(params);
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
    handleToolItem(method, itemId, data, (params.turn_id as string) || "");
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
    const type = data.prompt_type as string;
    if (method === "item.started") {
      if (type === "permission") {
        showPromptItemRequest({
          ...data,
          thread_id: (params.thread_id as string) || uiState.sessionId,
        });
      } else {
        showConversationPrompt(
          itemId,
          (params.thread_id as string) || uiState.sessionId,
          data,
          submitConversationPromptResponse,
        );
      }
    } else if (method === "item.completed") {
      if (type === "permission" && data.cleared) {
        clearPermissionRequests(
          typeof data.request_id === "string" ? data.request_id : undefined,
        );
      } else {
        completeConversationPrompt(data);
      }
    }
    return;
  }
  if (kind === "status") {
    handleStatusItem(method, itemId, data);
    return;
  }
  if (kind === "guidance_preview") {
    if (method === "item.started") {
      const text = (data.text as string) || "";
      if (text) {
        replacePendingGuidanceWithServerItem(
          (params.thread_id as string) || uiState.sessionId,
          text,
        );
        appendMessageItem(itemId, { style: "guidance", text });
      }
    }
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

inputEl.addEventListener("paste", (event: ClipboardEvent) => {
  if (Array.from(event.clipboardData?.files ?? []).some((f) => f.type.startsWith("image/"))) {
    return;
  }
  const text = event.clipboardData?.getData("text/plain") ?? "";
  if (text.includes("\n")) {
    event.preventDefault();
    const token = registerTextPaste(text);
    const start = inputEl.selectionStart ?? inputEl.value.length;
    const end = inputEl.selectionEnd ?? start;
    inputEl.value = `${inputEl.value.slice(0, start)}${token}${inputEl.value.slice(end)}`;
    const cursor = start + token.length;
    inputEl.setSelectionRange(cursor, cursor);
  }
});

composerEl.addEventListener("submit", (event: SubmitEvent) => {
  event.preventDefault();
  const tokens = imageAttachmentTokens();
  const text = [expandPasteTokens(inputEl.value.trim()), tokens]
    .filter(Boolean)
    .join(" ");
  if (
    !text ||
    uiState.isSwitchingModel ||
    uiState.isSwitchingProfile ||
    uiState.isSwitchingThread
  ) {
    return;
  }
  if (!isRpcConnected()) {
    showSessionError("发送", new Error("未连接到后端"));
    return;
  }
  clearPasteEntries();
  clearImageAttachments();
  const conversationPrompt = pendingConversationPrompt(uiState.sessionId);
  if (conversationPrompt) {
    hideSlashMenu();
    hideRefMenu();
    pushHistory(text);
    submitConversationPromptResponse(conversationPrompt, text, text);
    return;
  }
  if (text.startsWith("/") && !isKnownSlashCommand(text)) {
    inputEl.value = "";
    hideSlashMenu();
    hideRefMenu();
    return;
  }

  const threadId = uiState.sessionId;
  const sendContextGeneration = threadContextGeneration;
  const isGuidance = uiState.isRunning;
  const style = isGuidance ? "guidance" : "text";
  const itemId = createLocalItemId(isGuidance ? "guidance" : "user");
  if (!isGuidance) {
    rememberPendingLocalMessage(threadId, itemId, text, style);
    appendMessageItem(itemId, { style, text });
    syncEmptyState();
    inputEl.value = "";
  }
  hideSlashMenu();
  hideRefMenu();
  pushHistory(text);

  if (isGuidance) {
    btnSendEl.classList.add("guidance-pending");
    rpcCall("session.submit", { text, thread_id: threadId })
      .then((result: unknown) => {
        if ((result as { ok?: boolean } | null)?.ok === false) {
          throw new Error("后端未接受发送请求");
        }
        if (isCurrentSendContext(threadId, sendContextGeneration) && (!inputEl.value || inputEl.value === text)) inputEl.value = "";
      })
      .catch((error: Error) => {
        if (!isCurrentSendContext(threadId, sendContextGeneration)) return;
        removePendingLocalMessage(itemId);
        if (!inputEl.value) inputEl.value = text;
        showSessionError("发送", error);
      })
      .finally(() => {
        if (isCurrentSendContext(threadId, sendContextGeneration)) {
          btnSendEl.classList.remove("guidance-pending");
        }
      });
    return;
  }

  setRunning(true);
  rpcCall("session.submit", { text, thread_id: threadId })
    .then((result: unknown) => {
      if ((result as { ok?: boolean } | null)?.ok === false) {
        throw new Error("后端未接受发送请求");
      }
    })
    .catch((error: Error) => {
      if (!isCurrentSendContext(threadId, sendContextGeneration)) return;
      setRunning(false);
      removePendingLocalMessage(itemId);
      if (!inputEl.value) inputEl.value = text;
      showSessionError("发送", error);
    });
});


export function _resetWorkbenchForTest(): void {
  threadActivationGeneration += 1;
  threadContextGeneration += 1;
  pendingActivationTarget = null;
  pendingActivationSnapshot = null;
  staleSnapshotThreadIds.clear();
  lastSnapshotRevisionByThread.clear();
  lastWorkspaceRevision = 0;
  pendingSnapshotRequests.clear();
  incrementalStreamStates.clear();
  renderedItemIdsByThread.clear();
  threadTurnContexts.clear();
  transcriptWindows.clear();
  clearCommittedStreams();
  clearActiveStreams();
  resetFileChangeCards();
  localItemSequence = 0;
  forgetPendingLocalMessages();
  resetConversationPrompts();
  clearPasteEntries();
  clearImageAttachments();
  _resetHistoryForTest();
  _resetCommandCatalogForTest();
  _resetSettingsForTest();
  _resetIntegrationsForTest();
  _resetContextMenuForTest();
  _resetDialogForTest();
  _resetSidebarForTest();
  _resetNavigationForTest();
  _resetWorkspaceForTest();
  _resetModeControlsForTest();
  _resetWorkbenchStateForTest();
  _resetConnectionForTest();
  _resetRpcForTest();
  inputEl.value = "";
  transcriptEl.replaceChildren();
  btnSendEl.classList.remove("guidance-pending");
  setRunning(false);
  hideRefMenu();
  hideSlashMenu();
  registerNotificationHandlers();
  initializeModeControls();
  initializeSettingsModal();
  initIntegrationsPanel();
  initContextMenu();
  initializeSidebarCallbacks();

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
  initSidebarToggle();
  initSidebarResizer();
  initThreadNavigation(switchThread);
  initModelControls();
  syncEmptyState();
}


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

let lastEmptyCtrlCAt = 0;

function isTauriRuntime(): boolean {
  return Boolean((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__);
}

function cancelRunningTurn(): void {
  if (!isRpcConnected()) return;
  rpcCall("session.cancel", { thread_id: uiState.sessionId })
    .then(() => setRunning(false))
    .catch(() => setRunning(false));
}

function requestAppQuit(): boolean {
  if (!isTauriRuntime()) return false;
  void import("@tauri-apps/api/window")
    .then(({ getCurrentWindow }) => getCurrentWindow().close())
    .catch(() => {});
  return true;
}

function handleCtrlCInterrupt(event: KeyboardEvent): boolean {
  const selectionStart = inputEl.selectionStart ?? 0;
  const selectionEnd = inputEl.selectionEnd ?? 0;
  if (selectionEnd > selectionStart) return false;
  if (uiState.isRunning) {
    event.preventDefault();
    cancelRunningTurn();
    return true;
  }
  if (inputEl.value !== "") {
    event.preventDefault();
    pushHistory(inputEl.value);
    inputEl.value = "";
    return true;
  }
  const now = Date.now();
  if (now - lastEmptyCtrlCAt < 3000 && requestAppQuit()) {
    event.preventDefault();
    lastEmptyCtrlCAt = 0;
    return true;
  }
  lastEmptyCtrlCAt = now;
  return false;
}

inputEl.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (refMenuVisible() && uiState.refCandidates.length > 0) {
    const count = uiState.refCandidates.length;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      uiState.refSelectedIndex = (uiState.refSelectedIndex + 1) % count;
      updateRefMenu();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      uiState.refSelectedIndex = (uiState.refSelectedIndex - 1 + count) % count;
      updateRefMenu();
      return;
    }
    if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
      event.preventDefault();
      const selected = uiState.refCandidates[uiState.refSelectedIndex];
      if (selected) {
        acceptRefCandidate(selected);
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      hideRefMenu();
      return;
    }
  }
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
    if (event.key === "Tab") {
      const completed = completeSlashInput(inputEl.value);
      if (completed !== null && completed !== inputEl.value) {
        event.preventDefault();
        inputEl.value = completed;
        inputEl.setSelectionRange(completed.length, completed.length);
        inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
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
  if (event.key === "Escape" && uiState.isRunning) {
    event.preventDefault();
    cancelRunningTurn();
    return;
  }
  if (event.key === "c" && event.ctrlKey && !event.metaKey) {
    if (handleCtrlCInterrupt(event)) return;
  }
  if (event.key === "d" && event.ctrlKey && !event.metaKey && inputEl.value === "") {
    if (requestAppQuit()) {
      event.preventDefault();
      return;
    }
  }
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    if (inputEl.value === "" || isHistoryBrowsing()) {
      const recalled =
        event.key === "ArrowUp"
          ? historyPrev(inputEl.value)
          : historyNext();
      if (recalled !== null) {
        event.preventDefault();
        inputEl.value = recalled;
        inputEl.setSelectionRange(recalled.length, recalled.length);
      }
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

inputEl.addEventListener("input", () => {
  resetHistoryNavigation();
  const value = inputEl.value;
  if (value.startsWith("/")) {
    const matched = matchSlashCommands(value);
    if (matched.length > 0) {
      hideRefMenu();
      uiState.slashCommands = matched;
      uiState.slashSelectedIndex = 0;
      showSlashMenu();
      return;
    }
  }
  hideSlashMenu();
  scheduleRefUpdate();
});
