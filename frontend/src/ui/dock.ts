import { renderTodoPanel } from "../utils/render";
import type { DockTab } from "../utils/types";

let activeTab: DockTab = "todo";

function terminalDrawer(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#terminal-drawer");
}

function terminalEntry(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#terminal-toggle");
}

function updateTerminalToggleState(): void {
  const expanded = Boolean(terminalDrawer() && !terminalDrawer()?.hidden);
  document.querySelectorAll<HTMLElement>("[data-terminal-toggle]").forEach((button) => {
    button.classList.toggle("active", expanded);
    button.setAttribute("aria-expanded", String(expanded));
  });
}

function focusTerminal(): void {
  const pane = document.querySelector<HTMLElement>("#terminal-pane");
  const target = pane?.querySelector<HTMLElement>("input, button, [tabindex]") || pane;
  target?.focus();
}

export function openTerminalDrawer(focus = false): boolean {
  const drawer = terminalDrawer();
  if (!drawer) return false;
  drawer.hidden = false;
  updateTerminalToggleState();
  if (focus) focusTerminal();
  return true;
}

export function closeTerminalDrawer(restoreFocus = false): boolean {
  const drawer = terminalDrawer();
  if (!drawer) return false;
  drawer.hidden = true;
  updateTerminalToggleState();
  if (restoreFocus) terminalEntry()?.focus();
  return false;
}

export function toggleTerminalDrawer(): boolean {
  const drawer = terminalDrawer();
  if (!drawer) return false;
  return drawer.hidden
    ? openTerminalDrawer(true)
    : closeTerminalDrawer(true);
}

function updateDockToggleState(): void {
  const dock = document.querySelector<HTMLElement>("#dock");
  const button = document.querySelector<HTMLElement>("#dock-toggle");
  if (!dock || !button) return;
  const expanded = !dock.classList.contains("collapsed");
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute("aria-label", expanded ? "隐藏右侧栏" : "显示右侧栏");
  button.title = expanded ? "隐藏右侧栏" : "显示右侧栏";
}

export function initDock(): void {
  const dock = document.querySelector<HTMLElement>("#dock");
  if (!dock || dock.dataset.initialized === "true") return;
  dock.dataset.initialized = "true";

  const tabs = dock.querySelectorAll<HTMLElement>(".vx-dock-tab[data-tab]");
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      switchTab(tab.dataset.tab as DockTab);
    });
  }

  document.querySelector<HTMLElement>("#dock-toggle")?.addEventListener("click", () => {
    toggleDock();
  });

  document.querySelectorAll<HTMLElement>("[data-terminal-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      toggleTerminalDrawer();
    });
  });

  document.querySelectorAll<HTMLElement>("[data-terminal-close]").forEach((button) => {
    button.addEventListener("click", () => {
      closeTerminalDrawer(true);
    });
  });

  updateTerminalToggleState();
  updateDockToggleState();
  updateDockStrip();
}

export function switchTab(tab: DockTab): void {
  if (tab === "terminal") {
    openTerminalDrawer();
    return;
  }
  activeTab = tab;

  const tabs = document.querySelectorAll<HTMLElement>(".vx-dock-tab[data-tab]");
  for (const item of tabs) {
    item.classList.toggle("active", item.dataset.tab === tab);
    item.setAttribute("aria-selected", String(item.dataset.tab === tab));
    item.tabIndex = item.dataset.tab === tab ? 0 : -1;
  }

  const panes = document.querySelectorAll<HTMLElement>(".vx-dock-pane");
  for (const pane of panes) {
    pane.hidden = pane.dataset.pane !== tab;
  }
}

export function renderTodoInDock(
  items: { id?: string; content: string; status: string }[] | null,
  summary: string,
): void {
  const todoPanel = document.querySelector<HTMLElement>("#todo-panel");
  if (!todoPanel) return;

  if ((!items || items.length === 0) && !summary) {
    renderTodoPanel(todoPanel, [], "");
    todoPanel.classList.remove("visible");
    return;
  }

  if (!items || items.length === 0) {
    todoPanel.replaceChildren();
    if (summary) {
      const summaryEl = document.createElement("span");
      summaryEl.className = "todo-summary";
      summaryEl.textContent = summary;
      todoPanel.append(summaryEl);
      todoPanel.classList.add("visible");
    } else {
      todoPanel.classList.remove("visible");
    }
    return;
  }

  renderTodoPanel(todoPanel, items, summary || "");
  todoPanel.classList.add("visible");
}

export function toggleDock(): void {
  const dock = document.querySelector<HTMLElement>("#dock");
  if (!dock) return;
  dock.classList.toggle("collapsed");
  updateDockToggleState();
  updateDockStrip();
}

function updateDockStrip(): void {
  const strip = document.querySelector<HTMLElement>("#dock-strip");
  if (!strip) return;
  strip.hidden = true;
}

export function getActiveTab(): DockTab {
  return activeTab;
}

export function _resetForTest(): void {
  activeTab = "todo";
  const drawer = terminalDrawer();
  if (drawer) drawer.hidden = true;
  updateTerminalToggleState();
  updateDockToggleState();
}
