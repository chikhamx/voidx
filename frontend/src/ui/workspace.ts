import {
  DEFAULT_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
} from "../services/state";
import { switchWorkspace } from "../services/connection";

let expandedSidebarWidth = DEFAULT_SIDEBAR_WIDTH;

function workbenchShell(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".vx-workbench-shell");
}

function sidebarToggle(): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>("#titlebar-sidebar-toggle");
}

function updateSidebarToggle(): void {
  const shell = workbenchShell();
  const button = sidebarToggle();
  if (!shell || !button) return;
  const expanded = !shell.classList.contains("sidebar-collapsed");
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute("aria-label", expanded ? "隐藏侧栏" : "显示侧栏");
  button.title = expanded ? "隐藏侧栏" : "显示侧栏";
}

export function initWorkspaceControls(): void {
  document
    .querySelector("#btn-open-workspace")
    ?.addEventListener("click", () => {
      void openWorkspacePicker();
    });
}

export function setSidebarWidth(width: number): void {
  const clamped = Math.max(
    MIN_SIDEBAR_WIDTH,
    Math.min(MAX_SIDEBAR_WIDTH, Math.round(width)),
  );
  const shell = workbenchShell();
  if (!shell?.classList.contains("sidebar-collapsed")) {
    expandedSidebarWidth = clamped;
  }
  (shell || document.documentElement).style.setProperty(
    "--vx-sidebar-width",
    `${clamped}px`,
  );
}

export function toggleSidebar(): boolean {
  const shell = workbenchShell();
  if (!shell) return false;

  const collapsed = shell.classList.toggle("sidebar-collapsed");
  if (!collapsed) {
    setSidebarWidth(expandedSidebarWidth);
  } else {
    const inlineWidth = Number.parseFloat(shell.style.getPropertyValue("--vx-sidebar-width"));
    if (Number.isFinite(inlineWidth) && inlineWidth > 0) {
      expandedSidebarWidth = Math.max(
        MIN_SIDEBAR_WIDTH,
        Math.min(MAX_SIDEBAR_WIDTH, inlineWidth),
      );
    }
    shell.style.setProperty("--vx-sidebar-width", "0px");
  }
  updateSidebarToggle();
  return collapsed;
}

export function initSidebarToggle(): void {
  const button = sidebarToggle();
  if (!button) return;
  if (button.dataset.initialized !== "true") {
    button.dataset.initialized = "true";
    button.addEventListener("click", () => {
      toggleSidebar();
    });
  }
  updateSidebarToggle();
}

export function _resetWorkspaceForTest(): void {
  expandedSidebarWidth = DEFAULT_SIDEBAR_WIDTH;
  const shell = workbenchShell();
  shell?.classList.remove("sidebar-collapsed");
  updateSidebarToggle();
}

export function initSidebarResizer(): void {
  const resizer = document.querySelector<HTMLElement>("#sidebar-resizer");
  if (!resizer || resizer.dataset.initialized === "true") return;
  resizer.dataset.initialized = "true";

  resizer.addEventListener("pointerdown", (event: PointerEvent) => {
    event.preventDefault();
    setSidebarWidth(event.clientX);
    resizer.classList.add("dragging");
    resizer.setPointerCapture?.(event.pointerId);

    const onPointerMove = (moveEvent: PointerEvent) => {
      setSidebarWidth(moveEvent.clientX);
    };
    const onPointerUp = (upEvent: PointerEvent) => {
      resizer.classList.remove("dragging");
      resizer.releasePointerCapture?.(upEvent.pointerId);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  });
}

export function isDesktopRuntime(): boolean {
  const win = window as unknown as Record<string, unknown>;
  return Boolean(win.__TAURI_INTERNALS__ || win.__TAURI__);
}

export async function openWorkspacePicker(): Promise<void> {
  if (!isDesktopRuntime()) {
    return;
  }
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    title: "选择项目文件夹",
  });
  if (typeof selected !== "string" || !selected) {
    return;
  }
  await switchWorkspace(selected);
}
