// @ts-nocheck
import { describe, it, expect, beforeEach, vi } from "vitest";
import { handleItem } from "../../src/main";
import { appendMessageItem, appendThoughtItem, handleStatusItem, handleToolItem } from "../../src/utils/render";

beforeEach(() => {
  const transcript = document.querySelector("#transcript");
  if (transcript) transcript.innerHTML = "";
  const todo = document.querySelector("#todo-panel");
  if (todo) {
    todo.innerHTML = "";
    todo.classList.remove("visible");
  }
});

describe("appendMessageItem", () => {
  it("renders text style message with markdown-body", () => {
    appendMessageItem("msg-1", { style: "text", text: "hello world" });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".message-item");
    expect(item).not.toBeNull();
    expect(item.className).toBe("message-item message-text");
    expect(item.dataset.itemId).toBe("msg-1");
    expect(item.querySelector(".markdown-body")).not.toBeNull();
    expect(item.textContent).toContain("hello world");
  });

  it("renders markdown style with markdown-body", () => {
    appendMessageItem("msg-2", { style: "markdown", text: "**bold**" });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".message-item.message-markdown");
    expect(item).not.toBeNull();
    expect(item.querySelector(".markdown-body")).not.toBeNull();
  });

  it("renders guidance style with markdown-body", () => {
    appendMessageItem("msg-3", { style: "guidance", text: "## Tip" });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".message-item.message-guidance");
    expect(item).not.toBeNull();
    expect(item.querySelector(".markdown-body")).not.toBeNull();
  });

  it("defaults to text style when no style given", () => {
    appendMessageItem("msg-4", { text: "plain" });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".message-item.message-text");
    expect(item).not.toBeNull();
  });

  it("handles empty text gracefully", () => {
    appendMessageItem("msg-5", { style: "text", text: "" });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".message-item");
    expect(item).not.toBeNull();
    expect(item.querySelector(".markdown-body")).not.toBeNull();
  });
});

describe("handleToolItem", () => {
  it("creates tool card on item.started", () => {
    handleToolItem("item.started", "t1", {
      tool_call_id: "c1",
      tool_name: "bash",
      args: { cmd: "ls" },
    });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".tool-item");
    expect(item).not.toBeNull();
    expect(item.dataset.toolId).toBe("c1");
    expect(item.dataset.itemId).toBe("t1");
    expect(item.querySelector(".tool-name").textContent).toBe("bash");
    expect(item.querySelector(".tool-spinner").textContent).toBe("running");
  });

  it("renders args as JSON in pre element", () => {
    handleToolItem("item.started", "t2", {
      tool_call_id: "c2",
      tool_name: "read",
      args: { path: "/tmp/file.txt" },
    });
    const transcript = document.querySelector("#transcript");
    const args = transcript.querySelector(".tool-args");
    expect(args).not.toBeNull();
    expect(args.textContent).toContain("path");
    expect(args.textContent).toContain("/tmp/file.txt");
  });

  it("renders string args as-is", () => {
    handleToolItem("item.started", "t3", {
      tool_call_id: "c3",
      tool_name: "search",
      args: "pattern",
    });
    const transcript = document.querySelector("#transcript");
    const args = transcript.querySelector(".tool-args");
    expect(args.textContent).toBe("pattern");
  });

  it("uses label when tool_name missing", () => {
    handleToolItem("item.started", "t4", {
      tool_call_id: "c4",
      label: "custom tool",
    });
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".tool-name").textContent).toBe("custom tool");
  });

  it("defaults to 'tool' when no name or label", () => {
    handleToolItem("item.started", "t5", {
      tool_call_id: "c5",
    });
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".tool-name").textContent).toBe("tool");
  });

  it("appends detail on item.delta", () => {
    handleToolItem("item.started", "t6", {
      tool_call_id: "c6",
      tool_name: "bash",
    });
    handleToolItem("item.delta", "t6", {
      tool_call_id: "c6",
      detail: "command output",
    });
    const transcript = document.querySelector("#transcript");
    const detail = transcript.querySelector(".tool-detail");
    expect(detail).not.toBeNull();
    expect(detail.textContent).toBe("command output");
  });

  it("appends diff_text on item.delta", () => {
    handleToolItem("item.started", "t7", {
      tool_call_id: "c7",
      tool_name: "edit",
    });
    handleToolItem("item.delta", "t7", {
      tool_call_id: "c7",
      diff_text: "+added line",
    });
    const transcript = document.querySelector("#transcript");
    const diff = transcript.querySelector(".diff-content");
    expect(diff).not.toBeNull();
    expect(diff.children[0].className).toBe("diff-add");
  });

  it("updates spinner to done on item.completed with ok=true", () => {
    handleToolItem("item.started", "t8", {
      tool_call_id: "c8",
      tool_name: "bash",
    });
    handleToolItem("item.completed", "t8", {
      tool_call_id: "c8",
      ok: true,
      detail: "success",
    });
    const transcript = document.querySelector("#transcript");
    const status = transcript.querySelector(".tool-status");
    expect(status.textContent).toBe("done");
    expect(status.className).toContain("ok");
  });

  it("updates spinner to failed on item.completed with ok=false", () => {
    handleToolItem("item.started", "t9", {
      tool_call_id: "c9",
      tool_name: "bash",
    });
    handleToolItem("item.completed", "t9", {
      tool_call_id: "c9",
      ok: false,
      detail: "error occurred",
    });
    const transcript = document.querySelector("#transcript");
    const status = transcript.querySelector(".tool-status");
    expect(status.textContent).toBe("failed");
    expect(status.className).toContain("err");
  });

  it("appends detail on item.completed", () => {
    handleToolItem("item.started", "t10", {
      tool_call_id: "c10",
      tool_name: "bash",
    });
    handleToolItem("item.completed", "t10", {
      tool_call_id: "c10",
      ok: true,
      detail: "finished output",
    });
    const transcript = document.querySelector("#transcript");
    const details = transcript.querySelectorAll(".tool-detail");
    expect(details).toHaveLength(1);
    expect(details[0].textContent).toBe("finished output");
  });

  it("truncates extremely long detail on item.completed", () => {
    handleToolItem("item.started", "t12", {
      tool_call_id: "c12",
      tool_name: "bash",
    });
    const longDetail = Array.from({ length: 25 }, (_, i) => `line ${i + 1}`).join("\n");
    handleToolItem("item.completed", "t12", {
      tool_call_id: "c12",
      ok: true,
      detail: longDetail,
    });
    const transcript = document.querySelector("#transcript");
    const details = transcript.querySelectorAll(".tool-detail");
    const lastDetail = details[details.length - 1];
    expect(lastDetail.textContent).toContain("truncated");
    expect(lastDetail.textContent).toContain("15 more lines");
  });


  it("toggles hidden attribute on header click", () => {
    handleToolItem("item.started", "t11", {
      tool_call_id: "c11",
      tool_name: "bash",
    });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".tool-item");
    const body = item.querySelector(".tool-body");
    expect(body.hidden).toBe(true);
    item.querySelector(".tool-header").click();
    expect(body.hidden).toBe(false);
    const chevron = item.querySelector(".tool-chevron");
    expect(chevron.classList.contains("open")).toBe(true);
    item.querySelector(".tool-header").click();
    expect(body.hidden).toBe(true);
  });

  it("shows elapsed on item.completed", () => {
    handleToolItem("item.started", "t12", {
      tool_call_id: "c12",
      tool_name: "bash",
    });
    handleToolItem("item.completed", "t12", {
      tool_call_id: "c12",
      ok: true,
      elapsed: 3.5,
    });
    const transcript = document.querySelector("#transcript");
    const elapsed = transcript.querySelector(".tool-elapsed");
    expect(elapsed).not.toBeNull();
    expect(elapsed.textContent).toBe("3.5s");
  });

  it("renders diff lines with add/del/context classes", () => {
    handleToolItem("item.started", "t13", {
      tool_call_id: "c13",
      tool_name: "edit",
    });
    handleToolItem("item.delta", "t13", {
      tool_call_id: "c13",
      diff_text: "--- a\n+++ b\n@@ -1,2 +1,2 @@\n-old\n+new\n ctx",
    });
    const transcript = document.querySelector("#transcript");
    const diff = transcript.querySelector(".diff-content");
    expect(diff).not.toBeNull();
    expect(diff.children[0].className).toBe("diff-meta");
    expect(diff.children[1].className).toBe("diff-meta");
    expect(diff.children[2].className).toBe("diff-hunk");
    expect(diff.children[3].className).toBe("diff-del");
    expect(diff.children[4].className).toBe("diff-add");
    expect(diff.children[5].className).toBe("diff-context");
  });

  it("groups consecutive tool calls and shows the latest tool summary", () => {
    handleToolItem("item.started", "t14-1", {
      tool_call_id: "c14-1",
      tool_name: "bash",
      raw_args: { command: "ls" },
    });
    handleToolItem("item.started", "t14-2", {
      tool_call_id: "c14-2",
      tool_name: "read",
      args: { path: "/tmp/file.txt" },
    });

    const transcript = document.querySelector("#transcript");
    const group = transcript.querySelector(".tool-group");
    expect(transcript.children).toHaveLength(1);
    expect(group).not.toBeNull();
    expect(group.querySelectorAll(".tool-item")).toHaveLength(2);
    expect(group.querySelector(".tool-group-name").textContent).toContain("ran 1 command, read 1 file");
    expect(group.querySelector(".tool-group-args").textContent).toBe("");
    expect(group.querySelector(".tool-group-body").hidden).toBe(true);
  });

  it("starts a new tool group after a non-tool transcript item", () => {
    handleToolItem("item.started", "t15-1", {
      tool_call_id: "c15-1",
      tool_name: "bash",
    });
    appendMessageItem("m15", { text: "between tools" });
    handleToolItem("item.started", "t15-2", {
      tool_call_id: "c15-2",
      tool_name: "read",
    });

    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelectorAll(".tool-group")).toHaveLength(2);
    expect(transcript.children[1].classList.contains("message-item")).toBe(true);
  });

  it("starts a new tool group across thought items", () => {
    handleToolItem("item.started", "t15a-1", {
      tool_call_id: "c15a-1",
      tool_name: "bash",
    });
    appendThoughtItem("thought-15a", { text: "checking next command" });
    handleToolItem("item.started", "t15a-2", {
      tool_call_id: "c15a-2",
      tool_name: "bash",
    });

    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelectorAll(".tool-group")).toHaveLength(2);
    expect(transcript.querySelectorAll(".thought-item")).toHaveLength(1);
    expect(transcript.querySelectorAll(".tool-item")).toHaveLength(2);
  });

  it("merges adjacent thought items but not thoughts separated by tools", () => {
    appendThoughtItem("thought-1", { text: "first thought" });
    appendThoughtItem("thought-2", { text: "second thought" });
    handleToolItem("item.started", "t15b-1", {
      tool_call_id: "c15b-1",
      tool_name: "bash",
    });
    appendThoughtItem("thought-3", { text: "third thought" });

    const transcript = document.querySelector("#transcript");
    const thoughts = transcript.querySelectorAll(".thought-item");
    expect(thoughts).toHaveLength(2);
    expect(thoughts[0].textContent).toContain("first thought");
    expect(thoughts[0].textContent).toContain("second thought");
    expect(thoughts[1].textContent).toContain("third thought");
  });

  it("expands grouped tool calls three at a time", () => {
    for (let i = 1; i <= 7; i += 1) {
      handleToolItem("item.started", `t16-${i}`, {
        tool_call_id: `c16-${i}`,
        tool_name: `tool-${i}`,
      });
    }

    const transcript = document.querySelector("#transcript");
    const group = transcript.querySelector(".tool-group");
    const body = group.querySelector(".tool-group-body");
    const items = [...group.querySelectorAll(".tool-item")];

    group.querySelector(".tool-group-header").click();

    expect(body.hidden).toBe(false);
    expect(items.filter((item) => !item.hidden)).toHaveLength(3);
    expect(group.querySelector(".tool-group-expand-more")).not.toBeNull();

    group.querySelector(".tool-group-expand-more").click();
    expect(items.filter((item) => !item.hidden)).toHaveLength(6);

    group.querySelector(".tool-group-expand-more").click();
    expect(items.filter((item) => !item.hidden)).toHaveLength(7);
    expect(group.querySelector(".tool-group-expand-more")).toBeNull();
  });

  it("shows verb and highlighted filename target for file edits", () => {
    handleToolItem("item.started", "ts1", {
      tool_call_id: "cs1",
      tool_name: "replace",
      args: { file_path: "/src/utils/manager.py" },
    });
    const transcript = document.querySelector("#transcript");
    const summary = transcript.querySelector(".tool-summary");
    expect(summary.textContent).toContain("edited");
    const target = summary.querySelector(".tool-target");
    expect(target).not.toBeNull();
    expect(target.textContent).toBe("manager.py");
  });

  it("shows full command in the row summary without hard truncation", () => {
    const cmd = "./test.py --backend -- src/tests/test_mcp/test_descriptions.py src/tests/test_mcp/test_mcp.py -q";
    handleToolItem("item.started", "ts2", {
      tool_call_id: "cs2",
      tool_name: "bash",
      raw_args: { command: cmd },
    });
    const transcript = document.querySelector("#transcript");
    const summary = transcript.querySelector(".tool-summary");
    expect(summary.textContent).toContain("ran");
    expect(summary.textContent).toContain(cmd);
  });

  it("shows accumulated diff stats in the row header when diff_text streams in", () => {
    handleToolItem("item.started", "ts3", {
      tool_call_id: "cs3",
      tool_name: "replace",
      args: { file_path: "/src/manager.py" },
    });
    handleToolItem("item.delta", "ts3", {
      tool_call_id: "cs3",
      diff_text: "--- a/manager.py\n+++ b/manager.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+more",
    });
    handleToolItem("item.delta", "ts3", {
      tool_call_id: "cs3",
      diff_text: "@@ -10,1 +10,1 @@\n-old2\n+new2",
    });
    const transcript = document.querySelector("#transcript");
    const stats = transcript.querySelector(".tool-stats");
    expect(stats).not.toBeNull();
    expect(stats.textContent).toContain("+3");
    expect(stats.textContent).toContain("-2");
  });

  it("combines categories in the group summary", () => {
    handleToolItem("item.started", "ts4-1", {
      tool_call_id: "cs4-1",
      tool_name: "replace",
      args: { file_path: "/a.py" },
    });
    handleToolItem("item.started", "ts4-2", {
      tool_call_id: "cs4-2",
      tool_name: "write",
      args: { file_path: "/b.py" },
    });
    handleToolItem("item.started", "ts4-3", {
      tool_call_id: "cs4-3",
      tool_name: "bash",
      raw_args: { command: "ls" },
    });
    const transcript = document.querySelector("#transcript");
    const name = transcript.querySelector(".tool-group-name");
    expect(name.textContent).toContain("edited 2 files");
    expect(name.textContent).toContain("ran 1 command");
  });
});

describe("handleItem routing", () => {
  it("routes message kind to appendMessageItem", () => {
    handleItem("item.started", {
      kind: "message",
      item_id: "m1",
      data: { style: "text", text: "routed" },
    });
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".message-item")).not.toBeNull();
  });

  it("routes tool kind to handleToolItem", () => {
    handleItem("item.started", {
      kind: "tool",
      item_id: "t1",
      data: { tool_call_id: "rc1", tool_name: "bash" },
    });
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".tool-item")).not.toBeNull();
  });

  it("routes assistant_stream kind to stream buffer", () => {
    handleItem("item.started", {
      kind: "assistant_stream",
      item_id: "s1",
      data: { phase: "text" },
    });
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).not.toBeNull();
  });

  it("routes todo kind to renderTodoPanel", () => {
    handleItem("item.started", {
      kind: "todo",
      item_id: "td1",
      data: {
        items: [{ id: "x", content: "task", status: "pending" }],
        summary: "plan",
      },
    });
    const todo = document.querySelector("#todo-panel");
    expect(todo.querySelector(".todo-item")).not.toBeNull();
  });

  it("marks todo items done when todo item completes", () => {
    handleItem("item.started", {
      kind: "todo",
      item_id: "td1",
      data: {
        items: [{ id: "x", content: "task", status: "active" }],
        summary: "running",
      },
    });
    handleItem("item.completed", {
      kind: "todo",
      item_id: "td1",
      data: {},
    });

    const todo = document.querySelector("#todo-panel");
    const item = todo.querySelector(".todo-item");
    expect(item.classList.contains("active")).toBe(false);
    expect(item.classList.contains("done")).toBe(true);
    expect(item.textContent).toContain("task");
  });

  it("routes status kind through started and completed lifecycle", () => {
    handleItem("item.started", {
      kind: "status",
      item_id: "st1",
      data: { status_id: "workflow:tdd", label: "Workflow", detail: "running" },
    });
    const transcript = document.querySelector("#transcript");
    const item = transcript.querySelector(".status-item");
    expect(item).not.toBeNull();
    expect(item.classList.contains("running")).toBe(true);
    expect(item.textContent).toContain("Workflow");

    handleItem("item.completed", {
      kind: "status",
      item_id: "st1",
      data: { status_id: "workflow:tdd", label: "Workflow", ok: true },
    });

    expect(item.classList.contains("running")).toBe(false);
    expect(item.classList.contains("completed")).toBe(true);
  });
});

describe("handleStatusItem", () => {
  it("updates an existing status item by item id", () => {
    handleStatusItem("item.started", "status-1", {
      status_id: "workflow:debug",
      label: "Debug workflow",
      detail: "checking",
    });
    handleStatusItem("item.completed", "status-1", {
      status_id: "workflow:debug",
      ok: true,
    });
    const item = document.querySelector(".status-item");
    expect(item.classList.contains("running")).toBe(false);
    expect(item.classList.contains("completed")).toBe(true);
  });
});
