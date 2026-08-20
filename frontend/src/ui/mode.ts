export type RuntimeProfile = string;

export interface AgentProfileDiagnostic {
  path: string;
  code: string;
  message: string;
  severity: "error" | "warning";
}

export interface AgentProfileInfo {
  name: string;
  display_name: string;
  revision: number;
  content_hash: string;
  source: "bundled" | "global" | "project";
  run_mode: string;
  hitl_mode: "interactive" | "autonomous";
  availability: "available" | "unavailable";
  diagnostics: AgentProfileDiagnostic[];
}

interface ModeControlsOptions {
  listProfiles?: () => Promise<{ profiles: AgentProfileInfo[] }>;
}

let boundSwitcher: HTMLElement | null = null;
let switchCallback: ((profile: RuntimeProfile) => void) | null = null;
let listProfiles: (() => Promise<{ profiles: AgentProfileInfo[] }>) | null = null;
let menuController: AbortController | null = null;
let currentProfile = "coding";
let profilesByName = new Map<string, AgentProfileInfo>();

function menuElements() {
  const switcher = document.querySelector<HTMLElement>("#runtime-profile-switcher");
  const trigger = document.querySelector<HTMLButtonElement>("#mode-trigger");
  const menu = document.querySelector<HTMLElement>("#mode-menu");
  return { switcher, trigger, menu };
}

function menuOptions(menu: HTMLElement): HTMLButtonElement[] {
  return [...menu.querySelectorAll<HTMLButtonElement>("[data-profile]")].filter((option) => !option.disabled);
}

function focusSelectedOption(menu: HTMLElement): void {
  const options = menuOptions(menu);
  const selected = options.find((option) => option.getAttribute("aria-selected") === "true");
  (selected ?? options[0])?.focus();
}

function focusAdjacentOption(menu: HTMLElement, direction: 1 | -1): void {
  const options = menuOptions(menu);
  if (options.length === 0) return;
  const currentIndex = options.indexOf(document.activeElement as HTMLButtonElement);
  const nextIndex = currentIndex === -1
    ? direction === 1 ? 0 : options.length - 1
    : (currentIndex + direction + options.length) % options.length;
  options[nextIndex]?.focus();
}

function profileDescription(profile: AgentProfileInfo): string {
  const metadata = `${profile.run_mode} · ${profile.hitl_mode} · ${profile.source}`;
  const diagnostics = profile.diagnostics.map((item) => item.message).join(" · ");
  return diagnostics ? `${metadata} · ${diagnostics}` : metadata;
}

function renderMenu(profiles: AgentProfileInfo[]): void {
  const { menu } = menuElements();
  if (!menu) return;
  profilesByName = new Map(profiles.map((profile) => [profile.name, profile]));
  menu.replaceChildren(...profiles.map((profile) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "vx-mode-option";
    button.dataset.profile = profile.name;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(profile.name === currentProfile));
    button.disabled = profile.availability !== "available";
    const text = document.createElement("span");
    text.className = "vx-mode-option-text";
    const name = document.createElement("span");
    name.className = "vx-mode-option-name";
    name.textContent = profile.display_name;
    const description = document.createElement("span");
    description.className = "vx-mode-option-desc";
    description.textContent = profileDescription(profile);
    text.append(name, description);
    button.append(text);
    return button;
  }));
  renderRuntimeProfile(currentProfile);
}

async function refreshProfiles(): Promise<AgentProfileInfo[]> {
  if (!listProfiles) return [...profilesByName.values()];
  const result = await listProfiles();
  renderMenu(result.profiles || []);
  return result.profiles || [];
}

async function openMenu(): Promise<void> {
  const { trigger, menu } = menuElements();
  if (!trigger || !menu) return;
  await refreshProfiles();
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  focusSelectedOption(menu);
}

function closeMenu({ restoreFocus = true }: { restoreFocus?: boolean } = {}): void {
  const { trigger, menu } = menuElements();
  if (!trigger || !menu) return;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  if (restoreFocus) trigger.focus();
}

async function selectProfile(profile: string): Promise<void> {
  const profiles = await refreshProfiles();
  const selected = profiles.find((item) => item.name === profile);
  if (!selected || selected.availability !== "available") return;
  closeMenu();
  switchCallback?.(selected.name);
}

function handleSwitcherClick(event: Event): void {
  const target = event.target as Element | null;
  if (target?.closest("#mode-trigger")) {
    const { menu } = menuElements();
    if (!menu) return;
    if (menu.hidden) void openMenu();
    else closeMenu();
    return;
  }
  const button = target?.closest<HTMLButtonElement>("[data-profile]");
  if (button?.dataset.profile && !button.disabled) void selectProfile(button.dataset.profile);
}

export function isRuntimeProfile(value: unknown): value is RuntimeProfile {
  return typeof value === "string" && value.trim().length > 0;
}

export function runtimeProfileLabel(profile: RuntimeProfile): string {
  return profilesByName.get(profile)?.display_name || profile;
}

export function runtimeProfileRunMode(profile: RuntimeProfile): string {
  return profilesByName.get(profile)?.run_mode || "";
}

export function renderRuntimeProfile(profile: RuntimeProfile): void {
  currentProfile = profile;
  document.querySelectorAll<HTMLButtonElement>("#runtime-profile-switcher [data-profile]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.profile === profile));
  });
  const label = runtimeProfileLabel(profile);
  const triggerLabel = document.querySelector<HTMLElement>("#mode-trigger-label");
  if (triggerLabel) triggerLabel.textContent = label;
  const badge = document.querySelector<HTMLElement>("#chat-header-mode");
  if (badge) {
    badge.textContent = label;
    badge.dataset.profile = profile;
  }
}

export function initModeControls(
  onSwitch: (profile: RuntimeProfile) => void,
  options: ModeControlsOptions = {},
): void {
  const { switcher, menu } = menuElements();
  switchCallback = onSwitch;
  listProfiles = options.listProfiles ?? null;
  if (!switcher || !menu) return;
  if (switcher !== boundSwitcher) {
    boundSwitcher?.removeEventListener("click", handleSwitcherClick);
    switcher.addEventListener("click", handleSwitcherClick);
    boundSwitcher = switcher;
  }
  menuController?.abort();
  menuController = new AbortController();
  const { signal } = menuController;
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !switcher.contains(event.target as Node)) closeMenu({ restoreFocus: false });
  }, { signal });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (!menu.hidden && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      focusAdjacentOption(menu, event.key === "ArrowDown" ? 1 : -1);
    }
  }, { signal });
}

export function _resetModeControlsForTest(): void {
  boundSwitcher?.removeEventListener("click", handleSwitcherClick);
  boundSwitcher = null;
  switchCallback = null;
  listProfiles = null;
  profilesByName = new Map();
  currentProfile = "coding";
  menuController?.abort();
  menuController = null;
}
