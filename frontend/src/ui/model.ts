import { rpcCall } from "../rpc";
import {
  uiState,
  providerSelectEl,
  modelSelectEl,
  inputEl,
  btnSendEl,
  updateStatusBar,
  PENDING_MODEL_LABEL,
} from "../services/state";
import type { ProfileSummary } from "../utils/types";
import type { SettingsSnapshot } from "./settings";

export function initModelControls(): void {
  // Bind click listener for custom model pill dropdown
  const modelPill = document.querySelector("#model-pill");
  const dropdownEl = document.querySelector("#model-dropdown");
  if (modelPill && dropdownEl && modelPill.dataset.initialized !== "true") {
    modelPill.dataset.initialized = "true";
    modelPill.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = dropdownEl.hidden;
      if (isHidden) {
        populateCustomModelDropdown();
      }
      dropdownEl.hidden = !isHidden;
    });

    dropdownEl.addEventListener("click", (e) => {
      e.stopPropagation();
    });

    document.addEventListener("click", () => {
      dropdownEl.hidden = true;
      const reasoningDropdown = document.querySelector("#reasoning-dropdown");
      if (reasoningDropdown) {
        (reasoningDropdown as HTMLElement).hidden = true;
      }
    });
  }

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

const REASONING_LEVELS = ["off", "low", "medium", "high", "xhigh"];
const REASONING_LABELS = ["关闭", "低", "中", "高", "高级"];

export function populateCustomModelDropdown(): void {
  const dropdownEl = document.querySelector("#model-dropdown");
  if (!dropdownEl) return;
  dropdownEl.replaceChildren();

  if (uiState.configuredProfiles.length === 0) {
    const emptyEl = document.createElement("div");
    emptyEl.className = "vx-model-dropdown-empty";
    emptyEl.textContent = "无可用模型，请至设置中配置";
    dropdownEl.append(emptyEl);
    return;
  }

  for (const profile of uiState.configuredProfiles) {
    if (!profile.provider || !profile.model) continue;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "vx-model-dropdown-item";
    
    if (profile.provider === uiState.provider && profile.model === uiState.model) {
      btn.classList.add("active");
    }

    btn.innerHTML = `
      <span class="vx-model-item-title">${profile.model}</span>
      <span class="vx-model-item-subtitle">${profile.provider}</span>
    `;

    btn.addEventListener("click", () => {
      if (providerSelectEl && modelSelectEl) {
        providerSelectEl.value = profile.provider || "";
        populateModelOptions(profile.provider || "", profile.model || "");
        modelSelectEl.value = profile.model || "";
        modelSelectEl.dispatchEvent(new Event("change"));
      }
      dropdownEl.hidden = true;
    });

    dropdownEl.append(btn);
  }
}

export function populateModelControls(): void {
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

export function populateModelOptions(
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

export function applySettingsRuntimeState(snapshot: SettingsSnapshot): void {
  const oldProfilesJson = JSON.stringify(uiState.configuredProfiles);
  uiState.configuredProfiles = configuredProfilesFromSnapshot(snapshot);
  const profilesChanged = JSON.stringify(uiState.configuredProfiles) !== oldProfilesJson;

  if (snapshot.permissions && typeof snapshot.permissions.permission_mode === "string") {
    uiState.permissionMode = snapshot.permissions.permission_mode;
  }
  const model = (snapshot.model || {}) as Record<string, unknown>;
  const provider = typeof model.provider === "string" ? model.provider : "";
  const modelName = typeof model.model === "string" ? model.model : "";
  const reasoningEffort = typeof model.reasoning_effort === "string" ? model.reasoning_effort : "xhigh";
  uiState.reasoningEffort = reasoningEffort;

  const profileConfigured = resolveProfileConfigured(snapshot, provider, modelName);
  uiState.profileConfigured = profileConfigured;

  const modelChanged = provider !== uiState.provider || modelName !== uiState.model;

  if (modelChanged && (provider || modelName)) {
    applyRuntimeState({
      provider,
      model: modelName,
      profile_configured: profileConfigured,
    });
  } else {
    if (profilesChanged) {
      populateModelControls();
    }
    updateStatusBar();
  }
}

export function configuredProfilesFromSnapshot(snapshot: SettingsSnapshot): ProfileSummary[] {
  return (snapshot.profiles || []).filter(
    (profile) => profile.configured === true && profile.provider && profile.model,
  );
}

export function resolveProfileConfigured(
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

export function applyRuntimeState(params: Record<string, unknown>): void {
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
  if (typeof params.permission_mode === "string") {
    uiState.permissionMode = params.permission_mode;
  }
  if (typeof params.ai_approval_count === "number") {
    uiState.aiApprovalCount = params.ai_approval_count;
  }
  if (hasProviderModel) {
    populateModelControls();
  }
  updateStatusBar();
}

export function parseProviderModel(
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

export function initPermissionControls(): void {
  const permissionPill = document.querySelector("#permission-pill");
  const dropdownEl = document.querySelector("#permission-dropdown");
  if (permissionPill && dropdownEl && permissionPill.dataset.initialized !== "true") {
    permissionPill.dataset.initialized = "true";
    permissionPill.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = dropdownEl.hidden;
      if (isHidden) {
        populatePermissionDropdown();
      }
      dropdownEl.hidden = !isHidden;
    });

    dropdownEl.addEventListener("click", (e) => {
      e.stopPropagation();
    });

    document.addEventListener("click", () => {
      dropdownEl.hidden = true;
    });
  }
}

export function populatePermissionDropdown(): void {
  const dropdownEl = document.querySelector("#permission-dropdown");
  if (!dropdownEl) return;
  dropdownEl.replaceChildren();

  const headerEl = document.createElement("div");
  headerEl.className = "vx-permission-dropdown-header";
  headerEl.innerHTML = `
    <span class="vx-permission-header-title">应如何批准 voidx 操作？</span>
    <button type="button" class="vx-permission-learn-more" id="permission-learn-more">了解更多</button>
  `;
  dropdownEl.append(headerEl);

  const learnMoreBtn = headerEl.querySelector("#permission-learn-more");
  if (learnMoreBtn) {
    learnMoreBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dropdownEl.hidden = true;
      const settingsDialog = document.querySelector<HTMLDialogElement>("#settings-dialog");
      if (settingsDialog) {
        settingsDialog.showModal();
        const tabBtn = settingsDialog.querySelector<HTMLElement>('.settings-tab[data-tab="permissions"]');
        if (tabBtn) {
          tabBtn.click();
        }
      }
    });
  }

  const listEl = document.createElement("div");
  listEl.className = "vx-permission-dropdown-list";

  const options = [
    {
      mode: "read_only",
      title: "只读模式",
      desc: "只允许读取文件和进行安全的安全检查，禁止任何修改操作",
      icon: `<svg class="vx-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`
    },
    {
      mode: "safe",
      title: "安全模式",
      desc: "对检测到的潜在风险操作请求批准，保障系统安全",
      icon: `<svg class="vx-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`
    },
    {
      mode: "ai_approval",
      title: "AI 审批",
      desc: "先将受限工具参数发送给所选模型预审，不确定时仍由你确认",
      icon: `<svg class="vx-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l3 2"/></svg>`
    },
    {
      mode: "project_trusted",
      title: "项目已信任",
      desc: "在此项目中自动批准常见操作，其他敏感操作仍需提示",
      icon: `<svg class="vx-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 11 2 2 4-4"/></svg>`
    },
    {
      mode: "full_access",
      title: "完全访问",
      desc: "可不受限制地访问互联网、执行命令和修改电脑上的任何文件",
      icon: `<svg class="vx-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`
    }
  ];

  for (const opt of options) {
    const itemEl = document.createElement("button");
    itemEl.type = "button";
    itemEl.className = "vx-permission-dropdown-item";
    if (uiState.permissionMode === opt.mode) {
      itemEl.classList.add("active");
    }

    itemEl.innerHTML = `
      <div class="vx-permission-item-icon">${opt.icon}</div>
      <div class="vx-permission-item-content">
        <div class="vx-permission-item-title">${opt.title}</div>
        <div class="vx-permission-item-desc">${opt.desc}</div>
      </div>
      <div class="vx-permission-item-check">${uiState.permissionMode === opt.mode ? "✓" : ""}</div>
    `;

    itemEl.addEventListener("click", () => {
      const patch = {
        permissions: {
          permission_mode: opt.mode
        }
      };
      rpcCall("settings.update", { patch })
        .then((result) => {
          const settings = (result as { settings?: SettingsSnapshot } | undefined)?.settings;
          if (settings) {
            applySettingsRuntimeState(settings);
          }
        })
        .catch((err) => {
          console.error("Failed to update permission mode:", err);
        });
      dropdownEl.hidden = true;
    });

    listEl.append(itemEl);
  }

  dropdownEl.append(listEl);
}

export function initReasoningControls(): void {
  const reasoningPill = document.querySelector("#reasoning-pill");
  const dropdownEl = document.querySelector("#reasoning-dropdown");
  if (reasoningPill && dropdownEl && reasoningPill.dataset.initialized !== "true") {
    reasoningPill.dataset.initialized = "true";
    reasoningPill.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = dropdownEl.hidden;
      if (isHidden) {
        populateReasoningDropdown();
      }
      dropdownEl.hidden = !isHidden;
    });

    dropdownEl.addEventListener("click", (e) => {
      e.stopPropagation();
    });

    document.addEventListener("click", () => {
      dropdownEl.hidden = true;
    });
  }
}

export function populateReasoningDropdown(): void {
  const dropdownEl = document.querySelector("#reasoning-dropdown");
  if (!dropdownEl) return;
  dropdownEl.replaceChildren();

  const sliderContainer = document.createElement("div");
  sliderContainer.className = "vx-reasoning-slider-container";

  const headerEl = document.createElement("div");
  headerEl.className = "vx-reasoning-header";

  const titleSpan = document.createElement("span");
  titleSpan.className = "vx-reasoning-title";
  
  const initialIndex = REASONING_LEVELS.indexOf(uiState.reasoningEffort || "xhigh");
  const initialLabel = REASONING_LABELS[initialIndex !== -1 ? initialIndex : 4];
  titleSpan.textContent = `${initialLabel}`;

  const chevronSpan = document.createElement("span");
  chevronSpan.className = "vx-reasoning-chevron";
  chevronSpan.textContent = " ›";

  headerEl.append(titleSpan, chevronSpan);

  const wrapperEl = document.createElement("div");
  wrapperEl.className = "vx-reasoning-slider-wrapper";

  const inputRange = document.createElement("input");
  inputRange.type = "range";
  inputRange.min = "0";
  inputRange.max = "4";
  inputRange.step = "1";
  inputRange.className = "vx-reasoning-slider";
  
  const initialVal = initialIndex !== -1 ? initialIndex : 4;
  inputRange.value = String(initialVal);

  const updateSliderProgress = (val: number) => {
    const pct = (val / 4) * 100;
    inputRange.style.background = `linear-gradient(to right, #2f99ff 0%, #2f99ff ${pct}%, #e5e7eb ${pct}%, #e5e7eb 100%)`;
  };
  updateSliderProgress(initialVal);

  const dotsContainer = document.createElement("div");
  dotsContainer.className = "vx-reasoning-dots";
  for (let i = 0; i <= 4; i++) {
    const dot = document.createElement("span");
    dot.className = `vx-reasoning-dot ${i <= initialVal ? "active" : ""}`;
    dotsContainer.append(dot);
  }

  inputRange.addEventListener("input", () => {
    const val = parseInt(inputRange.value);
    titleSpan.textContent = `${REASONING_LABELS[val]}`;
    updateSliderProgress(val);
    
    const dots = dotsContainer.querySelectorAll(".vx-reasoning-dot");
    dots.forEach((dot, idx) => {
      if (idx <= val) {
        dot.classList.add("active");
      } else {
        dot.classList.remove("active");
      }
    });
  });

  inputRange.addEventListener("change", () => {
    const val = parseInt(inputRange.value);
    const effort = REASONING_LEVELS[val];
    uiState.reasoningEffort = effort;
    updateStatusBar();

    const patch = {
      model: {
        reasoning_effort: effort
      }
    };
    rpcCall("settings.update", { patch })
      .then((result) => {
        const settings = (result as { settings?: SettingsSnapshot } | undefined)?.settings;
        if (settings) {
          applySettingsRuntimeState(settings);
        }
      })
      .catch((err) => {
        console.error("Failed to update reasoning effort:", err);
      });
  });

  wrapperEl.append(inputRange, dotsContainer);
  sliderContainer.append(headerEl, wrapperEl);
  dropdownEl.append(sliderContainer);
}
