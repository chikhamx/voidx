let threadSelectCb = null;
let newThreadCb = null;
let currentThreads = [];
let newChatBtnBound = false;

export function renderSidebar(threads, activeThreadId) {
  currentThreads = threads;
  const list = document.querySelector("#session-list");
  if (!list) return;

  list.replaceChildren();

  for (const thread of threads) {
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

    item.addEventListener("click", () => {
      if (threadSelectCb) {
        threadSelectCb(thread.thread_id);
      }
    });

    list.append(item);
  }
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

export function _resetForTest() {
  threadSelectCb = null;
  newThreadCb = null;
  currentThreads = [];
  newChatBtnBound = false;
}
