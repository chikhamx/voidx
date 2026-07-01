let threadSelectCb = null;
let newThreadCb = null;
let threadForkCb = null;
let threadDeleteCb = null;
let threadRenameCb = null;
let currentThreads = [];
let newChatBtnBound = false;

export function renderSidebar(threads, activeThreadId) {
  currentThreads = threads;
  const list = document.querySelector("#session-list");
  if (!list) return;

  list.replaceChildren();

  for (const thread of threads) {
    list.append(_createSessionItem(thread, activeThreadId));
  }
}

export function addThread(thread, activeThreadId) {
  currentThreads.push(thread);
  const list = document.querySelector("#session-list");
  if (!list) return;

  if (activeThreadId) {
    for (const item of list.querySelectorAll(".vx-session-item.active")) {
      item.classList.remove("active");
    }
  }

  list.append(_createSessionItem(thread, activeThreadId));
}

function _createSessionItem(thread, activeThreadId) {
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
  menuBtn.addEventListener("click", (e) => {
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

function _toggleActionMenu(item) {
  const existing = item.querySelector(".vx-session-actions");
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
  forkBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    actions.remove();
    if (threadForkCb) threadForkCb(item.dataset.threadId);
  });

  const renameBtn = document.createElement("button");
  renameBtn.className = "vx-session-action";
  renameBtn.dataset.action = "rename";
  renameBtn.textContent = "Rename";
  renameBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    actions.remove();
    if (threadRenameCb) threadRenameCb(item.dataset.threadId);
  });

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "vx-session-action";
  deleteBtn.dataset.action = "delete";
  deleteBtn.textContent = "Delete";
  deleteBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    actions.remove();
    if (threadDeleteCb) threadDeleteCb(item.dataset.threadId);
  });

  actions.append(forkBtn, renameBtn, deleteBtn);
  item.append(actions);
}

export function updateThreadStatus(threadId, status) {
  const item = document.querySelector(`.vx-session-item[data-thread-id="${threadId}"]`);
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

export function filterSessions(query) {
  const list = document.querySelector("#session-list");
  if (!list) return;

  const q = (query || "").toLowerCase();
  const items = list.querySelectorAll(".vx-session-item");
  for (const item of items) {
    const title = item.querySelector(".vx-session-title");
    const text = (title?.textContent || "").toLowerCase();
    item.hidden = q !== "" && !text.includes(q);
  }
}

export function onThreadSelect(callback) {
  threadSelectCb = callback;
}

export function onNewThread(callback) {
  newThreadCb = callback;
  if (!newChatBtnBound) {
    const btn = document.querySelector("#btn-new-chat");
    if (btn) {
      btn.addEventListener("click", () => {
        if (newThreadCb) newThreadCb();
      });
    }
    newChatBtnBound = true;
  }
}

export function onThreadFork(callback) {
  threadForkCb = callback;
}

export function onThreadDelete(callback) {
  threadDeleteCb = callback;
}

export function onThreadRename(callback) {
  threadRenameCb = callback;
}

export function _resetForTest() {
  threadSelectCb = null;
  newThreadCb = null;
  threadForkCb = null;
  threadDeleteCb = null;
  threadRenameCb = null;
  currentThreads = [];
  newChatBtnBound = false;
}