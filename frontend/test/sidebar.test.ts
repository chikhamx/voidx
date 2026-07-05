// @ts-nocheck
import { readFileSync } from "node:fs";
import { join } from "node:path";
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
  it("renders session items grouped by workspace", () => {
    const threads = [
      { thread_id: "t1", title: "Session 1", status: "idle", workspace: "/Users/me/workspace/voidx", updated_at: "2026-07-05T13:50:00+08:00" },
      { thread_id: "t2", title: "Session 2", status: "running", workspace: "/Users/me/workspace/imcore-sdk", updated_at: "2026-07-04T13:50:00+08:00" },
    ];

    renderSidebar(threads, "t1", "voidx");

    const list = document.querySelector("#session-list");
    const groups = list.querySelectorAll(".vx-workspace-session-group");
    expect(groups).toHaveLength(2);

    const currentGroup = list.querySelector('.vx-workspace-session-group[data-workspace="/Users/me/workspace/voidx"]');
    const otherGroup = list.querySelector('.vx-workspace-session-group[data-workspace="/Users/me/workspace/imcore-sdk"]');
    expect(currentGroup).not.toBeNull();
    expect(otherGroup).not.toBeNull();

    expect(currentGroup.querySelector(".vx-workspace-session-name").textContent).toBe("voidx");
    expect(otherGroup.querySelector(".vx-workspace-session-name").textContent).toBe("imcore-sdk");
    expect(list.querySelectorAll(".vx-session-item")).toHaveLength(2);
    expect(currentGroup.querySelector(".vx-session-item").parentElement.className).toContain("vx-session-children");
  });

  it("does not render the removed current-project card or history heading", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    expect(document.querySelector("#project-list")).toBeNull();
    expect(document.querySelector(".vx-sidebar-history")).toBeNull();
    expect(document.querySelector("#sidebar").textContent).not.toContain("历史会话");
  });

  it("places current workspace group first", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/zeta" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t3", title: "C", status: "idle", workspace: "/tmp/alpha" },
    ];

    renderSidebar(threads, "t2", "voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups[0].dataset.workspace).toBe("/tmp/voidx");
    expect(groups[1].dataset.workspace).toBe("/tmp/alpha");
    expect(groups[2].dataset.workspace).toBe("/tmp/zeta");
  });

  it("merges duplicate workspace entries that share the current project label", () => {
    renderSidebar([
      { thread_id: "t1", title: "Path workspace", status: "idle", workspace: "/Users/me/workspace/voidx" },
      { thread_id: "t2", title: "Fallback workspace", status: "idle", directory: "." },
    ], "t1", "voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups).toHaveLength(1);
    expect(groups[0].querySelector(".vx-workspace-session-name").textContent).toBe("voidx");
    expect(groups[0].querySelectorAll(".vx-session-item")).toHaveLength(2);
  });

  it("collapses and expands a workspace session tree from the row arrow", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t3", title: "C", status: "idle", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/voidx"]');
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(3);

    const toggle = group.querySelector(".vx-workspace-collapse-toggle");
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    toggle.click();

    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    toggle.click();

    expect(group.classList.contains("collapsed")).toBe(false);
    expect(group.querySelector(".vx-session-children").hidden).toBe(false);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(3);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows five more sessions each time and supports collapsing", () => {
    const threads = Array.from({ length: 13 }, (_, index) => ({
      thread_id: `t${index + 1}`,
      title: `Session ${index + 1}`,
      status: "idle",
      workspace: "/Users/me/workspace/voidx",
    }));

    renderSidebar(threads, "t1", "voidx");

    expect(document.querySelectorAll(".vx-session-item")).toHaveLength(5);
    expect(document.querySelector(".vx-session-item[data-thread-id='t6']")).toBeNull();

    let expand = document.querySelector(".vx-workspace-expand-more");
    expect(expand).not.toBeNull();
    expect(expand.textContent).toContain("展开显示");
    expect(document.querySelector(".vx-workspace-collapse")).toBeNull();

    expand.click();

    expect(document.querySelectorAll(".vx-session-item")).toHaveLength(10);
    expect(document.querySelector(".vx-session-item[data-thread-id='t10']")).not.toBeNull();
    expect(document.querySelector(".vx-session-item[data-thread-id='t11']")).toBeNull();
    expect(document.querySelector(".vx-workspace-collapse")).not.toBeNull();

    expand = document.querySelector(".vx-workspace-expand-more");
    expand.click();

    expect(document.querySelectorAll(".vx-session-item")).toHaveLength(13);
    expect(document.querySelector(".vx-session-item[data-thread-id='t13']")).not.toBeNull();
    expect(document.querySelector(".vx-workspace-expand-more")).toBeNull();

    document.querySelector(".vx-workspace-collapse").click();

    expect(document.querySelectorAll(".vx-session-item")).toHaveLength(5);
    expect(document.querySelector(".vx-session-item[data-thread-id='t6']")).toBeNull();
  });

  it("renders expand and collapse controls as adjacent muted text without chevrons", () => {
    const threads = Array.from({ length: 11 }, (_, index) => ({
      thread_id: `t${index + 1}`,
      title: `Session ${index + 1}`,
      status: "idle",
      workspace: "/Users/me/workspace/voidx",
    }));

    renderSidebar(threads, "t1", "voidx");
    document.querySelector(".vx-workspace-expand-more").click();

    const controls = document.querySelector(".vx-workspace-expand-controls");
    const buttons = controls.querySelectorAll(".vx-workspace-expand");

    expect(controls).not.toBeNull();
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toContain("展开显示");
    expect(buttons[1].textContent).toContain("折叠显示");
    expect(controls.querySelector("svg")).toBeNull();
    expect([...buttons].every((button) => button.querySelector(".vx-sidebar-row-icon") === null)).toBe(true);
  });

  it("defines compact hover styling for expand controls", () => {
    const styles = readFileSync(join(process.cwd(), "styles.css"), "utf8");

    expect(styles).toMatch(/\.vx-workspace-expand-controls \{[^}]*display: flex;[^}]*gap: 14px;[^}]*padding-left: 37px;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workspace-expand \{[^}]*color: var\(--vx-text-dim\);[^}]*font-size: 13px;[^}]*width: auto;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workspace-expand:hover \{[^}]*background: transparent;[^}]*color: var\(--vx-text-secondary\);[^}]*\}/);
  });

  it("truncates long session titles and shows relative time", () => {
    renderSidebar([
      {
        thread_id: "t1",
        title: "这是一个特别特别特别长的会话标题，需要在侧栏中截断显示不能把右侧时间挤出去",
        status: "idle",
        workspace: "/Users/me/workspace/voidx",
        updated_at: new Date(Date.now() - 12 * 60 * 1000).toISOString(),
      },
    ], "t1", "voidx");

    const item = document.querySelector(".vx-session-item");
    const title = item.querySelector(".vx-session-title");
    const time = item.querySelector(".vx-session-time");

    expect(title.textContent).toContain("这是一个特别特别");
    expect(time.textContent).toBe("12 分");
    expect(title.className).toContain("vx-session-title");
  });

  it("renders real svg icons for workspace and session rows", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/Users/me/workspace/voidx" },
    ], "t1", "voidx");

    const groupIcon = document.querySelector(".vx-workspace-session-row .vx-sidebar-row-icon svg");
    const sessionIcon = document.querySelector(".vx-session-item .vx-sidebar-row-icon svg");

    expect(groupIcon).not.toBeNull();
    expect(sessionIcon).not.toBeNull();
    expect(document.querySelector("#session-list").textContent).not.toContain("▣");
  });

  it("keeps workspace plus buttons clickable but visually hidden until hover or focus", () => {
    const cb = vi.fn();
    onNewThread(cb);
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");
    const styles = readFileSync(join(process.cwd(), "styles.css"), "utf8");
    const workspaceBtn = document.querySelector(".vx-workspace-session-new-chat");

    expect(workspaceBtn).not.toBeNull();
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-directory-new-chat \{[^}]*opacity: 0;[^}]*pointer-events: auto;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workbench-shell \.vx-directory-row:hover \.vx-directory-new-chat,[\s\S]*\.vx-workbench-shell \.vx-directory-new-chat:focus-visible \{[^}]*opacity: 1;[^}]*\}/);

    workspaceBtn.click();

    expect(cb).toHaveBeenCalledWith("/tmp/proj");
  });

  it("marks active thread with active class", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
    ], "t2", "voidx");

    const items = document.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(false);
    expect(items[1].classList.contains("active")).toBe(true);
  });

  it("shows running indicator for running sessions", () => {
    renderSidebar([
      { thread_id: "t1", title: "Running", status: "running", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    const item = document.querySelector(".vx-session-item");
    expect(item.classList.contains("running")).toBe(true);
  });

  it("shows write-lock waiting badge for sessions waiting on workspace lock", () => {
    renderSidebar([
      { thread_id: "t1", title: "Waiting", status: "waiting_for_write_lock", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    const item = document.querySelector(".vx-session-item");
    const badge = item.querySelector(".vx-session-lock-badge");

    expect(item.classList.contains("waiting-for-write-lock")).toBe(true);
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe("等待写锁");
    expect(badge.getAttribute("title")).toBe("Waiting for workspace write lock");
  });

  it("handles empty thread list", () => {
    renderSidebar([], "", "proj");
    const list = document.querySelector("#session-list");
    expect(list.children).toHaveLength(0);
  });

  it("uses thread_id as data attribute", () => {
    renderSidebar([{ thread_id: "abc123", title: "Test", status: "idle", workspace: "/tmp/proj" }], "abc123", "proj");
    const item = document.querySelector(".vx-session-item");
    expect(item.dataset.threadId).toBe("abc123");
  });

  it("uses project name as workspace fallback", () => {
    renderSidebar([{ thread_id: "t1", title: "A", status: "idle", directory: "." }], "t1", "proj");
    const group = document.querySelector('.vx-workspace-session-group[data-workspace="proj"]');
    expect(group).not.toBeNull();
  });

  it("sets project name in sidebar header", () => {
    renderSidebar([{ thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/myproject" }], "t1", "myproject");
    const header = document.querySelector(".vx-project-name");
    expect(header.textContent).toBe("myproject");
  });
});

describe("addThread", () => {
  it("adds thread to existing workspace group", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" }, "t2");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/proj"]');
    const items = group.querySelectorAll(".vx-session-item");
    expect(items).toHaveLength(2);
    expect(items[0].dataset.threadId).toBe("t2");
  });

  it("creates new workspace group when thread workspace is new", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/other" }, "t2");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/other"]');
    expect(group).not.toBeNull();
    expect(group.querySelector(".vx-session-item").dataset.threadId).toBe("t2");
  });

  it("handles workspace names with special characters safely", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: '/tmp/foo"bar' }, "t2");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    const matched = [...groups].filter((g) => g.dataset.workspace === '/tmp/foo"bar');
    expect(matched).toHaveLength(1);
    expect(matched[0].querySelector(".vx-session-item").dataset.threadId).toBe("t2");
  });

  it("removes active class from previous active item", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" }, "t2");

    const items = document.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(true);
    expect(items[1].classList.contains("active")).toBe(false);
  });
});

describe("updateThreadStatus", () => {
  it("updates status class without re-rendering list", () => {
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    updateThreadStatus("t1", "running");

    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(true);
  });

  it("does nothing for unknown thread_id", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    updateThreadStatus("unknown", "running");
    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(false);
  });
});

describe("filterSessions", () => {
  it("filters sessions by title query and hides empty groups", () => {
    renderSidebar([
      { thread_id: "t1", title: "Python project", status: "idle", workspace: "/tmp/A" },
      { thread_id: "t2", title: "Rust project", status: "idle", workspace: "/tmp/B" },
      { thread_id: "t3", title: "Go stuff", status: "idle", workspace: "/tmp/A" },
    ], "t1", "proj");

    filterSessions("rust");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    const visibleGroups = [...groups].filter((g) => !g.hidden);
    expect(visibleGroups).toHaveLength(1);
    expect(visibleGroups[0].dataset.workspace).toBe("/tmp/B");

    const visible = [...document.querySelectorAll(".vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(1);
    expect(visible[0].textContent).toContain("Rust");
  });

  it("shows all when query is empty", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" },
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

    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "", "proj");

    document.querySelector(".vx-session-item").click();

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

  it("calls callback with workspace when workspace new chat button is clicked", () => {
    const cb = vi.fn();
    onNewThread(cb);

    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    const workspaceBtn = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/proj"] .vx-workspace-session-new-chat');
    workspaceBtn.click();

    expect(cb).toHaveBeenCalledWith("/tmp/proj");
  });

  it("calls callback with project fallback for missing workspace", () => {
    const cb = vi.fn();
    onNewThread(cb);

    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", directory: "" },
    ], "t1", "proj");

    const workspaceBtn = document.querySelector('.vx-workspace-session-group[data-workspace="proj"] .vx-workspace-session-new-chat');
    workspaceBtn.click();

    expect(cb).toHaveBeenCalledWith("proj");
  });
});

describe("session item actions", () => {
  it("renders action menu button for each session", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    const menuBtn = item.querySelector(".vx-session-menu-btn");
    expect(menuBtn).not.toBeNull();
  });

  it("shows fork/rename/delete actions when menu button clicked", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();

    expect(item.querySelector('[data-action="fork"]')).not.toBeNull();
    expect(item.querySelector('[data-action="rename"]')).not.toBeNull();
    expect(item.querySelector('[data-action="delete"]')).not.toBeNull();
  });

  it("calls onThreadFork when fork action clicked", () => {
    const cb = vi.fn();
    onThreadFork(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="fork"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadDelete when delete action clicked", () => {
    const cb = vi.fn();
    onThreadDelete(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="delete"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadRename when rename action clicked", () => {
    const cb = vi.fn();
    onThreadRename(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    const item = document.querySelector(".vx-session-item");
    item.querySelector(".vx-session-menu-btn").click();
    item.querySelector('[data-action="rename"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });
});
