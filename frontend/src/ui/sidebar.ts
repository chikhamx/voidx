import { iconSvg } from "../utils/icons";

export interface ThreadInfo {
  thread_id: string;
  title?: string;
  status?: string;
  workspace?: string;
  directory?: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
  runtime_profile?: string;
}


interface WorkspaceGroup {
  workspace: string;
  label: string;
  sessions: ThreadInfo[];
}

type ThreadCallback = (threadId: string) => void;
type NewThreadCallback = (directory: string, profile?: string) => void;


const SESSION_PREVIEW_LIMIT = 5;

let threadSelectCb: ThreadCallback | null = null;
let newThreadCb: NewThreadCallback | null = null;
let threadDeleteCb: ThreadCallback | null = null;
let threadRenameCb: ThreadCallback | null = null;
let currentThreads: ThreadInfo[] = [];
let currentProjectName = "Project";
let currentWorkspacePath = "";
let newChatBtnBound = false;
let projectExpanded = true;
let projectHeaderBound = false;
let chatExpanded = true;
let chatHeaderBound = false;
const workspaceVisibleCounts = new Map<string, number>();

const expandedWorkspaces = new Set<string>();

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
  return (
    group.label === currentProjectName ||
    group.workspace === currentProjectName ||
    (
      currentWorkspacePath !== "" &&
      _workspaceGroupKey(group.workspace) === _workspaceGroupKey(currentWorkspacePath)
    )
  );
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
  if (currentWorkspacePath) {
    const key = _workspaceGroupKey(currentWorkspacePath);
    if (!map.has(key)) {
      groups.push({
        workspace: currentWorkspacePath,
        label: _workspaceBasename(currentWorkspacePath),
        sessions: [],
      });
    }
  }
  groups.sort((a, b) => {
    const aCurrent = _isCurrentWorkspace(a);
    const bCurrent = _isCurrentWorkspace(b);
    if (aCurrent && !bCurrent) return -1;
    if (!aCurrent && bCurrent) return 1;
    return a.label.localeCompare(b.label);
  });

  return groups;
}

function _svgIcon(name: "folder" | "folder-open" | "message" | "plus" | "pencil" | "trash" | "chevron-down" | "chevron-right" | "terminal"): HTMLElement {
  const icon = document.createElement("span");
  icon.className = "vx-sidebar-row-icon";
  icon.innerHTML = iconSvg(name, 16, 1.5);
  return icon;
}


function _updateDocTitle(activeTitle: string | null): void {
  document.title = "";
}

export function renderSidebar(
  threads: ThreadInfo[],
  activeThreadId: string | null,
  projectName: string,
  workspacePath = "",
): void {
  currentThreads = threads;
  currentProjectName = projectName || "Project";
  currentWorkspacePath = _normalizeWorkspacePath(workspacePath || currentWorkspacePath);
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  const chatHeader = document.querySelector<HTMLElement>("#chat-header");
  if (chatHeader) {
    chatHeader.hidden = !activeThreadId;
  }

  list.replaceChildren();

  const header = document.querySelector<HTMLElement>(".vx-project-name");
  if (header) header.textContent = currentProjectName;

  const codingThreads = threads.filter((t) => t.runtime_profile !== "chat");
  const chatThreads = threads.filter((t) => t.runtime_profile === "chat");

  const groups = groupByWorkspace(codingThreads);
  for (const group of groups) {
    list.append(_createWorkspaceGroup(group, activeThreadId));
  }

  const chatList = document.querySelector<HTMLElement>("#chat-session-list");
  if (chatList) {
    chatList.replaceChildren();
    for (const thread of chatThreads) {
      chatList.append(_createSessionItem(thread, activeThreadId));
    }
    chatList.hidden = !chatExpanded;
  }

  const chatHeading = document.querySelector<HTMLElement>("#chat-heading");
  if (chatHeading) {
    if (!chatHeaderBound) {
      chatHeading.addEventListener("click", () => {
        chatExpanded = !chatExpanded;
        if (chatList) {
          chatList.hidden = !chatExpanded;
        }
      });
      chatHeaderBound = true;
    }
  }



  const sidebarHeader = document.querySelector<HTMLElement>(".vx-project-heading");
  if (sidebarHeader) {
    const folderSpan = sidebarHeader.querySelector<HTMLElement>(".vx-sidebar-row-icon");
    if (folderSpan) {
      const newIcon = _svgIcon(projectExpanded ? "folder-open" : "folder");
      newIcon.className = "vx-sidebar-row-icon";
      folderSpan.replaceWith(newIcon);
    }
    
    if (!projectHeaderBound) {
      sidebarHeader.style.cursor = "pointer";
      sidebarHeader.addEventListener("click", (e: MouseEvent) => {
        if ((e.target as HTMLElement).closest("#btn-open-workspace")) {
          return;
        }
        projectExpanded = !projectExpanded;
        list.hidden = !projectExpanded;
        
        const fs = sidebarHeader.querySelector<HTMLElement>(".vx-sidebar-row-icon");
        if (fs) {
          const newIcon = _svgIcon(projectExpanded ? "folder-open" : "folder");
          newIcon.className = "vx-sidebar-row-icon";
          fs.replaceWith(newIcon);
        }
      });
      projectHeaderBound = true;
    }
  }

  list.hidden = !projectExpanded;

  const activeThread = threads.find((t) => t.thread_id === activeThreadId);
  const activeTitle = activeThread ? (activeThread.title || activeThread.thread_id.slice(0, 8)) : (activeThreadId ? "New session" : null);
  _updateDocTitle(activeTitle);

  const chatTitle = document.querySelector<HTMLElement>("#chat-header-title");
  if (chatTitle) {
    if (activeThreadId && activeTitle) {
      const wsName = activeThread ? _workspaceBasename(_threadWorkspace(activeThread)) : currentProjectName;
      chatTitle.innerHTML = `<span class="vx-chat-header-workspace">${wsName}</span><span class="vx-chat-header-separator"> / </span><span class="vx-chat-header-session-title">${activeTitle}</span>`;
    } else {
      chatTitle.textContent = "";
    }
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

  renderSidebar(currentThreads, activeThreadId, currentProjectName, currentWorkspacePath);
}

export function removeThread(threadId: string, activeThreadId: string | null): void {
  currentThreads = currentThreads.filter((t) => t.thread_id !== threadId);
  renderSidebar(currentThreads, activeThreadId, currentProjectName, currentWorkspacePath);
}

export function findReusableEmptyThread(directory: string, profile?: string): ThreadInfo | null {
  const workspace = _normalizeWorkspacePath(directory || currentProjectName || "Project");
  const targetProfile = profile || "coding";
  return (
    currentThreads.find((thread) => (
      _sameWorkspace(_threadWorkspace(thread), workspace) &&
      _isReusableEmptyThread(thread) &&
      (thread.runtime_profile || "coding") === targetProfile
    )) || null
  );
}


function _createWorkspaceGroup(group: WorkspaceGroup, activeThreadId: string | null): HTMLElement {
  const groupEl = document.createElement("div");
  groupEl.className = "vx-workspace-session-group vx-directory-group";
  const collapsed = !expandedWorkspaces.has(group.workspace);
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
    if (!expandedWorkspaces.has(group.workspace)) return;
    const count = _visibleCountForWorkspace(group.workspace, group.sessions.length);
    for (const thread of group.sessions.slice(0, count)) {
      children.append(_createSessionItem(thread, activeThreadId));
    }
  };
  const renderExpandControls = (): void => {
    controlsEl?.remove();
    controlsEl = null;
    if (!expandedWorkspaces.has(group.workspace) || group.sessions.length <= SESSION_PREVIEW_LIMIT) {
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
      expand.textContent = "展开显示";
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


  let folderIcon = _svgIcon(collapsed ? "folder" : "folder-open");
  row.append(folderIcon);

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

  row.addEventListener("click", (e: MouseEvent) => {
    if ((e.target as HTMLElement).closest(".vx-workspace-session-new-chat")) {
      return;
    }
    const nextCollapsed = expandedWorkspaces.has(group.workspace);
    if (nextCollapsed) {
      expandedWorkspaces.delete(group.workspace);
    } else {
      expandedWorkspaces.add(group.workspace);
    }
    groupEl.classList.toggle("collapsed", nextCollapsed);
    children.hidden = nextCollapsed;

    const newIcon = _svgIcon(nextCollapsed ? "folder" : "folder-open");
    folderIcon.replaceWith(newIcon);
    folderIcon = newIcon;

    if (nextCollapsed) {
      children.replaceChildren();
      controlsEl?.remove();
      controlsEl = null;
      return;
    }
    renderVisibleSessions();
    renderExpandControls();
  });

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
    item.insertBefore(dot, item.querySelector(".vx-session-actions"));
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
    item.insertBefore(badge, item.querySelector(".vx-session-actions"));
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
    const chatTitle = document.querySelector<HTMLElement>("#chat-header-title");
    if (chatTitle) {
      const wsName = _workspaceBasename(_threadWorkspace(thread));
      const activeTitle = thread.title || thread.thread_id.slice(0, 8);
      chatTitle.innerHTML = `<span class="vx-chat-header-workspace">${wsName}</span><span class="vx-chat-header-separator"> / </span><span class="vx-chat-header-session-title">${activeTitle}</span>`;
    }
  }
  const iconName = thread.runtime_profile === "chat" ? "message" : "terminal";
  item.append(_svgIcon(iconName));


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

  item.append(_createSessionActions(thread.thread_id));

  item.addEventListener("click", () => {
    document.querySelectorAll<HTMLElement>(".vx-session-item.active").forEach((activeItem) => {
      activeItem.classList.remove("active");
    });
    item.classList.add("active");
    const activeTitle = thread.title || thread.thread_id.slice(0, 8);
    const chatTitle = document.querySelector<HTMLElement>("#chat-header-title");
    if (chatTitle) {
      const wsName = _workspaceBasename(_threadWorkspace(thread));
      chatTitle.innerHTML = `<span class="vx-chat-header-workspace">${wsName}</span><span class="vx-chat-header-separator"> / </span><span class="vx-chat-header-session-title">${activeTitle}</span>`;
    }
    _updateDocTitle(activeTitle);
    if (threadSelectCb) {
      threadSelectCb(thread.thread_id);
    }
  });

  return item;
}

function _createSessionActions(threadId: string): HTMLElement {
  const actions = document.createElement("div");
  actions.className = "vx-session-actions";

  const renameBtn = document.createElement("button");
  renameBtn.className = "vx-session-action-icon";
  renameBtn.dataset.action = "rename";
  renameBtn.title = "Rename";
  renameBtn.setAttribute("aria-label", "Rename session");
  renameBtn.append(_svgIcon("pencil"));
  renameBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    if (threadRenameCb) threadRenameCb(threadId);
  });

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "vx-session-action-icon";
  deleteBtn.dataset.action = "delete";
  deleteBtn.title = "Delete";
  deleteBtn.setAttribute("aria-label", "Delete session");
  deleteBtn.append(_svgIcon("trash"));
  deleteBtn.addEventListener("click", (e: MouseEvent) => {
    e.stopPropagation();
    if (threadDeleteCb) threadDeleteCb(threadId);
  });

  actions.append(renameBtn, deleteBtn);
  return actions;
}

export function updateThreadStatus(threadId: string, status: string): void {
  const thread = currentThreads.find((t) => t.thread_id === threadId);
  if (thread) {
    thread.status = status;
  }

  const item = document.querySelector<HTMLElement>(
    `.vx-session-item[data-thread-id="${threadId}"]`,
  );
  if (!item) return;

  _syncSessionStatusPresentation(item, status);
}

export function filterSessions(query: string): void {
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  const q = (query || "").toLowerCase();
  const groups = list.querySelectorAll<HTMLElement>(".vx-workspace-session-group");
  for (const group of groups) {
    const workspace = group.dataset.workspace || "";
    const matchingThreads = currentThreads.filter((thread) => (
      _workspaceGroupKey(_threadWorkspace(thread)) === _workspaceGroupKey(workspace) &&
      ((thread.title || thread.thread_id).toLowerCase()).includes(q)
    ));
    if (q !== "" && !expandedWorkspaces.has(workspace) && matchingThreads.length > 0) {
      const activeThreadId = document.querySelector<HTMLElement>(".vx-session-item.active")?.dataset.threadId || null;
      const children = group.querySelector<HTMLElement>(".vx-session-children");
      children?.replaceChildren(...matchingThreads.map((thread) => _createSessionItem(thread, activeThreadId)));
    }
    const items = group.querySelectorAll<HTMLElement>(".vx-session-item");
    let anyVisible = false;
    for (const item of items) {
      const title = item.querySelector<HTMLElement>(".vx-session-title");
      const text = (title?.textContent || "").toLowerCase();
      const visible = q === "" || text.includes(q);
      item.hidden = !visible;
      if (visible) anyVisible = true;
    }
    const children = group.querySelector<HTMLElement>(".vx-session-children");
    if (children) {
      const collapsed = !expandedWorkspaces.has(workspace);
      if (q === "" && collapsed) {
        children.replaceChildren();
      }
      children.hidden = q === "" && collapsed;
    }
    group.hidden = q !== "" && !anyVisible;
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
    const btnRestricted = document.querySelector<HTMLElement>("#btn-new-chat-restricted");
    if (btnRestricted) {
      btnRestricted.addEventListener("click", () => {
        if (newThreadCb) newThreadCb("", "chat");
      });
    }
    newChatBtnBound = true;
  }
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
  threadDeleteCb = null;
  threadRenameCb = null;
  currentThreads = [];
  currentProjectName = "Project";
  currentWorkspacePath = "";
  newChatBtnBound = false;
  projectExpanded = true;
  projectHeaderBound = false;
  chatExpanded = true;
  chatHeaderBound = false;
  workspaceVisibleCounts.clear();
  expandedWorkspaces.clear();
}
