export const RUNTIME_PROFILES = ["chat", "coding", "goal", "loop"] as const;

export type RuntimeProfile = (typeof RUNTIME_PROFILES)[number];

const PROFILE_LABELS: Record<RuntimeProfile, string> = {
  chat: "Chat",
  coding: "Coding",
  goal: "Goal",
  loop: "Loop",
};

let boundSwitcher: HTMLElement | null = null;
let switchCallback: ((profile: RuntimeProfile) => void) | null = null;

function handleModeClick(event: Event): void {
  const button = (event.target as Element | null)?.closest<HTMLElement>("[data-profile]");
  if (button && isRuntimeProfile(button.dataset.profile)) switchCallback?.(button.dataset.profile);
}

export function isRuntimeProfile(value: unknown): value is RuntimeProfile {
  return typeof value === "string" && RUNTIME_PROFILES.includes(value as RuntimeProfile);
}

export function renderRuntimeProfile(profile: RuntimeProfile): void {
  document.querySelectorAll<HTMLButtonElement>("#runtime-profile-switcher [data-profile]").forEach((button) => {
    const active = button.dataset.profile === profile;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const badge = document.querySelector<HTMLElement>("#chat-header-mode");
  if (badge) {
    badge.textContent = PROFILE_LABELS[profile];
    badge.dataset.profile = profile;
  }
}

export function initModeControls(onSwitch: (profile: RuntimeProfile) => void): void {
  const switcher = document.querySelector<HTMLElement>("#runtime-profile-switcher");
  switchCallback = onSwitch;
  if (!switcher || switcher === boundSwitcher) return;
  boundSwitcher?.removeEventListener("click", handleModeClick);
  switcher.addEventListener("click", handleModeClick);
  boundSwitcher = switcher;
}

export function _resetModeControlsForTest(): void {
  boundSwitcher?.removeEventListener("click", handleModeClick);
  boundSwitcher = null;
  switchCallback = null;
}
