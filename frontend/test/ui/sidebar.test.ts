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
  onThreadDelete,
  onThreadRename,
  findReusableEmptyThread,
  _resetForTest,
} from "../../src/ui/sidebar";

beforeEach(() => {
  _resetForTest();
  const list = document.querySelector("#session-list");
  if (list) list.innerHTML = "";
});

function readCombinedStyles(filePath: string): string {
  let content = readFileSync(filePath, "utf8");
  const importRegex = /@import\s+url\(["']([^"']+)["']\);/g;
  content = content.replace(importRegex, (_, importPath) => {
    const absoluteImportPath = join(join(filePath, ".."), importPath);
    return readCombinedStyles(absoluteImportPath);
  });
  return content;
}

function readStylesCSS(): string {
  return readCombinedStyles(join(process.cwd(), "css/styles.css"));
}

function expandWorkspace(workspace = "") {
  const selector = workspace
    ? `.vx-workspace-session-group[data-workspace="${workspace}"] .vx-workspace-session-row`
    : ".vx-workspace-session-row";
  document.querySelector(selector).click();
}

  it("does not render a workspace icon in the project heading", () => {
    renderSidebar([], "", "voidx", "/tmp/voidx");

    expect(document.querySelector(".vx-project-heading .vx-sidebar-row-icon")).toBeNull();
    expect(document.querySelector("#btn-open-workspace")).not.toBeNull();
  });

  it("uses folder icons for collapsed and expanded workspace groups", () => {
    renderSidebar([
      { thread_id: "folder-thread", title: "Folder session", status: "idle", workspace: "/tmp/folder-workspace" },
      { thread_id: "folder-thread-2", title: "Another session", status: "idle", workspace: "/tmp/folder-workspace" },
    ], "folder-thread", "folder-workspace", "/tmp/folder-workspace");

    const group = document.querySelector<HTMLElement>('.vx-workspace-session-group[data-workspace="/tmp/folder-workspace"]')!;
    const icon = group.querySelector<HTMLElement>(".vx-workspace-session-row .vx-sidebar-row-icon svg")!;
    expect(icon.innerHTML).toContain('M20 20a2 2 0 0 0 2-2V8');

    group.querySelector<HTMLElement>(".vx-workspace-session-row")!.click();

    const expandedIcon = group.querySelector<HTMLElement>(".vx-workspace-session-row .vx-sidebar-row-icon svg")!;
    expect(expandedIcon.innerHTML).toContain('m6 14 1.45-2.7');
  });

  it("uses one unified new-session entry", () => {
    expect(document.querySelectorAll("#btn-new-chat, #btn-new-chat-restricted, #btn-new-loop, #btn-new-goal")).toHaveLength(1);
    expect(document.querySelector("#btn-new-chat").textContent).toContain("新建会话");
  });

  it("uses the explicit temporary marker as the only temporary-slot predicate", () => {
    renderSidebar([
      { thread_id: "legacy", title: "未命名会话", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "temporary", title: "未命名会话", status: "idle", runtime_profile: "chat", temporary: true, workspace: "/tmp/voidx" },
    ], "temporary", "voidx");

    expect(document.querySelector('#temporary-session-list .vx-session-item[data-thread-id="temporary"]')).not.toBeNull();
    expect(document.querySelector('#temporary-session-list .vx-session-item[data-thread-id="legacy"]')).toBeNull();
    expandWorkspace("/tmp/voidx");
    expect(document.querySelector('#session-list .vx-session-item[data-thread-id="legacy"]')).not.toBeNull();
  });

  it("keeps profiled sessions in the project tree without mode dots", () => {
    renderSidebar([
      { thread_id: "chat", title: "Chat", status: "idle", runtime_profile: "chat", workspace: "/tmp/voidx" },
      { thread_id: "goal", title: "Goal", status: "idle", runtime_profile: "goal", workspace: "/tmp/voidx" },
    ], "chat", "voidx");
    expandWorkspace("/tmp/voidx");

    expect(document.querySelector('#session-list .vx-session-item[data-thread-id="chat"]')).not.toBeNull();
    expect(document.querySelector('#session-list .vx-session-item[data-thread-id="goal"]')).not.toBeNull();
    expect(document.querySelector(".vx-session-profile-dot")).toBeNull();
  });


  it("does not reuse a profiled empty session for a generic new session", () => {
    renderSidebar([
      { thread_id: "goal-empty", title: "New session", status: "idle", message_count: 0, runtime_profile: "goal", workspace: "/tmp/voidx" },
    ], null, "voidx", "/tmp/voidx");

    expect(findReusableEmptyThread("/tmp/voidx")).toBeNull();
  });

  it("reuses an empty session only for the requested profile", () => {
    renderSidebar([
      { thread_id: "chat-empty", title: "New session", status: "idle", message_count: 0, runtime_profile: "chat", temporary: true, workspace: "/tmp/voidx" },
      { thread_id: "goal-empty", title: "New session", status: "idle", message_count: 0, runtime_profile: "goal", temporary: true, workspace: "/tmp/voidx" },
    ], null, "voidx", "/tmp/voidx");

    expect(findReusableEmptyThread("/tmp/voidx", "goal")?.thread_id).toBe("goal-empty");
  });

  it.each([
    "running",
    "waiting_for_user",
    "waiting_for_write_lock",
    "cancelling",
    "failed",
  ])("does not reuse an empty %s session", (status) => {
    renderSidebar([
      { thread_id: `${status}-empty`, title: "New session", status, message_count: 0, runtime_profile: "goal", workspace: "/tmp/voidx" },
    ], null, "voidx", "/tmp/voidx");

    expect(findReusableEmptyThread("/tmp/voidx", "goal")).toBeNull();
  });

  it("does not reuse an explicit basename workspace as the current full path", () => {
    renderSidebar([
      { thread_id: "goal-basename", title: "New session", status: "idle", message_count: 0, runtime_profile: "goal", workspace: "voidx" },
    ], null, "voidx", "/tmp/a/voidx");

    expect(findReusableEmptyThread("/tmp/a/voidx", "goal")).toBeNull();
  });

  it("does not reuse an empty profile session from a same-named workspace", () => {
    renderSidebar([
      { thread_id: "goal-other", title: "New session", status: "idle", message_count: 0, runtime_profile: "goal", workspace: "/tmp/b/voidx" },
    ], null, "voidx", "/tmp/a/voidx");

    expect(findReusableEmptyThread("/tmp/a/voidx", "goal")).toBeNull();
  });

  it("keeps mixed legacy unprofiled sessions visible in the project section", () => {
    renderSidebar([
      { thread_id: "legacy", title: "Existing work", status: "idle", message_count: 4, workspace: "/tmp/voidx" },
      { thread_id: "coding", title: "Coding", status: "idle", runtime_profile: "coding", workspace: "/tmp/voidx" },
    ], "legacy", "voidx");

    expect(document.querySelector("#temporary-session-list .vx-session-item")).toBeNull();
    expandWorkspace("/tmp/voidx");

    const legacy = document.querySelector('.vx-session-item[data-thread-id="legacy"]');
    expect(legacy).not.toBeNull();
    expect(legacy?.classList.contains("active")).toBe(true);
    expect(legacy?.textContent).toContain("Existing work");
  });

  it("keeps non-empty default-title legacy sessions in the project section", () => {
    renderSidebar([
      { thread_id: "legacy-default-title", title: "New session", status: "idle", message_count: 1, workspace: "/tmp/voidx" },
      { thread_id: "temporary", title: "New session", status: "idle", message_count: 0, temporary: true, workspace: "/tmp/voidx" },
    ], "legacy-default-title", "voidx");

    expect(document.querySelector("#temporary-session-list .vx-session-item")?.getAttribute("data-thread-id")).toBe("temporary");
    expandWorkspace("/tmp/voidx");

    const legacy = document.querySelector('.vx-session-item[data-thread-id="legacy-default-title"]');
    expect(legacy).not.toBeNull();
    expect(legacy?.classList.contains("active")).toBe(true);
  });

  it("does not let running empty sessions consume the temporary slot", () => {
    renderSidebar([
      { thread_id: "running", title: "New session", status: "running", message_count: 0, temporary: true, workspace: "/tmp/voidx" },
      { thread_id: "temporary", title: "New session", status: "idle", message_count: 0, temporary: true, workspace: "/tmp/voidx" },
    ], "running", "voidx");

    expect(document.querySelector("#temporary-session-list .vx-session-item")?.getAttribute("data-thread-id")).toBe("temporary");
    expandWorkspace("/tmp/voidx");
    expect(document.querySelector('.vx-session-item[data-thread-id="running"]')).not.toBeNull();
  });

  it("keeps at most one reusable temporary session and preserves profiled sessions in the project tree", () => {
    renderSidebar([
      { thread_id: "temp-old", title: "New session", status: "idle", message_count: 0, temporary: true, workspace: "/tmp/voidx" },
      { thread_id: "temp-active", title: "New session", status: "idle", message_count: 0, temporary: true, workspace: "/tmp/voidx" },
      { thread_id: "loop", title: "Loop session", status: "idle", runtime_profile: "loop", workspace: "/tmp/voidx" },
      { thread_id: "goal", title: "Goal session", status: "idle", runtime_profile: "goal", workspace: "/tmp/voidx" },
      { thread_id: "chat", title: "Chat session", status: "idle", runtime_profile: "chat", workspace: "/tmp/voidx" },
    ], "temp-active", "voidx");

    const temporaryItems = document.querySelectorAll("#temporary-session-list .vx-session-item");
    expect(temporaryItems).toHaveLength(1);
    expect(temporaryItems[0].getAttribute("data-thread-id")).toBe("temp-active");
    expandWorkspace("/tmp/voidx");
    expect(document.querySelector("#session-list")?.textContent).toContain("Loop session");
    expect(document.querySelector("#session-list")?.textContent).toContain("Goal session");
    expect(document.querySelector("#session-list")?.textContent).toContain("Chat session");
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
    expect(currentGroup.classList.contains("collapsed")).toBe(true);
    expect(otherGroup.classList.contains("collapsed")).toBe(true);
    expect(currentGroup.querySelector(".vx-session-children").hidden).toBe(true);
    expect(otherGroup.querySelector(".vx-session-children").hidden).toBe(true);
  });

  it("renders the current workspace before session metadata arrives", () => {
    renderSidebar([], "", "voidx", "/Users/me/workspace/voidx");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/Users/me/workspace/voidx"]');
    expect(group).not.toBeNull();
    expect(group.querySelector(".vx-workspace-session-name").textContent).toBe("voidx");
    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
  });

  it("restores the project section after it was bound before sessions arrived", () => {
    renderSidebar([], "", "voidx", "/tmp/voidx");
    renderSidebar([
      { thread_id: "t1", title: "Coding work", status: "idle", runtime_profile: "coding", workspace: "/tmp/voidx" },
    ], "t1", "voidx", "/tmp/voidx");

    const heading = document.querySelector(".vx-project-heading");
    const list = document.querySelector("#session-list");
    expect(list.hidden).toBe(false);

    heading.click();
    expect(list.hidden).toBe(true);

    heading.click();
    expect(list.hidden).toBe(false);
  });

  it("removes the project heading handler when sidebar state is reset", () => {
    const threads = [
      { thread_id: "t1", title: "Coding work", status: "idle", runtime_profile: "coding", workspace: "/tmp/voidx" },
    ];
    renderSidebar(threads, "t1", "voidx", "/tmp/voidx");

    _resetForTest();
    renderSidebar(threads, "t1", "voidx", "/tmp/voidx");

    const heading = document.querySelector<HTMLElement>(".vx-project-heading")!;
    const list = document.querySelector<HTMLElement>("#session-list")!;
    heading.click();
    expect(list.hidden).toBe(true);
  });

  it("compresses collapsed workspace sessions into the workspace row", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/voidx"]');

    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);

    group.querySelector(".vx-workspace-session-row").click();

    expect(group.classList.contains("collapsed")).toBe(false);
    expect(group.querySelector(".vx-session-children").hidden).toBe(false);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(2);
  });

  it("preserves explicitly expanded workspaces on sidebar rerender", () => {
    const threads = [
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
    ];

    renderSidebar(threads, "t1", "voidx");
    expandWorkspace();
    expect(document.querySelectorAll("#session-list .vx-session-item")).toHaveLength(2);

    renderSidebar(threads, "t1", "voidx");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/voidx"]');
    expect(group.classList.contains("collapsed")).toBe(false);
    expect(group.querySelector(".vx-session-children").hidden).toBe(false);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(2);
  });

  it("keeps new workspaces collapsed by default after another workspace was expanded", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
    ], "t1", "voidx");
    expandWorkspace();

    renderSidebar([
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/other" },
    ], "t2", "other", "/tmp/other");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/other"]');
    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);
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

  it("keeps an explicit basename workspace separate from the current full path", () => {
    renderSidebar([
      { thread_id: "current", title: "Current", status: "idle", workspace: "/tmp/a/voidx" },
      { thread_id: "basename", title: "Basename", status: "idle", workspace: "voidx" },
    ], "current", "voidx", "/tmp/a/voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups).toHaveLength(2);
    expect(groups[0].getAttribute("data-workspace")).toBe("/tmp/a/voidx");
    expect(groups[1].getAttribute("data-workspace")).toBe("voidx");
  });

  it("keeps the current full path first instead of treating an explicit basename as current", () => {
    renderSidebar([
      { thread_id: "basename", title: "Basename", status: "idle", workspace: "voidx" },
      { thread_id: "zeta", title: "Zeta", status: "idle", workspace: "/tmp/zeta" },
    ], "basename", "voidx", "/tmp/a/voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups[0].getAttribute("data-workspace")).toBe("/tmp/a/voidx");
  });

  it("keeps same-named workspaces with different full paths separate", () => {
    renderSidebar([
      { thread_id: "current", title: "Current", status: "idle", workspace: "/tmp/a/voidx" },
      { thread_id: "other", title: "Other", status: "idle", workspace: "/tmp/b/voidx" },
    ], "current", "voidx", "/tmp/a/voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups).toHaveLength(2);
    expect(groups[0].getAttribute("data-workspace")).toBe("/tmp/a/voidx");
    expect(groups[1].getAttribute("data-workspace")).toBe("/tmp/b/voidx");
  });

  it("merges duplicate workspace entries that share the current project label", () => {
    renderSidebar([
      { thread_id: "t1", title: "Path workspace", status: "idle", workspace: "/Users/me/workspace/voidx" },
      { thread_id: "t2", title: "Fallback workspace", status: "idle", directory: "." },
    ], "t1", "voidx", "/Users/me/workspace/voidx");

    const groups = document.querySelectorAll(".vx-workspace-session-group");
    expect(groups).toHaveLength(1);
    expect(groups[0].querySelector(".vx-workspace-session-name").textContent).toBe("voidx");
    expandWorkspace();
    expect(groups[0].querySelectorAll(".vx-session-item")).toHaveLength(2);
  });

  it("collapses and expands a workspace session tree by clicking the row", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t3", title: "C", status: "idle", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/voidx"]');
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);
    expect(group.querySelector(".vx-workspace-collapse-toggle")).toBeNull();

    const row = group.querySelector(".vx-workspace-session-row");
    row.click();

    expect(group.classList.contains("collapsed")).toBe(false);
    expect(group.querySelector(".vx-session-children").hidden).toBe(false);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(3);

    row.click();

    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
  });

  it("shows five more sessions each time and supports collapsing", () => {
    const threads = Array.from({ length: 13 }, (_, index) => ({
      thread_id: `t${index + 1}`,
      title: `Session ${index + 1}`,
      status: "idle",
      workspace: "/Users/me/workspace/voidx",
    }));

    renderSidebar(threads, "t1", "voidx");
    expandWorkspace();

    expect(document.querySelectorAll("#session-list .vx-session-item")).toHaveLength(5);
    expect(document.querySelector(".vx-session-item[data-thread-id='t6']")).toBeNull();

    let expand = document.querySelector(".vx-workspace-expand-more");
    expect(expand).not.toBeNull();
    expect(expand.textContent).toBe("展开显示");
    expect(document.querySelector(".vx-workspace-collapse")).toBeNull();

    expand.click();

    expect(document.querySelectorAll("#session-list .vx-session-item")).toHaveLength(10);
    expect(document.querySelector(".vx-session-item[data-thread-id='t10']")).not.toBeNull();
    expect(document.querySelector(".vx-session-item[data-thread-id='t11']")).toBeNull();
    expect(document.querySelector(".vx-workspace-collapse")).not.toBeNull();

    expand = document.querySelector(".vx-workspace-expand-more");
    expand.click();

    expect(document.querySelectorAll("#session-list .vx-session-item")).toHaveLength(13);
    expect(document.querySelector(".vx-session-item[data-thread-id='t13']")).not.toBeNull();
    expect(document.querySelector(".vx-workspace-expand-more")).toBeNull();

    document.querySelector(".vx-workspace-collapse").click();

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/Users/me/workspace/voidx"]');
    expect(group.classList.contains("collapsed")).toBe(true);
    expect(group.querySelector(".vx-session-children").hidden).toBe(true);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);
    expect(group.querySelector(".vx-workspace-expand-controls")).toBeNull();
  });

  it("renders expand and collapse controls as adjacent muted text without chevrons", () => {
    const threads = Array.from({ length: 11 }, (_, index) => ({
      thread_id: `t${index + 1}`,
      title: `Session ${index + 1}`,
      status: "idle",
      workspace: "/Users/me/workspace/voidx",
    }));

    renderSidebar(threads, "t1", "voidx");
    expandWorkspace();
    document.querySelector(".vx-workspace-expand-more").click();

    const controls = document.querySelector(".vx-workspace-expand-controls");
    const buttons = controls.querySelectorAll(".vx-workspace-expand");

    expect(controls).not.toBeNull();
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toBe("展开显示");
    expect(buttons[1].textContent).toContain("折叠显示");
    expect(controls.querySelector("svg")).toBeNull();
    expect([...buttons].every((button) => button.querySelector(".vx-sidebar-row-icon") === null)).toBe(true);
  });

  it("defines compact hover styling for expand controls", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-workspace-expand-controls \{[^}]*display: flex;[^}]*gap: var\(--vx-space-3\);[^}]*padding-left: var\(--vx-space-6\);[^}]*\}/);
    expect(styles).toMatch(/\.vx-workspace-expand \{[^}]*color: var\(--vx-text-dim\);[^}]*font-size: var\(--vx-text-xs\);[^}]*width: auto;[^}]*\}/);
    expect(styles).toMatch(/\.vx-workspace-expand:hover \{[^}]*background: transparent;[^}]*color: var\(--vx-text-secondary\);[^}]*\}/);
  });

  it("keeps collapsed workspace sessions visually hidden despite grid styling", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-session-children\[hidden\] \{[^}]*display: none;[^}]*\}/);
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

    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    const title = item.querySelector(".vx-session-title");
    const time = item.querySelector(".vx-session-time");

    expect(title.textContent).toContain("这是一个特别特别");
    expect(time.textContent).toBe("12 分");
    expect(title.className).toContain("vx-session-title");
  });

  it("renders icons for workspace rows but not for session rows", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/Users/me/workspace/voidx" },
    ], "t1", "voidx");

    expandWorkspace();
    const groupIcon = document.querySelector(".vx-workspace-session-row .vx-sidebar-row-icon svg");
    const sessionItem = document.querySelector("#session-list .vx-session-item");

    expect(groupIcon).not.toBeNull();
    expect(sessionItem.querySelector(":scope > .vx-sidebar-row-icon")).toBeNull();
    expect(document.querySelector("#session-list").textContent).not.toContain("▣");
  });

  it("keeps workspace plus buttons clickable but visually hidden until hover or focus", () => {
    const cb = vi.fn();
    onNewThread(cb);
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");
    const styles = readStylesCSS();
    const workspaceBtn = document.querySelector(".vx-workspace-session-new-chat");

    expect(workspaceBtn).not.toBeNull();
    expect(styles).toMatch(/\.vx-directory-new-chat \{[^}]*opacity: 0;[^}]*\}/);
    expect(styles).toMatch(/\.vx-directory-row:hover \.vx-directory-new-chat,[\s\S]*\.vx-directory-new-chat:focus-visible \{[^}]*opacity: 1;[^}]*\}/);

    workspaceBtn.click();

    expect(cb).toHaveBeenCalledWith("/tmp/proj");
  });

  it("does not render a collapse chevron on the workspace row", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    expect(document.querySelector(".vx-workspace-collapse-toggle")).toBeNull();
  });

  it("marks active thread with active class", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/voidx" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/voidx" },
    ], "t2", "voidx");

    expandWorkspace();
    const items = document.querySelectorAll(".vx-session-item");
    expect(items[0].classList.contains("active")).toBe(false);
    expect(items[1].classList.contains("active")).toBe(true);
  });

  it("shows running indicator for running sessions", () => {
    renderSidebar([
      { thread_id: "t1", title: "Running", status: "running", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    expect(item.classList.contains("running")).toBe(true);
  });

  it("shows write-lock waiting badge for sessions waiting on workspace lock", () => {
    renderSidebar([
      { thread_id: "t1", title: "Waiting", status: "waiting_for_write_lock", workspace: "/tmp/voidx" },
    ], "t1", "voidx");

    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    const badge = item.querySelector(".vx-session-lock-badge");

    expect(item.classList.contains("waiting-for-write-lock")).toBe(true);
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe("等待写锁");
    expect(badge.getAttribute("title")).toBe("Waiting for workspace write lock");
  });

  it("applies status updates received while a workspace is collapsed", () => {
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    updateThreadStatus("t1", "running");
    expandWorkspace();

    expect(document.querySelector(".vx-session-item").classList.contains("running")).toBe(true);
  });

  it("handles empty thread list", () => {
    renderSidebar([], "", "proj");
    const list = document.querySelector("#session-list");
    expect(list.children).toHaveLength(0);
  });

  it("uses thread_id as data attribute", () => {
    renderSidebar([{ thread_id: "abc123", title: "Test", status: "idle", workspace: "/tmp/proj" }], "abc123", "proj");
    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    expect(item.dataset.threadId).toBe("abc123");
  });

  it("uses project name as workspace fallback", () => {
    renderSidebar([{ thread_id: "t1", title: "A", status: "idle", directory: "." }], "t1", "proj");
    const group = document.querySelector('.vx-workspace-session-group[data-workspace="proj"]');
    expect(group).not.toBeNull();
  });

  it("does not render a redundant project name header", () => {
    renderSidebar([{ thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/myproject" }], "t1", "myproject");

    expect(document.querySelector(".vx-project-name")).toBeNull();
    const sidebar = document.querySelector("#sidebar");
    expect(sidebar?.firstElementChild?.id).toBe("runtime-profile-switcher");
    expect(sidebar?.firstElementChild?.nextElementSibling?.classList.contains("vx-sidebar-nav")).toBe(true);
  });
});

describe("addThread", () => {
  it("adds thread to existing workspace group", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" }, "t2");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/proj"]');
    expandWorkspace('/tmp/proj');
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
    expandWorkspace('/tmp/other');
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
    matched[0].querySelector(".vx-workspace-session-row").click();
    expect(matched[0].querySelector(".vx-session-item").dataset.threadId).toBe("t2");
  });

  it("removes active class from previous active item", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    addThread({ thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" }, "t2");

    expandWorkspace('/tmp/proj');
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
    expandWorkspace();

    updateThreadStatus("t1", "running");

    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(true);
  });

  it("does nothing for unknown thread_id", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    expandWorkspace();
    updateThreadStatus("unknown", "running");
    const item = document.querySelector('.vx-session-item[data-thread-id="t1"]');
    expect(item.classList.contains("running")).toBe(false);
  });
});

describe("filterSessions", () => {
  it("keeps explicit basename search results out of the current full-path group", () => {
    renderSidebar([
      { thread_id: "current", title: "Current work", status: "idle", workspace: "/tmp/a/voidx" },
      { thread_id: "basename", title: "Basename result", status: "idle", workspace: "voidx" },
    ], "current", "voidx", "/tmp/a/voidx");

    filterSessions("basename");

    const visibleGroups = [...document.querySelectorAll<HTMLElement>(".vx-workspace-session-group")].filter((group) => !group.hidden);
    expect(visibleGroups).toHaveLength(1);
    expect(visibleGroups[0].dataset.workspace).toBe("voidx");
  });

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

    const visible = [...document.querySelectorAll("#session-list .vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(1);
    expect(visible[0].textContent).toContain("Rust");
  });

  it("filters collapsed sessions in merged current workspace groups", () => {
    renderSidebar([
      { thread_id: "t1", title: "Path workspace", status: "idle", workspace: "/Users/me/workspace/voidx" },
      { thread_id: "t2", title: "Fallback workspace", status: "idle", directory: "." },
    ], "t1", "voidx");

    filterSessions("fallback");

    const visibleGroups = [...document.querySelectorAll(".vx-workspace-session-group")].filter((group) => !group.hidden);
    const visibleItems = [...document.querySelectorAll("#session-list .vx-session-item")].filter((item) => !item.hidden);
    expect(visibleGroups).toHaveLength(1);
    expect(visibleItems).toHaveLength(1);
    expect(visibleItems[0].textContent).toContain("Fallback workspace");
  });

  it("shows all when query is empty", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
      { thread_id: "t2", title: "B", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    expandWorkspace();
    filterSessions("");

    const visible = [...document.querySelectorAll("#session-list .vx-session-item")].filter(
      (el) => !el.hidden,
    );
    expect(visible).toHaveLength(2);
  });

  it("keeps collapsed workspace rows visible when query is empty", () => {
    renderSidebar([
      { thread_id: "t1", title: "A", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    filterSessions("");

    const group = document.querySelector('.vx-workspace-session-group[data-workspace="/tmp/proj"]');
    expect(group.hidden).toBe(false);
    expect(group.querySelectorAll(".vx-session-item")).toHaveLength(0);
  });
});

describe("onThreadSelect", () => {
  it("calls callback when session item is clicked", () => {
    const cb = vi.fn();
    onThreadSelect(cb);

    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "", "proj");

    expandWorkspace();
    document.querySelector(".vx-session-item").click();

    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("marks clicked session active immediately", () => {
    const cb = vi.fn();
    onThreadSelect(cb);

    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" },
      { thread_id: "t2", title: "S2", status: "idle", workspace: "/tmp/proj" },
    ], "t1", "proj");

    expandWorkspace();
    document.querySelector('.vx-session-item[data-thread-id="t2"]').click();

    expect(document.querySelector('.vx-session-item[data-thread-id="t1"]').classList.contains("active")).toBe(false);
    expect(document.querySelector('.vx-session-item[data-thread-id="t2"]').classList.contains("active")).toBe(true);
    expect(cb).toHaveBeenCalledWith("t2");
  });


  it("renders active session titles and workspace labels as text", () => {
    let chatTitle = document.querySelector("#chat-header-title");
    if (!chatTitle) {
      chatTitle = document.createElement("span");
      chatTitle.id = "chat-header-title";
      document.body.append(chatTitle);
    }
    chatTitle.replaceChildren();

    renderSidebar([
      {
        thread_id: "t1",
        title: "<img data-title-xss>",
        status: "idle",
        message_count: 1,
        workspace: "<svg data-workspace-xss>",
      },
      {
        thread_id: "t2",
        title: "<script data-click-xss>bad</script>",
        status: "idle",
        workspace: "<img data-click-workspace-xss>",
      },
    ], "t1", "proj");

    expect(chatTitle.querySelector("[data-title-xss]")).toBeNull();
    expect(chatTitle.querySelector("[data-workspace-xss]")).toBeNull();
    expect(chatTitle.querySelector(".vx-chat-header-session-title").textContent).toBe("<img data-title-xss>");
    expect(chatTitle.querySelector(".vx-chat-header-workspace").textContent).toBe("<svg data-workspace-xss>");

    const targetGroup = Array.from(document.querySelectorAll(".vx-workspace-session-group")).find(
      (group) => (group as HTMLElement).dataset.workspace === "<img data-click-workspace-xss>",
    );
    targetGroup.querySelector(".vx-workspace-session-row").click();

    document.querySelector('.vx-session-item[data-thread-id="t2"]').click();

    expect(chatTitle.querySelector("[data-click-xss]")).toBeNull();
    expect(chatTitle.querySelector("[data-click-workspace-xss]")).toBeNull();
    expect(chatTitle.querySelector(".vx-chat-header-session-title").textContent).toBe("<script data-click-xss>bad</script>");
    expect(chatTitle.querySelector(".vx-chat-header-workspace").textContent).toBe("<img data-click-workspace-xss>");
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

  it("removes the global new chat listener when sidebar state is reset", () => {
    const oldCallback = vi.fn();
    const newCallback = vi.fn();
    onNewThread(oldCallback);

    _resetForTest();
    onNewThread(newCallback);
    document.querySelector<HTMLElement>("#btn-new-chat")!.click();

    expect(oldCallback).not.toHaveBeenCalled();
    expect(newCallback).toHaveBeenCalledTimes(1);
  });

  it("does not expose mode-specific new-session buttons", () => {
    expect(document.querySelector("#btn-new-chat-restricted")).toBeNull();
    expect(document.querySelector("#btn-new-loop")).toBeNull();
    expect(document.querySelector("#btn-new-goal")).toBeNull();
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
  it("renders rename and delete icon buttons for each session", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    expandWorkspace();
    const item = document.querySelector(".vx-session-item");

    expect(item.querySelector('[data-action="rename"]')).not.toBeNull();
    expect(item.querySelector('[data-action="delete"]')).not.toBeNull();
    expect(item.querySelector('[data-action="fork"]')).toBeNull();
    expect(item.querySelector(".vx-session-menu-btn")).toBeNull();
    expect(item.querySelectorAll(".vx-session-action-icon")).toHaveLength(2);
  });

  it("calls onThreadDelete when delete action clicked", () => {
    const cb = vi.fn();
    onThreadDelete(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    item.querySelector('[data-action="delete"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("calls onThreadRename when rename action clicked", () => {
    const cb = vi.fn();
    onThreadRename(cb);
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    expandWorkspace();
    const item = document.querySelector(".vx-session-item");
    item.querySelector('[data-action="rename"]').click();
    expect(cb).toHaveBeenCalledWith("t1");
  });

  it("keeps session action icons hidden until hover or focus", () => {
    renderSidebar([{ thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj" }], "t1", "proj");
    expandWorkspace();
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-session-actions \{[^}]*opacity: 0;[^}]*pointer-events: none;[^}]*\}/);
    expect(styles).toMatch(/\.vx-session-item:hover \.vx-session-actions,[\s\S]*\.vx-session-actions:focus-within \{[^}]*opacity: 1;[^}]*pointer-events: auto;[^}]*\}/);
    expect(styles).toMatch(/\.vx-session-action-icon \{[^}]*height: 24px;[^}]*width: 24px;[^}]*\}/);
    expect(styles).toMatch(/\.vx-session-action-icon:hover,[\s\S]*\.vx-session-action-icon:focus-visible \{[^}]*background: var\(--vx-bg-hover\);[^}]*color: var\(--vx-text-primary\);[^}]*\}/);
  });
});

describe("recent sessions", () => {
  const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60000).toISOString();

  it("renders up to five recent sessions across workspaces, newest first", () => {
    renderSidebar([
      { thread_id: "old-1", title: "Old 1", status: "idle", workspace: "/tmp/A", updated_at: iso(60) },
      { thread_id: "new-1", title: "New 1", status: "idle", workspace: "/tmp/B", updated_at: iso(1) },
      { thread_id: "mid-1", title: "Mid 1", status: "idle", workspace: "/tmp/A", updated_at: iso(10) },
      { thread_id: "new-2", title: "New 2", status: "idle", workspace: "/tmp/C", updated_at: iso(2) },
      { thread_id: "mid-2", title: "Mid 2", status: "idle", workspace: "/tmp/B", updated_at: iso(20) },
      { thread_id: "new-3", title: "New 3", status: "idle", workspace: "/tmp/A", updated_at: iso(3) },
      { thread_id: "old-2", title: "Old 2", status: "idle", workspace: "/tmp/C", updated_at: iso(120) },
    ], null, "voidx");

    const items = [...document.querySelectorAll<HTMLElement>("#recent-session-list .vx-session-item")];
    expect(items.map((item) => item.dataset.threadId)).toEqual(["new-1", "new-2", "new-3", "mid-1", "mid-2"]);
  });

  it("excludes temporary sessions from the recent list", () => {
    renderSidebar([
      { thread_id: "temp", title: "未命名会话", status: "idle", temporary: true, message_count: 0, workspace: "/tmp/proj" },
      { thread_id: "real", title: "Real", status: "idle", workspace: "/tmp/proj", updated_at: iso(5) },
    ], "real", "proj");

    expect(document.querySelector('#recent-session-list .vx-session-item[data-thread-id="temp"]')).toBeNull();
    expect(document.querySelector('#recent-session-list .vx-session-item[data-thread-id="real"]')).not.toBeNull();
  });

  it("hides the recent section when there are no sessions", () => {
    renderSidebar([], null, "voidx");

    expect(document.querySelector<HTMLElement>("#recent-session-section")?.hidden).toBe(true);
  });


  it("collapses and expands recent sessions from its heading", () => {
    renderSidebar([
      { thread_id: "t1", title: "Alpha", status: "idle", workspace: "/tmp/proj", updated_at: iso(5) },
    ], "t1", "proj");

    const heading = document.querySelector<HTMLElement>(".vx-recent-heading")!;
    const list = document.querySelector<HTMLElement>("#recent-session-list")!;
    expect(list.hidden).toBe(false);
    expect(heading.getAttribute("aria-expanded")).toBe("true");

    heading.click();
    expect(list.hidden).toBe(true);
    expect(heading.getAttribute("aria-expanded")).toBe("false");

    heading.click();
    expect(list.hidden).toBe(false);
    expect(heading.getAttribute("aria-expanded")).toBe("true");
  });
  it("hides the recent section while filtering and restores it when cleared", () => {
    renderSidebar([
      { thread_id: "t1", title: "Alpha", status: "idle", workspace: "/tmp/proj", updated_at: iso(5) },
    ], "t1", "proj");

    expect(document.querySelector<HTMLElement>("#recent-session-section")?.hidden).toBe(false);
    filterSessions("alpha");
    expect(document.querySelector<HTMLElement>("#recent-session-section")?.hidden).toBe(true);
    filterSessions("");
    expect(document.querySelector<HTMLElement>("#recent-session-section")?.hidden).toBe(false);
  });

  it("syncs running status across recent and workspace copies", () => {
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj", updated_at: iso(5) },
    ], "t1", "proj");
    expandWorkspace();

    updateThreadStatus("t1", "running");

    const items = [...document.querySelectorAll<HTMLElement>('.vx-session-item[data-thread-id="t1"]')];
    expect(items.length).toBeGreaterThan(1);
    expect(items.every((item) => item.classList.contains("running"))).toBe(true);
  });
  it("syncs active highlight across recent and workspace copies on click", () => {
    const cb = vi.fn();
    onThreadSelect(cb);
    renderSidebar([
      { thread_id: "t1", title: "S1", status: "idle", workspace: "/tmp/proj", updated_at: iso(5) },
    ], "t1", "proj");
    expandWorkspace();

    const recentCopy = document.querySelector<HTMLElement>('#recent-session-list .vx-session-item[data-thread-id="t1"]')!;
    recentCopy.click();

    const copies = [...document.querySelectorAll<HTMLElement>('.vx-session-item[data-thread-id="t1"]')];
    expect(copies.length).toBeGreaterThan(1);
    expect(copies.every((item) => item.classList.contains("active"))).toBe(true);
    expect(cb).toHaveBeenCalledWith("t1");
  });
});

describe("sidebar typography", () => {
  it("uses regular font weight for sidebar rows", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-directory-row \{[^}]*font-weight: 400;[^}]*\}/);
    expect(styles).toMatch(/\.vx-new-chat \{[^}]*font-weight: 400;[^}]*\}/);
  });

  it("keeps the recent heading typography from overriding sidebar heading styles", () => {
    const styles = readStylesCSS();

    expect(styles).not.toMatch(/\.vx-recent-heading\s*\{[^}]*font:\s*inherit;/);
  });

  it("does not use the selected-session background for the current workspace row", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-directory-group\.active \.vx-directory-row \{[^}]*background:\s*transparent;[^}]*\}/);
    expect(styles).toMatch(/\.vx-directory-group\.active \.vx-directory-row:hover \{[^}]*background:\s*var\(--vx-bg-hover\);[^}]*\}/);
  });

  it("lays out session rows without a leading icon column", () => {
    const styles = readStylesCSS();

    expect(styles).toMatch(/\.vx-session-item \{[^}]*grid-template-columns: minmax\(0, 1fr\) max-content;[^}]*\}/);
  });
});
