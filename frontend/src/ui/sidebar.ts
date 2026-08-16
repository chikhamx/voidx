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
  temporary?: boolean;
}


interface WorkspaceGroup {
  workspace: string;
  label: string;
  sessions: ThreadInfo[];
}

type ThreadCallback = (threadId: string) => void;
type NewThreadCallback = (directory: string, profile?: string) => void;


const SESSION_PREVIEW_LIMIT = 5;
const RECENT_SESSION_LIMIT = 5;

let threadSelectCb: ThreadCallback | null = null;
let newThreadCb: NewThreadCallback | null = null;
let threadDeleteCb: ThreadCallback | null = null;
let threadRenameCb: ThreadCallback | null = null;
let currentThreads: ThreadInfo[] = [];
let currentProjectName = "Project";
let currentWorkspacePath = "";
let newChatButton: HTMLElement | null = null;
let projectExpanded = true;
let projectHeaderBound = false;
let projectHeaderEl: HTMLElement | null = null;
let projectSectionHasSessions = false;
let recentExpanded = true;
let recentHeaderBound = false;
let recentHeaderEl: HTMLElement | null = null;
const workspaceVisibleCounts = new Map<string, number>();

const expandedWorkspaces = new Set<string>();

function _workspaceBasename(workspace: string): string {
  if (!workspace || workspace === ".") return currentProjectName || "Project";
  return _normalizeWorkspacePath(workspace).replace(/^.*[\\/]/, "") || workspace;
}


function _renderChatHeaderTitle(workspace: string, title: string): void {
  const chatTitle = document.querySelector<HTMLElement>("#chat-header-title");
  if (!chatTitle) return;

  const workspaceEl = document.createElement("span");
  workspaceEl.className = "vx-chat-header-workspace";
  workspaceEl.textContent = workspace;
  const separatorEl = document.createElement("span");
  separatorEl.className = "vx-chat-header-separator";
  separatorEl.textContent = " / ";
  const titleEl = document.createElement("span");
  titleEl.className = "vx-chat-header-session-title";
  titleEl.textContent = title;
  chatTitle.replaceChildren(workspaceEl, separatorEl, titleEl);
}

function _normalizeWorkspacePath(workspace: string): string {
  return (workspace || "").trim().replace(/[\\/]+$/, "");
}

function _workspaceGroupKey(workspace: string): string {
  const normalized = _normalizeWorkspacePath(workspace);
  if (!normalized || normalized === ".") {
    return currentWorkspacePath || currentProjectName;
  }
  return normalized;
}

function _threadWorkspace(thread: ThreadInfo): string {
  const workspace = (thread.workspace || thread.directory || "").trim();
  if (workspace && workspace !== ".") return _normalizeWorkspacePath(workspace);
  return currentWorkspacePath || currentProjectName || "Project";
}

function _sameWorkspace(a: string, b: string): boolean {
  return _workspaceGroupKey(a) === _workspaceGroupKey(b);
}

function _isReusableEmptyThread(thread: ThreadInfo): boolean {
  if (thread.status === "running") return false;
  if (typeof thread.message_count === "number") return thread.message_count === 0;
  const title = (thread.title || "").trim();
  return title === "" || title === "New session" || title === "新对话" || title === "未命名会话";
}

function _isCurrentWorkspace(group: WorkspaceGroup): boolean {
  if (currentWorkspacePath) {
    return _sameWorkspace(group.workspace, currentWorkspacePath);
  }
  return group.workspace === currentProjectName || group.label === currentProjectName;
}

function _visibleCountForWorkspace(workspace: string, total: number): number {
  return Math.min(workspaceVisibleCounts.get(workspace) || SESSION_PREVIEW_LIMIT, total);
}

function _threadRecency(thread: ThreadInfo): number {
  const timestamp = Date.parse(thread.updated_at || thread.created_at || "");
  return Number.isNaN(timestamp) ? 0 : timestamp;
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

function _svgIcon(name: "folder" | "folder-open" | "plus" | "pencil" | "trash" | "chevron-down" | "chevron-right"): HTMLElement {
  const icon = document.createElement("span");
  icon.className = "vx-sidebar-row-icon";
  icon.innerHTML = iconSvg(name, 16, 1.5);
  return icon;
}


function _updateDocTitle(): void {
  document.title = "";
}


function _handleProjectHeadingClick(event: MouseEvent): void {
  if ((event.target as HTMLElement).closest("#btn-open-workspace")) return;
  projectExpanded = !projectExpanded;
  const list = document.querySelector<HTMLElement>("#session-list");
  if (list) list.hidden = !projectExpanded || !projectSectionHasSessions;
}

function _syncRecentSection(): void {
  const section = document.querySelector<HTMLElement>("#recent-session-section");
  const heading = document.querySelector<HTMLElement>("#recent-session-heading");
  const list = document.querySelector<HTMLElement>("#recent-session-list");
  if (!section || !heading || !list) return;

  const hasSessions = list.childElementCount > 0;
  const visible = !section.hidden && recentExpanded && hasSessions;
  heading.setAttribute("aria-expanded", String(recentExpanded));
  list.hidden = !visible;
}

function _bindRecentHeading(): void {
  const heading = document.querySelector<HTMLElement>("#recent-session-heading");
  if (!heading || heading === recentHeaderEl) return;
  recentHeaderEl?.removeEventListener("click", _handleRecentHeadingClick);
  heading.addEventListener("click", _handleRecentHeadingClick);
  recentHeaderEl = heading;
  recentHeaderBound = true;
}

function _handleRecentHeadingClick(): void {
  recentExpanded = !recentExpanded;
  _syncRecentSection();
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

  const temporaryCandidates = threads.filter(
    (thread) => thread.temporary === true && thread.status === "idle" && _isReusableEmptyThread(thread),
  );
  const activeTemporaryThread = temporaryCandidates.find((thread) => thread.thread_id === activeThreadId);
  const temporaryThreads = activeTemporaryThread
    ? [activeTemporaryThread]
    : temporaryCandidates.slice(0, 1);
  const temporaryCandidateIds = new Set(temporaryCandidates.map((thread) => thread.thread_id));
  const projectThreads = threads.filter((thread) => !temporaryCandidateIds.has(thread.thread_id));

  const temporarySection = document.querySelector<HTMLElement>("#temporary-session-section");
  const temporaryList = document.querySelector<HTMLElement>("#temporary-session-list");
  if (temporaryList) {
    temporaryList.replaceChildren(...temporaryThreads.map((thread) => _createSessionItem(thread, activeThreadId)));
  }
  if (temporarySection) temporarySection.hidden = temporaryThreads.length === 0;

  const recentSection = document.querySelector<HTMLElement>("#recent-session-section");
  const recentList = document.querySelector<HTMLElement>("#recent-session-list");
  if (recentList) {
    const recentThreads = projectThreads
      .filter((thread) => thread.temporary !== true)
      .sort((a, b) => _threadRecency(b) - _threadRecency(a))
      .slice(0, RECENT_SESSION_LIMIT);
    recentList.replaceChildren(...recentThreads.map((thread) => _createSessionItem(thread, activeThreadId)));
    if (recentSection) recentSection.hidden = recentThreads.length === 0;
    _bindRecentHeading();
    _syncRecentSection();
  }

  const groups = groupByWorkspace(projectThreads);
  for (const group of groups) {
    list.append(_createWorkspaceGroup(group, activeThreadId));
  }

  projectSectionHasSessions = projectThreads.length > 0;
  list.hidden = !projectExpanded || !projectSectionHasSessions;


  const sidebarHeader = document.querySelector<HTMLElement>(".vx-project-heading");
  if (sidebarHeader) {
    projectHeaderEl = sidebarHeader;
    sidebarHeader.querySelector<HTMLElement>(".vx-sidebar-row-icon")?.remove();
    if (!projectHeaderBound) {
      sidebarHeader.style.cursor = "pointer";
      sidebarHeader.addEventListener("click", _handleProjectHeadingClick);
      projectHeaderBound = true;
    }
  }

  const activeThread = threads.find((t) => t.thread_id === activeThreadId);
  const activeTitle = activeThread ? (activeThread.title || activeThread.thread_id.slice(0, 8)) : (activeThreadId ? "New session" : null);
  _updateDocTitle();

  if (activeThreadId && activeTitle) {
    const wsName = activeThread ? _workspaceBasename(_threadWorkspace(activeThread)) : currentProjectName;
    _renderChatHeaderTitle(wsName, activeTitle);
  } else {
    document.querySelector<HTMLElement>("#chat-header-title")?.replaceChildren();
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
  return (
    currentThreads.find((thread) =>
      _sameWorkspace(_threadWorkspace(thread), workspace) &&
      thread.temporary === true &&
      thread.status === "idle" &&
      _isReusableEmptyThread(thread) &&
      (profile ? thread.runtime_profile === profile : !thread.runtime_profile)
    ) || null
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
        row.click();
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
      workspaceVisibleCounts.delete(group.workspace);
      renderVisibleSessions();
      renderExpandControls();
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
    const wsName = _workspaceBasename(_threadWorkspace(thread));
    const activeTitle = thread.title || thread.thread_id.slice(0, 8);
    _renderChatHeaderTitle(wsName, activeTitle);
  }
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
    document.querySelectorAll<HTMLElement>(`.vx-session-item[data-thread-id="${thread.thread_id}"]`).forEach((copy) => {
      copy.classList.add("active");
    });
    const activeTitle = thread.title || thread.thread_id.slice(0, 8);
    const wsName = _workspaceBasename(_threadWorkspace(thread));
    _renderChatHeaderTitle(wsName, activeTitle);
    _updateDocTitle();
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

  const items = document.querySelectorAll<HTMLElement>(
    `.vx-session-item[data-thread-id="${threadId}"]`,
  );
  for (const item of items) {
    _syncSessionStatusPresentation(item, status);
  }
}

export function filterSessions(query: string): void {
  const list = document.querySelector<HTMLElement>("#session-list");
  if (!list) return;

  const q = (query || "").toLowerCase();
  const recentSection = document.querySelector<HTMLElement>("#recent-session-section");
  if (recentSection) {
    const recentList = recentSection.querySelector<HTMLElement>("#recent-session-list");
    recentSection.hidden = q !== "" || !recentList || recentList.childElementCount === 0;
    _syncRecentSection();
  }
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

function _handleNewChatClick(): void {
  newThreadCb?.("");
}

export function onNewThread(callback: NewThreadCallback): void {
  newThreadCb = callback;
  const button = document.querySelector<HTMLElement>("#btn-new-chat");
  if (button !== newChatButton) {
    newChatButton?.removeEventListener("click", _handleNewChatClick);
    button?.addEventListener("click", _handleNewChatClick);
    newChatButton = button;
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
  newChatButton?.removeEventListener("click", _handleNewChatClick);
  newChatButton = null;
  projectExpanded = true;
  projectHeaderEl?.removeEventListener("click", _handleProjectHeadingClick);
  projectHeaderEl = null;
  projectHeaderBound = false;
  projectSectionHasSessions = false;
  recentExpanded = true;
  if (recentHeaderBound) {
    recentHeaderEl?.removeEventListener("click", _handleRecentHeadingClick);
  }
  recentHeaderEl = null;
  recentHeaderBound = false;
  workspaceVisibleCounts.clear();
  expandedWorkspaces.clear();
}
