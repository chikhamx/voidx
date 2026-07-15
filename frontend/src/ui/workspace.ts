import {
  MIN_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  uiState,
} from "../services/state";
import { switchWorkspace } from "../services/connection";

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
  const shell = document.querySelector<HTMLElement>(".vx-workbench-shell");
  (shell || document.documentElement).style.setProperty(
    "--vx-sidebar-width",
    `${clamped}px`,
  );
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
