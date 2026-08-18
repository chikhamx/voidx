// @ts-nocheck
import { beforeEach, describe, expect, it } from "vitest";
import { initSettingsModal, renderSettingsModal, collectSettingsPatch, closeSettingsModal, _resetSettingsForTest } from "../../src/ui/settings";

beforeEach(() => {
  _resetSettingsForTest();
});

describe("initSettingsModal", () => {
  it("captures DOM elements from settings dialog", () => {
    initSettingsModal();
    const dialog = document.querySelector("#settings-dialog");
    expect(dialog).not.toBeNull();
  });
});

describe("renderSettingsModal", () => {
  it("renders model tab content by default", () => {
    initSettingsModal();
    renderSettingsModal({
      model: { provider: "openai", model: "gpt-5.5", base_url: "https://api.openai.com", protocol: "openai" },
      profiles: [{ name: "openai-default", provider: "openai", model: "gpt-5.5", base_url: null, protocol: null, configured: true }],
      permissions: { permission_mode: "safe" },
      user_profile: { language: "en", tone: "direct" },
      code_ide: "cursor",
      update_check: { enabled: true },
      paths: { workspace_settings: "/tmp/.voidx/settings.json", global_settings: "/tmp/.voidx/settings.json", skills_state: "/tmp/.voidx/skills_state.json" },
    });

    const content = document.querySelector("#settings-content");
    expect(content).not.toBeNull();
    const text = content.textContent;
    expect(text).toContain("openai");
    expect(text).toContain("gpt-5.5");
  });

  it("renders a manage-providers entry instead of the inline add form", () => {
    initSettingsModal();
    renderSettingsModal({ model: { provider: "deepseek", model: "deepseek-v4-flash" } });

    const content = document.querySelector("#settings-content");
    expect(content.textContent).toContain("供应商 / 模型");
    expect(document.querySelector("#btn-manage-providers")).not.toBeNull();
    expect(document.querySelector('[name="new_provider"]')).toBeNull();
    expect(document.querySelector('[name="new_model"]')).toBeNull();
    expect(document.querySelector('[name="new_api_key"]')).toBeNull();
  });

  it("uses a 256 second compaction timeout when settings omit it", () => {
    initSettingsModal();
    renderSettingsModal({ model: { provider: "openai", model: "gpt-5.5" } });

    expect(document.querySelector('[name="compaction_timeout"]').value).toBe("256");
    expect(collectSettingsPatch()).toMatchObject({
      compaction: { timeout_seconds: 256 },
    });
  });


  it("renders the model configuration center for chat, compaction, and AI approval", () => {
    initSettingsModal();
    renderSettingsModal({
      model: { provider: "openai", model: "gpt-5.5", reasoning_effort: "high" },
      profiles: [
        { name: "openai/gpt-5.5", provider: "openai", model: "gpt-5.5", base_url: null, protocol: "openai", configured: true },
        { name: "anthropic/claude", provider: "anthropic", model: "claude", base_url: null, protocol: "anthropic", configured: false },
      ],
      compaction: { profile_name: "openai/gpt-5.5", reasoning_effort: "low", timeout_seconds: 45 },
      permissions: {
        permission_mode: "ai_approval",
        ai_approval: { profile_name: "openai/gpt-5.5", timeout_seconds: 9 },
      },
    });

    const content = document.querySelector("#settings-content");
    expect(content.textContent).toContain("主对话");
    expect(content.textContent).toContain("上下文压缩");
    expect(content.textContent).toContain("AI 审批模型");
    expect(document.querySelector('[name="model_reasoning_effort"]').value).toBe("high");
    expect(document.querySelector('[name="compaction_profile"]').value).toBe("openai/gpt-5.5");
    expect(document.querySelector('[name="compaction_reasoning_effort"]').value).toBe("low");
    expect(document.querySelector('[name="compaction_timeout"]').value).toBe("45");
    expect(document.querySelector('[name="ai_approval_profile"]').value).toBe("openai/gpt-5.5");
    expect(document.querySelector('[name="ai_approval_timeout"]').value).toBe("9");
  });

  it("renders ask-first permission presets instead of low-level controls", () => {
    initSettingsModal();
    renderSettingsModal({ permissions: { permission_mode: "project_trusted" } });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    const content = document.querySelector("#settings-content");
    const preset = document.querySelector('[name="permission_mode"]');

    expect(preset).not.toBeNull();
    expect(preset.value).toBe("project_trusted");
    expect(content.textContent).toContain("Project trusted");
    expect(document.querySelector('[name="approval_policy"]')).toBeNull();
    expect(document.querySelector('[name="sandbox_mode"]')).toBeNull();
  });

  it("defaults legacy permission fields to safe without compatibility mapping", () => {
    initSettingsModal();
    renderSettingsModal({ permissions: { permission_mode: "safe" } });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    const preset = document.querySelector('[name="permission_mode"]');

    expect(preset).not.toBeNull();
    expect(preset.value).toBe("safe");
  });

  it("renders preferences tab", () => {
    initSettingsModal();
    renderSettingsModal({ user_profile: { language: "zh-CN", tone: "concise" } });
    document.querySelector(".settings-tab[data-tab='preferences']").click();
    // input values are not in textContent, query directly
    const langInput = document.querySelector('[name="language"]');
    const toneInput = document.querySelector('[name="tone"]');
    expect(langInput.value).toBe("zh-CN");
    expect(toneInput.value).toBe("concise");
  });

  it("renders code tab", () => {
    initSettingsModal();
    renderSettingsModal({ code_ide: "vscode" });
    document.querySelector(".settings-tab[data-tab='code']").click();
    const select = document.querySelector('[name="code_ide"]');
    expect(select).not.toBeNull();
    expect(select.value).toBe("vscode");
  });

  it("renders advanced tab", () => {
    initSettingsModal();
    renderSettingsModal({ update_check: { enabled: false, last_checked_at: null, latest_version: null } });
    document.querySelector(".settings-tab[data-tab='advanced']").click();
    const content = document.querySelector("#settings-content");
    expect(content.textContent).toContain("Update checks");
    expect(content.textContent).toContain("never");
  });
});

describe("collectSettingsPatch", () => {
  it("collects only the ask-first permission preset", () => {
    initSettingsModal();
    renderSettingsModal({ permissions: { permission_mode: "safe" } });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    const select = document.querySelector('[name="permission_mode"]');
    if (select) select.value = "full_access";

    const patch = collectSettingsPatch();
    expect(patch.permissions).toEqual({
      permission_mode: "full_access",
    });
  });

  it("does not overwrite AI approval configuration when saving permission preset", () => {
    initSettingsModal();
    renderSettingsModal({
      permissions: {
        permission_mode: "ai_approval",
        ai_approval: { profile_name: "openai/gpt-5.5", timeout_seconds: 9 },
      },
    });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    document.querySelector('[name="permission_mode"]').value = "safe";

    expect(collectSettingsPatch()).toEqual({
      permissions: { permission_mode: "safe" },
    });
  });



  it("collects code_ide from active tab", () => {
    initSettingsModal();
    renderSettingsModal({ code_ide: "trae" });
    document.querySelector(".settings-tab[data-tab='code']").click();
    const select = document.querySelector('[name="code_ide"]');
    if (select) select.value = "cursor";

    const patch = collectSettingsPatch();
    expect(patch.code_ide).toBe("cursor");
  });

});


  it("collects model-purpose reasoning, compaction, and AI approval settings", () => {
    initSettingsModal();
    renderSettingsModal({
      model: { provider: "openai", model: "gpt-5.5", reasoning_effort: "medium" },
      profiles: [{ name: "openai/gpt-5.5", provider: "openai", model: "gpt-5.5", base_url: null, protocol: "openai", configured: true }],
      compaction: { profile_name: "", reasoning_effort: null, timeout_seconds: 60 },
      permissions: { permission_mode: "safe", ai_approval: { profile_name: "", timeout_seconds: 12 } },
    });
    document.querySelector('[name="model_reasoning_effort"]').value = "max";
    document.querySelector('[name="compaction_profile"]').value = "openai/gpt-5.5";
    document.querySelector('[name="compaction_reasoning_effort"]').value = "low";
    document.querySelector('[name="compaction_timeout"]').value = "75";
    document.querySelector('[name="ai_approval_profile"]').value = "openai/gpt-5.5";
    document.querySelector('[name="ai_approval_timeout"]').value = "8";

    const patch = collectSettingsPatch();

    expect(patch).toMatchObject({
      model: { reasoning_effort: "max" },
      compaction: { profile_name: "openai/gpt-5.5", reasoning_effort: "low", timeout_seconds: 75 },
      permissions: { ai_approval: { profile_name: "openai/gpt-5.5", timeout_seconds: 8 } },
    });
  });

describe("closeSettingsModal", () => {
  it("removes open attribute from dialog", () => {
    initSettingsModal();
    const dialog = document.querySelector("#settings-dialog");
    dialog.setAttribute("open", "");
    closeSettingsModal();
    expect(dialog.hasAttribute("open")).toBe(false);
  });
});
