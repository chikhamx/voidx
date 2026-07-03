interface ProfileSummary {
  name: string;
  provider: string;
  model: string;
  configured?: boolean;
}

export interface SettingsSnapshot {
  model?: Record<string, unknown>;
  profiles?: ProfileSummary[];
  paths?: Record<string, string>;
  permissions?: Record<string, unknown>;
  user_profile?: { language?: string; tone?: string };
  parallel_subagents?: { enabled?: boolean; max_concurrent?: number };
  code_ide?: string;
  update_check?: { enabled?: boolean; last_checked_at?: number; latest_version?: string };
  [k: string]: unknown;
}

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
};

export function initSettingsModal({ onSave }: { onSave?: (patch: Record<string, unknown>) => Promise<unknown> | void } = {}): void {
  state.dialog = document.querySelector("#settings-dialog");
  state.content = document.querySelector("#settings-content");
  state.error = document.querySelector("#settings-error");
  state.save = document.querySelector("#settings-save");
  state.close = document.querySelector("#settings-close");
  state.tabs = document.querySelector("#settings-tabs");
  state.onSave = onSave ?? null;
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
    (state.content?.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value) || "";
  const checked = (name: string): boolean =>
    Boolean(state.content?.querySelector<HTMLInputElement>(`[name="${name}"]`)?.checked);
  switch (state.activeTab) {
    case "permissions":
      return {
        permissions: {
          permission_mode: value("permission_mode"),
          sandbox_mode: value("sandbox_mode"),
          approval_policy: value("approval_policy"),
        },
      };
    case "preferences":
      return {
        user_profile: {
          language: value("language"),
          tone: value("tone"),
        },
        parallel_subagents: {
          enabled: checked("parallel_enabled"),
          max_concurrent: Number(value("parallel_max") || 4),
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

export function _resetSettingsForTest() {
  state = { dialog: null, content: null, error: null, save: null, close: null, tabs: null, activeTab: "model", snapshot: {}, onSave: null };
}

async function saveSettingsModal() {
  try {
    setError("");
    const result = await state.onSave?.(collectSettingsPatch());
    if (result?.settings) renderSettingsModal(result.settings);
    closeSettingsModal();
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

// ── tab renderers ──────────────────────────────────────────────────────

function renderModelTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const model = snapshot.model || {};
  const profiles = snapshot.profiles || [];
  const paths = snapshot.paths || {};
  const frag = document.createDocumentFragment();
  frag.append(
    section("当前模型", [
      readonlyRow("Provider", model.provider || ""),
      readonlyRow("Model", model.model || ""),
      readonlyRow("Base URL", model.base_url || "—"),
      readonlyRow("Protocol", model.protocol || "auto"),
      readonlyRow("Reasoning", model.reasoning_effort || "xhigh"),
      readonlyRow("Context window", model.context_window ? `${model.context_window.toLocaleString()} tokens` : "Auto"),
    ]),
    section("已配置 Profiles", [
      ...(profiles.length
        ? profiles.map((p) => {
            const nameEl = document.createElement("span");
            nameEl.textContent = p.name;
            const detailEl = document.createElement("span");
            detailEl.className = "settings-readonly";
            detailEl.textContent = `${p.provider}/${p.model}${p.configured ? " · key ✓" : " · key ✗"}`;
            const row = rowBase(p.name);
            row.append(nameEl);
            row.append(detailEl);
            return row;
          })
        : [readonlyRow("", "暂无已保存的 model profile")]),
    ]),
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
  const frag = document.createDocumentFragment();
  frag.append(
    section("权限模式", [
      selectRow("Permission mode", "permission_mode", permissions.permission_mode || "default", ["default", "read-only", "accept-edits", "auto-review", "full-access", "custom"]),
      selectRow("Sandbox", "sandbox_mode", permissions.sandbox_mode || "workspace-write", ["read-only", "workspace-write", "danger-full-access"]),
      selectRow("Approval", "approval_policy", permissions.approval_policy || "untrusted", ["untrusted", "on-failure", "on-request", "never"]),
    ]),
    section("沙箱路径", [
      readonlyRow("Workspace write paths", (permissions.sandbox_workspace_write || []).join(", ") || "—"),
    ]),
    section("当前规则", [
      readonlyRow("Approval reviewer", permissions.approval_reviewer || "—"),
    ]),
  );
  return frag;
}

function renderPreferencesTab(snapshot: SettingsSnapshot = {}): DocumentFragment {
  const userProfile = snapshot.user_profile || {};
  const parallel = snapshot.parallel_subagents || {};
  const frag = document.createDocumentFragment();
  frag.append(
    section("回复偏好", [
      inputRow("Language", "language", userProfile.language || ""),
      inputRow("Tone", "tone", userProfile.tone || ""),
    ]),
    section("并行子代理", [
      checkboxRow("Parallel subagents", "parallel_enabled", Boolean(parallel.enabled)),
      numberRow("Max concurrent", "parallel_max", parallel.max_concurrent || 4, 1, 8),
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

// ── form helpers ────────────────────────────────────────────────────────

function section(title: string, children: HTMLElement[]): HTMLElement {
  const el = document.createElement("section");
  el.className = "settings-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  el.append(heading, ...children);
  return el;
}

function readonlyRow(label: string, value: string): HTMLLabelElement {
  const row = rowBase(label);
  const span = document.createElement("span");
  span.className = "settings-readonly";
  span.textContent = value;
  row.append(span);
  return row;
}

function inputRow(label: string, name: string, value: string): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.name = name;
  input.value = value;
  row.append(input);
  return row;
}

function numberRow(label: string, name: string, value: string, min: number, max: number): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.type = "number";
  input.name = name;
  input.value = String(value);
  input.min = String(min);
  input.max = String(max);
  row.append(input);
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

function selectRow(label: string, name: string, value: string, options: string[]): HTMLLabelElement {
  const row = rowBase(label);
  const select = document.createElement("select");
  select.name = name;
  for (const optionValue of options) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionValue || (optionValue ? optionValue : "none");
    select.append(option);
  }
  select.value = value;
  row.append(select);
  return row;
}

function rowBase(label: string): HTMLLabelElement {
  const row = document.createElement("label");
  row.className = "settings-row";
  const text = document.createElement("span");
  text.textContent = label;
  row.append(text);
  return row;
}

function setError(message: string): void {
  if (state.error) state.error.textContent = message;
}
