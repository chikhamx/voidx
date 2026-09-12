/// <reference types="vite/client" />
import "../css/tokens.css";
import "../css/base.css";
import "../css/layout.css";
import "../css/chat.css";
import "../css/composer.css";
import "../css/components.css";
import {
  renderTranscript,
  renderTranscriptBlocksDetached,
  quiesceStreamsForBlockedInstallNoCallback,
  quiesceTranscriptViewportForBlockedInstallNoDom,
  validateTranscriptViewportBlockedInstallReady,
  renderHistoricalTranscriptPage,
  historicalTranscriptPageNodes,
  appendMessageItem,
  handleToolItem,
  handleStatusItem,
  appendThoughtItem,
  appendNoticeItem,
  appendDiffItem,
  setTranscriptElement,
  appendStreamText,
    getOrCreateStream,
  commitStream,
  clearCommittedStreams,
  clearActiveStreams,
  stripRichMarkup,
  snapshotTurnText,
  forceTranscriptScrollToBottom,
  type BlockedViewportQuiesceToken,
  isTranscriptFollowing,
  selectTranscriptWindowAnchor,
  getTranscriptInteractionGeneration,
  prepareTranscriptForSynchronousPrepend,
  resetTranscriptViewport,
  peekTranscriptLiveOwners,
  validateTranscriptLiveOwnerTokens,
  planTranscriptDomWindow,
    DEFAULT_BLOCK_ESTIMATE_PX,
  DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET,
  type TranscriptDomWindowState,
  type TranscriptSpacerSegment,
  type TranscriptExternalInsertion,
  type TranscriptLayoutEntry,
  claimCommittedStreamsForDescriptors,
  stampTranscriptBlockOwnership,
  reserveCommittedStream,
  validateCommittedStreamReservations,
  commitCommittedStreamReservationsNoFail,
  releaseCommittedStreamReservations,
  type CommittedStreamReservation,
  mergeCanonicalTranscript,
  applyTranscriptDomWindowTransaction,
  measureTranscriptLogicalBlockExtent,
} from "./utils";
import {
  prepareBlockedFileToolCacheState,
  publishBlockedFileToolCachesNoFail,
  quiesceFileToolCachesForBlockedInstallNoDom,
  resetFileChangeCards,
  peekFileChangeCard,
  reserveFileChangeCard,
  validateFileChangeCardReservations,
  commitFileChangeCardReservationsNoFail,
  releaseFileChangeCardReservations,
  type FileChangeCardReservation,
} from "./utils/render-file-changes";
import type {
  IncrementalStreamState,
  TranscriptSnapshot,
  SlashCommand,
} from "./utils";
import {
  buildTranscriptDescriptors,
  collectExistingTranscriptBlocksSafely,
} from "./utils/transcript-reconciliation";
import { sha256 } from "./utils/sha256";

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
    onThreadFork,
  filterSessions,
  initDock,
  renderTodoInDock,
  switchTab,
  openTerminalDrawer,
  initTerminal,
  appendTerminalOutput,
  onTerminalInput,
  onTerminalStart,
    onTerminalResize,
    onTerminalStop,
    terminateActiveTerminal,
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
  quiesceConversationPromptForBlockedInstallNoDom,
  validateBlockedPromptQuiesceToken,
  peekConversationPromptToken,
  validateConversationPromptToken,
} from "./ui/prompt";
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
  transcriptReturnBottomEl,
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
setTranscriptElement(transcriptEl, transcriptReturnBottomEl);
initTheme();
initDock();
initTerminal();
interface PendingLocalMessage {
  threadId: string;
  itemId: string;
  text: string;
  style: "text" | "guidance";
}

export interface PendingLocalMessageToken {
  threadId: string;
  itemId: string;
  element: HTMLElement;
  generation: number;
}

interface SnapshotUserEntry {
  id: string;
  text: string;
  style: "user" | "guidance";
  rendered: boolean;
}

let pendingLocalMessages: PendingLocalMessage[] = [];
let pendingLocalGeneration = 0;
let knownSnapshotUserNodeIds = new Map<string, Set<string>>();
let localItemSequence = 0;

export function peekPendingLocalMessageTokens(threadId: string): PendingLocalMessageToken[] {
  return pendingLocalMessages
    .filter((pending) => pending.threadId === threadId)
    .map((pending) => {
      const element = pendingLocalMessageElement(pending.itemId);
      return element ? {
        threadId,
        itemId: pending.itemId,
        element,
        generation: pendingLocalGeneration,
      } : null;
    })
    .filter((token): token is PendingLocalMessageToken => token !== null);
}

export function validatePendingLocalMessageTokens(
  threadId: string,
  tokens: readonly PendingLocalMessageToken[],
): boolean {
  if (tokens.some((token) => token.threadId !== threadId
    || token.generation !== pendingLocalGeneration)) return false;
  const pending = pendingLocalMessages.filter((message) => message.threadId === threadId);
  if (pending.length !== tokens.length) return false;
  return pending.every((message, index) => {
    const token = tokens[index];
    return token.itemId === message.itemId
      && token.element === pendingLocalMessageElement(message.itemId);
  });
}

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
  pendingLocalGeneration += 1;
}


function pendingLocalMessageElement(itemId: string): HTMLElement | null {
  return [...transcriptEl.querySelectorAll<HTMLElement>(`[data-item-id="${itemId}"]`)]
    .find((element) => !element.dataset.reconcileKey) ?? null;
}
function appendPendingLocalMessage(pending: PendingLocalMessage): void {
  appendMessageItem(pending.itemId, { style: pending.style, text: pending.text });
}


function pendingLocalHandoffs(
  threadId: string,
  snapshot: TranscriptSnapshot,
): ReadonlyMap<string, HTMLElement> {
  const knownIds = knownSnapshotUserNodeIds.get(threadId) ?? new Set<string>();
  const candidates = snapshotUserEntries(snapshot).filter((entry) => !knownIds.has(entry.id));
  const handoffs = new Map<string, HTMLElement>();
  const used = new Set<string>();
  for (const pending of pendingLocalMessages) {
    if (pending.threadId !== threadId) continue;
    const expectedStyle = pending.style === "guidance" ? "guidance" : "user";
    const candidate = candidates.find((entry) => (
      !used.has(entry.id)
      && entry.style === expectedStyle
      && entry.text === pending.text
    ));
    if (!candidate) continue;
    const element = pendingLocalMessageElement(pending.itemId);
    if (!element) continue;
    used.add(candidate.id);
    const node = (snapshot.nodes || []).find((item) => item.id === candidate.id);
    const hasStructuredTurnBody = node?.node_type === "turn"
      && Array.isArray(node.body_lines)
      && node.body_lines.length > 0;
    if (!hasStructuredTurnBody) handoffs.set(candidate.id, element);
  }
  return handoffs;
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
    if (renderedEntry) {
      pendingLocalMessageElement(pending.itemId)?.remove();
      continue;
    }
    retained.push(pending);
    if (!pendingLocalMessageElement(pending.itemId)) {
      appendPendingLocalMessage(pending);
    }
  }

  if (retained.length !== pendingLocalMessages.length
    || retained.some((pending, index) => pending !== pendingLocalMessages[index])) {
    pendingLocalMessages = retained;
    pendingLocalGeneration += 1;
  }
}

function forgetPendingLocalMessage(itemId: string): void {
  const retained = pendingLocalMessages.filter((pending) => pending.itemId !== itemId);
  if (retained.length !== pendingLocalMessages.length) {
    pendingLocalMessages = retained;
    pendingLocalGeneration += 1;
  }
}

function removePendingLocalMessage(itemId: string): void {
  forgetPendingLocalMessage(itemId);
  pendingLocalMessageElement(itemId)?.remove();
}


function forgetPendingLocalMessages(): void {
  if (pendingLocalMessages.length > 0) {
    pendingLocalMessages = [];
    pendingLocalGeneration += 1;
  }
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
  }).catch(() => { });
});

onTerminalResize((terminalId: string, cols: number, rows: number) => {
    rpcCall("terminal.resize", {
        terminal_id: terminalId,
        cols,
        rows,
    }).catch(() => { });
});

onTerminalStop((terminalId: string) => {
    rpcCall("terminal.stop", {
        terminal_id: terminalId,
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
interface SnapshotRecoveryState {
  mode: "ordinary-gap" | "blocked";
  requestedRevision: number | null;
  inFlight: boolean;
  retryAttempt: number;
  timerGeneration: number;
}
const snapshotRecoveryStates = new Map<string, SnapshotRecoveryState>();
let socketGeneration = 0;
let incrementalStreamStates = new Map<string, IncrementalStreamState>();
let renderedItemIdsByThread = new Map<string, Set<string>>();

function sendPendingSnapshotRecoveries(): void {
  if (!isRpcConnected()) return;
  for (const [threadId, state] of snapshotRecoveryStates) {
    if (state.inFlight) continue;
    rpcNotify("snapshot.requested", { thread_id: threadId });
    state.inFlight = true;
  }
}

onSocketChange((ws) => {
  socketGeneration += 1;
  const generation = socketGeneration;
  lastWorkspaceRevision = 0;
  lastSnapshotRevisionByThread.clear();
  incrementalStreamStates.clear();
  renderedItemIdsByThread.clear();
  for (const state of snapshotRecoveryStates.values()) {
    state.inFlight = false;
    state.timerGeneration += 1;
  }
    if (!ws) {
        terminateActiveTerminal();
        return;
    }
    const onOpen = (): void => {
        if (generation !== socketGeneration) return;
        sendPendingSnapshotRecoveries();
    };
    const onClose = (): void => {
        if (generation !== socketGeneration) return;
        terminateActiveTerminal();
    for (const state of snapshotRecoveryStates.values()) {
      state.inFlight = false;
      state.timerGeneration += 1;
    }
  };
  if (typeof ws.addEventListener === "function") {
    ws.addEventListener("open", onOpen);
    ws.addEventListener("close", onClose);
  }
  if (ws.readyState === WebSocket.OPEN) onOpen();
});
interface ThreadTurnContext {
  currentTurnId: string | null;
  retiredTurnIds: Set<string>;
}
const threadTurnContexts = new Map<string, ThreadTurnContext>();

const TRANSCRIPT_PAGE_SIZE = 40;
interface TranscriptWindowState extends TranscriptDomWindowState {
  loading: boolean;
}
const transcriptWindows = new Map<string, TranscriptWindowState>();

export function _peekTranscriptWindowSnapshotForTest(
  threadId: string,
): TranscriptSnapshot | null {
  return transcriptWindows.get(threadId)?.snapshot ?? null;
}

export function _peekTranscriptWindowHeightKeysForTest(
  threadId: string,
): string[] | null {
  const state = transcriptWindows.get(threadId);
  return state ? [...state.heights.keys()] : null;
}

export function _peekTranscriptWindowStateForTest(
    threadId: string,
): TranscriptWindowState | null {
    return transcriptWindows.get(threadId) ?? null;
}


function createTranscriptWindowSpacer(segment: TranscriptSpacerSegment): HTMLElement {
  const element = document.createElement("div");
  element.className = "transcript-window-spacer";
  element.setAttribute("aria-hidden", "true");
  element.tabIndex = -1;
  element.dataset.transcriptSpacer = JSON.stringify(segment);
  element.style.height = `${segment.cssHeightPx}px`;
  return element;
}

function transcriptRowGapPx(): number {
  const value = Number.parseFloat(getComputedStyle(transcriptEl).rowGap);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

let programmaticScrollTopTarget: number | null = null;
let programmaticScrollTimeout: ReturnType<typeof setTimeout> | null = null;

function writeTranscriptScrollTop(value: number): void {
    if (!Number.isFinite(value) || Math.abs(transcriptEl.scrollTop - value) < 0.5) return;
    transcriptEl.scrollTop = value;
    programmaticScrollTopTarget = transcriptEl.scrollTop;
    if (programmaticScrollTimeout !== null) {
        clearTimeout(programmaticScrollTimeout);
    }
    programmaticScrollTimeout = setTimeout(() => {
        programmaticScrollTopTarget = null;
        programmaticScrollTimeout = null;
    }, 50);
}

function installInitialTranscriptDomWindow(
  threadId: string,
  snapshot: TranscriptSnapshot,
): TranscriptWindowState | null {
  let descriptors: ReturnType<typeof buildTranscriptDescriptors>;
  try {
    descriptors = buildTranscriptDescriptors(snapshot.nodes || []);
  } catch {
    return null;
  }

  const sourceElements = Array.from(transcriptEl.children);
  if (!sourceElements.every((element): element is HTMLElement => element instanceof HTMLElement)) {
    return null;
  }
  const sourceChildren = sourceElements;
  const collected = collectExistingTranscriptBlocksSafely(transcriptEl, descriptors);
  if (collected.status !== "collected"
    || collected.index.byKey.size > 0
    || collected.spacers.length > 0
    || collected.index.entries.some((entry) => entry.kind !== "retained")) return null;

  const pendingTokens = peekPendingLocalMessageTokens(threadId);
  const promptToken = peekConversationPromptToken(threadId);
  const liveOwnerTokens = peekTranscriptLiveOwners();
  const validateExternalOwners = (): boolean => (
    validatePendingLocalMessageTokens(threadId, pendingTokens)
    && (promptToken
      ? validateConversationPromptToken(promptToken)
      : peekConversationPromptToken(threadId) === null)
    && validateTranscriptLiveOwnerTokens(liveOwnerTokens)
  );
  if (!validateExternalOwners()) return null;

  const estimates = new Map<string, number>();
  const entries: TranscriptLayoutEntry[] = descriptors.map((descriptor, descriptorIndex) => {
    estimates.set(descriptor.rendererShapeVersion, DEFAULT_BLOCK_ESTIMATE_PX);
    return {
      kind: "canonical",
      key: descriptor.key,
      descriptorIndex,
      extentPx: DEFAULT_BLOCK_ESTIMATE_PX,
    };
  });
  entries.push(...sourceChildren.map((element, index) => ({
    kind: "external" as const,
    id: `initial-external:${index}`,
    element,
    extentPx: measureTranscriptLogicalBlockExtent([element as HTMLElement])
      ?? DEFAULT_BLOCK_ESTIMATE_PX,
    insertion: { edge: "after-all" } as TranscriptExternalInsertion,
  })));
  const plan = planTranscriptDomWindow({
    entries,
    attachedKeys: new Set(),
    pinnedKeys: new Set(),
    rowGapPx: transcriptRowGapPx(),
    budget: DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET,
    viewport: {
      scrollTop: transcriptEl.scrollTop,
      clientHeight: transcriptEl.clientHeight,
      following: true,
    },
    activation: "initial",
    anchorKey: null,
  });

  let detached: ReturnType<typeof renderTranscriptBlocksDetached>;
  try {
    detached = renderTranscriptBlocksDetached(
      descriptors.filter((descriptor) => plan.nextAttachedKeys.has(descriptor.key)),
    );
  } catch {
    return null;
  }
  for (const block of detached.blocks) {
    for (const root of block.roots) root.remove();
  }
  const stagedBlocks = new Map(detached.blocks.map((block) => [block.key, block]));
  if (stagedBlocks.size !== plan.materializeKeys.length) return null;

  const spacers = plan.spacerSegments.map((segment) => ({
    element: createTranscriptWindowSpacer(segment),
    segment,
  }));
  const spacerByStart = new Map(spacers.map((spacer) => [spacer.segment.startIndex, spacer]));
  const nextChildren: Element[] = [];
  for (let index = 0; index < descriptors.length;) {
    const spacer = spacerByStart.get(index);
    if (spacer) {
      nextChildren.push(spacer.element);
      index = spacer.segment.endIndex;
      continue;
    }
    const descriptor = descriptors[index];
    const block = stagedBlocks.get(descriptor.key);
    if (plan.nextAttachedKeys.has(descriptor.key) && block) nextChildren.push(...block.roots);
    index += 1;
  }
  nextChildren.push(...sourceChildren);

  const interactionGeneration = getTranscriptInteractionGeneration();
  const currentState: TranscriptWindowState = {
    threadId,
    snapshot,
    descriptors,
    heights: new Map(),
    estimates,
    attachedKeys: new Set(),
    pinnedKeys: new Set(),
    spacerSegments: [],
    generation: 0,
    loadingEarlier: false,
    loading: false,
  };
  const nextState: TranscriptWindowState = {
    ...currentState,
    attachedKeys: new Set(plan.nextAttachedKeys),
    spacerSegments: [...plan.spacerSegments],
    generation: 1,
  };
  const reservations: FileChangeCardReservation[] = [];
  const reserveFileChanges = (): boolean => {
    for (const [key, state] of detached.stagedFileChangeStates) {
      const reservation = reserveFileChangeCard(key, null, state);
      if (!reservation) return false;
      reservations.push(reservation);
    }
    return true;
  };
  const validateOwners = (): boolean => (
    validateExternalOwners()
    && validateFileChangeCardReservations(reservations)
  );
  const result = applyTranscriptDomWindowTransaction({
    root: transcriptEl,
    sourceChildren,
    externalSourceChildren: new Set(sourceChildren),
    nextChildren,
    expectedNextChildren: [...nextChildren],
    existingBlocks: new Map(),
    plan,
    stagedBlocks,
    spacers,
    anchorJournal: null,
    expectedInteractionGeneration: interactionGeneration,
    expectedWindowGeneration: 0,
    validateInteractionGeneration: (generation) => (
      generation === interactionGeneration
      && getTranscriptInteractionGeneration() === interactionGeneration
    ),
    validateWindowGeneration: (generation) => (
      generation === 0 && !transcriptWindows.has(threadId)
    ),
    measureBlock: (block) => measureTranscriptLogicalBlockExtent(block.roots)
      ?? estimates.get(block.primary.dataset.reconcileShape ?? "")
      ?? DEFAULT_BLOCK_ESTIMATE_PX,
    writeScrollTop: writeTranscriptScrollTop,
    resolvePrimaryByKey: () => null,
    currentState,
    nextState,
    reserveOwnerReservations: reserveFileChanges,
    validateOwnerReservations: validateOwners,
    commitOwnerReservationsNoFail: () => {
      commitFileChangeCardReservationsNoFail(reservations);
    },
    releaseOwnerReservations: () => {
      releaseFileChangeCardReservations(reservations);
    },
  });
    return result.status === "applied" ? nextState : null;
}


function applyTranscriptWindowReplan(
  state: TranscriptWindowState,
  snapshot: TranscriptSnapshot,
  descriptors: TranscriptWindowState["descriptors"],
  interactionGeneration: number,
  preserveAnchor = true,
  viewportClientHeight = transcriptEl.clientHeight,
): boolean {

  const { syntheticBlocks, claimByKey } = claimCommittedStreamsForDescriptors(descriptors);
  const collected = collectExistingTranscriptBlocksSafely(transcriptEl, state.descriptors, syntheticBlocks);
  if (collected.status !== "collected") return false;
  const existingBlocks = collected.index.byKey;
  const pendingTokens = peekPendingLocalMessageTokens(state.threadId);
  const promptToken = peekConversationPromptToken(state.threadId);
  const liveOwnerTokens = peekTranscriptLiveOwners();
  const validateExternalOwners = (): boolean => (
    validatePendingLocalMessageTokens(state.threadId, pendingTokens)
    && (promptToken
      ? validateConversationPromptToken(promptToken)
      : peekConversationPromptToken(state.threadId) === null)
    && validateTranscriptLiveOwnerTokens(liveOwnerTokens)
  );
  if (!validateExternalOwners()) return false;

  const retained = collected.index.entries.flatMap((entry, entryIndex) => {
    if (entry.kind !== "retained") return [];
    let previousKey: string | null = null;
    let nextKey: string | null = null;
    for (let index = entryIndex - 1; index >= 0; index -= 1) {
      const candidate = collected.index.entries[index];
      if (candidate.kind === "block") {
        previousKey = candidate.key;
        break;
      }
    }
    for (let index = entryIndex + 1; index < collected.index.entries.length; index += 1) {
      const candidate = collected.index.entries[index];
      if (candidate.kind === "block") {
        nextKey = candidate.key;
        break;
      }
    }
    const insertion: TranscriptExternalInsertion = previousKey
      ? { afterKey: previousKey }
      : nextKey
        ? { beforeKey: nextKey }
        : { edge: "after-all" };
    const rect = entry.root.getBoundingClientRect();
    const extentPx = Number.isFinite(rect.height) && rect.height > 0
      ? rect.height
      : DEFAULT_BLOCK_ESTIMATE_PX;
    return [{
      id: `external:${entryIndex}`,
      element: entry.root,
      insertion,
      extentPx,
    }];
  });
  const candidateKeys = new Set(descriptors.map((descriptor) => descriptor.key));
  const following = isTranscriptFollowing();
  const transcriptRect = transcriptEl.getBoundingClientRect();
  const pendingRoots = new Set(pendingTokens.map((token) => token.element));
  const anchor = preserveAnchor
    ? selectTranscriptWindowAnchor({
        blocks: collected.index.blocks.filter((block) => candidateKeys.has(block.key)),
        viewportTop: transcriptRect.top,
        viewportBottom: transcriptRect.top + transcriptEl.clientHeight,
        following,
        pendingRoots,
      })
    : null;
  const ownerPinnedKeys = new Set(
    [...state.pinnedKeys].filter((key) => candidateKeys.has(key)),
  );
  const pinnedKeys = new Set(ownerPinnedKeys);
  if (anchor) pinnedKeys.add(anchor.key);
  for (const key of claimByKey.keys()) pinnedKeys.add(key);
  const descriptorIndices = new Map(
    descriptors.map((descriptor, index) => [descriptor.key, index]),
  );
  const externalByBoundary = new Map<number, typeof retained>();
  for (const external of retained) {
    let boundary: number;
    if ("afterKey" in external.insertion) {
      const index = descriptorIndices.get(external.insertion.afterKey);
      if (index === undefined) return false;
      boundary = index + 1;
    } else if ("beforeKey" in external.insertion) {
      const index = descriptorIndices.get(external.insertion.beforeKey);
      if (index === undefined) return false;
      boundary = index;
    } else {
      boundary = external.insertion.edge === "before-all" ? 0 : descriptors.length;
    }
    const bucket = externalByBoundary.get(boundary) ?? [];
    bucket.push(external);
    externalByBoundary.set(boundary, bucket);
  }
  const entries = descriptors.flatMap((descriptor, descriptorIndex) => [
    ...(externalByBoundary.get(descriptorIndex) ?? []).map((external) => ({
      kind: "external" as const,
      id: external.id,
      element: external.element,
      extentPx: external.extentPx,
      insertion: external.insertion,
    })),
    {
      kind: "canonical" as const,
      key: descriptor.key,
      descriptorIndex,
      extentPx: state.heights.get(descriptor.key)
        ?? state.estimates.get(descriptor.rendererShapeVersion)
        ?? DEFAULT_BLOCK_ESTIMATE_PX,
    },
  ]);
  entries.push(...(externalByBoundary.get(descriptors.length) ?? []).map((external) => ({
    kind: "external" as const,
    id: external.id,
    element: external.element,
    extentPx: external.extentPx,
    insertion: external.insertion,
  })));
  const planned = planTranscriptDomWindow({
    entries,
    attachedKeys: new Set(
      [...existingBlocks.keys()].filter((key) => candidateKeys.has(key)),
    ),
    pinnedKeys,
    rowGapPx: transcriptRowGapPx(),
    budget: DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET,
    viewport: {
      scrollTop: transcriptEl.scrollTop,
      clientHeight: viewportClientHeight,
      following,
    },
    activation: "replan",
    anchorKey: anchor?.key ?? null,
  });
  const changedAttachedKeys = descriptors
    .filter((descriptor) => {
      const existing = existingBlocks.get(descriptor.key);
      return existing !== undefined
        && planned.nextAttachedKeys.has(descriptor.key)
        && existing.fingerprint !== descriptor.fingerprint;
    })
    .map((descriptor) => descriptor.key);
  const removedCanonicalKeys = [...existingBlocks.keys()]
    .filter((key) => !candidateKeys.has(key));
  const plan = {
    ...planned,
    materializeKeys: [...new Set([...planned.materializeKeys, ...changedAttachedKeys])],
    trimKeys: [...new Set([
      ...planned.trimKeys,
      ...changedAttachedKeys,
      ...removedCanonicalKeys,
    ])],
    };

    const hasNoPlanChanges = plan.materializeKeys.length === 0
        && plan.trimKeys.length === 0
        && changedAttachedKeys.length === 0
        && removedCanonicalKeys.length === 0
        && claimByKey.size === 0
        && state.snapshot === snapshot
        && state.descriptors === descriptors;

    if (hasNoPlanChanges) {
        return true;
    }

  let detached: ReturnType<typeof renderTranscriptBlocksDetached>;
  try {
    const materialize = new Set(plan.materializeKeys);
    detached = renderTranscriptBlocksDetached(
      descriptors.filter((descriptor) => materialize.has(descriptor.key)),
    );
  } catch {
    return false;
  }
  const fileChanges = new Map<string, {
    expected: ReturnType<typeof peekFileChangeCard>;
    next: FileChangeCardReservation["next"];
  }>();
  for (const key of plan.trimKeys) {
    const block = existingBlocks.get(key);
    for (const fileKey of block?.ownedFileChangeKeys ?? []) {
      fileChanges.set(fileKey, { expected: peekFileChangeCard(fileKey), next: null });
    }
  }
  for (const [key, next] of detached.stagedFileChangeStates) {
    fileChanges.set(key, { expected: peekFileChangeCard(key), next });
  }
  const fileReservations: FileChangeCardReservation[] = [];
  const reserveFileChanges = (): boolean => {
    for (const [key, change] of fileChanges) {
      const reservation = reserveFileChangeCard(key, change.expected, change.next);
      if (!reservation) return false;
      fileReservations.push(reservation);
    }
    return true;
  };
  const streamReservations: CommittedStreamReservation[] = [];
  const claimedAttachedBlocks = [...claimByKey.entries()].flatMap(([key, claim]) => {
    if (!plan.nextAttachedKeys.has(key)) return [];
    const block = existingBlocks.get(key);
    return block && block.primary === claim.element ? [block] : [];
  });
  const reserveCommittedStreams = (): boolean => {
    for (const block of claimedAttachedBlocks) {
      const reservation = reserveCommittedStream(claimByKey.get(block.key)!);
      if (!reservation) return false;
      streamReservations.push(reservation);
    }
    return true;
  };
  const validateOwners = (): boolean => (
    validateExternalOwners()
    && validateFileChangeCardReservations(fileReservations)
    && validateCommittedStreamReservations(streamReservations)
  );
  const promotedExistingKeys = new Set(claimedAttachedBlocks.map((block) => block.key));
  const promotedMetadata = new Map<HTMLElement, Map<string, string | null>>();
  const metadataAttributes = [
    "data-reconcile-key",
    "data-reconcile-fingerprint",
    "data-reconcile-shape",
    "data-reconcile-root-count",
    "data-reconcile-member-node-ids",
    "data-reconcile-tool-call-ids",
    "data-reconcile-file-change-keys",
    "data-reconcile-turn-id",
  ];
  for (const block of claimedAttachedBlocks) {
    promotedMetadata.set(block.primary, new Map(
      metadataAttributes.map((name) => [name, block.primary.getAttribute(name)]),
    ));
  }
  const promoteExistingBlocks = (): void => {
    for (const block of claimedAttachedBlocks) stampTranscriptBlockOwnership(block);
  };
  const rollbackPromotedBlocks = (): void => {
    for (const [element, metadata] of promotedMetadata) {
      for (const [name, value] of metadata) {
        if (value === null) element.removeAttribute(name);
        else element.setAttribute(name, value);
      }
    }
  };
  for (const block of detached.blocks) {
    for (const root of block.roots) root.remove();
  }
  const stagedBlocks = new Map(detached.blocks.map((block) => [block.key, block]));
  if (stagedBlocks.size !== plan.materializeKeys.length) return false;

  const spacers = plan.spacerSegments.map((segment) => ({
    element: createTranscriptWindowSpacer(segment),
    segment,
  }));
  const spacerByStart = new Map(spacers.map((spacer) => [spacer.segment.startIndex, spacer]));
  const nextChildren: Element[] = [];
  for (let index = 0; index <= descriptors.length;) {
    for (const external of externalByBoundary.get(index) ?? []) {
      nextChildren.push(external.element);
    }
    if (index === descriptors.length) break;
    const spacer = spacerByStart.get(index);
    if (spacer) {
      nextChildren.push(spacer.element);
      index = spacer.segment.endIndex;
      continue;
    }
    const descriptor = descriptors[index];
    const block = stagedBlocks.get(descriptor.key) ?? existingBlocks.get(descriptor.key);
    if (plan.nextAttachedKeys.has(descriptor.key) && block) nextChildren.push(...block.roots);
    index += 1;
  }

  const nextState: TranscriptWindowState = {
    ...state,
    snapshot,
    descriptors,
    attachedKeys: new Set(plan.nextAttachedKeys),
    pinnedKeys: ownerPinnedKeys,
    spacerSegments: [...plan.spacerSegments],
    generation: state.generation + 1,
    loadingEarlier: false,
    loading: state.loading,
    heights: new Map(
      [...state.heights].filter(([key]) => candidateKeys.has(key)),
    ),
    estimates: new Map(state.estimates),
  };
  const rootTop = transcriptEl.getBoundingClientRect().top;
  const anchorJournal = anchor ? {
    key: anchor.key,
    oldRoot: anchor.primary,
    offsetFromViewportTop: anchor.primary.getBoundingClientRect().top - rootTop,
    scrollTop: transcriptEl.scrollTop,
    scrollHeight: transcriptEl.scrollHeight,
    interactionGeneration,
  } : null;
  const result = applyTranscriptDomWindowTransaction({
    root: transcriptEl,
    sourceChildren: Array.from(transcriptEl.children),
    externalSourceChildren: new Set(retained.map((external) => external.element)),
    nextChildren,
    expectedNextChildren: [...nextChildren],
    existingBlocks,
    promotedExistingKeys,
    promoteExistingBlocks,
    rollbackPromotedBlocks,
    plan,
    stagedBlocks,
    spacers,
    anchorJournal,
    expectedInteractionGeneration: interactionGeneration,
    expectedWindowGeneration: state.generation,
    validateInteractionGeneration: (generation) => (
      generation === interactionGeneration
      && getTranscriptInteractionGeneration() === interactionGeneration
    ),
    validateWindowGeneration: (generation) => (
      generation === state.generation
      && transcriptWindows.get(state.threadId) === state
    ),
    measureBlock: (block) => measureTranscriptLogicalBlockExtent(block.roots)
      ?? state.estimates.get(block.primary.dataset.reconcileShape ?? "")
      ?? DEFAULT_BLOCK_ESTIMATE_PX,
    writeScrollTop: (value) => {
      writeTranscriptScrollTop(value);
    },
    resolvePrimaryByKey: (key) => (
      Array.from(transcriptEl.children).find(
        (child) => (child as HTMLElement).dataset.reconcileKey === key,
      ) as HTMLElement | undefined ?? null
    ),
    currentState: state,
    nextState,
    ownerPinnedKeys,
    reserveOwnerReservations: () => (
      reserveFileChanges() && reserveCommittedStreams()
    ),
    validateOwnerReservations: validateOwners,
    commitOwnerReservationsNoFail: () => {
      for (const block of claimedAttachedBlocks) stampTranscriptBlockOwnership(block);
      commitCommittedStreamReservationsNoFail(streamReservations);
      commitFileChangeCardReservationsNoFail(fileReservations);
    },
    releaseOwnerReservations: () => {
      releaseCommittedStreamReservations(streamReservations);
      releaseFileChangeCardReservations(fileReservations);
    },
  });
  if (result.status !== "applied") return false;
    transcriptWindows.set(state.threadId, nextState);
  return true;
}


function applyEarlierPageToTranscriptWindow(
  state: TranscriptWindowState,
  page: TranscriptSnapshot,
  interactionGeneration: number,
): boolean {
  const existingIds = new Set(state.snapshot.nodes.map((node) => node.id));
  const pageNodes = historicalTranscriptPageNodes(page.nodes || [], existingIds);
  const merge = mergeCanonicalTranscript(
    state.snapshot,
    { ...page, nodes: pageNodes },
    "earlier-page",
  );
  return merge.status === "merged"
    && applyTranscriptWindowReplan(
      state,
      merge.snapshot,
      merge.descriptors,
      interactionGeneration,
    );
}

function loadEarlierTranscriptPage(): void {
  const threadId = uiState.sessionId;
  if (!threadId || uiState.isSwitchingThread) return;
  const state = transcriptWindows.get(threadId);
  if (!state || state.loading || !state.snapshot.has_earlier) return;
  const beforeCursor = state.snapshot.before_cursor;
  const usesOpaqueCursor = typeof beforeCursor === "string";
  if (!usesOpaqueCursor && typeof state.snapshot.before_turn_id !== "number") return;

  state.loading = true;
  const contextGeneration = threadContextGeneration;
  const interactionGeneration = getTranscriptInteractionGeneration();
  const pageParams = usesOpaqueCursor
    ? { thread_id: threadId, before_cursor: beforeCursor, turn_limit: TRANSCRIPT_PAGE_SIZE }
    : {
        thread_id: threadId,
        before_turn_id: state.snapshot.before_turn_id,
        turn_limit: TRANSCRIPT_PAGE_SIZE,
      };
  void rpcCall("transcript.page", pageParams)
    .then((result: unknown) => {
      if (
        uiState.sessionId !== threadId ||
        uiState.isSwitchingThread ||
        threadContextGeneration !== contextGeneration
      ) return;
      if (!result || typeof result !== "object") return;
      const page = result as TranscriptSnapshot;
      if (!Array.isArray(page.nodes) || page.thread_id !== threadId) return;
      const current = transcriptWindows.get(threadId);
      if (!current || current !== state) return;
      const requiresEpochMatch = usesOpaqueCursor
        || typeof current.snapshot.transcript_epoch === "string";
      if (requiresEpochMatch && (
        typeof current.snapshot.transcript_epoch !== "string"
        || typeof page.transcript_epoch !== "string"
        || page.transcript_epoch !== current.snapshot.transcript_epoch
      )) return;

      if (!prepareTranscriptForSynchronousPrepend(interactionGeneration)) return;
      if (!applyEarlierPageToTranscriptWindow(current, page, interactionGeneration)) return;
      const next = transcriptWindows.get(threadId);
      if (next && next !== state) next.loading = false;
      syncEmptyState();
    })
    .catch((error: unknown) => {
      console.warn("voidx: transcript page failed", error);
    })
    .finally(() => {
      state.loading = false;
      const current = transcriptWindows.get(threadId);
      if (current === state) current.loading = false;
    });
}

function shouldReplanTranscriptWindow(
    state: TranscriptWindowState,
    scrollTop: number,
    clientHeight: number,
): boolean {
    if (state.attachedKeys.size === 0) return true;
    if (state.spacerSegments.length === 0) {
        return state.attachedKeys.size < state.descriptors.length;
    }

    const threshold = Math.max(clientHeight, 300);
    const viewportStart = scrollTop - threshold;
    const viewportEnd = scrollTop + clientHeight + threshold;

    for (const spacer of state.spacerSegments) {
        if (viewportEnd > spacer.canonicalStartPx && viewportStart < spacer.canonicalEndPx) {
            return true;
        }
    }

    return false;
}

let transcriptScrollReplanQueued = false;
let transcriptScrollWasAtTop = false;

function handleTranscriptScroll(): void {
    const currentScrollTop = transcriptEl.scrollTop;
    if (programmaticScrollTopTarget !== null) {
        const isTargetScroll = Math.abs(currentScrollTop - programmaticScrollTopTarget) < 1.0;
        programmaticScrollTopTarget = null;
        if (programmaticScrollTimeout !== null) {
            clearTimeout(programmaticScrollTimeout);
            programmaticScrollTimeout = null;
        }
        if (isTargetScroll) return;
    }
    const threadId = uiState.sessionId;
    const contextGeneration = threadContextGeneration;
    if (currentScrollTop <= 24) {
        transcriptScrollWasAtTop = true;
    }
    if (transcriptScrollReplanQueued) return;
    transcriptScrollReplanQueued = true;
    queueMicrotask(() => {
        transcriptScrollReplanQueued = false;
        const wasAtTopBeforeReplan = transcriptScrollWasAtTop;
        transcriptScrollWasAtTop = false;
        if (!threadId
            || uiState.sessionId !== threadId
            || uiState.isSwitchingThread
            || threadContextGeneration !== contextGeneration) return;
        const state = transcriptWindows.get(threadId);
        if (!state) return;
        const interactionGeneration = getTranscriptInteractionGeneration();
        const shouldReplan = shouldReplanTranscriptWindow(
            state,
            transcriptEl.scrollTop,
            transcriptEl.clientHeight,
        );
        if (shouldReplan) {
            applyTranscriptWindowReplan(
                state,
                state.snapshot,
                state.descriptors,
                interactionGeneration,
            );
        }
        if (wasAtTopBeforeReplan || transcriptEl.scrollTop <= 24) loadEarlierTranscriptPage();
  });
}

transcriptEl.addEventListener("scroll", handleTranscriptScroll);

let lastTranscriptClientWidth = 0;
let lastTranscriptClientHeight = 0;

function handleTranscriptResize(): void {
    const width = transcriptEl.clientWidth;
    const height = transcriptEl.clientHeight;
    if (width === lastTranscriptClientWidth && height === lastTranscriptClientHeight) return;
    lastTranscriptClientWidth = width;
    lastTranscriptClientHeight = height;
  const threadId = uiState.sessionId;
  const contextGeneration = threadContextGeneration;
  queueMicrotask(() => {
    if (!threadId
      || uiState.sessionId !== threadId
      || uiState.isSwitchingThread
      || threadContextGeneration !== contextGeneration) return;
    const state = transcriptWindows.get(threadId);
    if (!state) return;
    applyTranscriptWindowReplan(
      state,
      state.snapshot,
      state.descriptors,
      getTranscriptInteractionGeneration(),
    );
  });
}

if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => handleTranscriptResize()).observe(transcriptEl);
}

export function _notifyTranscriptResizeForTest(): void {
  handleTranscriptResize();
}

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

function scheduleBlockedRecoveryRetry(
  threadId: string,
  state: SnapshotRecoveryState,
): void {
  state.retryAttempt += 1;
  state.timerGeneration += 1;
  const timerGeneration = state.timerGeneration;
  const generation = socketGeneration;
  const delay = Math.min(250 * 2 ** (state.retryAttempt - 1), 4000);
  setTimeout(() => {
    if (generation !== socketGeneration || uiState.sessionId !== threadId) return;
    const current = snapshotRecoveryStates.get(threadId);
    if (
      current !== state
      || current.mode !== "blocked"
      || current.timerGeneration !== timerGeneration
      || !isRpcConnected()
    ) return;
    current.inFlight = false;
    rpcNotify("snapshot.requested", { thread_id: threadId });
    current.inFlight = true;
    scheduleBlockedRecoveryRetry(threadId, current);
  }, delay);
}

function requestSnapshotRecovery(
  threadId?: unknown,
  options: { blocked?: boolean } = {},
): void {
  const targetThreadId = typeof threadId === "string" && threadId
    ? threadId
    : uiState.sessionId;
  if (!targetThreadId) return;
  let state = snapshotRecoveryStates.get(targetThreadId);
  if (!state) {
    state = {
      mode: options.blocked ? "blocked" : "ordinary-gap",
      requestedRevision: lastSnapshotRevisionByThread.get(targetThreadId) ?? null,
      inFlight: false,
      retryAttempt: 0,
      timerGeneration: 0,
    };
    snapshotRecoveryStates.set(targetThreadId, state);
  } else if (options.blocked && state.mode !== "blocked") {
    state.mode = "blocked";
    state.retryAttempt = 0;
    state.timerGeneration += 1;
    state.inFlight = false;
  }
  if (options.blocked && state.mode === "blocked") state.inFlight = false;
  if (!state.inFlight && isRpcConnected()) {
    rpcNotify("snapshot.requested", { thread_id: targetThreadId });
    state.inFlight = true;
  }
  if (options.blocked && state.mode === "blocked" && state.retryAttempt === 0) {
    scheduleBlockedRecoveryRetry(targetThreadId, state);
  }
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
    snapshotRecoveryStates.has(recoveryThreadId) ||
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
  appendStreamText(itemId, text, phase, "replace");
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
  if (snapshotRecoveryStates.has(identity.threadId)) return false;

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
  appendStreamText(itemId, text, phase, op);
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

function freezeIncrementalStreamForRecovery(state: IncrementalStreamState): void {
    const stream = getOrCreateStream(state.itemId, state.phase);
    stream.committed = true;
    stream.pendingProjectionUpdate = null;
    stream.textEl.querySelector(".stream-cursor")?.remove();
    if (state.phase === "thinking") {
        stream.thinkingEl.hidden = false;
        stream.thinkingBody.textContent = state.text;
    } else if (stream.markdownProjection) {
        stream.markdownProjection.update(state.text, "replace");
    } else {
        stream.textEl.textContent = state.text;
    }
    if (!stream.attached) {
        document.querySelector("#transcript")?.append(stream.el);
        stream.attached = true;
    }
}

type IncrementalCompletionDisposition = "legacy" | "reject" | "commit";

function validateIncrementalStreamCompletion(
    params: Record<string, unknown>,
    data: Record<string, unknown>,
): IncrementalCompletionDisposition {
    const threadId = typeof params.thread_id === "string" ? params.thread_id : "";
    const turnId = typeof params.turn_id === "string" ? params.turn_id : "";
    const itemId = typeof params.item_id === "string" ? params.item_id : "";
    const state = Array.from(incrementalStreamStates.values()).find(
        (candidate) => candidate.threadId === threadId
            && candidate.turnId === turnId
            && candidate.itemId === itemId,
    );
    if (!state) {
        if (!isIncrementalStreamData(data)) return "legacy";
        requestSnapshotRecovery(threadId);
        return "reject";
    }

    const textByteLength = data.text_byte_length;
    const contentHash = data.content_hash;
    if (
        snapshotRecoveryStates.has(threadId)
        || data.stream_id !== state.streamId
        || data.revision !== state.revision
        || !isRevision(textByteLength)
        || typeof contentHash !== "string"
        || !/^[0-9a-f]{64}$/.test(contentHash)
        || new TextEncoder().encode(state.text).length !== textByteLength
        || sha256(state.text) !== contentHash
    ) {
        freezeIncrementalStreamForRecovery(state);
        requestSnapshotRecovery(threadId);
        return "reject";
    }
    return "commit";
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
    transcriptScrollReplanQueued = false;
    transcriptScrollWasAtTop = false;
    programmaticScrollTopTarget = null;
    if (programmaticScrollTimeout !== null) {
        clearTimeout(programmaticScrollTimeout);
        programmaticScrollTimeout = null;
    }
    lastTranscriptClientWidth = 0;
    lastTranscriptClientHeight = 0;
  if (threadId !== uiState.sessionId) {
    if (uiState.sessionId) {
      retireThreadTurn(uiState.sessionId);
      clearIncrementalStreamStatesForThread(uiState.sessionId);
      snapshotRecoveryStates.delete(uiState.sessionId);
      transcriptWindows.delete(uiState.sessionId);
    }
    threadContextGeneration += 1;
    clearCommittedStreams();
    clearActiveStreams();
    resetTranscriptViewport();
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
    terminateActiveTerminal();
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

    onThreadFork((threadId: string) => {
        rpcCall("session.fork", { thread_id: threadId })
            .then((res: unknown) => {
                const info = res as { thread_id: string };
                if (info?.thread_id) {
                    void switchThread(info.thread_id);
                }
            })
            .catch((err: Error) => {
                showSessionError("会话分叉", err);
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

interface BlockedSnapshotPrebuilt {
  fragment: DocumentFragment;
  fileCaches: ReturnType<typeof prepareBlockedFileToolCacheState>;
  pendingMessages: PendingLocalMessage[];
  knownUserIds: Map<string, Set<string>>;
  renderedIds: Map<string, Set<string>>;
  incrementalStates: Map<string, IncrementalStreamState>;
  workspaceRevision: number;
  snapshotRevision: number;
  threadId: string;
  recoveryState: SnapshotRecoveryState;
  windowState: TranscriptWindowState;
}

function publishBlockedFullSnapshotNoFail(prebuilt: BlockedSnapshotPrebuilt): void {
  transcriptEl.replaceChildren(prebuilt.fragment);
  publishBlockedFileToolCachesNoFail(prebuilt.fileCaches);
  if (pendingLocalMessages !== prebuilt.pendingMessages) pendingLocalGeneration += 1;
  pendingLocalMessages = prebuilt.pendingMessages;
  knownSnapshotUserNodeIds = prebuilt.knownUserIds;
  renderedItemIdsByThread = prebuilt.renderedIds;
  incrementalStreamStates = prebuilt.incrementalStates;
  lastWorkspaceRevision = prebuilt.workspaceRevision;
  lastSnapshotRevisionByThread.set(prebuilt.threadId, prebuilt.snapshotRevision);
  transcriptWindows.set(prebuilt.threadId, prebuilt.windowState);
  prebuilt.recoveryState.timerGeneration += 1;
  snapshotRecoveryStates.delete(prebuilt.threadId);
}

function installBlockedFullSnapshot(
  threadId: string,
  params: Record<string, unknown>,
  snapshot: TranscriptSnapshot,
): boolean {
  const recoveryState = snapshotRecoveryStates.get(threadId);
  const incomingWorkspaceRevision = workspaceRevision(params);
  const incomingSnapshotRevision = snapshotRevision(params);
  if (
    !recoveryState
    || recoveryState.mode !== "blocked"
    || snapshot.windowed === true
    || incomingWorkspaceRevision === null
    || incomingSnapshotRevision === null
    || (recoveryState.requestedRevision !== null
      && incomingSnapshotRevision < recoveryState.requestedRevision)
  ) return false;

  const descriptors = buildTranscriptDescriptors(snapshot.nodes || []);
  const detached = renderTranscriptBlocksDetached(descriptors);
  const handoffs = pendingLocalHandoffs(threadId, snapshot);
  const handedOffElements = new Set(handoffs.values());
  const nextPending = pendingLocalMessages.filter((pending) => {
    if (pending.threadId !== threadId) return true;
    const element = pendingLocalMessageElement(pending.itemId);
    return !element || !handedOffElements.has(element);
  });
  for (const pending of nextPending) {
    if (pending.threadId !== threadId) continue;
    const current = pendingLocalMessageElement(pending.itemId);
    if (current) detached.fragment.append(current.cloneNode(true));
  }
  const nextKnown = new Map(knownSnapshotUserNodeIds);
  nextKnown.set(threadId, new Set(snapshotUserEntries(snapshot).map((entry) => entry.id)));
  const nextRendered = new Map(renderedItemIdsByThread);
  nextRendered.set(threadId, new Set((snapshot.nodes || []).map((node) => node.id)));
  const nextIncremental = new Map(
    [...incrementalStreamStates].filter(([, state]) => state.threadId !== threadId),
  );
  const estimates = new Map<string, number>();
  for (const descriptor of descriptors) {
    estimates.set(descriptor.rendererShapeVersion, DEFAULT_BLOCK_ESTIMATE_PX);
  }
  const previousWindowGeneration = transcriptWindows.get(threadId)?.generation ?? 0;
  const windowState: TranscriptWindowState = {
    threadId,
    snapshot,
    descriptors,
    heights: new Map(),
    estimates,
    attachedKeys: new Set(detached.blocks.map((block) => block.key)),
    pinnedKeys: new Set(),
    spacerSegments: [],
    generation: previousWindowGeneration + 1,
    loadingEarlier: false,
    loading: false,
  };
  const prebuilt: BlockedSnapshotPrebuilt = {
    fragment: detached.fragment,
    fileCaches: prepareBlockedFileToolCacheState(detached.context.fileChanges.cards),
    pendingMessages: nextPending,
    knownUserIds: nextKnown,
    renderedIds: nextRendered,
    incrementalStates: nextIncremental,
    workspaceRevision: incomingWorkspaceRevision,
    snapshotRevision: incomingSnapshotRevision,
    threadId,
    recoveryState,
    windowState,
  };

  quiesceStreamsForBlockedInstallNoCallback();
  quiesceFileToolCachesForBlockedInstallNoDom();
  const viewportProof: BlockedViewportQuiesceToken | null =
    quiesceTranscriptViewportForBlockedInstallNoDom();
  const promptProof = quiesceConversationPromptForBlockedInstallNoDom();
  if (
    !viewportProof
    || !validateTranscriptViewportBlockedInstallReady(viewportProof)
    || !validateBlockedPromptQuiesceToken(promptProof)
    || uiState.sessionId !== threadId
    || snapshotRecoveryStates.get(threadId) !== recoveryState
  ) return false;

  try {
    publishBlockedFullSnapshotNoFail(prebuilt);
    queueMicrotask(() => {
      if (uiState.sessionId !== threadId
        || uiState.isSwitchingThread
        || transcriptWindows.get(threadId) !== windowState) return;
      applyTranscriptWindowReplan(
        windowState,
        windowState.snapshot,
        windowState.descriptors,
        getTranscriptInteractionGeneration(),
        true,
        Math.max(transcriptEl.clientHeight, DEFAULT_BLOCK_ESTIMATE_PX),
      );
    });
    return true;
  } catch {
    recoveryState.inFlight = false;
    if (recoveryState.retryAttempt === 0) scheduleBlockedRecoveryRetry(threadId, recoveryState);
    return false;
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
    recoveryThreadId && snapshotRecoveryStates.has(recoveryThreadId),
  );
  if (revision !== null && revision < lastWorkspaceRevision) return;
  const snapshot = params.active_snapshot || { nodes: [] };
  const snapshotData = snapshot && typeof snapshot === "object"
    ? snapshot as Record<string, unknown>
    : null;
  const authoritativeRecoverySnapshot = Boolean(
    snapshotData
    && snapshotData.windowed !== true
    && snapshotRevision(params) !== null,
  );
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
  const typedSnapshot = snapshot as TranscriptSnapshot;
  const currentWindowState = transcriptWindows.get(activeThreadId) ?? null;
  const canonicalMerge = currentWindowState
    ? mergeCanonicalTranscript(
        currentWindowState.snapshot,
        typedSnapshot,
        typedSnapshot.windowed === true ? "windowed" : "full",
      )
    : null;
  if (canonicalMerge?.status === "stale") {
    requestSnapshotRecovery(activeThreadId, {
      blocked: canonicalMerge.recovery === "blocked",
    });
    return;
  }
  const canonicalSnapshot = canonicalMerge?.status === "merged"
    ? canonicalMerge.snapshot
    : typedSnapshot;
  const canonicalDescriptors = canonicalMerge?.status === "merged"
    ? canonicalMerge.descriptors
    : null;
  const blockedRecovery = snapshotRecoveryStates.get(activeThreadId)?.mode === "blocked";
  if (blockedRecovery && authoritativeRecoverySnapshot) {
    if (installBlockedFullSnapshot(activeThreadId, params, typedSnapshot)) {
      syncEmptyState();
      scrollToBottom();
    }
    return;
  }
  const subsequentWindowApplied = currentWindowState !== null
    && canonicalDescriptors !== null
    ? applyTranscriptWindowReplan(
        currentWindowState,
        canonicalSnapshot,
        canonicalDescriptors,
        getTranscriptInteractionGeneration(),
      )
    : false;
  if (currentWindowState && !subsequentWindowApplied) {
    requestSnapshotRecovery(activeThreadId, { blocked: typedSnapshot.windowed !== true });
    return;
  }
  const initialWindowState = typedSnapshot.windowed === true
    && !transcriptWindows.has(activeThreadId)
    ? installInitialTranscriptDomWindow(activeThreadId, typedSnapshot)
    : null;
  if (!initialWindowState && !subsequentWindowApplied) {
    const renderResult = renderTranscript(transcriptEl, typedSnapshot, {
      pendingLocalHandoffs: pendingLocalHandoffs(activeThreadId, typedSnapshot),
    });
    if (renderResult.status !== "applied") {
      requestSnapshotRecovery(activeThreadId, { blocked: true });
      return;
    }
  }

  if (revision !== null) lastWorkspaceRevision = revision;
  if (activeThreadId) rememberSnapshotRevision(activeThreadId, params);
  if (typedSnapshot.windowed || subsequentWindowApplied) {
    if (initialWindowState) {
      transcriptWindows.set(activeThreadId, initialWindowState);
    } else if (!subsequentWindowApplied) {
      const descriptors = canonicalDescriptors
        ?? buildTranscriptDescriptors(canonicalSnapshot.nodes || []);
      const collected = collectExistingTranscriptBlocksSafely(transcriptEl, descriptors);
      if (collected.status !== "collected") {
        requestSnapshotRecovery(activeThreadId);
        return;
      }
      const previous = currentWindowState;
      const fallbackState: TranscriptWindowState = {
        threadId: activeThreadId,
        snapshot: canonicalSnapshot,
        descriptors,
        heights: new Map(previous?.heights),
        estimates: new Map(previous?.estimates),
        attachedKeys: new Set(collected.index.byKey.keys()),
        pinnedKeys: new Set(previous?.pinnedKeys),
        spacerSegments: collected.spacers.map((spacer) => spacer.segment),
        generation: (previous?.generation ?? 0) + 1,
        loadingEarlier: false,
        loading: false,
      };
      transcriptWindows.set(activeThreadId, fallbackState);
      queueMicrotask(() => {
        if (uiState.sessionId !== activeThreadId
          || uiState.isSwitchingThread
          || transcriptWindows.get(activeThreadId) !== fallbackState) return;
        applyTranscriptWindowReplan(
          fallbackState,
          fallbackState.snapshot,
          fallbackState.descriptors,
          getTranscriptInteractionGeneration(),
          true,
          Math.max(transcriptEl.clientHeight, DEFAULT_BLOCK_ESTIMATE_PX),
        );
      });
    }
  } else {
    transcriptWindows.delete(activeThreadId);
  }
  if (recovering) {
    clearIncrementalStreamStatesForThread(recoveryThreadId);
    renderedItemIdsByThread.delete(recoveryThreadId);
  }
  if (authoritativeRecoverySnapshot) {
    snapshotRecoveryStates.delete(activeThreadId);
    snapshotRecoveryStates.delete(uiState.sessionId);
  }
  restorePendingLocalMessages(activeThreadId, typedSnapshot);
  syncEmptyState();
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
    syncEmptyState(
      method === "item.started" && params.kind === "assistant_stream",
    );
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
        const completionDisposition = validateIncrementalStreamCompletion(params, data);
        if (completionDisposition === "reject") return;
        const result = commitStream(itemId);
        if (completionDisposition === "commit") clearIncrementalStreamItem(params);
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
  forceTranscriptScrollToBottom();
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
    const isContinueCommand = text.trim() === "/continue";
    if (uiState.isRunning && isContinueCommand) {
        inputEl.value = "";
        hideSlashMenu();
        hideRefMenu();
        return;
    }

    const isGuidance = uiState.isRunning;
    const style = isGuidance ? "guidance" : "text";
    const itemId = createLocalItemId(isGuidance ? "guidance" : "user");
    if (!isGuidance) {
        if (!isContinueCommand) {
            rememberPendingLocalMessage(threadId, itemId, text, style);
            appendMessageItem(itemId, { style, text });
            syncEmptyState();
        }
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
        if (!isContinueCommand) {
            removePendingLocalMessage(itemId);
        }
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
  snapshotRecoveryStates.clear();
  incrementalStreamStates.clear();
  renderedItemIdsByThread.clear();
  threadTurnContexts.clear();
  transcriptWindows.clear();
  clearCommittedStreams();
  clearActiveStreams();
    transcriptScrollReplanQueued = false;
    transcriptScrollWasAtTop = false;
    programmaticScrollTopTarget = null;
    if (programmaticScrollTimeout !== null) {
        clearTimeout(programmaticScrollTimeout);
        programmaticScrollTimeout = null;
    }
    lastTranscriptClientWidth = 0;
    lastTranscriptClientHeight = 0;
  resetTranscriptViewport();
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
