import { renderMarkdown, renderUserMessage, highlightCode } from './markdown';
import { takeCommittedStreams, clearActiveStreams, appendStreamText, commitStream, getTranscriptElement } from './stream';
import type { TranscriptNode, Payload } from '../rpc/protocol';
import { iconSvg } from './icons';
import type {
  MessageItemData, TodoItem, ByIdMap,
  TranscriptSnapshot,
} from './render-types';
import { handleToolItem } from './render-tool-items';
import { renderFileChangeSummary } from './render-file-changes';
import { appendThoughtItem } from './render-thought-items';
import { appendNoticeItem, appendDiffItem, appendCompactionDivider } from './render-notice-status';

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
  if (renderFileChangeSummary(itemId, text)) {
    return;
  }
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

export function snapshotTurnText(node: TranscriptNode): string {
  const payload = node.payload as Record<string, unknown> | undefined;
  const rawText = String(payload?.raw_text
    ?? [node.header || node.title || "", ...(node.body_lines ?? [])].join("\n"));
  return rawText
    .replace(/^\s*\[bold[^\]]*\]/, "")
    .replace(/\[\/\]/g, "")
    .replace(/^\s*(?:❯|>)\s*/, "")
    .trim();
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

function assistantComparisonText(text: string): string {
  const source = String(text || "").replace(/^\s*●\s+/, "");
  return (renderMarkdown(source).textContent || "").trim();
}

function snapshotAssistantText(node: TranscriptNode): string {
  const payload = node.payload as Record<string, unknown> | undefined;
  return assistantComparisonText(
    String(payload?.raw_text
      ?? stripRichMarkup((node.body_lines ?? []).join("\n"))),
  );
}

function committedStreamText(element: HTMLElement): string {
  return (element.querySelector<HTMLElement>(".markdown-body")?.textContent || "").trim();
}

function takeCommittedStreamsForSnapshot(snapshot: TranscriptSnapshot): HTMLElement[] {
  const nodeIds = new Set(snapshot.nodes.map((node) => node.id));
  const committed = takeCommittedStreams();

  // 快照中已有同 id 的 assistant 节点:元素保留在 DOM,由 diff 跳过重建。
  const unmatched = committed.filter((el) => !nodeIds.has(el.dataset.streamId || ""));
  if (unmatched.length === 0) {
    return [];
  }

  // 仅对 id 不匹配的已提交流元素做文本去重(正常流程 id 一致,零额外开销)。
  const coveredTexts = new Map<string, number>();
  for (const node of snapshot.nodes) {
    if (node.node_type !== "assistant") continue;
    const text = snapshotAssistantText(node);
    if (text) coveredTexts.set(text, (coveredTexts.get(text) || 0) + 1);
  }

  const kept: HTMLElement[] = [];
  for (const el of unmatched) {
    const text = committedStreamText(el);
    const count = coveredTexts.get(text) || 0;
    if (count === 0) {
      kept.push(el);
      continue;
    }
    if (count === 1) coveredTexts.delete(text);
    else coveredTexts.set(text, count - 1);
    el.remove();
  }
  return kept;
}

function collectTranscriptIds(root: HTMLElement): Map<string, HTMLElement> {
  const byId = new Map<string, HTMLElement>();
  for (const el of root.querySelectorAll<HTMLElement>("[data-item-id], [data-stream-id], [data-compaction-item-id]")) {
    const id = el.dataset.itemId || el.dataset.streamId || el.dataset.compactionItemId;
    if (id && !byId.has(id)) byId.set(id, el);
  }
  return byId;
}

function reorderElementForNode(
  root: HTMLElement,
  byId: Map<string, HTMLElement>,
  node: TranscriptNode,
): HTMLElement | null {
  let el = byId.get(node.id) || null;
  if (!el && node.tool_call_id) {
    el = root.querySelector<HTMLElement>(`[data-tool-id="${node.tool_call_id}"]`);
  }
  if (!el) return null;
  if (el.parentElement === root) return el;
  if (node.node_type === "tool_call" || node.node_type === "tool_result") {
    const group = el.closest<HTMLElement>(".tool-group");
    if (group && group.parentElement === root) return group;
  }
  return null;
}

function reorderTranscriptNodes(root: HTMLElement, nodes: TranscriptNode[]): void {
  const byId = collectTranscriptIds(root);
  const moved = new Set<HTMLElement>();
  let currentTurnId = "";
  let prev: HTMLElement | null = null;
  for (const node of nodes) {
    if (node.node_type === "turn") {
      currentTurnId = node.id;
    }
    const el = reorderElementForNode(root, byId, node);
    if (!el) continue;

    const block: HTMLElement[] = [];
    if (node.node_type === "assistant") {
      const thought = byId.get(`${node.id}-thought`);
      if (thought && thought !== el) {
        block.push(thought);
      }
    }
    block.push(el);
    if (node.node_type === "tool_call" || node.node_type === "tool_result") {
      const card = [...root.querySelectorAll<HTMLElement>(".file-change-card")]
        .find((candidate) => candidate.dataset.turnId === currentTurnId);
      if (card && card.parentElement === root && card !== el) {
        block.push(card);
      }
    }

    for (const blockEl of block) {
      if (moved.has(blockEl)) continue;
      moved.add(blockEl);
      if (prev) {
        if (blockEl.previousElementSibling !== prev) {
          prev.insertAdjacentElement("afterend", blockEl);
        }
      } else if (blockEl !== root.firstElementChild) {
        root.insertBefore(blockEl, root.firstElementChild);
      }
      prev = blockEl;
    }
  }
}

export function renderTranscript(root: HTMLElement, snapshot: TranscriptSnapshot): void {
  const nodes = snapshot.nodes || [];
  const nodeIds = new Set(nodes.map((node) => node.id));

  // 已提交流元素去重:重复文本移除(快照将重建),窗口外内容保留在 DOM。
  const committed = takeCommittedStreamsForSnapshot(snapshot);
  const keep = new Set<HTMLElement>(committed);

  clearActiveStreams();

  const existingById = collectTranscriptIds(root);
  const toolById = new Map<string, HTMLElement>();
  for (const el of root.querySelectorAll<HTMLElement>("[data-tool-id]")) {
    if (el.dataset.toolId && !toolById.has(el.dataset.toolId)) {
      toolById.set(el.dataset.toolId, el);
    }
  }

  let currentTurnId = "";

  for (const node of nodes) {
    if (node.node_type === "turn") {
      currentTurnId = node.id;
    }
    const payload = node.payload as Record<string, unknown> | undefined;
    const toolEl = node.tool_call_id ? toolById.get(node.tool_call_id) ?? null : null;
    const existing = node.node_type === "tool_result"
      ? toolEl
      : toolEl || existingById.get(node.id);
    if (existing) {
      if (node.node_type === "tool_call" && payload?.diff_text) {
        handleToolItem("item.delta", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          diff_text: String(payload.diff_text),
        }, currentTurnId);
      }
      continue;
    }
    switch (node.node_type) {
      case "message": {
        const style = String(payload?.style || "text");
        const rawText = String(payload?.raw_text
          ?? stripRichMarkup([node.header, ...(node.body_lines ?? [])].join("\n")));
        if (renderFileChangeSummary(node.id, rawText)) {
          break;
        }
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
        const result = commitStream(node.id, false);
        if (result?.thinking) {
          appendThoughtItem(
            `${node.id}-thought`,
            {
              text: result.thinking,
              elapsed: node.elapsed ?? null,
            },
            result.el,
          );
        }
        break;
      }
      case "tool_call":
        handleToolItem("item.started", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          tool_name: String(payload?.tool_name ?? ""),
          args: payload?.args as string | Record<string, unknown> | undefined,
          raw_args: payload?.raw_args as Record<string, unknown> | undefined,
        }, currentTurnId);
        if (payload?.diff_text) {
          handleToolItem("item.delta", node.id, {
            tool_call_id: node.tool_call_id ?? null,
            diff_text: String(payload.diff_text),
          }, currentTurnId);
        }
        handleToolItem("item.completed", node.id, {
          tool_call_id: node.tool_call_id ?? null,
          ok: node.status !== "error",
          elapsed: node.elapsed ?? null,
          detail: String(payload?.summary ?? ""),
        }, currentTurnId);
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
      case "turn": {
        currentTurnId = node.id;
        const text = snapshotTurnText(node);
        if (text) {
          appendMessageItem(node.id, { style: "user", text });
        }
        break;
      }
      case "checkpoint": {
        const row = document.createElement("details");
        row.className = "checkpoint-row";
        row.dataset.itemId = node.id;
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
      // root / startup / todo / permission / subagent → skip
    }
  }

  // 删除快照中不存在的 DOM 元素(窗口外 committed 元素除外)。
  for (const el of Array.from(
    root.querySelectorAll<HTMLElement>("[data-item-id], [data-stream-id], [data-compaction-item-id]"),
  )) {
    const id = el.dataset.itemId || el.dataset.streamId || el.dataset.compactionItemId;
    const baseId = id && id.endsWith("-thought") ? id.slice(0, -"-thought".length) : id;
    if (baseId && !nodeIds.has(baseId) && !keep.has(el)) {
      el.remove();
    }
  }

  // 窗口外 committed 元素仍在 DOM 中,无需重新追加。
  for (const el of keep) {
    if (!el.isConnected) {
      root.append(el);
    }
  }

  // 按快照顺序校正节点位置(分页前插等场景)。
  reorderTranscriptNodes(root, nodes);
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
