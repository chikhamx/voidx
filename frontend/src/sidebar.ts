export interface ThreadInfo {
  thread_id: string;
  title?: string;
  status?: string;
  workspace?: string;
  directory?: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
}

interface WorkspaceGroup {
  workspace: string;
  label: string;
  sessions: ThreadInfo[];
}

type ThreadCallback = (threadId: string) => void;
type NewThreadCallback = (directory: string) => void;

const SESSION_PREVIEW_LIMIT = 5;

let threadSelectCb: ThreadCallback | null = null;
let newThreadCb: NewThreadCallback | null = null;
let threadForkCb: ThreadCallback | null = null;
let threadDeleteCb: ThreadCallback | null = null;
let threadRenameCb: ThreadCallback | null = null;
let currentThreads: ThreadInfo[] = [];
let currentProjectName = "Project";
let newChatBtnBound = false;
const workspaceVisibleCounts = new Map<string, number>();
const collapsedWorkspaces = new Set<string>();

function _workspaceBasename(workspace: string): string {
  if (!workspace || workspace === ".") return currentProjectName || "Project";
  return _normalizeWorkspacePath(workspace).replace(/^.*[\\/]/, "") || workspace;
}

function _normalizeWorkspacePath(workspace: string): string {
  return (workspace || "").trim().replace(/[\\/]+$/, "");
}

function _workspaceGroupKey(workspace: string): string {
  const normalized = _normalizeWorkspacePath(workspace);
  const label = _workspaceBasename(normalized);
  if (label === currentProjectName || normalized === currentProjectName) {
    return currentProjectName;
  }
  return normalized || label;
}

function _threadWorkspace(thread: ThreadInfo): string {
  const workspace = (thread.workspace || "").trim();
  if (workspace && workspace !== ".") return _normalizeWorkspacePath(workspace);
  return currentProjectName || "Project";
}

function _sameWorkspace(a: string, b: string): boolean {
  return _workspaceGroupKey(a) === _workspaceGroupKey(b);
}

function _isReusableEmptyThread(thread: ThreadInfo): boolean {
  if (thread.status === "running") return false;
  if (typeof thread.message_count === "number") return thread.message_count === 0;
  const title = (thread.title || "").trim();
  return title === "" || title === "New session" || title === "新对话";
}

function _isCurrentWorkspace(group: WorkspaceGroup): boolean {
  return group.label === currentProjectName || group.workspace === currentProjectName;
}

function _visibleCountForWorkspace(workspace: string, total: number): number {
  return Math.min(workspaceVisibleCounts.get(workspace) || SESSION_PREVIEW_LIMIT, total);
}

function _formatSessionTime(value: string | undefined): string {
  if (!value) return "";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "";
  const diffMinutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
  if (diffMinutes < 60) return `${Math.max(1, diffMinutes)} 分`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} 时`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays} 天`;
  return `${Math.floor(diffDays / 7)} 周`;
}

function _findWorkspaceGroup(list: HTMLElement, workspace: string): HTMLElement | null {
  for (const el of list.querySelectorAll<HTMLElement>(".vx-workspace-session-group")) {
    if (el.dataset.workspace === workspace) return el;
  }
  return null;
}

function groupByWorkspace(threads: ThreadInfo[]): WorkspaceGroup[] {
  const map = new Map<string, WorkspaceGroup>();
  for (const thread of threads) {
    const workspace = _threadWorkspace(thread);
    const key = _workspaceGroupKey(workspace);
    let group = map.get(key);
    if (!group) {
      group = {
        workspace,
        label: _workspaceBasename(workspace),
        sessions: [],
      };
      map.set(key, group);
    }
    group.sessions.push(thread);
  }

  const groups = [...map.values()];
  groups.sort((a, b) => {
    const aCurrent = _isCurrentWorkspace(a);
    const bCurrent = _isCurrentWorkspace(b);
    if (aCurrent && !bCurrent) return -1;
    if (!aCurrent && bCurrent) return 1;
    return a.label.localeCompare(b.label);
  });

  return groups;
}

function _svgIcon(name: "folder" | "message" | "plus" | "more" | "chevron-down" | "chevron-right"): HTMLElement {
  const icon = document.createElement("span");
  icon.className = "vx-sidebar-row-icon";
  const paths: Record<typeof name, string> = {
    folder: '<path d="M3.5 6.5h5l1.4 1.7h6.6v7.3a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2Z"/><path d="M3.5 6.5V5a2 2 0 0 1 2-2h3.2l1.4 1.7h4.4a2 2 0 0 1 2 2v1.7"/>',
    message: '<path d="M5 5.5h10a2 2 0 0 1 2 2v5.8a2 2 0 0 1-2 2H8.4L5 18v-2.7a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2Z"/>',
    plus: '<path d="M10 4.5v11"/><path d="M4.5 10h11"/>',
    more: '<circle cx="5.5" cy="10" r="1"/><circle cx="10" cy="10" r="1"/><circle cx="14.5" cy="10" r="1"/>',
    "chevron-down": '<path d="m5.5 8 4.5 4.5L14.5 8"/>',
    "chevron-right": '<path d="m8 5.5 4.5 4.5L8 14.5"/>',
  };
  icon.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">${paths[name]}</svg>`;
  return icon;
}

export function renderSidebar(threads: ThreadInfo[], activeThreadId: string | null, projectName: string): void {
  currentThreads = threads;
  currentProjectName = projectName || "Project";
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  list.replaceChildren();

  const header = document.querySelector<HTMLElement>(".vx-project-name");
  if (header) header.textContent = currentProjectName;

  const groups = groupByWorkspace(threads);
  for (const group of groups) {
    list.append(_createWorkspaceGroup(group, activeThreadId));
  }
}

export function addThread(thread: ThreadInfo, activeThreadId: string | null): void {
  currentThreads = [
    thread,
    ...currentThreads.filter((existing) => existing.thread_id !== thread.thread_id),
  ];
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  if (activeThreadId) {
    for (const item of list.querySelectorAll<HTMLElement>(".vx-session-item.active")) {
      item.classList.remove("active");
    }
  }

  renderSidebar(currentThreads, activeThreadId, currentProjectName);
}

export function findReusableEmptyThread(directory: string): ThreadInfo | null {
  const workspace = _normalizeWorkspacePath(directory || currentProjectName || "Project");
  return (
    currentThreads.find((thread) => (
      _sameWorkspace(_threadWorkspace(thread), workspace) &&
      _isReusableEmptyThread(thread)
    )) || null
  );
}

function _createWorkspaceGroup(group: WorkspaceGroup, activeThreadId: string | null): HTMLElement {
  const groupEl = document.createElement("div");
  groupEl.className = "vx-workspace-session-group vx-directory-group";
  const collapsed = collapsedWorkspaces.has(group.workspace);
  if (collapsed) {
    groupEl.classList.add("collapsed");
  }
  if (_isCurrentWorkspace(group)) {
    groupEl.classList.add("active");
  }
  groupEl.dataset.workspace = group.workspace;
  groupEl.dataset.directory = group.workspace;

  const row = document.createElement("div");
  row.className = "vx-workspace-session-row vx-directory-row";
  const renderVisibleSessions = (): void => {
    children.replaceChildren();
    if (collapsedWorkspaces.has(group.workspace)) return;
    const count = _visibleCountForWorkspace(group.workspace, group.sessions.length);
    for (const thread of group.sessions.slice(0, count)) {
      children.append(_createSessionItem(thread, activeThreadId));
    }
  };
  const renderExpandControls = (): void => {
    controlsEl?.remove();
    controlsEl = null;
    if (collapsedWorkspaces.has(group.workspace) || group.sessions.length <= SESSION_PREVIEW_LIMIT) {
      return;
    }
    const visibleCount = _visibleCountForWorkspace(group.workspace, group.sessions.length);
    const controls = document.createElement("div");
    controls.className = "vx-workspace-expand-controls";
    controlsEl = controls;

    if (visibleCount < group.sessions.length) {
      const remaining = group.sessions.length - visibleCount;
      const expand = document.createElement("button");
      expand.type = "button";
      expand.className = "vx-workspace-expand vx-workspace-expand-more";
      expand.textContent = `展开显示 ${Math.min(SESSION_PREVIEW_LIMIT, remaining)} 个`;
      expand.addEventListener("click", () => {
        workspaceVisibleCounts.set(
          group.workspace,
          Math.min(visibleCount + SESSION_PREVIEW_LIMIT, group.sessions.length),
        );
        renderVisibleSessions();
        renderExpandControls();
      });
      controls.append(expand);
    }

    if (visibleCount > SESSION_PREVIEW_LIMIT) {
      const collapse = document.createElement("button");
      collapse.type = "button";
      collapse.className = "vx-workspace-expand vx-workspace-collapse";
      collapse.textContent = "折叠显示";
      collapse.addEventListener("click", () => {
        workspaceVisibleCounts.set(group.workspace, SESSION_PREVIEW_LIMIT);
        renderVisibleSessions();
        renderExpandControls();
      });
      controls.append(collapse);
    }

    groupEl.append(controls);
  };

  const collapseToggle = document.createElement("button");
  collapseToggle.type = "button";
  collapseToggle.className = "vx-workspace-collapse-toggle";
  collapseToggle.title = collapsed ? "展开会话" : "折叠会话";
  collapseToggle.setAttribute("aria-label", collapsed ? `展开 ${group.label}` : `折叠 ${group.label}`);
  collapseToggle.setAttribute("aria-expanded", String(!collapsed));
  collapseToggle.append(_svgIcon(collapsed ? "chevron-right" : "chevron-down"));
  collapseToggle.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    const nextCollapsed = !collapsedWorkspaces.has(group.workspace);
    if (!nextCollapsed) {
      collapsedWorkspaces.delete(group.workspace);
    } else {
      collapsedWorkspaces.add(group.workspace);
    }
    groupEl.classList.toggle("collapsed", nextCollapsed);
    children.hidden = nextCollapsed;
    collapseToggle.title = nextCollapsed ? "展开会话" : "折叠会话";
    collapseToggle.setAttribute("aria-label", nextCollapsed ? `展开 ${group.label}` : `折叠 ${group.label}`);
    collapseToggle.setAttribute("aria-expanded", String(!nextCollapsed));
    collapseToggle.replaceChildren(_svgIcon(nextCollapsed ? "chevron-right" : "chevron-down"));
    if (nextCollapsed) {
      children.replaceChildren();
      controlsEl?.remove();
      controlsEl = null;
      return;
    }
    renderVisibleSessions();
    renderExpandControls();
  });
  row.append(collapseToggle);
  row.append(_svgIcon("folder"));

  const name = document.createElement("span");
  name.className = "vx-workspace-session-name vx-directory-name";
  name.textContent = group.label;
  row.append(name);

  const newChatBtn = document.createElement("button");
  newChatBtn.className = "vx-workspace-session-new-chat vx-directory-new-chat";
  newChatBtn.title = "New session";
  newChatBtn.setAttribute("aria-label", `New session in ${group.label}`);
  newChatBtn.append(_svgIcon("plus"));
  newChatBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    if (newThreadCb) newThreadCb(group.workspace);
  });
  row.append(newChatBtn);

  groupEl.append(row);

  const children = document.createElement("div");
  children.className = "vx-session-children";
  children.hidden = collapsed;
  groupEl.append(children);

  let controlsEl: HTMLElement | null = null;
  renderVisibleSessions();
  renderExpandControls();

  return groupEl;
}

function _syncSessionStatusPresentation(item: HTMLElement, status: string | undefined): void {
  const running = status === "running";
  item.classList.toggle("running", running);
  let dot = item.querySelector<HTMLElement>(".vx-session-dot");
  if (running && !dot) {
    dot = document.createElement("span");
    dot.className = "vx-session-dot";
    item.insertBefore(dot, item.querySelector(".vx-session-menu-btn"));
  }
  if (!running) {
    dot?.remove();
  }

  const waitingForWriteLock = status === "waiting_for_write_lock";
  item.classList.toggle("waiting-for-write-lock", waitingForWriteLock);
  let badge = item.querySelector<HTMLElement>(".vx-session-lock-badge");
  if (waitingForWriteLock && !badge) {
    badge = document.createElement("span");
    badge.className = "vx-session-lock-badge";
    badge.textContent = "等待写锁";
    badge.title = "Waiting for workspace write lock";
    item.insertBefore(badge, item.querySelector(".vx-session-menu-btn"));
  }
  if (!waitingForWriteLock) {
    badge?.remove();
  }
}

function _createSessionItem(thread: ThreadInfo, activeThreadId: string | null): HTMLElement {
  const item = document.createElement("div");
  item.className = "vx-session-item";
  item.dataset.threadId = thread.thread_id;
  if (thread.thread_id === activeThreadId) {
    item.classList.add("active");
  }
  item.append(_svgIcon("message"));

  const title = document.createElement("span");
  title.className = "vx-session-title";
  title.textContent = thread.title || thread.thread_id.slice(0, 8);
  item.append(title);

  const timeLabel = _formatSessionTime(thread.updated_at || thread.created_at);
  if (timeLabel) {
    const time = document.createElement("span");
    time.className = "vx-session-time";
    time.textContent = timeLabel;
    item.append(time);
  }

  _syncSessionStatusPresentation(item, thread.status);

  const menuBtn = document.createElement("button");
  menuBtn.className = "vx-session-menu-btn";
  menuBtn.title = "Session actions";
  menuBtn.setAttribute("aria-label", "Session actions");
  menuBtn.append(_svgIcon("more"));
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

  _syncSessionStatusPresentation(item, status);
}

export function filterSessions(query: string): void {
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  const q = (query || "").toLowerCase();
  const groups = list.querySelectorAll<HTMLElement>(".vx-workspace-session-group");
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
  workspaceVisibleCounts.clear();
  collapsedWorkspaces.clear();
}
