// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  renderSidebar,
  addThread,
  updateThreadStatus,
  filterSessions,
  onThreadSelect,
  onNewThread,
  onThreadFork,
  onThreadDelete,
  onThreadRename,
  _resetForTest,
} from "../src/sidebar";

beforeEach(() => {
  _resetForTest();
  const list = document.querySelector("#session-list");
  if (list) list.innerHTML = "";
});

describe("renderSidebar", () => {
  it("renders session items grouped by directory", () => {
    const threads = [
      { thread_id: "t1", title: "Session 1", status: "idle", directory: "Frameworks" },
      { thread_id: "t2", title: "Session 2", status: "running", directory: "" },
    ];
    renderSidebar(threads, "t1", "voidx");

    const list = document.querySelector("#session-list");
    const groups = list.querySelectorAll(".vx-directory-group");
    expect(groups).toHaveLength(2);

    const rootGroup = list.querySelector('.vx-directory-group[data-directory=""]');
    const fwGroup = list.querySelector('.vx-directory-group[data-directory="Frameworks"]');
    expect(rootGroup).not.toBeNull();
    expect(fwGroup).not.toBeNull();

    const rootLabel = rootGroup.querySelector(".vx-directory-name");
    expect(rootLabel.textContent).toBe("Root");
    const fwLabel = fwGroup.querySelector(".vx-directory-name");
    expect(fwLabel.textContent).toBe("Frameworks");

    const items = list.querySelectorAll(".vx-session-item");
    expect(items).toHaveLength(2);
  });

  it("places root directory group first", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle", directory: "zeta" },
      { thread_id: "t2", title: "B", status: "idle", directory: "" },
      { thread_id: "t3", title: "C", status: "idle", directory: "alpha" },
    ];
    renderSidebar(threads, "t2", "proj");

    const list = document.querySelector("#session-list");
    const groups = list.querySelectorAll(".vx-directory-group");
    expect(groups[0].dataset.directory).toBe("");
    expect(groups[1].dataset.directory).toBe("alpha");
    expect(groups[2].dataset.directory).toBe("zeta");
  });

  it("marks active thread with active class", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
      { thread_id: "t2", title: "B", status: "idle", directory: "" },
    ];
    renderSidebar(threads, "t2", "proj");

    const list = document.querySelector("#session-list");
    const items = list.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(false);
    expect(items[1].classList.contains("active")).toBe(true);
  });

  it("shows running indicator for running sessions", () => {
    const threads = [
      { thread_id: "t1", title: "Running", status: "running", directory: "" },
    ];
    renderSidebar(threads, "t1", "proj");

    const list = document.querySelector("#session-list");
    const item = list.querySelector(".vx-session-item");
    expect(item.classList.contains("running")).toBe(true);
  });

  it("handles empty thread list", () => {
    renderSidebar([], "", "proj");
    const list = document.querySelector("#session-list");
    expect(list.children).toHaveLength(0);
  });

  it("uses thread_id as data attribute", () => {
    renderSidebar([{ thread_id: "abc123", title: "Test", status: "idle", directory: "" }], "abc123", "proj");
    const list = document.querySelector("#session-list");
    const item = list.querySelector(".vx-session-item");
    expect(item.dataset.threadId).toBe("abc123");
  });

  it("normalizes '.' directory as root", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle", directory: "." },
    ];
    renderSidebar(threads, "t1", "proj");
    const list = document.querySelector("#session-list");
    const group = list.querySelector('.vx-directory-group[data-directory=""]');
    expect(group).not.toBeNull();
  });

  it("sets project name in sidebar header", () => {
    renderSidebar([{ thread_id: "t1", title: "A", status: "idle", directory: "" }], "t1", "myproject");
    const header = document.querySelector(".vx-project-name");
    expect(header.textContent).toBe("myproject");
  });
});

describe("addThread", () => {
  it("adds thread to existing directory group", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "Frameworks" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", directory: "Frameworks" }, "t2");

    const group = document.querySelector('.vx-directory-group[data-directory="Frameworks"]');
    const items = group.querySelectorAll(".vx-session-item");
    expect(items).toHaveLength(2);
    expect(items[1].dataset.threadId).toBe("t2");
  });

  it("creates new directory group when thread directory is new", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", directory: "NewDir" }, "t2");

    const group = document.querySelector('.vx-directory-group[data-directory="NewDir"]');
    expect(group).not.toBeNull();
    const item = group.querySelector(".vx-session-item");
    expect(item.dataset.threadId).toBe("t2");
  });

  it("handles directory names with special characters safely", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", directory: 'foo"bar' }, "t2");

    const groups = document.querySelectorAll(".vx-directory-group");
    const matched = [...groups].filter((g) => g.dataset.directory === 'foo"bar');
    expect(matched).toHaveLength(1);
    expect(matched[0].querySelector(".vx-session-item").dataset.threadId).toBe("t2");
  });

  it("removes active class from previous active item", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", directory: "" }, "t2");

    const items = document.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(false);
    expect(items[1].classList.contains("active")).toBe(true);
  });
});

describe("updateThreadStatus", () => {
  it("updates status class without re-rendering list", () => {
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", directory: "" },
    ], "t1", "proj");

    updateThreadStatus("t1", "running");

    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(true);
  });

  it("does nothing for unknown thread_id", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    updateThreadStatus("unknown", "running");
    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(false);
  });
});

describe("filterSessions", () => {
  it("filters sessions by title query and hides empty groups", () => {
    renderSidebar([
      { thread_id: "t1", title: "Python project", status: "idle", directory: "A" },
      { thread_id: "t2", title: "Rust project", status: "idle", directory: "B" },
      { thread_id: "t3", title: "Go stuff", status: "idle", directory: "A" },
    ], "t1", "proj");

    filterSessions("rust");

    const groups = document.querySelectorAll(".vx-directory-group");
    const visibleGroups = [...groups].filter((g) => !g.hidden);
    expect(visibleGroups).toHaveLength(1);
    expect(visibleGroups[0].dataset.directory).toBe("B");

    const visible = [...document.querySelectorAll(".vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(1);
    expect(visible[0].textContent).toContain("Rust");
  });

  it("shows all when query is empty", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
      { thread_id: "t2", title: "B", status: "idle", directory: "" },
    ], "t1", "proj");

    filterSessions("");

    const visible = [...document.querySelectorAll(".vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(2);
  });
});

describe("onThreadSelect", () => {
  it("calls callback when session item is clicked", () => {
    const cb = vi.fn();
    onThreadSelect(cb);

    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "", "proj");

    const item = document.querySelector(".vx-session-item");
    item.click();

    expect(cb).toHaveBeenCalledWith("t1");
  });
});

describe("onNewThread", () => {
  it("calls callback with empty string when global new chat button is clicked", () => {
    const cb = vi.fn();
    onNewThread(cb);

    const btn = document.querySelector("#btn-new-chat");
    btn.click();

    expect(cb).toHaveBeenCalledWith("");
  });

  it("calls callback with directory when directory new chat button is clicked", () => {
    const cb = vi.fn();
    onNewThread(cb);

    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "Frameworks" },
    ], "t1", "proj");

    const dirBtn = document.querySelector('.vx-directory-group[data-directory="Frameworks"] .vx-directory-new-chat');
    dirBtn.click();

    expect(cb).toHaveBeenCalledWith("Frameworks");
  });

  it("calls callback with empty string for root directory new chat button", () => {
    const cb = vi.fn();
    onNewThread(cb);

    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
    ], "t1", "proj");

    const dirBtn = document.querySelector('.vx-directory-group[data-directory=""] .vx-directory-new-chat');
    dirBtn.click();

    expect(cb).toHaveBeenCalledWith("");
  });
});

describe("session item actions", () => {
  it("renders action menu button for each session", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    const menuBtn = item.querySelector(".vx-session-menu-btn");
    expect(menuBtn).not.toBeNull();
  });

  it("shows fork/rename/delete actions when menu button clicked", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    const menuBtn = item.querySelector(".vx-session-menu-btn");
    menuBtn.click();

    expect(item.querySelector('[data-action="fork"]')).not.toBeNull();
    expect(item.querySelector('[data-action="rename"]')).not.toBeNull();
    expect(item.querySelector('[data-action="delete"]')).not.toBeNull();
  });

  it("calls onThreadFork when fork action clicked", () => {
    const cb = vi.fn();
    onThreadFork(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="fork"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadDelete when delete action clicked", () => {
    const cb = vi.fn();
    onThreadDelete(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="delete"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadRename when rename action clicked", () => {
    const cb = vi.fn();
    onThreadRename(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", directory: "" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="rename"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });
});