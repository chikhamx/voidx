export interface ThreadInfo {
  thread_id: string;
  title?: string;
  status?: string;
  directory?: string;
}

interface DirectoryGroup {
  directory: string;
  label: string;
  sessions: ThreadInfo[];
}

type ThreadCallback = (threadId: string) => void;
type NewThreadCallback = (directory: string) => void;

let threadSelectCb: ThreadCallback | null = null;
let newThreadCb: NewThreadCallback | null = null;
let threadForkCb: ThreadCallback | null = null;
let threadDeleteCb: ThreadCallback | null = null;
let threadRenameCb: ThreadCallback | null = null;
let currentThreads: ThreadInfo[] = [];
let currentProjectName = "Project";
let newChatBtnBound = false;

function _normalizeDirectory(dir: string | undefined): string {
  if (!dir || dir === ".") return "";
  return dir;
}

function _findDirectoryGroup(list: HTMLElement, dir: string): HTMLElement | null {
  for (const el of list.querySelectorAll<HTMLElement>(".vx-directory-group")) {
    if (el.dataset.directory === dir) return el;
  }
  return null;
}

function groupByDirectory(threads: ThreadInfo[]): DirectoryGroup[] {
  const map = new Map<string, ThreadInfo[]>();
  for (const thread of threads) {
    const dir = _normalizeDirectory(thread.directory);
    let arr = map.get(dir);
    if (!arr) {
      arr = [];
      map.set(dir, arr);
    }
    arr.push(thread);
  }

  const groups: DirectoryGroup[] = [];
  for (const [dir, sessions] of map) {
    groups.push({
      directory: dir,
      label: dir === "" ? "Root" : dir,
      sessions,
    });
  }

  groups.sort((a, b) => {
    if (a.directory === "") return -1;
    if (b.directory === "") return 1;
    return a.label.localeCompare(b.label);
  });

  return groups;
}

export function renderSidebar(threads: ThreadInfo[], activeThreadId: string | null, projectName: string): void {
  currentThreads = threads;
  currentProjectName = projectName || "Project";
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  list.replaceChildren();

  const header = document.querySelector<HTMLElement>(".vx-project-name");
  if (header) header.textContent = currentProjectName;

  const groups = groupByDirectory(threads);
  for (const group of groups) {
    list.append(_createDirectoryGroup(group, activeThreadId));
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

  const dir = _normalizeDirectory(thread.directory);
  let groupEl = _findDirectoryGroup(list, dir);
  if (!groupEl) {
    const group: DirectoryGroup = {
      directory: dir,
      label: dir === "" ? "Root" : dir,
      sessions: [thread],
    };
    groupEl = _createDirectoryGroup(group, activeThreadId);
    const groups = groupByDirectory(currentThreads);
    const idx = groups.findIndex((g) => g.directory === dir);
    if (idx <= 0) {
      list.prepend(groupEl);
    } else {
      const prevDir = groups[idx - 1].directory;
      const prevEl = _findDirectoryGroup(list, prevDir);
      if (prevEl && prevEl.nextSibling) {
        list.insertBefore(groupEl, prevEl.nextSibling);
      } else {
        list.append(groupEl);
      }
    }
  } else {
    const children = groupEl.querySelector<HTMLElement>(".vx-session-children");
    if (children) {
      children.append(_createSessionItem(thread, activeThreadId));
    }
  }
}

function _createDirectoryGroup(group: DirectoryGroup, activeThreadId: string | null): HTMLElement {
  const groupEl = document.createElement("div");
  groupEl.className = "vx-directory-group";
  groupEl.dataset.directory = group.directory;

  const row = document.createElement("div");
  row.className = "vx-directory-row";

  const name = document.createElement("span");
  name.className = "vx-directory-name";
  name.textContent = group.label;
  row.append(name);

  const newChatBtn = document.createElement("button");
  newChatBtn.className = "vx-directory-new-chat";
  newChatBtn.textContent = "+";
  newChatBtn.title = "New session";
  newChatBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    if (newThreadCb) newThreadCb(group.directory);
  });
  row.append(newChatBtn);

  groupEl.append(row);

  const children = document.createElement("div");
  children.className = "vx-session-children";
  for (const thread of group.sessions) {
    children.append(_createSessionItem(thread, activeThreadId));
  }
  groupEl.append(children);

  return groupEl;
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
  const groups = list.querySelectorAll<HTMLElement>(".vx-directory-group");
  for (const group of groups) {
    const items = group.querySelectorAll<HTMLElement>(".vx-session-item");
    let anyVisible = false;
    for (const item of items) {
      const title = item.querySelector<HTMLElement>(".vx-session-title");
      const text = (title?.textContent || "").toLowerCase();
      const visible = q === "" || text.includes(q);
      item.hidden = !visible;
      if (visible) anyVisible = true;
    }
    group.hidden = !anyVisible;
  }
}

export function onThreadSelect(callback: ThreadCallback): void {
  threadSelectCb = callback;
}

export function onNewThread(callback: NewThreadCallback): void {
  newThreadCb = callback;
  if (!newChatBtnBound) {
    const btn = document.querySelector<HTMLElement>("#btn-new-chat");
    if (btn) {
      btn.addEventListener("click", () => {
        if (newThreadCb) newThreadCb("");
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
  currentProjectName = "Project";
  newChatBtnBound = false;
}
