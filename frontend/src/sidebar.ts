export interface ThreadInfo {
  thread_id: string;
  title?: string;
  status?: string;
}

type ThreadCallback = (threadId: string) => void;

let threadSelectCb: ThreadCallback | null = null;
let newThreadCb: (() => void) | null = null;
let threadForkCb: ThreadCallback | null = null;
let threadDeleteCb: ThreadCallback | null = null;
let threadRenameCb: ThreadCallback | null = null;
let currentThreads: ThreadInfo[] = [];
let newChatBtnBound = false;

export function renderSidebar(threads: ThreadInfo[], activeThreadId: string | null): void {
  currentThreads = threads;
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  list.replaceChildren();

  for (const thread of threads) {
    list.append(_createSessionItem(thread, activeThreadId));
  }
}

export function addThread(thread: ThreadInfo, activeThreadId: string | null): void {
  currentThreads.push(thread);
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  if (activeThreadId) {
    for (const item of list.querySelectorAll<HTMLElement>(".vx-session-item.active")) {
      item.classList.remove("active");
    }
  }

  list.append(_createSessionItem(thread, activeThreadId));
}

function _createSessionItem(thread: ThreadInfo, activeThreadId: string | null): HTMLElement {
  const item = document.createElement("div");
  item.className = "vx-session-item";
  item.dataset.threadId = thread.thread_id;
  if (thread.thread_id === activeThreadId) {
    item.classList.add("active");
  }
  if (thread.status === "running") {
    item.classList.add("running");
  }

  const title = document.createElement("span");
  title.className = "vx-session-title";
  title.textContent = thread.title || thread.thread_id.slice(0, 8);
  item.append(title);

  if (thread.status === "running") {
    const dot = document.createElement("span");
    dot.className = "vx-session-dot";
    item.append(dot);
  }

  const menuBtn = document.createElement("button");
  menuBtn.className = "vx-session-menu-btn";
  menuBtn.textContent = "...";
  menuBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    _toggleActionMenu(item);
  });
  item.append(menuBtn);

  item.addEventListener("click", () => {
    if (threadSelectCb) {
      threadSelectCb(thread.thread_id);
    }
  });

  return item;
}

function _toggleActionMenu(item: HTMLElement): void {
  const existing = item.querySelector<HTMLElement>(".vx-session-actions");
  if (existing) {
    existing.remove();
    return;
  }

  const actions = document.createElement("div");
  actions.className = "vx-session-actions";

  const forkBtn = document.createElement("button");
  forkBtn.className = "vx-session-action";
  forkBtn.dataset.action = "fork";
  forkBtn.textContent = "Fork";
  forkBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    actions.remove();
    if (threadForkCb) threadForkCb(item.dataset.threadId!);
  });

  const renameBtn = document.createElement("button");
  renameBtn.className = "vx-session-action";
  renameBtn.dataset.action = "rename";
  renameBtn.textContent = "Rename";
  renameBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    actions.remove();
    if (threadRenameCb) threadRenameCb(item.dataset.threadId!);
  });

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "vx-session-action";
  deleteBtn.dataset.action = "delete";
  deleteBtn.textContent = "Delete";
  deleteBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    actions.remove();
    if (threadDeleteCb) threadDeleteCb(item.dataset.threadId!);
  });

  actions.append(forkBtn, renameBtn, deleteBtn);
  item.append(actions);
}

export function updateThreadStatus(threadId: string, status: string): void {
  const item = document.querySelector<HTMLElement>(
    `.vx-session-item[data-thread-id="${threadId}"]`,
  );
  if (!item) return;

  const thread = currentThreads.find((t) => t.thread_id === threadId);
  if (thread) {
    thread.status = status;
  }

  if (status === "running") {
    item.classList.add("running");
  } else {
    item.classList.remove("running");
  }
}

export function filterSessions(query: string): void {
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  const q = (query || "").toLowerCase();
  const items = list.querySelectorAll<HTMLElement>(".vx-session-item");
  for (const item of items) {
    const title = item.querySelector<HTMLElement>(".vx-session-title");
    const text = (title?.textContent || "").toLowerCase();
    item.hidden = q !== "" && !text.includes(q);
  }
}

export function onThreadSelect(callback: ThreadCallback): void {
  threadSelectCb = callback;
}

export function onNewThread(callback: () => void): void {
  newThreadCb = callback;
  if (!newChatBtnBound) {
    const btn = document.querySelector<HTMLElement>("#btn-new-chat");
    if (btn) {
      btn.addEventListener("click", () => {
        if (newThreadCb) newThreadCb();
      });
    }
    newChatBtnBound = true;
  }
}

export function onThreadFork(callback: ThreadCallback): void {
  threadForkCb = callback;
}

export function onThreadDelete(callback: ThreadCallback): void {
  threadDeleteCb = callback;
}

export function onThreadRename(callback: ThreadCallback): void {
  threadRenameCb = callback;
}

export function _resetForTest(): void {
  threadSelectCb = null;
  newThreadCb = null;
  threadForkCb = null;
  threadDeleteCb = null;
  threadRenameCb = null;
  currentThreads = [];
  newChatBtnBound = false;
}