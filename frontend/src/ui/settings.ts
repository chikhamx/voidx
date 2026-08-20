import { getThemePreference, setThemePreference, type ThemePreference } from "./theme";
import { createCustomSelect } from "./custom-select";
import { inputRow, numberRow, rowBase, section } from "./form-rows";
import { openProvidersModal } from "./providers";
import { rpcCall } from "../rpc";
import type { ProfileSummary } from "../utils/types";
export type { ProfileSummary };

export interface SettingsSnapshot {
  model?: Record<string, unknown>;
  profiles?: ProfileSummary[];
  paths?: Record<string, string>;
  permissions?: Record<string, unknown>;
  compaction?: Record<string, unknown>;
  user_profile?: { language?: string; tone?: string };
  code_ide?: string;
  update_check?: { enabled?: boolean; last_checked_at?: number; latest_version?: string };
  [k: string]: unknown;
}

type PermissionMode = "read_only" | "safe" | "ai_approval" | "project_trusted" | "full_access";

interface PermissionModeConfig {
  label: string;
  description: string;
}

const REASONING_EFFORTS = ["", "none", "low", "medium", "high", "xhigh", "max"] as const;

function configuredProfileNames(snapshot: SettingsSnapshot): string[] {
  return (snapshot.profiles || []).filter((profile) => profile.configured).map((profile) => profile.name);
}

function profileOptions(snapshot: SettingsSnapshot): string[] {
  return ["", ...configuredProfileNames(snapshot)];
}

const PERMISSION_MODES: Record<PermissionMode, PermissionModeConfig> = {
  read_only: {
    label: "Read only",
    description: "Ask before writes or risky commands; no session-wide approval.",
  },
  safe: {
    label: "Safe",
    description: "Allow normal reads; ask for risky edits and commands.",
  },
  ai_approval: {
    label: "AI approval",
    description: "Let the selected model pre-screen dangerous calls; uncertain cases still require your approval. Projected arguments are sent to that model.",
  },
  project_trusted: {
    label: "Project trusted",
    description: "Allow routine project edits; ask for dynamic shell, external paths, and higher risks.",
  },
  full_access: {
    label: "Full access",
    description: "Run with full sandbox access while still asking for the highest-risk operations.",
  },
};

type AgentProfileRpc = (method: string, params: Record<string, unknown>) => Promise<unknown>;

interface SettingsState {
  dialog: HTMLDialogElement | null;
  content: HTMLElement | null;
  error: HTMLElement | null;
  save: HTMLButtonElement | null;
  close: HTMLButtonElement | null;
  tabs: HTMLElement | null;
  activeTab: string;
  snapshot: SettingsSnapshot;
  onSave: ((patch: Record<string, unknown>) => Promise<unknown> | void) | null;
  agentProfileRpc: AgentProfileRpc;
  loadedAgentProfile: Record<string, unknown> | null;
  agentProfileTargetGuard: { scope: string; revision: number; contentHash: string; exists: boolean } | null;
}

let state: SettingsState = {
  dialog: null,
  content: null,
  error: null,
  save: null,
  close: null,
  tabs: null,
  activeTab: "model",
  snapshot: {},
  onSave: null,
  agentProfileRpc: rpcCall,
  loadedAgentProfile: null,
  agentProfileTargetGuard: null,
};

export function initSettingsModal({
  onSave,
  agentProfileRpc,
}: {
  onSave?: (patch: Record<string, unknown>) => Promise<unknown> | void;
  agentProfileRpc?: AgentProfileRpc;
} = {}): void {
  state.dialog = document.querySelector("#settings-dialog");
  state.content = document.querySelector("#settings-content");
  state.error = document.querySelector("#settings-error");
  state.save = document.querySelector("#settings-save");
  state.close = document.querySelector("#settings-close");
  state.tabs = document.querySelector("#settings-tabs");
  state.onSave = onSave ?? null;
  state.agentProfileRpc = agentProfileRpc ?? rpcCall;
  state.close?.addEventListener("click", () => closeSettingsModal());
  state.save?.addEventListener("click", () => saveSettingsModal());
  if (state.tabs) {
    state.tabs.querySelectorAll<HTMLElement>(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.activeTab = tab.dataset.tab || "model";
        state.tabs!.querySelectorAll<HTMLElement>(".settings-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        renderActiveTab();
      });
    });
  }
}

export function renderSettingsModal(snapshot: SettingsSnapshot = {}): void {
  state.snapshot = snapshot;
  renderActiveTab();
  setError("");
}

function renderActiveTab() {
  if (!state.content) return;
  switch (state.activeTab) {
    case "model":
      state.content.replaceChildren(renderModelTab(state.snapshot));
      break;
    case "permissions":
      state.content.replaceChildren(renderPermissionsTab(state.snapshot));
      break;
    case "preferences":
      state.content.replaceChildren(renderPreferencesTab(state.snapshot));
      break;
    case "code":
      state.content.replaceChildren(renderCodeTab(state.snapshot));
      break;
    case "advanced":
      state.content.replaceChildren(renderAdvancedTab(state.snapshot));
      break;
    case "agent-profiles":
      state.content.replaceChildren(renderAgentProfilesTab());
      void refreshAgentProfiles();
      break;
  }
}

export async function openSettingsModal(snapshotPromise: Promise<SettingsSnapshot>): Promise<void> {
  if (!state.dialog) return;
  try {
    setError("");
    const snapshot = await snapshotPromise;
    state.activeTab = "model";
    if (state.tabs) {
      state.tabs.querySelectorAll<HTMLElement>(".settings-tab").forEach((t) => t.classList.remove("active"));
      const firstTab = state.tabs.querySelector<HTMLElement>(".settings-tab[data-tab='model']");
      if (firstTab) firstTab.classList.add("active");
    }
    renderSettingsModal(snapshot);
    if (typeof state.dialog.showModal === "function") {
      state.dialog.showModal();
    } else {
      state.dialog.setAttribute("open", "");
    }
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

export function closeSettingsModal() {
  if (!state.dialog) return;
  if (typeof state.dialog.close === "function") {
    state.dialog.close();
  } else {
    state.dialog.removeAttribute("open");
  }
}

export function collectSettingsPatch(): Record<string, unknown> {
  const value = (name: string): string =>
    (state.content?.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)?.value) || "";
  const checked = (name: string): boolean =>
    Boolean(state.content?.querySelector<HTMLInputElement>(`[name="${name}"]`)?.checked);
  switch (state.activeTab) {
    case "model":
      return collectModelPatch(value);
    case "permissions":
      return collectPermissionsPatch(value);
    case "preferences":
      return {
        user_profile: {
          language: value("language"),
          tone: value("tone"),
        },
      };
    case "code":
      return { code_ide: value("code_ide") };
    case "advanced":
      return { update_check: { enabled: checked("update_enabled") } };
    default:
      return {};
  }
}
function collectPermissionsPatch(value: (name: string) => string): Record<string, unknown> {
  const raw = value("permission_mode") || "safe";
  const preset = raw in PERMISSION_MODES ? (raw as PermissionMode) : "safe";
  return { permissions: { permission_mode: preset } };
}

function inferPermissionMode(permissions: Record<string, unknown> = {}): PermissionMode {
  const explicit = permissions.permission_mode;
  if (typeof explicit === "string" && explicit in PERMISSION_MODES) {
    return explicit as PermissionMode;
  }
  return "safe";
}


const DEFAULT_COMPACTION_TIMEOUT_SECONDS = 256;


function collectModelPatch(value: (name: string) => string): Record<string, unknown> {
  const reasoningEffort = value("model_reasoning_effort").trim();

  const patch: Record<string, unknown> = {};
  if (reasoningEffort) patch.model = { reasoning_effort: reasoningEffort };

  patch.compaction = {
    profile_name: value("compaction_profile"),
    reasoning_effort: value("compaction_reasoning_effort") || null,
    timeout_seconds: Number(value("compaction_timeout") || DEFAULT_COMPACTION_TIMEOUT_SECONDS),
  };
  patch.permissions = {
    ai_approval: {
      profile_name: value("ai_approval_profile"),
      timeout_seconds: Number(value("ai_approval_timeout") || 12),
    },
  };
  return patch;
}
export function _resetSettingsForTest() {
  state = {
    dialog: null,
    content: null,
    error: null,
    save: null,
    close: null,
    tabs: null,
    activeTab: "model",
    snapshot: {},
    onSave: null,
    agentProfileRpc: rpcCall,
    loadedAgentProfile: null,
    agentProfileTargetGuard: null,
  };
}

async function saveSettingsModal() {
  try {
    setError("");
    const result = await state.onSave?.(collectSettingsPatch());
    if (result && typeof result === "object" && "settings" in result) {
      renderSettingsModal((result as { settings: SettingsSnapshot }).settings);
    }
    closeSettingsModal();
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

// ── tab renderers ──────────────────────────────────────────────────────

function renderModelTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const model = snapshot.model || {};
  const paths = snapshot.paths || {};
  const compaction = (snapshot.compaction || {}) as Record<string, unknown>;
  const permissions = snapshot.permissions || {};
  const aiApproval = (permissions.ai_approval || {}) as Record<string, unknown>;
  const profileChoices = profileOptions(snapshot);
  const frag = document.createDocumentFragment();
  frag.append(
    section("主对话", [
      readonlyRow("Provider", String(model.provider || "")),
      readonlyRow("Model", String(model.model || "")),
      readonlyRow("Base URL", String(model.base_url || "—")),
      readonlyRow("Protocol", String(model.protocol || "auto")),
      selectRow("Reasoning", "model_reasoning_effort", String(model.reasoning_effort || "xhigh"), [...REASONING_EFFORTS]),
      readonlyRow("Context window", model.context_window ? `${Number(model.context_window).toLocaleString()} tokens` : "Auto"),
    ]),
    section("上下文压缩", [
      selectRow("Profile", "compaction_profile", String(compaction.profile_name || ""), profileChoices),
      selectRow("Reasoning", "compaction_reasoning_effort", String(compaction.reasoning_effort || ""), [...REASONING_EFFORTS]),
      numberRow("Timeout（秒）", "compaction_timeout", Number(compaction.timeout_seconds || DEFAULT_COMPACTION_TIMEOUT_SECONDS), 1, 300),
    ]),
    section("AI 审批模型", [
      selectRow("Profile", "ai_approval_profile", String(aiApproval.profile_name || ""), profileChoices),
      numberRow("Timeout（秒）", "ai_approval_timeout", Number(aiApproval.timeout_seconds || 12), 1, 60),
    ]),
    section("供应商 / 模型", [manageProvidersRow()]),
    section("存储位置", [
      readonlyRow("Workspace", paths.workspace_settings || ""),
      readonlyRow("Global", paths.global_settings || ""),
      readonlyRow("Skills", paths.skills_state || ""),
    ]),
  );
  return frag;
}
function renderPermissionsTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const permissions = snapshot.permissions || {};
  const preset = inferPermissionMode(permissions);
  const presetConfig = PERMISSION_MODES[preset];
  const frag = document.createDocumentFragment();
  frag.append(
    section("权限预设", [
      selectRow(
        "Permission preset",
        "permission_mode",
        preset,
        Object.keys(PERMISSION_MODES),
        (key) => PERMISSION_MODES[key as PermissionMode].label,
      ),
      readonlyRow("说明", presetConfig.description),
    ]),
    section("沙箱路径", [
      readonlyRow("Readable paths", [permissions.sandbox_readable_files, permissions.sandbox_readable_dirs].flat().join(", ") || "—"),
      readonlyRow("Writable paths", [permissions.sandbox_writable_files, permissions.sandbox_writable_dirs].flat().join(", ") || "—"),
    ]),
  );
  return frag;
}

function renderPreferencesTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const userProfile = snapshot.user_profile || {};
  const frag = document.createDocumentFragment();
  frag.append(
    section("外观", [themeRow()]),
    section("回复偏好", [
      inputRow("Language", "language", userProfile.language || ""),
      inputRow("Tone", "tone", userProfile.tone || ""),
    ]),
  );
  return frag;
}

function renderCodeTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const ide = snapshot.code_ide || "cursor";
  const frag = document.createDocumentFragment();
  frag.append(
    section("代码 IDE", [
      selectRow("IDE", "code_ide", ide, ["cursor", "vscode", "trae", "ghostty", "terminal", ""]),
    ]),
    section("信息", [
      readonlyRow("", "LSP 状态和诊断在插件面板中管理。"),
    ]),
  );
  return frag;
}

function renderAdvancedTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const updateCheck = snapshot.update_check || {};
  const frag = document.createDocumentFragment();
  frag.append(
    section("更新", [
      checkboxRow("Update checks", "update_enabled", updateCheck.enabled !== false),
      readonlyRow("Last checked", updateCheck.last_checked_at ? new Date(updateCheck.last_checked_at * 1000).toLocaleString() : "never"),
      readonlyRow("Latest version", updateCheck.latest_version || "—"),
    ]),
    section("维护", [
      readonlyRow("", "上下文压缩 / 调试日志 / 用量 等操作请使用 / 命令面板。"),
    ]),
  );
  return frag;
}

interface AgentProfileView {
  name: string;
  display_name: string;
  revision: number;
  content_hash: string;
  source: "bundled" | "global" | "project";
  diagnostics?: Array<{ message: string }>;
}

function renderAgentProfilesTab(): DocumentFragment {
  state.loadedAgentProfile = null;
  const fragment = document.createDocumentFragment();
  const root = document.createElement("div");
  root.id = "agent-profiles-settings";
  const list = document.createElement("div");
  list.id = "agent-profile-list";
  list.textContent = "Loading agent profiles…";
  const editor = document.createElement("div");
  editor.id = "agent-profile-editor";
  editor.hidden = true;
  const metadata = document.createElement("p");
  metadata.id = "agent-profile-metadata";
  const scope = document.createElement("select");
  scope.id = "agent-profile-scope";
  for (const value of ["project", "global"]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    scope.append(option);
  }
  const yaml = document.createElement("textarea");
  yaml.id = "agent-profile-yaml";
  yaml.rows = 18;
  yaml.spellcheck = false;
  const actions = document.createElement("div");
  actions.className = "settings-actions";
  for (const [id, label] of [
    ["agent-profile-validate", "Validate"],
    ["agent-profile-save", "Save"],
    ["agent-profile-delete", "Delete"],
  ]) {
    const button = document.createElement("button");
    button.type = "button";
    button.id = id;
    button.textContent = label;
    actions.append(button);
  }
  const diagnostics = document.createElement("div");
  diagnostics.id = "agent-profile-diagnostics";
  diagnostics.setAttribute("role", "status");
  editor.append(metadata, scope, yaml, actions, diagnostics);
  root.append(list, editor);
  fragment.append(root);
  root.querySelector("#agent-profile-validate")?.addEventListener("click", () => void validateLoadedAgentProfile());
  root.querySelector("#agent-profile-save")?.addEventListener("click", () => void saveLoadedAgentProfile());
  root.querySelector("#agent-profile-delete")?.addEventListener("click", () => void deleteLoadedAgentProfile());
  scope.addEventListener("change", () => void loadAgentProfileTargetGuard());
  return fragment;
}

function agentProfileEditorElement<T extends HTMLElement>(selector: string): T | null {
  return state.content?.querySelector<T>(selector) ?? null;
}

function renderAgentProfileDiagnostics(items: Array<{ message: string }> = []): void {
  const target = agentProfileEditorElement<HTMLElement>("#agent-profile-diagnostics");
  if (target) target.textContent = items.map((item) => item.message).join("\n");
}

async function refreshAgentProfiles(): Promise<void> {
  const result = await state.agentProfileRpc("list-agent-profiles", {}) as { profiles?: AgentProfileView[] };
  const list = agentProfileEditorElement<HTMLElement>("#agent-profile-list");
  if (!list) return;
  list.replaceChildren(...(result.profiles || []).map((profile) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.agentProfile = profile.name;
    button.textContent = `${profile.display_name} · ${profile.source}`;
    button.addEventListener("click", () => void loadAgentProfile(profile));
    return button;
  }));
}

async function loadAgentProfile(profile: AgentProfileView): Promise<void> {
  const result = await state.agentProfileRpc("get-agent-profile", {
    scope: profile.source,
    name: profile.name,
  }) as { profile: AgentProfileView; yaml: string; read_only: boolean };
  state.loadedAgentProfile = {
    ...result.profile,
    read_only: result.read_only,
  };
  state.agentProfileTargetGuard = {
    scope: result.profile.source,
    revision: result.profile.revision,
    contentHash: result.profile.content_hash,
    exists: true,
  };
  const editor = agentProfileEditorElement<HTMLElement>("#agent-profile-editor");
  const yaml = agentProfileEditorElement<HTMLTextAreaElement>("#agent-profile-yaml");
  const scope = agentProfileEditorElement<HTMLSelectElement>("#agent-profile-scope");
  const metadata = agentProfileEditorElement<HTMLElement>("#agent-profile-metadata");
  if (!editor || !yaml || !scope || !metadata) return;
  editor.hidden = false;
  yaml.value = result.yaml;
  yaml.readOnly = result.read_only;
  scope.value = result.profile.source === "global" ? "global" : "project";
  scope.disabled = result.read_only;
  metadata.textContent = `${result.profile.display_name} · revision ${result.profile.revision} · ${result.profile.content_hash}${result.read_only ? " · 只读" : ""}`;
  for (const id of ["agent-profile-save", "agent-profile-delete"]) {
    const button = agentProfileEditorElement<HTMLButtonElement>(`#${id}`);
    if (button) button.disabled = result.read_only;
  }
  const validate = agentProfileEditorElement<HTMLButtonElement>("#agent-profile-validate");
  if (validate) validate.disabled = result.read_only;
  renderAgentProfileDiagnostics(result.profile.diagnostics || []);
}

function loadedAgentProfileInput(): {
  profile: Record<string, unknown>;
  scope: string;
  name: string;
  yaml: string;
} | null {
  const profile = state.loadedAgentProfile;
  const scope = agentProfileEditorElement<HTMLSelectElement>("#agent-profile-scope")?.value;
  const yaml = agentProfileEditorElement<HTMLTextAreaElement>("#agent-profile-yaml")?.value;
  const name = typeof profile?.name === "string" ? profile.name : "";
  if (!profile || !scope || !name || yaml === undefined) return null;
  return { profile, scope, name, yaml };
}

async function validateLoadedAgentProfile(): Promise<void> {
  const input = loadedAgentProfileInput();
  if (!input) return;
  const result = await state.agentProfileRpc("validate-agent-profile", {
    scope: input.scope,
    name: input.name,
    yaml: input.yaml,
  }) as { diagnostics?: Array<{ message: string }> };
  renderAgentProfileDiagnostics(result.diagnostics || []);
}

function renderAgentProfileError(error: unknown): void {
  const value = error as { message?: string; data?: { current?: { revision?: number; content_hash?: string }; diagnostics?: Array<{ message: string }> } };
  const current = value.data?.current;
  const messages = value.data?.diagnostics?.map((item) => item.message) || [];
  if (current) messages.unshift(`Current revision ${current.revision ?? "?"} · ${current.content_hash ?? ""}`);
  if (!messages.length) messages.push(value.message || String(error));
  renderAgentProfileDiagnostics(messages.map((message) => ({ message })));
}

async function loadAgentProfileTargetGuard(): Promise<void> {
  const input = loadedAgentProfileInput();
  if (!input) return;
  state.agentProfileTargetGuard = null;
  try {
    const result = await state.agentProfileRpc("get-agent-profile", {
      scope: input.scope,
      name: input.name,
    }) as { profile: AgentProfileView };
    state.agentProfileTargetGuard = {
      scope: input.scope,
      revision: result.profile.revision,
      contentHash: result.profile.content_hash,
      exists: true,
    };
    renderAgentProfileDiagnostics(result.profile.diagnostics || []);
  } catch (error) {
    const value = error as { message?: string };
    if ((value.message || "").toLowerCase().includes("not found")) {
      state.agentProfileTargetGuard = {
        scope: input.scope,
        revision: 0,
        contentHash: "",
        exists: false,
      };
      renderAgentProfileDiagnostics([]);
      return;
    }
    renderAgentProfileError(error);
  }
}

async function saveLoadedAgentProfile(): Promise<void> {
  const input = loadedAgentProfileInput();
  const guard = state.agentProfileTargetGuard;
  if (!input || input.profile.read_only === true || !guard || guard.scope !== input.scope) return;
  try {
    const result = await state.agentProfileRpc("save-agent-profile", {
      scope: input.scope,
      name: input.name,
      yaml: input.yaml,
      expected_revision: guard.revision,
    }) as { snapshot?: { revision?: number; content_hash?: string }; diagnostics?: Array<{ message: string }> };
    if (result.snapshot) {
      state.loadedAgentProfile = { ...input.profile, ...result.snapshot };
      state.agentProfileTargetGuard = {
        scope: input.scope,
        revision: result.snapshot.revision ?? guard.revision,
        contentHash: result.snapshot.content_hash ?? guard.contentHash,
        exists: true,
      };
    }
    renderAgentProfileDiagnostics(result.diagnostics || []);
    await refreshAgentProfiles();
  } catch (error) {
    renderAgentProfileError(error);
  }
}

async function deleteLoadedAgentProfile(): Promise<void> {
  const input = loadedAgentProfileInput();
  const guard = state.agentProfileTargetGuard;
  if (!input || input.profile.read_only === true || !guard || guard.scope !== input.scope || !guard.exists) return;
  try {
    await state.agentProfileRpc("delete-agent-profile", {
      scope: input.scope,
      name: input.name,
      expected_hash: guard.contentHash,
    });
    state.loadedAgentProfile = null;
    const editor = agentProfileEditorElement<HTMLElement>("#agent-profile-editor");
    if (editor) editor.hidden = true;
    await refreshAgentProfiles();
  } catch (error) {
    renderAgentProfileError(error);
  }
}

// ── form helpers ────────────────────────────────────────────────────────

function readonlyRow(label: string, value: string): HTMLLabelElement {
  const row = rowBase(label);
  const span = document.createElement("span");
  span.className = "settings-readonly";
  span.textContent = value;
  row.append(span);
  return row;
}

function checkboxRow(label: string, name: string, value: boolean): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = value;
  row.append(input);
  return row;
}

function themeRow(): HTMLLabelElement {
  const labels: Record<ThemePreference, string> = { system: "跟随系统", light: "浅色", dark: "深色" };
  const row = selectRow("主题", "ui_theme", getThemePreference(), ["system", "light", "dark"], (v) => labels[v as ThemePreference] ?? v);
  row.querySelector('input[name="ui_theme"]')?.addEventListener("change", (e) => {
    const value = (e.target as HTMLInputElement).value;
    if (value === "system" || value === "light" || value === "dark") setThemePreference(value);
  });
  return row;
}

function selectRow(label: string, name: string, value: string, options: string[], optionLabel?: (value: string) => string): HTMLLabelElement {
  const row = rowBase(label);
  row.append(createCustomSelect({ name, value, options: [...options], optionLabel }));
  return row;
}

function manageProvidersRow(): HTMLElement {
  const row = document.createElement("div");
  row.className = "vx-providers-entry-row";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "btn-manage-providers";
  btn.className = "settings-save";
  btn.textContent = "管理供应商 / 模型…";
  btn.addEventListener("click", () => {
    void openProvidersModal(rpcCall("settings.get", {}) as Promise<SettingsSnapshot>);
  });
  row.append(btn);
  return row;
}

function setError(message: string): void {
  if (state.error) state.error.textContent = message;
}
export {};
