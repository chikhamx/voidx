// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  renderSidebar,
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
  it("renders session items for each thread", () => {
    const threads = [
      { thread_id: "t1", title: "Session 1", status: "idle" },
      { thread_id: "t2", title: "Session 2", status: "running" },
    ];
    renderSidebar(threads, "t1");

    const list = document.querySelector("#session-list");
    const items = list.querySelectorAll(".vx-session-item");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Session 1");
    expect(items[1].textContent).toContain("Session 2");
  });

  it("marks active thread with active class", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle" },
      { thread_id: "t2", title: "B", status: "idle" },
    ];
    renderSidebar(threads, "t2");

    const list = document.querySelector("#session-list");
    const items = list.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(false);
    expect(items[1].classList.contains("active")).toBe(true);
  });

  it("shows running indicator for running sessions", () => {
    const threads = [
      { thread_id: "t1", title: "Running", status: "running" },
    ];
    renderSidebar(threads, "t1");

    const list = document.querySelector("#session-list");
    const item = list.querySelector(".vx-session-item");
    expect(item.classList.contains("running")).toBe(true);
  });

  it("handles empty thread list", () => {
    renderSidebar([], "");
    const list = document.querySelector("#session-list");
    expect(list.children).toHaveLength(0);
  });

  it("uses thread_id as data attribute", () => {
    renderSidebar([{ thread_id: "abc123", title: "Test", status: "idle" }], "abc123");
    const list = document.querySelector("#session-list");
    const item = list.querySelector(".vx-session-item");
    expect(item.dataset.threadId).toBe("abc123");
  });
});

describe("updateThreadStatus", () => {
  it("updates status class without re-rendering list", () => {
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle" },
    ], "t1");

    updateThreadStatus("t1", "running");

    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(true);
  });

  it("does nothing for unknown thread_id", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
    updateThreadStatus("unknown", "running");
    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(false);
  });
});

describe("filterSessions", () => {
  it("filters sessions by title query", () => {
    renderSidebar([
      { thread_id: "t1", title: "Python project", status: "idle" },
      { thread_id: "t2", title: "Rust project", status: "idle" },
      { thread_id: "t3", title: "Go stuff", status: "idle" },
    ], "t1");

    filterSessions("rust");

    const list = document.querySelector("#session-list");
    const visible = [...list.querySelectorAll(".vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(1);
    expect(visible[0].textContent).toContain("Rust");
  });

  it("shows all when query is empty", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle" },
      { thread_id: "t2", title: "B", status: "idle" },
    ], "t1");

    filterSessions("");

    const list = document.querySelector("#session-list");
    const visible = [...list.querySelectorAll(".vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(2);
  });
});

describe("onThreadSelect", () => {
  it("calls callback when session item is clicked", () => {
    const cb = vi.fn();
    onThreadSelect(cb);

    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "");

    const item = document.querySelector(".vx-session-item");
    item.click();

    expect(cb).toHaveBeenCalledWith("t1");
  });
});

describe("onNewThread", () => {
  it("calls callback when new chat button is clicked", () => {
    const cb = vi.fn();
    onNewThread(cb);

    const btn = document.querySelector("#btn-new-chat");
    btn.click();

    expect(cb).toHaveBeenCalled();
  });
});

describe("session item actions", () => {
  it("renders action menu button for each session", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
    const item = document.querySelector(".vx-session-item");
    const menuBtn = item.querySelector(".vx-session-menu-btn");
    expect(menuBtn).not.toBeNull();
  });

  it("shows fork/rename/delete actions when menu button clicked", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
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
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="fork"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadDelete when delete action clicked", () => {
    const cb = vi.fn();
    onThreadDelete(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="delete"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadRename when rename action clicked", () => {
    const cb = vi.fn();
    onThreadRename(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle" }], "t1");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="rename"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });
});