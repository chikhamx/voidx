// @ts-nocheck
import { beforeEach, describe, expect, it } from "vitest";
import { initSettingsModal, renderSettingsModal, collectSettingsPatch, closeSettingsModal, _resetSettingsForTest } from "../src/settings";

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
      permissions: { permission_mode: "default", sandbox_mode: "workspace-write", approval_policy: "untrusted" },
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

  it("renders permissions tab when switched", () => {
    initSettingsModal();
    renderSettingsModal({ permissions: { permission_mode: "accept-edits", sandbox_mode: "read-only", approval_policy: "on-failure" } });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    const content = document.querySelector("#settings-content");
    expect(content.textContent).toContain("accept-edits");
    expect(content.textContent).toContain("on-failure");
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
  it("collects permissions from active tab", () => {
    initSettingsModal();
    renderSettingsModal({ permissions: { permission_mode: "accept-edits", sandbox_mode: "read-only", approval_policy: "on-failure" } });
    document.querySelector(".settings-tab[data-tab='permissions']").click();
    const select = document.querySelector('[name="permission_mode"]');
    if (select) select.value = "full-access";

    const patch = collectSettingsPatch();
    expect(patch.permissions.permission_mode).toBe("full-access");
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

describe("closeSettingsModal", () => {
  it("removes open attribute from dialog", () => {
    initSettingsModal();
    const dialog = document.querySelector("#settings-dialog");
    dialog.setAttribute("open", "");
    closeSettingsModal();
    expect(dialog.hasAttribute("open")).toBe(false);
  });
});
