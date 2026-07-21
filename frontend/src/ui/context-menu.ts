import { rpcCall } from "../rpc/client";
import { addImageAttachment } from "./image-attachments";

type ContextMenuState = {
  menu: HTMLElement | null;
  attachBtn: HTMLButtonElement | null;
  input: HTMLTextAreaElement | null;
};

let state: ContextMenuState = { menu: null, attachBtn: null, input: null };
let initController: AbortController | null = null;

export function initContextMenu(): void {
  if (initController) return;
  initController = new AbortController();
  const signal = initController.signal;

  state.menu = document.querySelector<HTMLElement>("#context-menu");
  state.attachBtn = document.querySelector<HTMLButtonElement>("#btn-attach");
  state.input = document.querySelector<HTMLTextAreaElement>("#input");

  const menu = state.menu;
  const attachBtn = state.attachBtn;
  if (!menu || !attachBtn) return;

  attachBtn.addEventListener("click", (e: MouseEvent) => {
    e.preventDefault();
    toggleContextMenu();
  }, { signal });

  menu
    .querySelectorAll<HTMLElement>(".context-menu-item:not(.disabled)")
    .forEach((item) => {
      item.addEventListener("click", () => {
        const action = item.dataset.action;
        handleAction(action);
        hideContextMenu();
      }, { signal });
    });

  state.input?.addEventListener("paste", (e: ClipboardEvent) => {
    const file = Array.from(e.clipboardData?.files ?? []).find((f) =>
      f.type.startsWith("image/"),
    );
    if (file) {
      e.preventDefault();
      void uploadImageBlob(file);
    }
  }, { signal });

  document.addEventListener("click", (e: MouseEvent) => {
    if (
      !menu.contains(e.target as Node) &&
      !attachBtn.contains(e.target as Node)
    ) {
      hideContextMenu();
    }
  }, { signal });
}

function toggleContextMenu(): void {
  if (!state.menu || !state.attachBtn) return;
  if (state.menu.hidden) {
    const btnRect = state.attachBtn.getBoundingClientRect();
    state.menu.style.bottom = window.innerHeight - btnRect.top + 8 + "px";
    state.menu.style.left = btnRect.left + "px";
    state.menu.hidden = false;
  } else {
    hideContextMenu();
  }
}

function hideContextMenu(): void {
  if (state.menu) state.menu.hidden = true;
}

function handleAction(action: string | undefined): void {
  switch (action) {
    case "paste":
      pasteFromClipboard();
      break;
    case "model":
      openModelSettings();
      break;
    case "skills":
    case "integrations":
      openIntegrations();
      break;
    case "file":
      void openFilePicker();
      break;
  }
}

async function pasteFromClipboard(): Promise<void> {
  try {
    const items = await navigator.clipboard.read();
    for (const item of items) {
      for (const type of item.types) {
        if (type.startsWith("image/")) {
          const blob = await item.getType(type);
          await uploadImageBlob(blob);
          return;
        }
      }
    }
    alert("剪贴板中没有图片");
  } catch {
    alert(
      "无法读取剪贴板。请在支持 Clipboard API 的浏览器中授予剪贴板权限后重试。",
    );
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      resolve(dataUrl.slice(dataUrl.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function uploadImageBlob(blob: Blob): Promise<void> {
  try {
    const data_base64 = await blobToBase64(blob);
    const result = (await rpcCall("attachments.saveImage", { data_base64 })) as {
      ok?: boolean;
      stem?: string;
      message?: string;
    };
    if (result?.ok && result.stem) {
      addImageAttachment(
        result.stem,
        `data:${blob.type || "image/png"};base64,${data_base64}`,
      );
    } else {
      alert(result?.message || "图片上传失败");
    }
  } catch {
    alert("图片上传失败：无法连接后端");
  }
}


function openIntegrations(): void {
  const btn = document.querySelector<HTMLButtonElement>("#btn-integrations");
  btn?.click();
}

function openModelSettings(): void {
  const btn = document.querySelector<HTMLButtonElement>("#btn-settings");
  btn?.click();
}

function addFileCommand(): void {
  if (!state.input) return;
  state.input.value = `${state.input.value}${state.input.value ? "\n" : ""}/file `;
  state.input.focus();
}

async function openFilePicker(): Promise<void> {
  if (!state.input) return;
  if (!isTauriDesktop()) {
    addFileCommand();
    return;
  }
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({
      multiple: true,
      directory: true,
      title: "选择文件或文件夹",
    });
    const paths = normalizeSelectedPaths(selected);
    if (paths.length === 0) return;
    insertAttachmentPaths(paths);
  } catch (err) {
    console.warn(
      "voidx: native file picker failed",
      err instanceof Error ? err.message : String(err),
    );
    addFileCommand();
  }
}

function isTauriDesktop(): boolean {
  const win = window as unknown as Record<string, unknown>;
  return Boolean(win.__TAURI_INTERNALS__ || win.__TAURI__);
}

function normalizeSelectedPaths(selected: unknown): string[] {
  if (typeof selected === "string") return [selected];
  if (!Array.isArray(selected)) return [];
  return selected.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function insertAttachmentPaths(paths: string[]): void {
  if (!state.input) return;
  const prefix = state.input.value && !/\s$/.test(state.input.value) ? " " : "";
  const attachments = paths.map((path) => `@${path}`).join(" ");
  state.input.value = `${state.input.value}${prefix}${attachments}`;
  state.input.focus();
}

export function _resetContextMenuForTest(): void {
  initController?.abort();
  initController = null;
  state = { menu: null, attachBtn: null, input: null };
}
export {};
