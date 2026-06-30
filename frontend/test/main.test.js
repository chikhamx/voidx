import { describe, it, expect, beforeEach, vi } from "vitest";
import { handleItem, appendMessageItem, handleToolItem } from "../src/main.js";

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
      tool_name: "grep",
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
    const diff = transcript.querySelector(".tool-diff");
    expect(diff).not.toBeNull();
    expect(diff.textContent).toBe("+added line");
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
    const spinner = transcript.querySelector(".tool-spinner");
    expect(spinner.textContent).toBe("done");
    expect(spinner.className).toContain("ok");
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
    const spinner = transcript.querySelector(".tool-spinner");
    expect(spinner.textContent).toBe("failed");
    expect(spinner.className).toContain("err");
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

  it("status kind produces no transcript children", () => {
    const transcript = document.querySelector("#transcript");
    const before = transcript.children.length;
    handleItem("item.started", {
      kind: "status",
      item_id: "st1",
      data: {},
    });
    expect(transcript.children.length).toBe(before);
  });
});
