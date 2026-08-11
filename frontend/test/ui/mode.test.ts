import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  RUNTIME_PROFILES,
  _resetModeControlsForTest,
  initModeControls,
  renderRuntimeProfile,
} from "../../src/ui/mode";

describe("runtime profile controls", () => {
  beforeEach(() => {
    document.querySelector("#runtime-profile-switcher")?.remove();
    document.querySelector("#chat-header-mode")?.remove();

    const switcher = document.createElement("div");
    switcher.id = "runtime-profile-switcher";
    for (const profile of RUNTIME_PROFILES) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.profile = profile;
      button.textContent = profile;
      switcher.append(button);
    }
    const badge = document.createElement("span");
    badge.id = "chat-header-mode";
    document.body.append(switcher, badge);
    _resetModeControlsForTest();
  });

  it("binds all four profiles and emits the selected profile", () => {
    const onSwitch = vi.fn();
    initModeControls(onSwitch);

    document.querySelector<HTMLButtonElement>('[data-profile="goal"]')!.click();

    expect(onSwitch).toHaveBeenCalledWith("goal");
    expect(document.querySelectorAll("#runtime-profile-switcher button")).toHaveLength(4);
  });


  it("removes the old listener before rebinding the same DOM", () => {
    const oldCallback = vi.fn();
    const newCallback = vi.fn();
    initModeControls(oldCallback);

    _resetModeControlsForTest();
    initModeControls(newCallback);
    document.querySelector<HTMLButtonElement>('[data-profile="chat"]')!.click();

    expect(oldCallback).not.toHaveBeenCalled();
    expect(newCallback).toHaveBeenCalledTimes(1);
    expect(newCallback).toHaveBeenCalledWith("chat");
  });
  it("renders one active accessible segment and a profile badge", () => {
    renderRuntimeProfile("loop");

    for (const profile of RUNTIME_PROFILES) {
      const button = document.querySelector<HTMLButtonElement>(`[data-profile="${profile}"]`)!;
      expect(button.classList.contains("active")).toBe(profile === "loop");
      expect(button.getAttribute("aria-pressed")).toBe(String(profile === "loop"));
    }
    const badge = document.querySelector<HTMLElement>("#chat-header-mode")!;
    expect(badge.textContent).toBe("Loop");
    expect(badge.dataset.profile).toBe("loop");
  });
});
