import type { ProfileSummary, SlashCommand } from "../utils/types";
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
  permissionMode: string;
  aiApprovalCount: number;
  reasoningEffort: string;
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
  permissionMode: "safe",
  aiApprovalCount: 0,
  reasoningEffort: "xhigh",
};

export const DEFAULT_WORKSPACE = "voidx";
export const PENDING_MODEL_LABEL = "等待模型状态";
export const DEFAULT_SIDEBAR_WIDTH = 260;
export const MIN_SIDEBAR_WIDTH = 210;
export const MAX_SIDEBAR_WIDTH = 420;

// DOM Cache
export const statusDotEl = document.querySelector("#status-dot")!;
export const statusModelEl = document.querySelector("#status-model");
export const statusWorkspaceEl = document.querySelector("#status-workspace");
export const statusSessionEl = document.querySelector("#status-session")!;
export const statusConnectionEl = document.querySelector("#status-connection")!;
export const statusSessionDetailEl = document.querySelector("#status-session-detail")!;
export const statusWorkspaceDetailEl = document.querySelector("#status-workspace-detail")!;
export const statusProviderModelEl = document.querySelector("#status-provider-model")!;
export const statusPermissionEl = document.querySelector("#status-permission");
export const statusRunningEl = document.querySelector("#status-running")!;
export const stripWorkspaceEl = document.querySelector("#strip-workspace")!;
export const stripPermissionEl = document.querySelector("#strip-permission");
export const stripProviderModelEl = document.querySelector("#strip-provider-model")!;
export const titlebarProjectEl = document.querySelector("#titlebar-project");
export const contextWorkspaceEl = document.querySelector("#context-workspace")!;
export const contextPermissionEl = document.querySelector("#context-permission");
export const contextProviderModelEl = document.querySelector("#context-provider-model")!;
export const emptyStateEl = document.querySelector<HTMLElement>("#empty-state")!;
export const transcriptEl = document.querySelector<HTMLElement>("#transcript")!;
export const mainCanvasEl = document.querySelector<HTMLElement>(".vx-main-canvas")!;
export const composerEl = document.querySelector<HTMLFormElement>("#composer")!;
export const inputEl = document.querySelector<HTMLTextAreaElement>("#input")!;
export const btnSendEl = document.querySelector<HTMLButtonElement>("#btn-send")!;
export const providerSelectEl = document.querySelector<HTMLSelectElement>("#provider-select")!;
export const modelSelectEl = document.querySelector<HTMLSelectElement>("#model-select")!;
export const slashMenuEl = document.querySelector<HTMLElement>("#slash-menu")!;
export const requestDialogEl = document.querySelector<HTMLDialogElement>("#request-dialog")!;
export const requestTitleEl = document.querySelector<HTMLElement>("#request-title")!;
export const requestDetailsEl = document.querySelector<HTMLElement>("#request-details")!;
export const requestControlsEl = document.querySelector<HTMLElement>("#request-controls")!;

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

  const modelPillTextEl = document.querySelector("#model-pill-text");
  if (modelPillTextEl) {
    modelPillTextEl.textContent = uiState.model || PENDING_MODEL_LABEL;
  }

  const reasoningPillTextEl = document.querySelector("#reasoning-pill-text");
  if (reasoningPillTextEl) {
    const REASONING_LEVELS = ["off", "low", "medium", "high", "xhigh"];
    const REASONING_PILL_LABELS = ["关闭", "低", "中", "高", "极"];
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
