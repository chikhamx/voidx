import type { ProfileSummary, SlashCommand } from "../utils/types";
import type { RefCandidate, RefToken } from "../ui/reference";
import type { SettingsSnapshot } from "../ui/settings";
import { iconSvg } from "../utils/icons";

/** 发送按钮图标：待机=向上箭头，运行=停止方块 */
export const sendArrowIcon = iconSvg("arrow-up", 18, 2);
export const sendStopIcon = iconSvg("stop", 16, 1.6);

export interface UiState {
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
  refCandidates: RefCandidate[];
  refSelectedIndex: number;
  refToken: RefToken | null;
  permissionMode: string;
  aiApprovalCount: number;
  reasoningEffort: string;
  usage: UsageSnapshot | null;
}

export interface UsageSnapshot {
  context_tokens: number;
  context_limit: number;
  total_tokens: number;
  cache_hit_rate: number | null;
  cache_hit_rate_estimated?: boolean;
}

function formatTokenCount(value: number): string {
  if (value >= 1000) {
    const scaled = value / 1000;
    const text = scaled >= 100 ? String(Math.round(scaled)) : scaled.toFixed(1);
    return `${text}k`;
  }
  return String(value);
}

export function formatUsageLabel(usage: UsageSnapshot | null): string {
  if (!usage) return "\u2014";
  const ctx = `${formatTokenCount(usage.context_tokens)}/${formatTokenCount(usage.context_limit)} ctx`;
  const total = `${formatTokenCount(usage.total_tokens)} total`;
  if (usage.cache_hit_rate === null || usage.cache_hit_rate === undefined) {
    return `${ctx} \u00b7 ${total}`;
  }
  const pct = Math.round(usage.cache_hit_rate * 100);
  const prefix = usage.cache_hit_rate_estimated ? "~" : "";
  return `${ctx} \u00b7 cache ${prefix}${pct}% \u00b7 ${total}`;
}

export const uiState: UiState = {
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
  refCandidates: [],
  refSelectedIndex: 0,
  refToken: null,
  permissionMode: "safe",
  aiApprovalCount: 0,
  reasoningEffort: "xhigh",
  usage: null,
};

export const DEFAULT_WORKSPACE = "voidx";
export const PENDING_MODEL_LABEL = "等待模型状态";
export const DEFAULT_SIDEBAR_WIDTH = 260;
export const MIN_SIDEBAR_WIDTH = 210;
export const MAX_SIDEBAR_WIDTH = 420;

// DOM Cache (populated by initStateDom on startup)
// DOM Cache
export let statusDotEl: HTMLElement;
export let statusModelEl: HTMLElement | null;
export let statusWorkspaceEl: HTMLElement | null;
export let statusSessionEl: HTMLElement;
export let statusConnectionEl: HTMLElement;
export let statusSessionDetailEl: HTMLElement;
export let statusWorkspaceDetailEl: HTMLElement;
export let statusProviderModelEl: HTMLElement;
export let statusPermissionEl: HTMLElement | null;
export let statusRunningEl: HTMLElement;
export let statusUsageEl: HTMLElement | null;
export let stripWorkspaceEl: HTMLElement;
export let stripPermissionEl: HTMLElement | null;
export let stripProviderModelEl: HTMLElement;
export let titlebarProjectEl: HTMLElement | null;
export let contextWorkspaceEl: HTMLElement;
export let contextPermissionEl: HTMLElement | null;
export let contextProviderModelEl: HTMLElement;
export let emptyStateEl: HTMLElement;
export let transcriptEl: HTMLElement;
export let mainCanvasEl: HTMLElement;
export let composerEl: HTMLFormElement;
export let inputEl: HTMLTextAreaElement;
export let btnSendEl: HTMLButtonElement;
export let providerSelectEl: HTMLSelectElement;
export let modelSelectEl: HTMLSelectElement;
export let slashMenuEl: HTMLElement;
export let refMenuEl: HTMLElement;
export let requestDialogEl: HTMLDialogElement;
export let requestTitleEl: HTMLElement;
export let requestDetailsEl: HTMLElement;
export let requestControlsEl: HTMLElement;

export function initStateDom(): void {
  statusDotEl = document.querySelector<HTMLElement>("#status-dot")!;
  statusModelEl = document.querySelector<HTMLElement>("#status-model");
  statusWorkspaceEl = document.querySelector<HTMLElement>("#status-workspace");
  statusSessionEl = document.querySelector<HTMLElement>("#status-session")!;
  statusConnectionEl = document.querySelector<HTMLElement>("#status-connection")!;
  statusSessionDetailEl = document.querySelector<HTMLElement>("#status-session-detail")!;
  statusWorkspaceDetailEl = document.querySelector<HTMLElement>("#status-workspace-detail")!;
  statusProviderModelEl = document.querySelector<HTMLElement>("#status-provider-model")!;
  statusPermissionEl = document.querySelector<HTMLElement>("#status-permission");
  statusRunningEl = document.querySelector<HTMLElement>("#status-running")!;
  statusUsageEl = document.querySelector<HTMLElement>("#status-usage");
  stripWorkspaceEl = document.querySelector<HTMLElement>("#strip-workspace")!;
  stripPermissionEl = document.querySelector<HTMLElement>("#strip-permission");
  stripProviderModelEl = document.querySelector<HTMLElement>("#strip-provider-model")!;
  titlebarProjectEl = document.querySelector<HTMLElement>("#titlebar-project");
  contextWorkspaceEl = document.querySelector<HTMLElement>("#context-workspace")!;
  contextPermissionEl = document.querySelector<HTMLElement>("#context-permission");
  contextProviderModelEl = document.querySelector<HTMLElement>("#context-provider-model")!;
  emptyStateEl = document.querySelector<HTMLElement>("#empty-state")!;
  transcriptEl = document.querySelector<HTMLElement>("#transcript")!;
  mainCanvasEl = document.querySelector<HTMLElement>(".vx-main-canvas")!;
  composerEl = document.querySelector<HTMLFormElement>("#composer")!;
  inputEl = document.querySelector<HTMLTextAreaElement>("#input")!;
  btnSendEl = document.querySelector<HTMLButtonElement>("#btn-send")!;
  providerSelectEl = document.querySelector<HTMLSelectElement>("#provider-select")!;
  modelSelectEl = document.querySelector<HTMLSelectElement>("#model-select")!;
  slashMenuEl = document.querySelector<HTMLElement>("#slash-menu")!;
  refMenuEl = document.querySelector<HTMLElement>("#ref-menu")!;
  requestDialogEl = document.querySelector<HTMLDialogElement>("#request-dialog")!;
  requestTitleEl = document.querySelector<HTMLElement>("#request-title")!;
  requestDetailsEl = document.querySelector<HTMLElement>("#request-details")!;
  requestControlsEl = document.querySelector<HTMLElement>("#request-controls")!;
}

export function workspaceBasename(workspace: string): string {
  return workspace
    ? workspace.replace(/^.*[\\/]/, "")
    : DEFAULT_WORKSPACE;
}

export function providerModelLabel(): string {
  if (!uiState.provider || !uiState.model) return PENDING_MODEL_LABEL;
  return `${uiState.provider || "custom"}/${uiState.model}`;
}

export function updateStatusBar(): void {
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
  if (statusUsageEl) statusUsageEl.textContent = formatUsageLabel(uiState.usage);

  const modelPillTextEl = document.querySelector("#model-pill-text");
  if (modelPillTextEl) {
    modelPillTextEl.textContent = uiState.model || PENDING_MODEL_LABEL;
  }

  const reasoningPillTextEl = document.querySelector("#reasoning-pill-text");
  if (reasoningPillTextEl) {
    const REASONING_LEVELS = ["none", "low", "medium", "high", "xhigh", "max", "ultra"];
    const REASONING_PILL_LABELS = ["关闭", "低", "中", "高", "极", "最大", "超"];
    const rIdx = REASONING_LEVELS.indexOf(uiState.reasoningEffort || "xhigh");
    reasoningPillTextEl.textContent = rIdx !== -1 ? REASONING_PILL_LABELS[rIdx] : "极";
  }

  const pillEl = document.querySelector("#permission-pill");
  const pillTextEl = document.querySelector("#permission-pill-text");
  if (pillEl && pillTextEl) {
    let text = "安全模式";
    let colorClass = "safe";
    if (uiState.permissionMode === "full_access") {
      text = "完全访问";
      colorClass = "full-access";
    } else if (uiState.permissionMode === "ai_approval") {
      text = uiState.aiApprovalCount > 0 ? `AI 审批 (${uiState.aiApprovalCount})` : "AI 审批";
      colorClass = "ai-approval";
    } else if (uiState.permissionMode === "project_trusted") {
      text = "项目已信任";
      colorClass = "project-trusted";
    } else if (uiState.permissionMode === "read_only") {
      text = "只读模式";
      colorClass = "read-only";
    }
    pillTextEl.textContent = text;
    pillEl.className = `vx-permission-pill ${colorClass}`;
  }
}

export function setConnectionStatus(status: string, message?: string): void {
  uiState.connection = status;
  statusDotEl.className = `status-dot ${status}`;
  if (statusModelEl && message && status === "disconnected") {
    statusModelEl.textContent = message;
  } else if (statusModelEl && status === "connected") {
    statusModelEl.textContent = "";
  }
  updateStatusBar();
}

export function setRunning(running: boolean): void {
  uiState.isRunning = running;
  btnSendEl.classList.toggle("running", running);
  btnSendEl.innerHTML = running ? sendStopIcon : sendArrowIcon;
  btnSendEl.setAttribute("aria-label", running ? "Cancel" : "Send");
  inputEl.disabled = uiState.isSwitchingModel;
  updateStatusBar();
}

export function syncEmptyState(): void {
  if (!emptyStateEl || !transcriptEl) return;
  const isEmpty = transcriptEl.children.length === 0;
  emptyStateEl.hidden = !isEmpty;
  mainCanvasEl?.classList.toggle("empty", isEmpty);
}

export function _resetWorkbenchStateForTest(): void {
  uiState.connection = "disconnected";
  uiState.provider = "";
  uiState.model = "";
  uiState.workspace = "";
  uiState.sessionId = "";
  uiState.isRunning = false;
  uiState.profileConfigured = null;
  uiState.configuredProfiles = [];
  uiState.isSwitchingModel = false;
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
}
