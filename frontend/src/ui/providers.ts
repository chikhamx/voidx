/**
 * Providers modal — 供应商/模型独立配置页。
 * 管理已配置的 model profile（列表 + 删除）并添加新的供应商/模型。
 * patch 语义与 settings.update 后端一致；onSave 由 main.ts 注入。
 */
import { createCustomSelect } from "./custom-select";
import { inputRow, rowBase, secretRow, section } from "./form-rows";
import { renderSettingsModal, type SettingsSnapshot } from "./settings";
import type { ProfileSummary } from "../utils/types";

interface ProvidersState {
  dialog: HTMLDialogElement | null;
  content: HTMLElement | null;
  error: HTMLElement | null;
  close: HTMLButtonElement | null;
  snapshot: SettingsSnapshot;
  onSave: ((patch: Record<string, unknown>) => Promise<unknown> | void) | null;
}

let state: ProvidersState = {
  dialog: null,
  content: null,
  error: null,
  close: null,
  snapshot: {},
  onSave: null,
};

let confirmingProfile: string | null = null;

const PROTOCOL_OPTIONS = ["", "openai", "anthropic", "deepseek", "gemini"];

export function initProvidersModal(
  { onSave }: { onSave?: (patch: Record<string, unknown>) => Promise<unknown> | void } = {},
): void {
  state.dialog = document.querySelector("#providers-dialog");
  state.content = document.querySelector("#providers-content");
  state.error = document.querySelector("#providers-error");
  state.close = document.querySelector("#providers-close");
  state.onSave = onSave ?? null;
  state.close?.addEventListener("click", () => closeProvidersModal());
}

export function renderProvidersModal(snapshot: SettingsSnapshot = {}): void {
  state.snapshot = snapshot;
  confirmingProfile = null;
  if (!state.content) return;
  state.content.replaceChildren(
    section("已配置的供应商 / 模型", renderProfileRows(snapshot.profiles || [])),
    renderAddSection(),
  );
  setError("");
}

export async function openProvidersModal(snapshotPromise: Promise<SettingsSnapshot>): Promise<void> {
  if (!state.dialog) return;
  try {
    setError("");
    const snapshot = await snapshotPromise;
    renderProvidersModal(snapshot);
    if (typeof state.dialog.showModal === "function") {
      state.dialog.showModal();
    } else {
      state.dialog.setAttribute("open", "");
    }
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

export function closeProvidersModal(): void {
  if (!state.dialog) return;
  if (typeof state.dialog.close === "function") {
    state.dialog.close();
  } else {
    state.dialog.removeAttribute("open");
  }
}

export function _resetProvidersForTest(): void {
  state = { dialog: null, content: null, error: null, close: null, snapshot: {}, onSave: null };
  confirmingProfile = null;
}

// ── profile list ─────────────────────────────────────────────────────────

function renderProfileRows(profiles: ProfileSummary[]): HTMLElement[] {
  if (profiles.length === 0) {
    const empty = document.createElement("div");
    empty.className = "settings-readonly";
    empty.textContent = "暂无已保存的 model profile";
    return [empty];
  }
  return profiles.map((profile) => {
    const row = document.createElement("div");
    row.className = "vx-provider-row";

    const nameEl = document.createElement("span");
    nameEl.className = "vx-provider-name";
    nameEl.textContent = profile.name;

    const detailEl = document.createElement("span");
    detailEl.className = "settings-readonly";
    detailEl.textContent = `${profile.provider}/${profile.model}${profile.configured ? " · key ✓" : " · key ✗"}`;

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "vx-provider-delete";
    deleteBtn.dataset.profile = profile.name;
    deleteBtn.dataset.provider = profile.provider || "";
    deleteBtn.textContent = "删除";
    deleteBtn.addEventListener("click", () => {
      if (confirmingProfile !== profile.name) {
        confirmingProfile = profile.name;
        deleteBtn.classList.add("confirming");
        deleteBtn.textContent = "确认删除？";
        return;
      }
      confirmingProfile = null;
      void submitPatch({
        provider_secrets: {
          provider: profile.provider || "",
          profile_name: profile.name,
          action: "delete",
        },
      });
    });

    row.append(nameEl, detailEl, deleteBtn);
    return row;
  });
}

// ── add form ─────────────────────────────────────────────────────────────

function renderAddSection(): HTMLElement {
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.id = "providers-add";
  addBtn.className = "settings-save vx-providers-add";
  addBtn.textContent = "添加供应商 / 模型";
  addBtn.addEventListener("click", () => {
    const patch = collectAddPatch();
    if (patch) void submitPatch(patch);
  });

  const actionRow = document.createElement("div");
  actionRow.className = "vx-providers-add-row";
  actionRow.append(addBtn);

  return section("新增供应商 / 模型", [
    inputRow("Provider", "provider", ""),
    inputRow("Model", "model", ""),
    inputRow("Base URL", "base_url", ""),
    protocolRow(),
    secretRow("API key", "api_key", ""),
    actionRow,
  ]);
}

function protocolRow(): HTMLLabelElement {
  const row = rowBase("Protocol");
  row.append(createCustomSelect({ name: "protocol", value: "", options: [...PROTOCOL_OPTIONS] }));
  return row;
}

function collectAddPatch(): Record<string, unknown> | null {
  const value = (name: string): string =>
    (state.content?.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? "").trim();
  const provider = value("provider");
  const model = value("model");
  if (!provider || !model) {
    setError("Provider 和 Model 为必填项");
    return null;
  }
  const baseUrl = value("base_url");
  const protocol = value("protocol");
  const apiKey = value("api_key");

  const modelPatch: Record<string, unknown> = { provider, model };
  if (baseUrl) modelPatch.base_url = baseUrl;
  if (protocol) modelPatch.protocol = protocol;

  const patch: Record<string, unknown> = { model: modelPatch };
  if (apiKey) {
    patch.provider_secrets = {
      provider,
      profile_name: `${provider}/${model}`,
      action: "set",
      api_key: apiKey,
    };
  }
  return patch;
}

// ── shared ───────────────────────────────────────────────────────────────

async function submitPatch(patch: Record<string, unknown>): Promise<void> {
  try {
    setError("");
    const result = await state.onSave?.(patch);
    const settings = (result as { settings?: SettingsSnapshot } | undefined)?.settings;
    if (settings) {
      renderProvidersModal(settings);
      const settingsDialog = document.querySelector<HTMLDialogElement>("#settings-dialog");
      if (settingsDialog?.open || settingsDialog?.hasAttribute("open")) {
        renderSettingsModal(settings);
      }
    }
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

function setError(message: string): void {
  if (state.error) state.error.textContent = message;
}
