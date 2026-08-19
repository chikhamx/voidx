import { beforeEach, describe, expect, it } from "vitest";
import {
  _resetWorkspaceForTest,
  initSidebarToggle,
  setSidebarWidth,
} from "../../src/ui/workspace";

const shell = document.querySelector<HTMLElement>(".vx-workbench-shell")!;
const sidebar = document.querySelector<HTMLElement>("#sidebar")!;

beforeEach(() => {
  _resetWorkspaceForTest();
  shell.classList.remove("sidebar-collapsed");
  shell.style.setProperty("--vx-sidebar-width", "260px");
  sidebar.classList.remove("collapsed");
  const left = document.querySelector<HTMLElement>(".vx-titlebar-left")!;
  left.querySelector("#titlebar-sidebar-toggle")?.remove();
  const button = document.createElement("button");
  button.id = "titlebar-sidebar-toggle";
  button.type = "button";
  left.prepend(button);
});

describe("titlebar sidebar toggle", () => {
  it("collapses and restores the sidebar width", () => {
    setSidebarWidth(320);
    initSidebarToggle();
    const button = document.querySelector<HTMLButtonElement>("#titlebar-sidebar-toggle")!;

    button.click();
    expect(shell.classList.contains("sidebar-collapsed")).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");

    button.click();
    expect(shell.classList.contains("sidebar-collapsed")).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(shell.style.getPropertyValue("--vx-sidebar-width")).toBe("320px");
  });
});
