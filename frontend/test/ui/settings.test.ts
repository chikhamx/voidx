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
      parallel_subagents: { enabled: true, max_concurrent: 4 },
      code_ide: "cursor",
      update_check: { enabled: true },
      paths: { workspace_settings: "/tmp/.voidx/settings.json", global_settings: "/tmp/.voidx/settings.json", skills_state: "/tmp/.voidx/skills_state.json" },
    });

    const content = document.querySelector("#settings-content");
    expect(content).not.toBeNull();
    const text = content.textContent;
    expect(text).toContain("openai");
    expect(text).toContain("gpt-5.5");
    expect(text).toContain("key ✓");
  });

  it("renders controls for adding a configured model profile", () => {
    initSettingsModal();
    renderSettingsModal({ model: { provider: "deepseek", model: "deepseek-v4-flash" } });

    const content = document.querySelector("#settings-content");
    expect(content.textContent).toContain("新增模型 / 供应商");
    expect(document.querySelector('[name="new_provider"]')).not.toBeNull();
    expect(document.querySelector('[name="new_model"]')).not.toBeNull();
    expect(document.querySelector('[name="new_api_key"]')).not.toBeNull();
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
    renderSettingsModal({ user_profile: { language: "zh-CN", tone: "concise" }, parallel_subagents: { enabled: false, max_concurrent: 2 } });
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


  it("collects code_ide from active tab", () => {
    initSettingsModal();
    renderSettingsModal({ code_ide: "trae" });
    document.querySelector(".settings-tab[data-tab='code']").click();
    const select = document.querySelector('[name="code_ide"]');
    if (select) select.value = "cursor";

    const patch = collectSettingsPatch();
    expect(patch.code_ide).toBe("cursor");
  });

  it("collects new model profile and provider secret from model tab", () => {
    initSettingsModal();
    renderSettingsModal({});
    document.querySelector('[name="new_provider"]').value = "xunfei-coding-plan";
    document.querySelector('[name="new_model"]').value = "astron-code-latest";
    document.querySelector('[name="new_base_url"]').value = "https://spark-api-open.xf-yun.com/v1";
    document.querySelector('[name="new_protocol"]').value = "openai";
    document.querySelector('[name="new_api_key"]').value = "sk-test";

    const patch = collectSettingsPatch();

    expect(patch).toEqual({
      model: {
        provider: "xunfei-coding-plan",
        model: "astron-code-latest",
        base_url: "https://spark-api-open.xf-yun.com/v1",
        protocol: "openai",
      },
      provider_secrets: {
        provider: "xunfei-coding-plan",
        profile_name: "xunfei-coding-plan/astron-code-latest",
        action: "set",
        api_key: "sk-test",
      },
    });
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
