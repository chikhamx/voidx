import { renderTodoPanel } from "./render.js";

let activeTab = "todo";
let initialized = false;

export function initDock() {
  const dock = document.querySelector("#dock");
  if (!dock || dock.dataset.initialized === "true") return;
  dock.dataset.initialized = "true";

  const tabs = dock.querySelectorAll(".vx-dock-tab");
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      switchTab(tab.dataset.tab);
    });
  }

  const toggleBtn = dock.querySelector("#dock-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      toggleDock();
    });
  }
}

export function switchTab(tab) {
  activeTab = tab;

  const tabs = document.querySelectorAll(".vx-dock-tab");
  for (const t of tabs) {
    t.classList.toggle("active", t.dataset.tab === tab);
  }

  const panes = document.querySelectorAll(".vx-dock-pane");
  for (const pane of panes) {
    pane.hidden = pane.dataset.pane !== tab;
  }
}

export function renderTodoInDock(items, summary) {
  const todoPanel = document.querySelector("#todo-panel");
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

export function toggleDock() {
  const dock = document.querySelector("#dock");
  if (!dock) return;
  dock.classList.toggle("collapsed");
}

export function getActiveTab() {
  return activeTab;
}

export function _resetForTest() {
  activeTab = "todo";
  initialized = false;
}
