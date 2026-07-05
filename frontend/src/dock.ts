import { renderTodoPanel } from "./render";
import type { DockTab } from "./types";

let activeTab: DockTab = "todo";

export function initDock(): void {
  const dock = document.querySelector<HTMLElement>("#dock");
  if (!dock || dock.dataset.initialized === "true") return;
  dock.dataset.initialized = "true";

  const tabs = dock.querySelectorAll<HTMLElement>(".vx-dock-tab");
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      switchTab(tab.dataset.tab as DockTab);
    });
  }

  const toggleBtn = dock.querySelector<HTMLElement>("#dock-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      toggleDock();
    });
  }

  const titlebarToggle = document.querySelector<HTMLElement>("#titlebar-dock-toggle");
  if (titlebarToggle) {
    titlebarToggle.addEventListener("click", () => {
      toggleDock();
    });
  }

  updateDockStrip();
}

export function switchTab(tab: DockTab): void {
  activeTab = tab;

  const tabs = document.querySelectorAll<HTMLElement>(".vx-dock-tab");
  for (const t of tabs) {
    t.classList.toggle("active", t.dataset.tab === tab);
    t.setAttribute("aria-selected", String(t.dataset.tab === tab));
  }

  const panes = document.querySelectorAll<HTMLElement>(".vx-dock-pane");
  for (const pane of panes) {
    pane.hidden = pane.dataset.pane !== tab;
  }
}

export function renderTodoInDock(
  items: { id: string; content: string; status: string }[] | null,
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
}
