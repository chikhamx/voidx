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
  if (!state.menu || !state.attachBtn) return;

  state.attachBtn.addEventListener("click", (e: MouseEvent) => {
    e.preventDefault();
    toggleContextMenu();
  }, { signal });

  state.menu
    .querySelectorAll<HTMLElement>(".context-menu-item:not(.disabled)")
    .forEach((item) => {
      item.addEventListener("click", () => {
        const action = item.dataset.action;
        handleAction(action);
        hideContextMenu();
      }, { signal });
    });

  document.addEventListener("click", (e: MouseEvent) => {
    if (
      state.menu &&
      !state.menu.contains(e.target as Node) &&
      e.target !== state.attachBtn
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
    case "web":
      openWebContext();
      break;
    case "file":
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
          const reader = new FileReader();
          reader.onload = () => {
            if (state.input) {
              state.input.value += "\n/paste\n";
              state.input.focus();
            }
          };
          reader.readAsDataURL(blob);
          return;
        }
      }
    }
    alert("剪贴板中没有图片");
  } catch {
    alert(
      "无法读取剪贴板。请在支持 Clipboard API 的浏览器中使用 /paste 命令粘贴图片。",
    );
  }
}

function openWebContext(): void {
  const btn = document.querySelector<HTMLButtonElement>("#btn-integrations");
  btn?.click();
}

export function _resetContextMenuForTest(): void {
  initController?.abort();
  initController = null;
  state = { menu: null, attachBtn: null, input: null };
}
