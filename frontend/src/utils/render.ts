import { renderMarkdown, renderUserMessage, highlightCode } from './markdown';
import { takeCommittedStreams, clearActiveStreams, appendStreamText, commitStream, getTranscriptElement } from './stream';
import type { TranscriptNode, Payload } from '../rpc/protocol';
import { iconSvg } from './icons';
import type {
  NodePayload, MessageItemData, ToolItemData, ThoughtItemData,
  NoticeItemData, DiffItemData, StatusItemData, TodoItem, ByIdMap,
  TranscriptSnapshot,
} from './render-types';
import { handleToolItem } from './render-tool-items';
import { appendThoughtItem } from './render-thought-items';
import { appendNoticeItem, appendDiffItem, appendCompactionDivider, handleStatusItem } from './render-notice-status';

export type { TranscriptSnapshot } from './render-types';
export { handleToolItem } from './render-tool-items';
export { appendThoughtItem } from './render-thought-items';
export { appendNoticeItem, appendDiffItem, appendCompactionDivider, handleStatusItem } from './render-notice-status';

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

export function truncateText(text: string, maxLines = 10, maxChars = 1000): string {
  if (!text) return "";
  const lines = text.split("\n");
  if (lines.length > maxLines) {
    const omitted = lines.length - maxLines;
    return lines.slice(0, maxLines).join("\n") + `\n... (truncated, ${omitted} more lines)`;
  }
  if (text.length > maxChars) {
    return text.slice(0, maxChars) + " ... (truncated)";
  }
  return text;
}


/* ── Item-path rendering functions (shared by snapshot recovery and live streaming) ── */

export function formatSwitchNotification(text: string): string | null {
  const clean = text.replace(/\x1B\[[0-9;]*[a-zA-Z]/g, "").trim();
  if (clean.includes("switched")) {
    const lines = clean.split("\n").map(l => l.trim()).filter(Boolean);
    let modelName = "";
    let isLocal = clean.toLowerCase().includes("local");

    for (const line of lines) {
      if (line.includes("switched")) {
        const match = line.match(/\(([^)]+)\)\s*✔\s*switched/);
        if (match && match[1]) {
          modelName = match[1];
          break;
        }
      }
    }

    if (!modelName && lines.length > 0) {
      for (const line of lines) {
        if (line.includes("switched")) {
          modelName = line.replace(/[\(\)]/g, "").replace("✔", "").replace("switched", "").trim();
          break;
        }
      }
    }

    if (!modelName && lines.length > 0) {
      modelName = lines[0].replace(/[\(\)]/g, "").replace("✔ switched", "").trim();
    }

    if (modelName) {
      return `✔ Switched model to ${modelName}${isLocal ? " (local)" : ""}`;
    }
  }
  return null;
}

function parseTurnStats(text: string): { duration: string; calls: string; input: string; output: string } | null {
  const clean = text.replace(/\[\/?(dim|cyan)\]/g, "");
  const match = clean.match(/✻\s*([\w.]+s)\s*·\s*(\d+)\s*calls\s*·\s*([\w.]+)\s*in\s*([\w.]+)\s*out/);
  if (match) {
    return {
      duration: match[1],
      calls: match[2],
      input: match[3],
      output: match[4]
    };
  }
  return null;
}

function renderTurnStats(duration: string, calls: string, input: string, output: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "vx-turn-stats";
  
  const durItem = document.createElement("span");
  durItem.className = "vx-stat-item vx-stat-duration";
  durItem.title = "Duration";
  durItem.innerHTML = `${iconSvg("clock", 12, 2.5)}<span>${duration}</span>`;

  const callsItem = document.createElement("span");
  callsItem.className = "vx-stat-item vx-stat-calls";
  callsItem.title = "Tool Calls";
  callsItem.innerHTML = `${iconSvg("terminal", 12, 2.5)}<span>${calls} calls</span>`;

  const tokensItem = document.createElement("span");
  tokensItem.className = "vx-stat-item vx-stat-tokens";
  tokensItem.title = "Tokens (Input / Output)";
  tokensItem.innerHTML = `
    ${iconSvg("cpu", 12, 2.5)}
    <span>${input} in</span>
    <span class="vx-stat-arrow">→</span>
    <span>${output} out</span>
  `;
  
  const divider1 = document.createElement("span");
  divider1.className = "vx-stat-divider";
  divider1.textContent = "·";

  const divider2 = document.createElement("span");
  divider2.className = "vx-stat-divider";
  divider2.textContent = "·";

  el.append(durItem, divider1, callsItem, divider2, tokensItem);
  return el;
}

export function appendMessageItem(itemId: string, data: MessageItemData): void {
  const text = data.text || "";
  const stats = parseTurnStats(text);
  if (stats) {
    const el = document.createElement("div");
    el.className = "message-item message-stats";
    el.dataset.itemId = itemId;
    el.append(renderTurnStats(stats.duration, stats.calls, stats.input, stats.output));
    
    const transcriptEl = getTranscriptElement();
    if (transcriptEl) {
      transcriptEl.append(el);
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
    return;
  }

  const switchNotify = formatSwitchNotification(text);

  if (switchNotify) {
    return;
  }

  const cleanText = text.trim();
  if (
    cleanText.includes("MCP connecting:") ||
    cleanText.includes("LSP setup failed:") ||
    cleanText.includes("LSP startup:") ||
    cleanText.includes("LSP warmup:") ||
    (cleanText.includes("→") && (cleanText.includes("warming...") || cleanText.includes("ready") || cleanText.includes("failed")))
  ) {
    return;
  }

  const el = document.createElement("div");
  const style = data.style || "text";
  el.className = `message-item message-${style}`;
  el.dataset.itemId = itemId;
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
          appendThoughtItem(node.id, {
            text: rawText,
            meta: (node.meta ?? undefined) as string | null | undefined,
            elapsed: node.elapsed ?? null,
          });
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
        const thinkingText = String(payload?.thinking_text || "");
        if (thinkingText) {
          appendStreamText(node.id, thinkingText, "thinking");
        }
        appendStreamText(
          node.id,
          rawText,
          payload?.phase === "thinking" ? "thinking" : "text",
        );
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
          detail: String(payload?.summary ?? ""),
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
          elapsed: node.elapsed ?? null,
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
      case "status":
        if (payload?.outcome === "compacted") {
          appendCompactionDivider(node.id, {
            outcome: "compacted",
            detail: String(payload.detail || ""),
            ok: node.status !== "error",
          });
        }
        break;
      case "checkpoint": {
        const row = document.createElement("details");
        row.className = "checkpoint-row";
        const summary = document.createElement("summary");
        summary.textContent = stripRichMarkup(node.header || "voidx plan");
        row.append(summary);
        const body = document.createElement("div");
        body.className = "checkpoint-row-body";
        body.textContent = (node.body_lines ?? []).map(stripRichMarkup).join("\n");
        row.append(body);
        root.append(row);
        break;
      }
      // root / turn / startup / todo / permission / subagent → skip
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
