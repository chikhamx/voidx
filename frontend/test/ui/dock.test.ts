// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  initDock,
  switchTab,
  renderTodoInDock,
  toggleDock,
  getActiveTab,
  _resetForTest,
} from "../../src/ui/dock";

beforeEach(() => {
  _resetForTest();
  const dock = document.querySelector("#dock");
  if (dock) dock.classList.remove("collapsed");
  const content = document.querySelector("#dock-content");
  if (content) {
    for (const pane of content.querySelectorAll(".vx-dock-pane")) {
      pane.hidden = pane.dataset.pane !== "todo";
    }
  }
  const tabs = document.querySelectorAll(".vx-dock-tab");
  for (const tab of tabs) {
    tab.classList.toggle("active", tab.dataset.tab === "todo");
  }
});

describe("initDock", () => {
  it("binds tab click events", () => {
    initDock();
    const terminalTab = document.querySelector('.vx-dock-tab[data-tab="terminal"]');
    terminalTab.click();
    expect(getActiveTab()).toBe("terminal");
  });

  it("binds dock toggle button", () => {
    initDock();
    const dock = document.querySelector("#dock");
    const toggleBtn = document.querySelector("#dock-toggle");
    toggleBtn.click();
    expect(dock.classList.contains("collapsed")).toBe(true);
  });
});

describe("switchTab", () => {
  it("activates the specified tab", () => {
    initDock();
    switchTab("terminal");
    expect(getActiveTab()).toBe("terminal");
  });

  it("shows the corresponding pane and hides others", () => {
    initDock();
    switchTab("diff");

    const todoPane = document.querySelector('.vx-dock-pane[data-pane="todo"]');
    const diffPane = document.querySelector('.vx-dock-pane[data-pane="diff"]');
    expect(todoPane.hidden).toBe(true);
    expect(diffPane.hidden).toBe(false);
  });

  it("updates active class on tab buttons", () => {
    initDock();
    switchTab("terminal");

    const terminalTab = document.querySelector('.vx-dock-tab[data-tab="terminal"]');
    const todoTab = document.querySelector('.vx-dock-tab[data-tab="todo"]');
    expect(terminalTab.classList.contains("active")).toBe(true);
    expect(todoTab.classList.contains("active")).toBe(false);
  });
});

describe("renderTodoInDock", () => {
  it("renders todo items into the todo pane", () => {
    initDock();
    const items = [
      { content: "Task A", status: "done" },
      { content: "Task B", status: "active" },
    ];
    renderTodoInDock(items, "1/2 done");

    const todoPanel = document.querySelector("#todo-panel");
    expect(todoPanel.classList.contains("visible")).toBe(true);
    const todoItems = todoPanel.querySelectorAll(".todo-item");
    expect(todoItems).toHaveLength(2);
    expect(todoItems[0].textContent).toContain("Task A");
    expect(todoItems[1].textContent).toContain("Task B");
  });

  it("shows summary text", () => {
    initDock();
    renderTodoInDock([], "all done");
    const todoPanel = document.querySelector("#todo-panel");
    expect(todoPanel.textContent).toContain("all done");
  });

  it("clears todo panel when items empty and no summary", () => {
    initDock();
    renderTodoInDock([], "");
    const todoPanel = document.querySelector("#todo-panel");
    expect(todoPanel.classList.contains("visible")).toBe(false);
  });
});

describe("toggleDock", () => {
  it("toggles collapsed class on dock", () => {
    initDock();
    const dock = document.querySelector("#dock");
    expect(dock.classList.contains("collapsed")).toBe(false);

    toggleDock();
    expect(dock.classList.contains("collapsed")).toBe(true);

    toggleDock();
    expect(dock.classList.contains("collapsed")).toBe(false);
  });
});
