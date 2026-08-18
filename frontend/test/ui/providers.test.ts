// @ts-nocheck
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  initProvidersModal,
  renderProvidersModal,
  openProvidersModal,
  closeProvidersModal,
  _resetProvidersForTest,
} from "../../src/ui/providers";

const PROFILES = [
  { name: "openai/gpt-5.5", provider: "openai", model: "gpt-5.5", base_url: null, protocol: null, configured: true },
  { name: "deepseek/v4", provider: "deepseek", model: "deepseek-v4", base_url: null, protocol: null, configured: false },
];

beforeEach(() => {
  _resetProvidersForTest();
});

function content() {
  return document.querySelector("#providers-content");
}

function errorEl() {
  return document.querySelector("#providers-error");
}

function field(name) {
  return content().querySelector(`[name="${name}"]`);
}

function fillAddForm(overrides = {}) {
  const values = {
    provider: "xunfei-coding-plan",
    model: "astron-code-latest",
    base_url: "https://spark-api-open.xf-yun.com/v1",
    protocol: "openai",
    api_key: "sk-test",
    ...overrides,
  };
  for (const [name, value] of Object.entries(values)) {
    const el = field(name);
    if (el) el.value = value;
  }
}

describe("initProvidersModal", () => {
  it("captures DOM elements from providers dialog", () => {
    initProvidersModal();
    expect(document.querySelector("#providers-dialog")).not.toBeNull();
  });
});

describe("renderProvidersModal", () => {
  it("renders the configured profile list with key status", () => {
    initProvidersModal();
    renderProvidersModal({ profiles: PROFILES });

    const rows = content().querySelectorAll(".vx-provider-row");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("openai/gpt-5.5");
    expect(rows[0].textContent).toContain("key ✓");
    expect(rows[1].textContent).toContain("key ✗");
  });

  it("renders an empty state when no profiles exist", () => {
    initProvidersModal();
    renderProvidersModal({ profiles: [] });

    expect(content().textContent).toContain("暂无已保存的 model profile");
    expect(content().querySelectorAll(".vx-provider-row").length).toBe(0);
  });

  it("renders the add form with all fields", () => {
    initProvidersModal();
    renderProvidersModal({});

    for (const name of ["provider", "model", "base_url", "protocol", "api_key"]) {
      expect(field(name), `missing field ${name}`).not.toBeNull();
    }
    expect(content().querySelector("#providers-add")).not.toBeNull();
  });
});

describe("add provider/model", () => {
  it("rejects submission when provider or model is empty", async () => {
    const onSave = vi.fn();
    initProvidersModal({ onSave });
    renderProvidersModal({});

    fillAddForm({ provider: "", model: "" });
    content().querySelector("#providers-add").click();
    await Promise.resolve();

    expect(onSave).not.toHaveBeenCalled();
    expect(errorEl().textContent).toContain("必填");
  });

  it("collects model patch and provider secret from the add form", async () => {
    const onSave = vi.fn().mockResolvedValue({ settings: {} });
    initProvidersModal({ onSave });
    renderProvidersModal({});

    fillAddForm();
    content().querySelector("#providers-add").click();
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

    expect(onSave.mock.calls[0][0]).toMatchObject({
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

  it("omits provider_secrets when the api key is empty", async () => {
    const onSave = vi.fn().mockResolvedValue({ settings: {} });
    initProvidersModal({ onSave });
    renderProvidersModal({});

    fillAddForm({ api_key: "", base_url: "", protocol: "" });
    content().querySelector("#providers-add").click();
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

    const patch = onSave.mock.calls[0][0];
    expect(patch.provider_secrets).toBeUndefined();
    expect(patch.model).toEqual({ provider: "xunfei-coding-plan", model: "astron-code-latest" });
  });

  it("re-renders from the returned settings snapshot after add", async () => {
    const onSave = vi.fn().mockResolvedValue({ settings: { profiles: PROFILES } });
    initProvidersModal({ onSave });
    renderProvidersModal({ profiles: [] });

    fillAddForm();
    content().querySelector("#providers-add").click();
    await vi.waitFor(() =>
      expect(content().querySelectorAll(".vx-provider-row").length).toBe(2),
    );
  });

  it("shows the error message when save fails", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("api_key is required"));
    initProvidersModal({ onSave });
    renderProvidersModal({});

    fillAddForm();
    content().querySelector("#providers-add").click();
    await vi.waitFor(() => expect(errorEl().textContent).toContain("api_key is required"));
  });
});

describe("delete profile", () => {
  it("first click only arms the inline confirmation", async () => {
    const onSave = vi.fn();
    initProvidersModal({ onSave });
    renderProvidersModal({ profiles: PROFILES });

    const btn = content().querySelector('.vx-provider-delete[data-profile="openai/gpt-5.5"]');
    btn.click();

    expect(onSave).not.toHaveBeenCalled();
    expect(btn.classList.contains("confirming")).toBe(true);
    expect(btn.textContent).toContain("确认删除");
  });

  it("second click sends the delete patch", async () => {
    const onSave = vi.fn().mockResolvedValue({ settings: { profiles: [PROFILES[1]] } });
    initProvidersModal({ onSave });
    renderProvidersModal({ profiles: PROFILES });

    const btn = content().querySelector('.vx-provider-delete[data-profile="openai/gpt-5.5"]');
    btn.click();
    content().querySelector('.vx-provider-delete[data-profile="openai/gpt-5.5"]').click();
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

    expect(onSave.mock.calls[0][0]).toEqual({
      provider_secrets: {
        provider: "openai",
        profile_name: "openai/gpt-5.5",
        action: "delete",
      },
    });
  });

  it("re-renders the list after a successful delete", async () => {
    const onSave = vi.fn().mockResolvedValue({ settings: { profiles: [PROFILES[1]] } });
    initProvidersModal({ onSave });
    renderProvidersModal({ profiles: PROFILES });

    const btn = content().querySelector('.vx-provider-delete[data-profile="openai/gpt-5.5"]');
    btn.click();
    content().querySelector('.vx-provider-delete[data-profile="openai/gpt-5.5"]').click();
    await vi.waitFor(() =>
      expect(content().querySelectorAll(".vx-provider-row").length).toBe(1),
    );

    expect(content().textContent).not.toContain("openai/gpt-5.5");
  });
});

describe("open/close", () => {
  it("openProvidersModal awaits the snapshot and shows the dialog", async () => {
    initProvidersModal();
    await openProvidersModal(Promise.resolve({ profiles: PROFILES }));

    const dialog = document.querySelector("#providers-dialog");
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);
    expect(content().querySelectorAll(".vx-provider-row").length).toBe(2);

    closeProvidersModal();
    expect(dialog.open === false || !dialog.hasAttribute("open")).toBe(true);
  });
});
