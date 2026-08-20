import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetModeControlsForTest,
  initModeControls,
  refreshModeMenu,
  renderRuntimeProfile,
  type AgentProfileInfo,
} from "../../src/ui/mode";

const profiles: AgentProfileInfo[] = [
  {
    name: "reviewer-v2",
    display_name: "Reviewer V2",
    revision: 3,
    content_hash: "abc",
    source: "project",
    run_mode: "review",
    hitl_mode: "interactive",
    availability: "available",
    diagnostics: [],
  },
  {
    name: "broken-custom",
    display_name: "Broken Custom",
    revision: 1,
    content_hash: "def",
    source: "global",
    run_mode: "custom",
    hitl_mode: "autonomous",
    availability: "unavailable",
    diagnostics: [{ path: "tools", code: "missing_tool", message: "Tool is not installed", severity: "error" }],
  },
];

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("runtime profile controls", () => {
  beforeEach(() => {
    document.querySelector("#runtime-profile-switcher")?.remove();
    document.querySelector("#chat-header-mode")?.remove();
    const switcher = document.createElement("div");
    switcher.id = "runtime-profile-switcher";
    switcher.innerHTML = `
      <button id="mode-trigger" aria-expanded="false"><span id="mode-trigger-label"></span></button>
      <div id="mode-menu" role="listbox" hidden></div>`;
    const badge = document.createElement("span");
    badge.id = "chat-header-mode";
    document.body.append(switcher, badge);
    _resetModeControlsForTest();
  });

  it("loads the menu dynamically from list-agent-profiles and uses public metadata", async () => {
    const listProfiles = vi.fn().mockResolvedValue({ profiles });
    initModeControls(vi.fn(), { listProfiles });

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();

    expect(listProfiles).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll("#mode-menu [data-profile]")).toHaveLength(2);
    expect(document.querySelector('[data-profile="reviewer-v2"]')?.textContent).toContain("Reviewer V2");
    expect(document.querySelector('[data-profile="reviewer-v2"]')?.textContent).toContain("review · interactive · project");
    const unavailable = document.querySelector<HTMLButtonElement>('[data-profile="broken-custom"]')!;
    expect(unavailable.disabled).toBe(true);
    expect(unavailable.textContent).toContain("Tool is not installed");
  });

  it("refreshes before switching and only emits a still-available opaque profile id", async () => {
    const listProfiles = vi.fn()
      .mockResolvedValueOnce({ profiles })
      .mockResolvedValueOnce({ profiles: [profiles[0]] });
    const onSwitch = vi.fn();
    initModeControls(onSwitch, { listProfiles });

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-profile="reviewer-v2"]')!.click();
    await flush();

    expect(listProfiles).toHaveBeenCalledTimes(2);
    expect(onSwitch).toHaveBeenCalledWith("reviewer-v2");
  });

  it("does not switch when a profile becomes unavailable during the pre-switch refresh", async () => {
    const listProfiles = vi.fn()
      .mockResolvedValueOnce({ profiles: [profiles[0]] })
      .mockResolvedValueOnce({ profiles: [{ ...profiles[0], availability: "unavailable", diagnostics: profiles[1].diagnostics }] });
    const onSwitch = vi.fn();
    initModeControls(onSwitch, { listProfiles });

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-profile="reviewer-v2"]')!.click();
    await flush();

    expect(onSwitch).not.toHaveBeenCalled();
    expect(document.querySelector<HTMLButtonElement>('[data-profile="reviewer-v2"]')!.disabled).toBe(true);
  });

  it("renders arbitrary profile ids using the latest display name without id branching", async () => {
    const listProfiles = vi.fn().mockResolvedValue({ profiles });
    initModeControls(vi.fn(), { listProfiles });
    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();

    renderRuntimeProfile("reviewer-v2");

    expect(document.querySelector("#mode-trigger-label")?.textContent).toBe("Reviewer V2");
    expect(document.querySelector("#chat-header-mode")?.textContent).toBe("Reviewer V2");
    expect(document.querySelector("#chat-header-mode")?.getAttribute("data-profile")).toBe("reviewer-v2");
  });

  it("renders a create-custom-agent action at the menu bottom and routes clicks to onCreateAgent", async () => {
    const listProfiles = vi.fn().mockResolvedValue({ profiles });
    const onSwitch = vi.fn();
    const onCreateAgent = vi.fn();
    initModeControls(onSwitch, { listProfiles, onCreateAgent });

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();

    const action = document.querySelector<HTMLButtonElement>('#mode-menu [data-action="new-agent"]');
    expect(action).not.toBeNull();
    expect(action!.textContent).toContain("新建自定义 Agent");
    action!.click();
    await flush();

    expect(onCreateAgent).toHaveBeenCalledTimes(1);
    expect(onSwitch).not.toHaveBeenCalled();
    expect(document.querySelector<HTMLElement>("#mode-menu")!.hidden).toBe(true);
  });

  it("refreshModeMenu re-fetches profiles and re-renders options", async () => {
    const listProfiles = vi.fn()
      .mockResolvedValueOnce({ profiles })
      .mockResolvedValueOnce({ profiles: [...profiles, { ...profiles[0], name: "new-agent-1", display_name: "New Agent 1" }] });
    initModeControls(vi.fn(), { listProfiles });

    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();
    expect(document.querySelectorAll("#mode-menu [data-profile]")).toHaveLength(2);

    await refreshModeMenu();

    expect(listProfiles).toHaveBeenCalledTimes(2);
    expect(document.querySelectorAll("#mode-menu [data-profile]")).toHaveLength(3);
    expect(document.querySelector('[data-profile="new-agent-1"]')?.textContent).toContain("New Agent 1");
  });

  it("keeps keyboard close behavior after async menu rendering", async () => {
    initModeControls(vi.fn(), { listProfiles: vi.fn().mockResolvedValue({ profiles }) });
    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const menu = document.querySelector<HTMLElement>("#mode-menu")!;
    trigger.click();
    await flush();
    expect(menu.hidden).toBe(false);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(trigger);
  });

  it("arrow keys can move focus onto the create action item", async () => {
    const listProfiles = vi.fn().mockResolvedValue({ profiles: [profiles[0]] });
    initModeControls(vi.fn(), { listProfiles, onCreateAgent: vi.fn() });
    document.querySelector<HTMLButtonElement>("#mode-trigger")!.click();
    await flush();

    // 打开时聚焦首个可用选项（profile）；再按 ArrowDown 应落到动作项上
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    expect(document.activeElement).toBe(document.querySelector('[data-action="new-agent"]'));
  });
});

