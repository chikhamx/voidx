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
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.id = "mode-trigger";
    trigger.setAttribute("aria-expanded", "false");
    const triggerLabel = document.createElement("span");
    triggerLabel.id = "mode-trigger-label";
    trigger.append(triggerLabel);
    const menu = document.createElement("div");
    menu.id = "mode-menu";
    menu.hidden = true;
    for (const profile of RUNTIME_PROFILES) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.profile = profile;
      button.textContent = profile;
      menu.append(button);
    }
    switcher.append(trigger, menu);
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
    expect(document.querySelectorAll("#runtime-profile-switcher [data-profile]")).toHaveLength(4);
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

  it("toggles the menu from the trigger and closes after selecting an option", () => {
    const onSwitch = vi.fn();
    initModeControls(onSwitch);

    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const menu = document.querySelector<HTMLElement>("#mode-menu")!;

    trigger.click();
    expect(menu.hidden).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");

    document.querySelector<HTMLButtonElement>('[data-profile="loop"]')!.click();
    expect(onSwitch).toHaveBeenCalledWith("loop");
    expect(menu.hidden).toBe(true);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes the menu on outside click and Escape", () => {
    initModeControls(vi.fn());

    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const menu = document.querySelector<HTMLElement>("#mode-menu")!;

    trigger.click();
    expect(menu.hidden).toBe(false);

    document.body.click();
    expect(menu.hidden).toBe(true);

    trigger.click();
    expect(menu.hidden).toBe(false);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(menu.hidden).toBe(true);
  });



  it("keeps focus on an outside input when the menu is already closed", () => {
    initModeControls(vi.fn());

    const input = document.querySelector<HTMLTextAreaElement>("#input")!;
    input.focus();
    input.click();

    expect(document.activeElement).toBe(input);
  });

  it("closes on outside input click without stealing its focus", () => {
    initModeControls(vi.fn());

    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const menu = document.querySelector<HTMLElement>("#mode-menu")!;
    const input = document.querySelector<HTMLTextAreaElement>("#input")!;

    trigger.click();
    expect(menu.hidden).toBe(false);

    input.focus();
    input.click();

    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(input);
  });
  it("focuses the selected option on open and returns focus to the trigger on Escape", () => {
    initModeControls(vi.fn());
    renderRuntimeProfile("goal");

    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const menu = document.querySelector<HTMLElement>("#mode-menu")!;
    const selected = document.querySelector<HTMLButtonElement>('[data-profile="goal"]')!;

    trigger.focus();
    trigger.click();
    expect(menu.hidden).toBe(false);
    expect(document.activeElement).toBe(selected);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(trigger);
  });

  it("cycles focus through options with arrow keys while the menu is open", () => {
    initModeControls(vi.fn());
    renderRuntimeProfile("coding");

    const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger")!;
    const options = [...document.querySelectorAll<HTMLButtonElement>("#mode-menu [data-profile]")];

    trigger.click();
    expect(document.activeElement).toBe(options[1]);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    expect(document.activeElement).toBe(options[2]);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp" }));
    expect(document.activeElement).toBe(options[1]);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp" }));
    expect(document.activeElement).toBe(options[0]);
  });
  it("renders one selected option, a trigger label, and a profile badge", () => {
    renderRuntimeProfile("loop");

    for (const profile of RUNTIME_PROFILES) {
      const button = document.querySelector<HTMLButtonElement>(`[data-profile="${profile}"]`)!;
      expect(button.getAttribute("aria-selected")).toBe(String(profile === "loop"));
    }
    const label = document.querySelector<HTMLElement>("#mode-trigger-label")!;
    expect(label.textContent).toBe("循环");
    const badge = document.querySelector<HTMLElement>("#chat-header-mode")!;
    expect(badge.textContent).toBe("Loop");
    expect(badge.dataset.profile).toBe("loop");
  });
});
