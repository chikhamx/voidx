import { renderMarkdown, renderUserMessage, highlightCode } from './markdown';
import { takeCommittedStreams, clearActiveStreams, appendStreamText, commitStream, getTranscriptElement } from './stream';
import type { TranscriptNode, Payload } from './protocol';

/* ── Local type aliases ── */

/** Concrete payload fields accessed by render.ts */
interface NodePayload {
  tool_name?: string;
  diff_text?: string;
  args?: string | Record<string, unknown>;
  raw_args?: { command?: string };
  name?: string;
  description?: string;
  style?: string;
  raw_text?: string;
  title?: string;
  [k: string]: unknown;
}

interface MessageItemData {
  style?: string;
  text?: string;
}

interface ToolItemData {
  tool_call_id?: string | null;
  tool_name?: string;
  label?: string;
  args?: string | Record<string, unknown>;
  raw_args?: { command?: string };
  diff_text?: string;
  detail?: string;
  ok?: boolean;
  elapsed?: number | null;
}

interface ThoughtItemData {
  text?: string;
  meta?: string | null;
}

interface NoticeItemData {
  style?: string;
  text?: string;
}

interface DiffItemData {
  text?: string;
  title?: string;
}

interface TodoItem {
  status: string;
  content: string;
}

export interface TranscriptSnapshot {
  nodes: TranscriptNode[];
}

type ByIdMap = Map<string, TranscriptNode>;

const RICH_TAG = /\[(\/)?(?:bold|dim|italic|underline|strike|red|green|yellow|blue|magenta|cyan|white|black|#[0-9A-Fa-f]{6})\]/g;

export function stripRichMarkup(text: unknown): string {
  return String(text || "").replace(RICH_TAG, "");
}

export function nodeClassName(node: TranscriptNode): string {
  const classes = ["node", `node-${node.node_type || "message"}`];
  if (node.status === "error") {
    classes.push("node-error");
  }
  if (node.status === "running") {
    classes.push("node-running");
  }
  if (node.collapsed) {
    classes.push("node-collapsed");
  }
  return classes.join(" ");
}

export function renderNodeElement(node: TranscriptNode, byId: ByIdMap): HTMLElement | null {
  const type = node.node_type;

  if (type === "root" || type === "startup" || type === "turn") {
    return null;
  }

  if (type === "todo") {
    return null;
  }

  const item = document.createElement("article");
  item.className = nodeClassName(node);
  item.dataset.nodeId = node.id;
  item.style.marginLeft = `${depthFor(node, byId) * 18}px`;

  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = stripRichMarkup(node.title || node.header || type);
  item.append(title);

  if (type === "subagent") {
    item.append(renderSubagentCard(node));
  }

  if (node.meta && (type === "thought" || type === "subagent")) {
    const meta = document.createElement("div");
    meta.className = "node-meta";
    meta.textContent = stripRichMarkup(node.meta);
    item.append(meta);
  }

  if (node.payload?.tool_name) {
    const tool = document.createElement("div");
    tool.className = "node-tool-meta";
    tool.textContent = formatToolMeta(node.payload);
    item.append(tool);
  }

  if (node.payload?.diff_text || type === "diff") {
    const diffText = String(node.payload?.diff_text || node.body_lines?.join("\n") || "");
    item.append(renderDiffBlock(diffText));
  } else if (type === "assistant") {
    const text = (node.body_lines ?? []).map(stripRichMarkup).join("\n");
    item.append(renderMarkdown(text));
  } else if (node.body_lines?.length) {
    item.append(renderBodyLines(node));
  }

  if (type === "tool_call" || type === "tool_result" || type === "thought" || type === "status") {
    title.addEventListener("click", () => {
      item.classList.toggle("node-collapsed");
    });
  } else {
    title.style.cursor = "default";
  }

  return item;
}

function renderBodyLines(node: TranscriptNode): HTMLDivElement {
  const body = document.createElement("div");
  body.className = "node-body";
  const text = (node.body_lines ?? []).map(stripRichMarkup).join("\n");
  if (node.node_type === "tool_call" || node.node_type === "tool_result") {
    const pre = document.createElement("pre");
    pre.className = "node-code";
    pre.innerHTML = highlightCode(text, "json");
    body.append(pre);
  } else {
    body.textContent = text;
  }
  return body;
}

export function renderDiffBlock(diffText: string): HTMLPreElement {
  const block = document.createElement("pre");
  block.className = "diff-content";
  for (const line of String(diffText).split("\n")) {
    const row = document.createElement("div");
    row.className = diffLineClass(line);
    row.textContent = line;
    block.append(row);
  }
  return block;
}

export function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "diff-meta";
  }
  if (line.startsWith("@@")) {
    return "diff-hunk";
  }
  if (line.startsWith("+")) {
    return "diff-add";
  }
  if (line.startsWith("-")) {
    return "diff-del";
  }
  return "diff-context";
}

function renderSubagentCard(node: TranscriptNode): HTMLDivElement {
  const card = document.createElement("div");
  card.className = "subagent-card";
  const header = document.createElement("div");
  header.className = "subagent-header";
  const name = document.createElement("span");
  name.className = "subagent-name";
  name.textContent = String(node.payload?.name || node.agent_name || "subagent");
  header.append(name);
  if (node.elapsed != null) {
    const elapsed = document.createElement("span");
    elapsed.className = "subagent-elapsed";
    elapsed.textContent = formatElapsed(node.elapsed);
    header.append(elapsed);
  }
  card.append(header);
  if (node.payload?.description) {
    const desc = document.createElement("div");
    desc.className = "subagent-steps";
    desc.textContent = String(node.payload.description);
    card.append(desc);
  }
  return card;
}

export function formatToolMeta(payload: Payload): string {
  const name = payload.tool_name || "tool";
  const args = payload.args ? ` ${payload.args}` : "";
  return `${name}${args}`.trim();
}

export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) {
    return "";
  }
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)}ms`;
  }
  return `${seconds.toFixed(1)}s`;
}


/* ── Item-path rendering functions (shared by snapshot recovery and live streaming) ── */

export function appendMessageItem(itemId: string, data: MessageItemData): void {
  const el = document.createElement("div");
  const style = data.style || "text";
  el.className = `message-item message-${style}`;
  el.dataset.itemId = itemId;
  const text = data.text || "";
  if (style === "markdown" || style === "guidance") {
    el.append(renderMarkdown(text));
  } else if (style === "text") {
    el.append(renderUserMessage(text));
  } else if (style === "ansi") {
    el.append(renderMarkdown(text));
  } else {
    const pre = document.createElement("pre");
    pre.textContent = text;
    el.append(pre);
  }
  const transcriptEl = getTranscriptElement();
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

export function handleToolItem(method: string, itemId: string, data: ToolItemData): void {
  let el: HTMLElement | null = document.querySelector<HTMLElement>(`[data-tool-id="${data.tool_call_id}"]`);
  const transcriptEl = getTranscriptElement();
  if (method === "item.started") {
    el = document.createElement("div");
    el.className = "tool-item";
    el.dataset.toolId = data.tool_call_id ?? "";
    el.dataset.itemId = itemId;

    const header = document.createElement("div");
    header.className = "tool-header";

    const chevron = document.createElement("span");
    chevron.className = "tool-chevron";
    chevron.textContent = "\u25B8";

    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = data.tool_name || data.label || "tool";

    const argsSummary = document.createElement("span");
    argsSummary.className = "tool-args-summary";
    argsSummary.textContent = summarizeArgs(data);

    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    spinner.textContent = "running";

    const toolEl = el;
    header.addEventListener("click", () => {
      const body = toolEl.querySelector<HTMLElement>(".tool-body");
      if (body) {
        body.hidden = !body.hidden;
        chevron.classList.toggle("open", !body.hidden);
      }
    });

    header.append(chevron, name, argsSummary, spinner);
    el.append(header);

    const body = document.createElement("div");
    body.className = "tool-body";
    body.hidden = true;
    el.append(body);

    if (data.args) {
      const args = document.createElement("pre");
      args.className = "tool-args";
      args.textContent = typeof data.args === "string"
        ? data.args
        : JSON.stringify(data.args, null, 2);
      body.append(args);
    }

    if (transcriptEl) {
      transcriptEl.append(el);
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } else if (el) {
    const body = el.querySelector<HTMLElement>(".tool-body");
    if (!body) return;
    if (method === "item.delta") {
      if (data.diff_text) {
        const diff = renderDiffBlock(data.diff_text);
        body.append(diff);
      } else if (data.detail) {
        const detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = data.detail;
        body.append(detail);
      }
    } else if (method === "item.completed") {
      const spinner = el.querySelector(".tool-spinner");
      if (spinner) {
        spinner.textContent = data.ok ? "done" : "failed";
        spinner.className = `tool-status ${data.ok ? "ok" : "err"}`;
      }
      if (data.elapsed) {
        const elapsed = document.createElement("span");
        elapsed.className = "tool-elapsed";
        elapsed.textContent = formatElapsed(data.elapsed);
        el.querySelector(".tool-header")?.append(elapsed);
      }
      if (data.detail) {
        const detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = data.detail;
        body.append(detail);
      }
    }
    if (transcriptEl) {
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } else if (method === "item.delta") {
    console.warn(`voidx: tool delta for unknown tool_call_id: ${data.tool_call_id}`);
  }
}

export function appendThoughtItem(itemId: string, data: ThoughtItemData): void {
  const el = document.createElement("div");
  el.className = "thought-item";
  el.dataset.itemId = itemId;

  const header = document.createElement("div");
  header.className = "thought-header";

  const chevron = document.createElement("span");
  chevron.className = "thought-chevron";
  chevron.textContent = "\u25B8";

  const label = document.createElement("span");
  label.className = "thought-label";
  label.textContent = data.meta || "Thinking";

  header.addEventListener("click", () => {
    const body = el.querySelector<HTMLElement>(".thought-body");
    if (body) {
      body.hidden = !body.hidden;
      chevron.classList.toggle("open", !body.hidden);
    }
  });

  header.append(chevron, label);
  el.append(header);

  const body = document.createElement("div");
  body.className = "thought-body";
  body.hidden = true;
  if (data.text) {
    const md = renderMarkdown(data.text);
    md.className = "markdown-body";
    body.append(md);
  }
  el.append(body);

  const transcriptEl = getTranscriptElement();
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

export function appendNoticeItem(itemId: string, data: NoticeItemData): void {
  const el = document.createElement("div");
  el.className = `notice-item notice-${data.style || "error"}`;
  el.dataset.itemId = itemId;

  const icon = document.createElement("span");
  icon.className = "notice-icon";
  icon.textContent = data.style === "warning" ? "!" : "\u2717";

  const text = document.createElement("span");
  text.className = "notice-text";
  text.textContent = stripRichMarkup(data.text || "");

  el.append(icon, text);

  const region = getOrCreateNoticeToastRegion();
  region.append(el);
  setTimeout(() => {
    el.classList.add("notice-toast-exiting");
    setTimeout(() => {
      el.remove();
      if (!region.childElementCount) {
        region.remove();
      }
    }, 250);
  }, 4000);
}

function getOrCreateNoticeToastRegion(): Element {
  let region: Element | null = document.querySelector(".notice-toast-region");
  if (region) {
    return region;
  }
  region = document.createElement("div");
  region.className = "notice-toast-region";
  region.setAttribute("role", "status");
  region.setAttribute("aria-live", "polite");
  document.body.append(region);
  return region;
}

export function appendDiffItem(itemId: string, data: DiffItemData): void {
  const el = document.createElement("div");
  el.className = "diff-item";
  el.dataset.itemId = itemId;

  const header = document.createElement("div");
  header.className = "diff-header";

  const chevron = document.createElement("span");
  chevron.className = "diff-chevron";
  chevron.textContent = "\u25B8";

  const title = document.createElement("span");
  title.className = "diff-title";
  title.textContent = data.title || "diff";

  header.addEventListener("click", () => {
    const body = el.querySelector<HTMLElement>(".diff-body");
    if (body) {
      body.hidden = !body.hidden;
      chevron.classList.toggle("open", !body.hidden);
    }
  });

  header.append(chevron, title);
  el.append(header);

  const body = document.createElement("div");
  body.className = "diff-body";
  body.hidden = true;
  if (data.text) {
    body.append(renderDiffBlock(data.text));
  }
  el.append(body);

  const transcriptEl = getTranscriptElement();
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

function summarizeArgs(data: ToolItemData): string {
  if (data.tool_name === "bash") {
    const cmd = typeof data.raw_args?.command === "string"
      ? data.raw_args.command
      : "";
    return cmd.length > 60 ? cmd.slice(0, 60) + "..." : cmd;
  }
  const args = data.args || "";
  const s = typeof args === "string" ? args : JSON.stringify(args);
  return s.length > 40 ? s.slice(0, 40) + "..." : s;
}
function depthFor(node: TranscriptNode, byId: ByIdMap): number {
  let depth = 0;
  let cursor: TranscriptNode | undefined = node;
  while (cursor?.parent_id && byId.has(cursor.parent_id)) {
    depth += 1;
    cursor = byId.get(cursor.parent_id);
  }
  return depth;
}

export function renderTranscript(root: HTMLElement, snapshot: TranscriptSnapshot): void {
  root.replaceChildren();
  const committed = takeCommittedStreams();
  clearActiveStreams();

  for (const node of snapshot.nodes) {
    const payload = node.payload as Record<string, unknown> | undefined;
    switch (node.node_type) {
      case "message": {
        const style = String(payload?.style || "text");
        const rawText = String(payload?.raw_text
          ?? stripRichMarkup([node.header, ...(node.body_lines ?? [])].join("\n")));
        if (style === "thought") {
          appendThoughtItem(node.id, { text: rawText, meta: (node.meta ?? undefined) as string | null | undefined });
        } else if (style === "error" || style === "warning") {
          appendNoticeItem(node.id, { style, text: rawText });
        } else if (style === "diff") {
          appendDiffItem(node.id, { text: rawText, title: String(payload?.title ?? "") });
        } else {
          appendMessageItem(node.id, { style, text: rawText });
        }
        break;
      }
      case "assistant": {
        const rawText = String(payload?.raw_text
          ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
        appendStreamText(node.id, rawText, "text");
        commitStream(node.id);
        break;
      }
      case "tool_call":
        handleToolItem("item.started", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          tool_name: String(payload?.tool_name ?? ""),
          args: payload?.args as string | Record<string, unknown> | undefined,
          raw_args: payload?.raw_args as { command?: string } | undefined,
        });
        if (payload?.diff_text) {
          handleToolItem("item.delta", node.id, {
            tool_call_id: node.tool_call_id ?? null,
            diff_text: String(payload.diff_text),
          });
        }
        handleToolItem("item.completed", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          ok: node.status !== "error",
          elapsed: node.elapsed ?? null,
        });
        break;
      case "tool_result": {
        const detailText = String(payload?.raw_text
          ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
        handleToolItem("item.delta", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          detail: detailText,
        });
        break;
      }
      case "thought": {
        const thoughtText = String(payload?.raw_text
          ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
        appendThoughtItem(node.id, {
          text: thoughtText,
          meta: (node.meta ?? undefined) as string | null | undefined,
        });
        break;
      }
      case "error": {
        const rawText = String(payload?.raw_text
          ?? stripRichMarkup(node.header ?? "").replace(/^[✗!]\s*/, ""));
        appendNoticeItem(node.id, { style: "error", text: rawText });
        break;
      }
      case "warn": {
        const rawText = String(payload?.raw_text
          ?? stripRichMarkup(node.header ?? "").replace(/^[✗!]\s*/, ""));
        appendNoticeItem(node.id, { style: "warning", text: rawText });
        break;
      }
      case "diff":
        appendDiffItem(node.id, {
          text: (node.body_lines ?? []).join("\n"),
          title: node.header ?? "",
        });
        break;
      // root / turn / startup / todo / status / permission / checkpoint / subagent → skip
    }
  }

  for (const el of committed) {
    if (el.isConnected) continue;
    root.append(el);
  }
}

export function renderTodoPanel(
  panel: HTMLElement,
  items: TodoItem[],
  summary: string,
): void {
  panel.replaceChildren();
  if (!items || items.length === 0) {
    panel.classList.remove("visible");
    return;
  }
  panel.classList.add("visible");
  if (summary) {
    const summaryEl = document.createElement("span");
    summaryEl.className = "todo-summary";
    summaryEl.textContent = summary;
    panel.append(summaryEl);
  }
  for (const item of items) {
    const el = document.createElement("div");
    el.className = `todo-item ${item.status}`;
    const icon = document.createElement("span");
    icon.className = "todo-icon";
    icon.textContent = item.status === "done" ? "\u2713" : item.status === "active" ? "\u25B6" : "\u25CB";
    const text = document.createElement("span");
    text.textContent = item.content;
    el.append(icon, text);
    panel.append(el);
  }
}
