/**
 * 主题系统 — 浅色 / 深色 / 跟随系统
 * 通过 <html data-theme="light|dark"> 驱动 tokens.css 双套变量。
 */

import { iconSvg } from "../utils/icons";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "voidx.theme";
let mediaListenerBound = false;

// localStorage 不可用（隐私模式、受限环境）时的内存回落
const memoryStore = new Map<string, string>();

function safeStorage(): Pick<Storage, "getItem" | "setItem"> {
  try {
    const ls = typeof window !== "undefined" ? window.localStorage : null;
    if (ls && typeof ls.getItem === "function" && typeof ls.setItem === "function") return ls;
  } catch {
    /* fall through to memory */
  }
  return { getItem: (k) => memoryStore.get(k) ?? null, setItem: (k, v) => void memoryStore.set(k, v) };
}

export function getThemePreference(): ThemePreference {
  const raw = safeStorage()?.getItem(STORAGE_KEY);
  return raw === "light" || raw === "dark" || raw === "system" ? raw : "dark";
}

export function systemTheme(): ResolvedTheme {
  try {
    if (typeof window.matchMedia === "function") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
  } catch {
    /* jsdom 等环境无 matchMedia 时回落浅色 */
  }
  return "light";
}

export function resolveTheme(pref: ThemePreference = getThemePreference()): ResolvedTheme {
  return pref === "system" ? systemTheme() : pref;
}

export function applyTheme(pref: ThemePreference = getThemePreference()): ResolvedTheme {
  const resolved = resolveTheme(pref);
  document.documentElement.dataset.theme = resolved;
  syncThemeToggle(resolved);
  return resolved;
}

export function setThemePreference(pref: ThemePreference): ResolvedTheme {
  const storage = safeStorage();
  if (storage) storage.setItem(STORAGE_KEY, pref);
  return applyTheme(pref);
}

/** 在浅色/深色间快速切换（写入显式偏好）。 */
export function toggleTheme(): ResolvedTheme {
  const applied = document.documentElement.dataset.theme;
  const current = applied === "light" || applied === "dark" ? applied : resolveTheme();
  return setThemePreference(current === "dark" ? "light" : "dark");
}

/** 同步侧栏底部主题按钮的图标与可访问名。 */
export function syncThemeToggle(resolved: ResolvedTheme = resolveTheme()): void {
  const btn = document.getElementById("btn-theme-toggle");
  if (!btn) return;
  const target = resolved === "dark" ? "light" : "dark";
  const svg = iconSvg(target === "light" ? "sun" : "moon");
  const iconSpan = btn.querySelector(".vx-sidebar-row-icon");
  if (iconSpan) {
    iconSpan.innerHTML = svg;
  } else {
    btn.innerHTML = svg;
  }
  btn.setAttribute("aria-label", target === "light" ? "切换到浅色主题" : "切换到深色主题");
  btn.setAttribute("title", target === "light" ? "切换到浅色主题" : "切换到深色主题");
}

export function initTheme(): void {
  applyTheme();
  const btn = document.getElementById("btn-theme-toggle");
  if (btn && !btn.dataset.themeBound) {
    btn.dataset.themeBound = "1";
    btn.addEventListener("click", () => toggleTheme());
  }
  if (!mediaListenerBound && typeof window.matchMedia === "function") {
    try {
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      media.addEventListener("change", () => {
        if (getThemePreference() === "system") applyTheme("system");
      });
      mediaListenerBound = true;
    } catch {
      /* 环境不支持时忽略 */
    }
  }
}

export function _resetThemeForTest(): void {
  mediaListenerBound = false;
  memoryStore.clear();
  const btn = document.getElementById("btn-theme-toggle");
  if (btn) delete btn.dataset.themeBound;
}
