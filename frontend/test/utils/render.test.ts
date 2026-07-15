// @ts-nocheck
import { describe, it, expect, vi } from "vitest";
import {
  stripRichMarkup,
  nodeClassName,
  diffLineClass,
  formatToolMeta,
  formatElapsed,
  renderNodeElement,
  renderTranscript,
  renderTodoPanel,
  appendNoticeItem,
} from "../../src/utils/render";
import { setTranscriptElement, _resetForTest as resetStreams } from "../../src/utils/stream";

describe("stripRichMarkup", () => {
  it("strips [bold] tags", () => {
    expect(stripRichMarkup("[bold]text[/bold]")).toBe("text");
  });

  it("strips color tags", () => {
    expect(stripRichMarkup("[red]err[/red] ok")).toBe("err ok");
  });

  it("strips hex color tags", () => {
    expect(stripRichMarkup("[#FF0000]hi[/#FF0000]")).toBe("hi");
  });

  it("returns empty string for null", () => {
    expect(stripRichMarkup(null)).toBe("");
  });

  it("returns empty string for undefined", () => {
    expect(stripRichMarkup(undefined)).toBe("");
  });

  it("handles text without tags", () => {
    expect(stripRichMarkup("plain text")).toBe("plain text");
  });
});

describe("nodeClassName", () => {
  it("builds base class from node_type", () => {
    expect(nodeClassName({ node_type: "assistant" })).toBe("node node-assistant");
  });

  it("defaults to message when no node_type", () => {
    expect(nodeClassName({})).toBe("node node-message");
  });

  it("adds error class", () => {
    expect(nodeClassName({ node_type: "tool_call", status: "error" })).toBe(
      "node node-tool_call node-error",
    );
  });

  it("adds running class", () => {
    expect(nodeClassName({ node_type: "thought", status: "running" })).toBe(
      "node node-thought node-running",
    );
  });

  it("adds collapsed class", () => {
    expect(nodeClassName({ node_type: "message", collapsed: true })).toBe(
      "node node-message node-collapsed",
    );
  });
});

describe("diffLineClass", () => {
  it("classifies +++ header as diff-meta", () => {
    expect(diffLineClass("+++ a/file.js")).toBe("diff-meta");
  });

  it("classifies --- header as diff-meta", () => {
    expect(diffLineClass("--- b/file.js")).toBe("diff-meta");
  });

  it("classifies @@ hunk as diff-hunk", () => {
    expect(diffLineClass("@@ -1,3 +1,4 @@")).toBe("diff-hunk");
  });

  it("classifies + as diff-add", () => {
    expect(diffLineClass("+added line")).toBe("diff-add");
  });

  it("classifies - as diff-del", () => {
    expect(diffLineClass("-removed line")).toBe("diff-del");
  });

  it("classifies context as diff-context", () => {
    expect(diffLineClass(" context line")).toBe("diff-context");
  });

  it("classifies plain text as diff-context", () => {
    expect(diffLineClass("plain")).toBe("diff-context");
  });
});

describe("formatToolMeta", () => {
  it("formats tool name with args", () => {
    expect(formatToolMeta({ tool_name: "bash", args: "{cmd: 'ls'}" })).toBe(
      "bash {cmd: 'ls'}",
    );
  });

  it("formats tool name without args", () => {
    expect(formatToolMeta({ tool_name: "read" })).toBe("read");
  });

  it("defaults to 'tool' when no tool_name", () => {
    expect(formatToolMeta({})).toBe("tool");
  });

  it("handles empty args", () => {
    expect(formatToolMeta({ tool_name: "write", args: "" })).toBe("write");
  });
});

describe("formatElapsed", () => {
  it("formats sub-second as milliseconds", () => {
    expect(formatElapsed(0.5)).toBe("500ms");
  });

  it("formats 0.05 as 50ms", () => {
    expect(formatElapsed(0.05)).toBe("50ms");
  });

  it("formats seconds with one decimal", () => {
    expect(formatElapsed(2.34)).toBe("2.3s");
  });

  it("returns empty for null", () => {
    expect(formatElapsed(null)).toBe("");
  });

  it("returns empty for NaN", () => {
    expect(formatElapsed(NaN)).toBe("");
  });
});

describe("renderNodeElement", () => {
  it("returns null for root type", () => {
    expect(renderNodeElement({ node_type: "root", id: "r1" }, new Map())).toBeNull();
  });

  it("returns null for turn type", () => {
    expect(renderNodeElement({ node_type: "turn", id: "t1" }, new Map())).toBeNull();
  });

  it("returns null for todo type", () => {
    expect(renderNodeElement({ node_type: "todo", id: "td1" }, new Map())).toBeNull();
  });

  it("renders assistant node with markdown body", () => {
    const node = {
      node_type: "assistant",
      id: "a1",
      body_lines: ["hello **world**"],
    };
    const el = renderNodeElement(node, new Map([["a1", node]]));
    expect(el.tagName).toBe("ARTICLE");
    expect(el.className).toBe("node node-assistant");
    expect(el.dataset.nodeId).toBe("a1");
    expect(el.querySelector(".markdown-body")).not.toBeNull();
  });

  it("renders tool_call node with code block", () => {
    const node = {
      node_type: "tool_call",
      id: "tc1",
      title: "bash",
      body_lines: ['{"cmd": "ls"}'],
      payload: { tool_name: "bash", args: '{"cmd": "ls"}' },
    };
    const el = renderNodeElement(node, new Map([["tc1", node]]));
    expect(el.className).toBe("node node-tool_call");
    expect(el.querySelector(".node-tool-meta").textContent).toBe('bash {"cmd": "ls"}');
    expect(el.querySelector(".node-code")).not.toBeNull();
  });

  it("renders diff node with diff block", () => {
    const node = {
      node_type: "diff",
      id: "d1",
      title: "changes",
      payload: { diff_text: "+added\n-removed" },
    };
    const el = renderNodeElement(node, new Map([["d1", node]]));
    const diffBlock = el.querySelector(".diff-content");
    expect(diffBlock).not.toBeNull();
    expect(diffBlock.children).toHaveLength(2);
    expect(diffBlock.children[0].className).toBe("diff-add");
    expect(diffBlock.children[1].className).toBe("diff-del");
  });

  it("renders subagent node with card", () => {
    const node = {
      node_type: "subagent",
      id: "sa1",
      title: "child agent",
      payload: { name: "explore", description: "searching codebase" },
    };
    const el = renderNodeElement(node, new Map([["sa1", node]]));
    expect(el.className).toBe("node node-subagent");
    const card = el.querySelector(".subagent-card");
    expect(card).not.toBeNull();
    expect(card.querySelector(".subagent-name").textContent).toBe("explore");
    expect(card.querySelector(".subagent-steps").textContent).toBe("searching codebase");
  });

  it("renders subagent with elapsed time", () => {
    const node = {
      node_type: "subagent",
      id: "sa2",
      title: "child",
      elapsed: 3.5,
      payload: { name: "plan" },
    };
    const el = renderNodeElement(node, new Map([["sa2", node]]));
    expect(el.querySelector(".subagent-elapsed").textContent).toBe("3.5s");
  });

  it("applies indentation based on depth", () => {
    const root = { node_type: "turn", id: "root1" };
    const child = {
      node_type: "message",
      id: "child1",
      parent_id: "root1",
      body_lines: ["nested"],
    };
    const byId = new Map([
      ["root1", root],
      ["child1", child],
    ]);
    const el = renderNodeElement(child, byId);
    expect(el.style.marginLeft).toBe("18px");
  });
});

describe("renderTranscript", () => {
  it("does not persist assistant thinking from transcript snapshot payload", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    renderTranscript(root, {
      nodes: [
        {
          node_type: "assistant",
          id: "a-thinking",
          payload: {
            raw_text: "final answer",
            thinking_text: "checking context",
          },
          body_lines: ["final answer"],
        },
      ],
    });

    const thinking = root.querySelector(".stream-thinking");
    expect(thinking).not.toBeNull();
    expect(thinking.hidden).toBe(true);
    expect(thinking.textContent).not.toContain("checking context");
    expect(root.querySelector(".markdown-body").textContent).toContain("final answer");
  });
});

describe("appendNoticeItem", () => {
  it("renders notices as auto-dismissing toast popups", () => {
    vi.useFakeTimers();
    appendNoticeItem("notice-1", { style: "warning", text: "[yellow]clangd ready[/yellow]" });

    const toastRegion = document.querySelector(".notice-toast-region");
    const item = toastRegion.querySelector(".notice-item.notice-warning");

    expect(toastRegion).not.toBeNull();
    expect(item).not.toBeNull();
    expect(item.dataset.itemId).toBe("notice-1");
    expect(item.textContent).toContain("clangd ready");
    expect(item.classList.contains("notice-toast-exiting")).toBe(false);

    vi.advanceTimersByTime(4000);
    expect(item.classList.contains("notice-toast-exiting")).toBe(true);

    vi.advanceTimersByTime(250);
    expect(document.querySelector(".notice-item.notice-warning")).toBeNull();
    expect(document.querySelector(".notice-toast-region")).toBeNull();
    vi.useRealTimers();
  });
});

describe("renderTodoPanel", () => {
  it("hides panel when no items", () => {
    const panel = document.createElement("section");
    renderTodoPanel(panel, [], "");
    expect(panel.classList.contains("visible")).toBe(false);
  });

  it("hides panel when items is null", () => {
    const panel = document.createElement("section");
    renderTodoPanel(panel, null, "");
    expect(panel.classList.contains("visible")).toBe(false);
  });

  it("shows panel with items and summary", () => {
    const panel = document.createElement("section");
    const items = [
      { id: "t1", content: "task one", status: "done" },
      { id: "t2", content: "task two", status: "active" },
      { id: "t3", content: "task three", status: "pending" },
    ];
    renderTodoPanel(panel, items, "My Plan");
    expect(panel.classList.contains("visible")).toBe(true);
    expect(panel.querySelector(".todo-summary").textContent).toBe("My Plan");
    expect(panel.querySelectorAll(".todo-item")).toHaveLength(3);
  });

  it("shows correct icon for done status", () => {
    const panel = document.createElement("section");
    renderTodoPanel(panel, [{ content: "done task", status: "done" }], "");
    expect(panel.querySelector(".todo-icon").textContent).toBe("\u2713");
  });

  it("shows correct icon for active status", () => {
    const panel = document.createElement("section");
    renderTodoPanel(panel, [{ content: "active task", status: "active" }], "");
    expect(panel.querySelector(".todo-icon").textContent).toBe("\u25B6");
  });

  it("shows correct icon for pending status", () => {
    const panel = document.createElement("section");
    renderTodoPanel(panel, [{ content: "pending task", status: "pending" }], "");
    expect(panel.querySelector(".todo-icon").textContent).toBe("\u25CB");
  });
});
