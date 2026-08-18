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
  appendThoughtItem,
  handleToolItem,
} from "../../src/utils/render";
import {
  setTranscriptElement,
  _resetForTest as resetStreams,
  appendStreamText,
  commitStream,
} from "../../src/utils/stream";

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
  it("preserves assistant thinking when rebuilding from a transcript snapshot", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const snapshot = {
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
    };
    renderTranscript(root, snapshot);
    renderTranscript(root, snapshot);

    const thought = root.querySelector(".thought-item");
    expect(thought).not.toBeNull();
    expect(thought?.textContent).toContain("checking context");
    expect(root.querySelector(".stream-buffer .markdown-body")?.textContent).toContain("final answer");
  });

  it("keeps assistant thought blocks before their replies after snapshot rebuild", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const snapshot = {
      nodes: [
        { node_type: "message", id: "before", payload: { style: "text", raw_text: "before" } },
        {
          node_type: "assistant",
          id: "assistant-1",
          payload: { raw_text: "first answer", thinking_text: "first thought" },
          body_lines: ["first answer"],
        },
        { node_type: "message", id: "between", payload: { style: "text", raw_text: "between" } },
        {
          node_type: "assistant",
          id: "assistant-2",
          payload: { raw_text: "second answer", thinking_text: "second thought" },
          body_lines: ["second answer"],
        },
      ],
    };

    renderTranscript(root, snapshot);
    renderTranscript(root, snapshot);

    const order = Array.from(root.children).map((el) => {
      if (el.classList.contains("thought-item")) return el.dataset.itemId;
      if (el.classList.contains("stream-buffer")) return el.dataset.streamId;
      return el.dataset.itemId || el.className;
    });
    expect(order).toEqual([
      "before",
      "assistant-1-thought",
      "assistant-1",
      "between",
      "assistant-2-thought",
      "assistant-2",
    ]);
  });
  it("keeps persisted thought nodes in transcript order", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const snapshot = {
      nodes: [
        { node_type: "turn", id: "turn-1", header: "request" },
        {
          node_type: "thought",
          id: "thought-1",
          payload: { raw_text: "first thought" },
          body_lines: ["first thought"],
        },
        {
          node_type: "assistant",
          id: "assistant-1",
          payload: { raw_text: "first answer" },
          body_lines: ["first answer"],
        },
        {
          node_type: "thought",
          id: "thought-2",
          payload: { raw_text: "second thought" },
          body_lines: ["second thought"],
        },
        {
          node_type: "assistant",
          id: "assistant-2",
          payload: { raw_text: "second answer" },
          body_lines: ["second answer"],
        },
      ],
    };

    renderTranscript(root, snapshot);
    renderTranscript(root, snapshot);

    const order = Array.from(root.children).map((el) => {
      if (el.classList.contains("thought-item")) return el.dataset.itemId;
      if (el.classList.contains("stream-buffer")) return el.dataset.streamId;
      return el.dataset.itemId || el.className;
    });
    expect(order).toEqual([
      "turn-1",
      "thought-1",
      "assistant-1",
      "thought-2",
      "assistant-2",
    ]);
  });


  it("deduplicates a committed Markdown reply against its snapshot source", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    appendStreamText("reply-markdown", "**第一条回复**", "text");
    commitStream("reply-markdown");

    const snapshot = {
      nodes: [
        {
          node_type: "assistant",
          id: "assistant-markdown",
          payload: { raw_text: "**第一条回复**" },
          body_lines: ["**第一条回复**"],
        },
      ],
    };
    renderTranscript(root, snapshot);
    renderTranscript(root, snapshot);

    expect(root.querySelectorAll(".stream-buffer")).toHaveLength(1);
    expect(root.textContent).toContain("第一条回复");
  });

  it("restores formatted tool arguments in a snapshot without an args body", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    renderTranscript(root, {
      nodes: [
        { node_type: "turn", id: "turn-tool-summary", header: "request" },
        {
          node_type: "tool_call",
          id: "tool-summary",
          tool_call_id: "tool-summary-call",
          status: "done",
          payload: {
            tool_name: "bash",
            args: 'command="[cyan]pytest -q[/cyan]"',
            raw_args: { command: "pytest -q" },
            summary: "2 passed",
          },
        },
      ],
    });

    const item = root.querySelector<HTMLElement>(".tool-item");
    expect(item?.querySelector(".tool-summary")?.textContent).toContain("pytest -q");
    expect(item?.querySelector(".tool-body .tool-args")).toBeNull();
    item?.querySelector<HTMLElement>(".tool-header")?.click();
    expect(item?.querySelector(".tool-body")?.textContent).toContain("2 passed");
  });

  it("restores one file change card for tool diffs in a snapshot turn", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);
    const snapshot = {
      nodes: [
        { node_type: "turn", id: "turn-files", header: "user request" },
        {
          node_type: "tool_call",
          id: "tool-files-1",
          tool_call_id: "call-files-1",
          status: "done",
          payload: {
            tool_name: "replace",
            diff_text: "--- a/src/one.ts\n+++ b/src/one.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
          },
        },
        {
          node_type: "tool_call",
          id: "tool-files-2",
          tool_call_id: "call-files-2",
          status: "done",
          payload: {
            tool_name: "write",
            diff_text: "--- a/src/two.ts\n+++ b/src/two.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
          },
        },
      ],
    };

    renderTranscript(root, snapshot);

    expect(root.querySelectorAll(".file-change-card")).toHaveLength(1);
    expect(root.querySelectorAll(".file-change-row")).toHaveLength(2);
    expect(root.querySelector(".tool-body .diff-content")).toBeNull();

    renderTranscript(root, snapshot);

    expect(root.querySelectorAll(".file-change-card")).toHaveLength(1);
    expect(root.querySelectorAll(".file-change-row")).toHaveLength(2);
  });
  it("preserves a live file change card when the snapshot omits transient diff text", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    handleToolItem("item.started", "tool-live", {
      tool_call_id: "call-live",
      tool_name: "replace",
    }, "turn-live");
    handleToolItem("item.delta", "tool-live", {
      tool_call_id: "call-live",
      diff_text: "--- a/src/live.ts\n+++ b/src/live.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
    }, "turn-live");
    expect(root.querySelector(".file-change-card")).not.toBeNull();

    renderTranscript(root, {
      nodes: [
        { node_type: "turn", id: "turn-live", header: "request" },
        {
          node_type: "tool_call",
          id: "tool-live",
          tool_call_id: "call-live",
          status: "done",
          payload: { tool_name: "replace", summary: "done" },
        },
      ],
    });

    expect(root.querySelector(".file-change-card")).not.toBeNull();
  });

  it("keeps a live file change card after its tool group when re-rendering a snapshot", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    handleToolItem("item.started", "tool-live-order", {
      tool_call_id: "call-live-order",
      tool_name: "replace",
    }, "turn-live-order");
    handleToolItem("item.delta", "tool-live-order", {
      tool_call_id: "call-live-order",
      diff_text: "--- a/src/live.ts\n+++ b/src/live.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
    }, "turn-live-order");

    renderTranscript(root, {
      nodes: [
        { node_type: "turn", id: "turn-live-order", header: "request" },
        {
          node_type: "tool_call",
          id: "tool-live-order",
          tool_call_id: "call-live-order",
          status: "done",
          payload: { tool_name: "replace", summary: "done" },
        },
      ],
    });

    const children = Array.from(root.children);
    const groupIndex = children.findIndex((el) => el.classList.contains("tool-group"));
    const cardIndex = children.findIndex((el) => el.classList.contains("file-change-card"));

    expect(groupIndex).toBeGreaterThanOrEqual(0);
    expect(cardIndex).toBe(groupIndex + 1);
    expect(root.querySelectorAll(".file-change-card")).toHaveLength(1);
    expect(root.querySelector(".file-change-path")?.textContent).toBe("src/live.ts");
  });

  it("keeps a live file change card directly after its tool group when later nodes render", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    handleToolItem("item.started", "tool-live-followed", {
      tool_call_id: "call-live-followed",
      tool_name: "replace",
    }, "turn-live-followed");
    handleToolItem("item.delta", "tool-live-followed", {
      tool_call_id: "call-live-followed",
      diff_text: "--- a/src/live.ts\n+++ b/src/live.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
    }, "turn-live-followed");
    appendStreamText("assistant-followed", "done", "text");
    commitStream("assistant-followed", false);

    renderTranscript(root, {
      nodes: [
        { node_type: "turn", id: "turn-live-followed", header: "request" },
        {
          node_type: "tool_call",
          id: "tool-live-followed",
          tool_call_id: "call-live-followed",
          status: "done",
          payload: { tool_name: "replace", summary: "done" },
        },
        {
          node_type: "assistant",
          id: "assistant-followed",
          payload: { raw_text: "done" },
          body_lines: ["done"],
        },
      ],
    });

    const children = Array.from(root.children);
    const groupIndex = children.findIndex((el) => el.classList.contains("tool-group"));
    const cardIndex = children.findIndex((el) => el.classList.contains("file-change-card"));
    const assistantIndex = children.findIndex((el) => el.dataset.streamId === "assistant-followed");

    expect(groupIndex).toBeGreaterThanOrEqual(0);
    expect(cardIndex).toBe(groupIndex + 1);
    expect(assistantIndex).toBe(cardIndex + 1);
  });

  it("does not insert a divider when thought items are merged", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    appendThoughtItem("thought-1", { text: "first thought" });
    appendThoughtItem("thought-2", { text: "second thought" });

    expect(root.querySelectorAll(".thought-item")).toHaveLength(1);
    expect(root.querySelector(".thought-divider")).toBeNull();
    expect(root.textContent).toContain("first thought");
    expect(root.textContent).toContain("second thought");
  });


  it("restores session change summaries as file change cards", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    renderTranscript(root, {
      nodes: [
        { node_type: "turn", id: "turn-summary", header: "request" },
        {
          node_type: "message",
          id: "change-summary",
          payload: {
            raw_text: [
              "  [dim]Modified[/dim]  [cyan]README.md[/cyan]  [#A6E22E]+140[/#A6E22E] [#FF4689]−40[/#FF4689]",
              "  [dim]Modified[/dim]  [cyan]AGENTS.md[/cyan]  [#A6E22E]+122[/#A6E22E] [#FF4689]−10[/#FF4689]",
            ].join("\n"),
          },
        },
      ],
    });

    expect(root.querySelectorAll(".file-change-card")).toHaveLength(1);
    expect(root.querySelectorAll(".file-change-row")).toHaveLength(2);
    expect(root.querySelector(".message-item.message-text")).toBeNull();
    expect(root.textContent).toContain("README.md");
    expect(root.textContent).toContain("AGENTS.md");
    expect(root.textContent).not.toContain("[cyan]");
    expect(root.textContent).not.toContain("[/#A6E22E]");

  });

  it("keeps session change summary cards before following assistant replies", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const snapshot = {
      nodes: [
        { node_type: "turn", id: "turn-summary-order", header: "request" },
        {
          node_type: "message",
          id: "change-summary-order",
          payload: {
            raw_text: "  [dim]Modified[/dim]  [cyan]README.md[/cyan]  [#A6E22E]+140[/#A6E22E] [#FF4689]−40[/#FF4689]",
          },
        },
        {
          node_type: "assistant",
          id: "assistant-after-summary",
          payload: { raw_text: "done" },
          body_lines: ["done"],
        },
      ],
    };

    renderTranscript(root, snapshot);
    renderTranscript(root, snapshot);

    const order = Array.from(root.children).map((el) => {
      if (el.classList.contains("file-change-card")) return el.dataset.itemId;
      if (el.classList.contains("stream-buffer")) return el.dataset.streamId;
      return el.dataset.itemId || el.className;
    });
    expect(order).toEqual([
      "turn-summary-order",
      "change-summary-order",
      "assistant-after-summary",
    ]);
  });

  it("restores a completed compaction divider from a status snapshot", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    renderTranscript(root, {
      nodes: [
        {
          node_type: "status",
          id: "compact-status",
          payload: { outcome: "compacted", detail: "Kept the latest work" },
          status: "done",
        },
      ],
    });

    expect(root.querySelector(".compaction-divider")?.textContent).toContain("上下文已压缩");
    expect(root.querySelector(".compaction-divider")?.textContent).toContain("Kept the latest work");
  });


  it("restores checkpoint nodes as collapsible system rows", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    renderTranscript(root, {
      nodes: [
        {
          node_type: "checkpoint",
          id: "checkpoint-1",
          header: "voidx plan",
          body_lines: ["Plan: Ship it", "1. Run tests"],
          payload: { checkpoint_id: "cp-1" },
        },
      ],
    });

    const row = root.querySelector("details.checkpoint-row");
    expect(row).not.toBeNull();
    expect(row.querySelector("summary").textContent).toContain("voidx plan");
    expect(row.textContent).toContain("Run tests");
  });
  it("reuses existing DOM nodes when re-rendering the same snapshot", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const snapshot = {
      nodes: [
        { node_type: "message", id: "m1", payload: { style: "text", raw_text: "hello" } },
        { node_type: "message", id: "m2", payload: { style: "text", raw_text: "world" } },
      ],
    };
    renderTranscript(root, snapshot);
    const first = root.querySelector('[data-item-id="m1"]');
    expect(first).not.toBeNull();

    renderTranscript(root, snapshot);

    const second = root.querySelector('[data-item-id="m1"]');
    expect(second).toBe(first);
    expect(root.querySelectorAll('[data-item-id]')).toHaveLength(2);
  });

  it("removes nodes missing from a re-rendered snapshot", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const first = { nodes: [
      { node_type: "message", id: "keep", payload: { style: "text", raw_text: "keep me" } },
      { node_type: "message", id: "drop", payload: { style: "text", raw_text: "drop me" } },
    ] };
    renderTranscript(root, first);
    expect(root.querySelector('[data-item-id="drop"]')).not.toBeNull();

    const second = { nodes: [
      { node_type: "message", id: "keep", payload: { style: "text", raw_text: "keep me" } },
    ] };
    renderTranscript(root, second);

    expect(root.querySelector('[data-item-id="drop"]')).toBeNull();
    expect(root.querySelector('[data-item-id="keep"]')).not.toBeNull();
  });
  it("keeps tool groups in snapshot order when prepending earlier pages", () => {
    resetStreams();
    const root = document.createElement("div");
    setTranscriptElement(root);

    const page1 = {
      nodes: [
        { node_type: "turn", id: "turn-1", header: "request 1" },
        {
          node_type: "tool_call",
          id: "tool-1",
          tool_call_id: "call-1",
          status: "done",
          payload: { tool_name: "bash", args: "pytest" },
        },
        { node_type: "message", id: "msg-2", payload: { style: "user", raw_text: "second" } },
      ],
    };
    renderTranscript(root, page1);

    const merged = {
      nodes: [
        { node_type: "turn", id: "turn-0", header: "earlier request" },
        { node_type: "turn", id: "turn-1", header: "request 1" },
        {
          node_type: "tool_call",
          id: "tool-1",
          tool_call_id: "call-1",
          status: "done",
          payload: { tool_name: "bash", args: "pytest" },
        },
        { node_type: "message", id: "msg-2", payload: { style: "user", raw_text: "second" } },
      ],
    };
    renderTranscript(root, merged);

    const order = Array.from(root.children).map((el) => (
      el.classList.contains("tool-group") ? "tool-group" : (el.dataset.itemId || el.className)
    ));
    expect(order).toEqual(["turn-0", "turn-1", "tool-group", "msg-2"]);
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
