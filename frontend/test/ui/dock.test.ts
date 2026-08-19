// @ts-nocheck
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  initDock,
  switchTab,
  renderTodoInDock,
  toggleDock,
  getActiveTab,
  _resetForTest,
} from "../../src/ui/dock";
import { openTerminalDrawer } from "../../src/ui/dock";

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

describe("initial dock markup", () => {
  it("starts fully hidden with the expand toggle in the main header", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const document = new DOMParser().parseFromString(html, "text/html");
    const dock = document.querySelector("#dock");
    const toggle = document.querySelector("#dock-toggle");

    expect(dock?.classList.contains("collapsed")).toBe(true);
    expect(document.querySelector(".vx-main-header > #dock-toggle")).toBe(toggle);
    expect(dock?.querySelector("#dock-toggle")).toBeNull();
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(toggle?.getAttribute("aria-label")).toBe("显示右侧栏");
  });
});

describe("initDock", () => {
  it("binds right panel tab click events", () => {
    initDock();
    const diffTab = document.querySelector('.vx-dock-tab[data-tab="diff"]');
    diffTab.click();
    expect(getActiveTab()).toBe("diff");
  });

  it("binds dock toggle button", () => {
    initDock();
    const dock = document.querySelector("#dock");
    const toggleBtn = document.querySelector("#dock-toggle");
    toggleBtn.click();
    expect(dock.classList.contains("collapsed")).toBe(true);
  });

  it("opens the main conversation terminal drawer without changing the right panel tab", () => {
    initDock();
    switchTab("todo");
    const drawer = document.querySelector("#terminal-drawer");
    const terminalToggle = document.querySelector("[data-terminal-toggle]");

    terminalToggle.click();

    expect(drawer.hidden).toBe(false);
    expect(terminalToggle.getAttribute("aria-expanded")).toBe("true");
    expect(getActiveTab()).toBe("todo");

    terminalToggle.click();
    expect(drawer.hidden).toBe(true);
    expect(document.querySelector("#terminal-pane")).not.toBeNull();
  });

  it("keeps the terminal drawer open when opened repeatedly", () => {
    const drawer = document.querySelector("#terminal-drawer");

    openTerminalDrawer();
    openTerminalDrawer();

    expect(drawer.hidden).toBe(false);
  });

  it("returns focus to the terminal entry when the drawer closes", () => {
    initDock();
    const entry = document.querySelector("#terminal-toggle");
    const close = document.querySelector("[data-terminal-close]");

    entry.click();
    close.focus();
    close.click();

    expect(document.querySelector("#terminal-drawer").hidden).toBe(true);
    expect(document.activeElement).toBe(entry);
  });
});

describe("switchTab", () => {
  it("activates the specified right panel tab", () => {
    initDock();
    switchTab("diff");
    expect(getActiveTab()).toBe("diff");
  });

  it("shows the corresponding pane and hides others", () => {
    initDock();
    switchTab("diff");

    const todoPane = document.querySelector('.vx-dock-pane[data-pane="todo"]');
    const diffPane = document.querySelector('.vx-dock-pane[data-pane="diff"]');
    expect(todoPane.hidden).toBe(true);
    expect(diffPane.hidden).toBe(false);
  });

  it("updates active class on right panel tab buttons", () => {
    initDock();
    switchTab("diff");

    const diffTab = document.querySelector('.vx-dock-tab[data-tab="diff"]');
    const todoTab = document.querySelector('.vx-dock-tab[data-tab="todo"]');
    expect(diffTab.classList.contains("active")).toBe(true);
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
