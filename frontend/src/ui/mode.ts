export const RUNTIME_PROFILES = ["chat", "coding", "goal", "loop"] as const;

export type RuntimeProfile = (typeof RUNTIME_PROFILES)[number];

const PROFILE_LABELS: Record<RuntimeProfile, string> = {
  chat: "Chat",
  coding: "Coding",
  goal: "Goal",
  loop: "Loop",
};

const PROFILE_TRIGGER_LABELS: Record<RuntimeProfile, string> = {
  chat: "聊天",
  coding: "编码",
  goal: "目标",
  loop: "循环",
};

let boundSwitcher: HTMLElement | null = null;
let switchCallback: ((profile: RuntimeProfile) => void) | null = null;
let menuController: AbortController | null = null;

function handleModeClick(event: Event): void {
  const button = (event.target as Element | null)?.closest<HTMLElement>("[data-profile]");
  if (button && isRuntimeProfile(button.dataset.profile)) {
    closeMenu();
    switchCallback?.(button.dataset.profile);
  }
}

function menuElements() {
  const switcher = document.querySelector<HTMLElement>("#runtime-profile-switcher");
  const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger");
  const menu = document.querySelector<HTMLElement>("#mode-menu");
  return { switcher, trigger, menu };
}

function openMenu(): void {
  const { trigger, menu } = menuElements();
  if (!trigger || !menu) return;
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
}

function closeMenu(): void {
  const { trigger, menu } = menuElements();
  if (!trigger || !menu) return;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
}

function toggleMenu(): void {
  const { menu } = menuElements();
  if (!menu) return;
  if (menu.hidden) openMenu();
  else closeMenu();
}

export function isRuntimeProfile(value: unknown): value is RuntimeProfile {
  return typeof value === "string" && RUNTIME_PROFILES.includes(value as RuntimeProfile);
}

export function renderRuntimeProfile(profile: RuntimeProfile): void {
  document.querySelectorAll<HTMLButtonElement>("#runtime-profile-switcher [data-profile]").forEach((button) => {
    const active = button.dataset.profile === profile;
    button.setAttribute("aria-selected", String(active));
  });

  const label = document.querySelector<HTMLElement>("#mode-trigger-label");
  if (label) label.textContent = PROFILE_TRIGGER_LABELS[profile];

  const badge = document.querySelector<HTMLElement>("#chat-header-mode");
  if (badge) {
    badge.textContent = PROFILE_LABELS[profile];
    badge.dataset.profile = profile;
  }
}

function handleSwitcherClick(event: Event): void {
  if ((event.target as Element | null)?.closest("#mode-trigger")) {
    toggleMenu();
    return;
  }
  handleModeClick(event);
}

export function initModeControls(onSwitch: (profile: RuntimeProfile) => void): void {
  const { switcher, trigger, menu } = menuElements();
  switchCallback = onSwitch;
  if (!switcher || !trigger || !menu) return;
  if (switcher === boundSwitcher) return;
  boundSwitcher?.removeEventListener("click", handleSwitcherClick);
  switcher.addEventListener("click", handleSwitcherClick);
  boundSwitcher = switcher;

  menuController?.abort();
  menuController = new AbortController();
  const { signal } = menuController;
  document.addEventListener("click", (event) => {
    if (!switcher.contains(event.target as Node)) closeMenu();
  }, { signal });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  }, { signal });
}

export function _resetModeControlsForTest(): void {
  boundSwitcher?.removeEventListener("click", handleSwitcherClick);
  boundSwitcher = null;
  switchCallback = null;
  menuController?.abort();
  menuController = null;
}
