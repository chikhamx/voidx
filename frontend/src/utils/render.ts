import { renderMarkdown, renderUserMessage, highlightCode } from './markdown';
import {
  appendStreamText,
  commitStream,
  getTranscriptElement,
  flushTranscriptReconciliationNow,
  getCommittedStreamCanonicalText,
  requestTranscriptFollowAfterMutation,
  peekCommittedStreamsForSnapshot,
  reserveCommittedStream,
  validateCommittedStreamReservations,
  commitCommittedStreamReservationsNoFail,
  releaseCommittedStreamReservations,
  type CommittedStreamReservation,
} from './stream';
import type { TranscriptNode, Payload } from '../rpc/protocol';
import {
  applyTranscriptReconciliation,
  buildTranscriptDescriptors,
  classifyProductionMessage,
  collectExistingTranscriptBlocks,
  planTranscriptReconciliation,
  type TranscriptNodeDescriptor,
  type TranscriptLogicalBlock,
} from './transcript-reconciliation';
import type {
  FileChangeCardReservation,
  HistoricalFileChangeContext,
} from './render-file-changes';
import { iconSvg } from './icons';
import type {
  MessageItemData, TodoItem, ByIdMap,
  TranscriptSnapshot,
} from './render-types';
import { handleToolItem, renderProductionToolItemDetached } from './render-tool-items';
import {
  createHistoricalFileChangeContext,
  renderFileChangeSummary,
  renderHistoricalFileChangeSummary,
  renderHistoricalFileChanges,
  peekFileChangeCard,
  reserveFileChangeCard,
  reserveExistingFileChangeCard,
  validateFileChangeCardReservations,
  commitFileChangeCardReservationsNoFail,
  releaseFileChangeCardReservations,
} from './render-file-changes';
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
      requestTranscriptFollowAfterMutation();
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
    requestTranscriptFollowAfterMutation();
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
  const canonicalText = getCommittedStreamCanonicalText(element);
  if (canonicalText !== null) {
    return assistantComparisonText(canonicalText);
  }
  return (element.querySelector<HTMLElement>(".markdown-body")?.textContent || "").trim();
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

export interface HistoricalFileChangeCardState {
  card: HTMLElement;
  files: Map<string, {
    path: string;
    operation: string;
    added: number;
    removed: number;
    diffText: string;
  }>;
  legacyFiles: Map<string, {
    path: string;
    operation: string;
    added: number;
    removed: number;
    diffText: string;
  }>;
  sources: Map<string, string>;
  expanded: boolean;
}

function appendHistoricalMessage(
  root: DocumentFragment,
  itemId: string,
  style: string,
  text: string,
): HTMLElement {
  const el = document.createElement("div");
  el.className = `message-item message-${style}`;
  el.dataset.itemId = itemId;
  el.append(style === "text" || style === "user"
    ? renderUserMessage(text)
    : renderMarkdown(text));
  root.append(el);
  return el;
}

function appendHistoricalThought(
  root: DocumentFragment,
  itemId: string,
  text: string,
  elapsed?: number | null,
): HTMLElement {
  const previous = root.lastElementChild as HTMLElement | null;
  if (previous?.classList.contains("thought-item")) {
    const oldText = previous.dataset.text || "";
    if (text && !oldText.includes(text)) {
      previous.dataset.text = oldText ? `${oldText}\n\n${text}` : text;
      previous.querySelector(".thought-body")?.append(renderMarkdown(text));
    }
    return previous;
  }
  const el = document.createElement("div");
  el.className = "thought-item";
  el.dataset.itemId = itemId;
  el.dataset.text = text;
  el.dataset.elapsed = String(elapsed || 0);
  const label = document.createElement("div");
  label.className = "thought-label";
  label.textContent = "thought";
  const body = document.createElement("div");
  body.className = "thought-body";
  body.append(renderMarkdown(text));
  el.append(label, body);
  root.append(el);
  return el;
}

function historicalToolGroup(
  root: DocumentFragment,
  groups: Map<string, HTMLElement>,
  turnId: string,
): HTMLElement {
  const key = turnId || "__unscoped__";
  let group = groups.get(key);
  if (group) return group;
  group = document.createElement("div");
  group.className = "tool-group";
  group.dataset.turnId = turnId;
  const body = document.createElement("div");
  body.className = "tool-group-body";
  group.append(body);
  groups.set(key, group);
  root.append(group);
  return group;
}

function appendHistoricalTool(
  root: DocumentFragment,
  groups: Map<string, HTMLElement>,
  tools: Map<string, HTMLElement>,
  fileContext: ReturnType<typeof createHistoricalFileChangeContext>,
  node: TranscriptNode,
  turnId: string,
): void {
  const payload = node.payload as Record<string, unknown> | undefined;
  const toolId = node.tool_call_id || node.id;
  let el = tools.get(toolId);
  if (!el) {
    el = document.createElement("div");
    el.className = "tool-item";
    el.dataset.itemId = node.id;
    el.dataset.toolId = toolId;
    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = String(payload?.tool_name || "tool");
    const detail = document.createElement("div");
    detail.className = "tool-body";
    el.append(name, detail);
    historicalToolGroup(root, groups, turnId)
      .querySelector(".tool-group-body")
      ?.append(el);
    tools.set(toolId, el);
  }
  if (payload?.diff_text) {
    renderHistoricalFileChanges(
      fileContext,
      turnId,
      String(payload.diff_text),
      toolId,
    );
  }
  const detailText = String(payload?.raw_text || payload?.summary || "");
  if (detailText) el.querySelector(".tool-body")?.append(renderMarkdown(detailText));
}


function transcriptRootItemId(element: HTMLElement): string {
  return element.dataset.itemId
    || element.dataset.streamId
    || element.dataset.compactionItemId
    || "";
}

function installHistoricalPageMetadata(
  fragment: DocumentFragment,
  descriptors: readonly TranscriptNodeDescriptor[],
): void {
  const roots = Array.from(fragment.children) as HTMLElement[];
  const starts = descriptors.map((descriptor) => {
    const memberIds = new Set(descriptor.memberNodeIds);
    const index = roots.findIndex((root) => {
      const itemId = transcriptRootItemId(root);
      return memberIds.has(itemId)
        || [...memberIds].some((memberId) => itemId === `${memberId}-thought`)
        || (root.classList.contains("tool-group") && root.dataset.turnId === descriptor.turnId);
    });
    return { descriptor, index };
  }).filter((entry) => entry.index >= 0);

  for (let index = 0; index < starts.length; index += 1) {
    const current = starts[index];
    const nextIndex = starts[index + 1]?.index ?? roots.length;
    const blockRoots = roots.slice(current.index, nextIndex);
    const ownedToolCallIds = new Set(
      current.descriptor.memberNodes
        .map((member) => member.tool_call_id)
        .filter((toolCallId): toolCallId is string => Boolean(toolCallId)),
    );
    const ownedFileChangeKeys = new Set(
      blockRoots
        .filter((root) => root.classList.contains("file-change-card"))
        .map((root) => root.dataset.turnId || current.descriptor.key),
    );
    installDetachedBlockMetadata(
      current.descriptor,
      blockRoots,
      ownedToolCallIds,
      ownedFileChangeKeys,
    );
  }
}
export function historicalTranscriptPageNodes(
  nodes: readonly TranscriptNode[],
  existingPageItemIds: ReadonlySet<string>,
): TranscriptNode[] {
  const renderedDescriptors = buildTranscriptDescriptors(nodes).filter((descriptor) => (
    !descriptor.memberNodeIds.some((id) => existingPageItemIds.has(id))
  ));
  const renderedNodeIds = new Set(
    renderedDescriptors.flatMap((descriptor) => descriptor.memberNodeIds),
  );
  return nodes.filter((node) => renderedNodeIds.has(node.id));
}

export function renderHistoricalTranscriptPage(
  snapshot: TranscriptSnapshot,
  existingPageItemIds: ReadonlySet<string>,
): DocumentFragment {
  const root = document.createDocumentFragment();
  const renderedNodes = historicalTranscriptPageNodes(snapshot.nodes || [], existingPageItemIds);
  const tools = new Map<string, HTMLElement>();
  const toolGroups = new Map<string, HTMLElement>();
  const fileContext = createHistoricalFileChangeContext(root);
  let currentTurnId = "";

  for (const node of renderedNodes) {
    const payload = node.payload as Record<string, unknown> | undefined;
    if (node.node_type === "turn") currentTurnId = node.id;
    switch (node.node_type) {
      case "turn": {
        const text = snapshotTurnText(node);
        if (text) appendHistoricalMessage(root, node.id, "user", text);
        break;
      }
      case "message": {
        const text = String(payload?.raw_text
          ?? stripRichMarkup([node.header, ...(node.body_lines ?? [])].join("\n")));
        const style = String(payload?.style || "text");
        if (renderHistoricalFileChangeSummary(fileContext, node.id, text)) break;
        if (style === "thought") appendHistoricalThought(root, node.id, text, node.elapsed);
        else appendHistoricalMessage(root, node.id, style, text);
        break;
      }
      case "assistant": {
        const thinking = String(payload?.thinking_text || "");
        if (thinking) appendHistoricalThought(root, `${node.id}-thought`, thinking, node.elapsed);
        const text = String(payload?.raw_text
          ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
        appendHistoricalMessage(root, node.id, "markdown", text);
        break;
      }
      case "thought": {
        const text = String(payload?.raw_text
          ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
        appendHistoricalThought(root, node.id, text, node.elapsed);
        break;
      }
      case "tool_call":
      case "tool_result":
        appendHistoricalTool(
          root,
          toolGroups,
          tools,
          fileContext,
          node,
          currentTurnId,
        );
        break;
      case "diff":
        appendHistoricalMessage(
          root,
          node.id,
          "diff",
          (node.body_lines ?? []).join("\n"),
        );
        break;
      case "error":
      case "warn":
        appendHistoricalMessage(
          root,
          node.id,
          node.node_type === "warn" ? "warning" : "error",
          String(payload?.raw_text ?? node.header ?? ""),
        );
        break;
      case "checkpoint": {
        const row = document.createElement("details");
        row.className = "checkpoint-row";
        row.dataset.itemId = node.id;
        const summary = document.createElement("summary");
        summary.textContent = stripRichMarkup(node.header || "voidx plan");
        const body = document.createElement("div");
        body.className = "checkpoint-row-body";
        body.textContent = (node.body_lines ?? []).map(stripRichMarkup).join("\n");
        row.append(summary, body);
        root.append(row);
        break;
      }
    }
  }
  installHistoricalPageMetadata(root, buildTranscriptDescriptors(renderedNodes));
  return root;
}


export type RenderTranscriptResult =
  | { status: "applied" }
  | { status: "stale"; reason: string };

export function renderTranscript(
  root: HTMLElement,
  snapshot: TranscriptSnapshot,
  options: { pendingLocalHandoffs?: ReadonlyMap<string, HTMLElement> } = {},
): RenderTranscriptResult {
  const descriptors = buildTranscriptDescriptors(snapshot.nodes || []);

  const availableClaims = [...peekCommittedStreamsForSnapshot()];
  const syntheticBlocks = new Map<HTMLElement, TranscriptLogicalBlock>();
  const claimByKey = new Map<string, (typeof availableClaims)[number]>();
  for (const descriptor of descriptors) {
    if (descriptor.memberNodes.length !== 1 || descriptor.memberNodes[0].node_type !== "assistant") continue;
    const payload = descriptor.memberNodes[0].payload as Record<string, unknown> | undefined;
    if (String(payload?.thinking_text ?? "")) continue;
    const expectedText = snapshotAssistantText(descriptor.memberNodes[0]);
    const claimIndex = availableClaims.findIndex((claim) => committedStreamText(claim.element) === expectedText);
    if (claimIndex < 0) continue;
    const [claim] = availableClaims.splice(claimIndex, 1);
    syntheticBlocks.set(claim.element, {
      key: descriptor.key,
      fingerprint: descriptor.fingerprint,
      rendererShapeVersion: descriptor.rendererShapeVersion,
      turnId: descriptor.turnId,
      roots: [claim.element],
      primary: claim.element,
      memberNodeIds: new Set(descriptor.memberNodeIds),
      ownedToolCallIds: new Set(),
      ownedFileChangeKeys: new Set(),
    });
    claimByKey.set(descriptor.key, claim);
  }

  const pendingHandoffByKey = new Map<string, TranscriptLogicalBlock>();
  for (const descriptor of descriptors) {
    if (descriptor.memberNodes.length !== 1) continue;
    const element = options.pendingLocalHandoffs?.get(descriptor.memberNodes[0].id);
    if (!element || syntheticBlocks.has(element)) continue;
    const block: TranscriptLogicalBlock = {
      key: descriptor.key,
      fingerprint: descriptor.fingerprint,
      rendererShapeVersion: descriptor.rendererShapeVersion,
      turnId: descriptor.turnId,
      roots: [element],
      primary: element,
      memberNodeIds: new Set(descriptor.memberNodeIds),
      ownedToolCallIds: new Set(),
      ownedFileChangeKeys: new Set(),
    };
    syntheticBlocks.set(element, block);
    pendingHandoffByKey.set(descriptor.key, block);
  }

  const existing = collectExistingTranscriptBlocks(root, descriptors, syntheticBlocks);
  const plan = planTranscriptReconciliation(existing, descriptors, {
    windowed: snapshot.windowed === true,
  });
  if (plan.requiresFullRecovery) {
    return { status: "stale", reason: plan.reason ?? "full recovery required" };
  }
  const neededKeys = new Set([
    ...plan.replace.map((entry) => entry.descriptor.key),
    ...plan.insert.map((descriptor) => descriptor.key),
  ]);
  const detached = renderTranscriptBlocksDetached(
    descriptors.filter((descriptor) => neededKeys.has(descriptor.key)),
  );

  const reservations: CommittedStreamReservation[] = [];
  const fileReservations: FileChangeCardReservation[] = [];
  const releaseReservations = (): void => {
    releaseCommittedStreamReservations(reservations);
    releaseFileChangeCardReservations(fileReservations);
  };
  for (const block of plan.keep) {
    const claim = claimByKey.get(block.key);
    if (!claim) continue;
    const reservation = reserveCommittedStream(claim);
    if (!reservation) {
      releaseReservations();
      return { status: "stale", reason: `committed stream reservation conflict: ${block.key}` };
    }
    reservations.push(reservation);
  }
  const fileOwnershipChanges = [
    ...plan.replace.map((entry) => ({ block: entry.current, remove: false })),
    ...plan.remove.map((block) => ({ block, remove: true })),
  ];
  for (const change of fileOwnershipChanges) {
    for (const key of change.block.ownedFileChangeKeys) {
      const proof = peekFileChangeCard(key);
      if (!proof) continue;
      const reservation = change.remove
        ? reserveFileChangeCard(key, proof, null)
        : reserveExistingFileChangeCard(proof);
      if (!reservation) {
        releaseReservations();
        return { status: "stale", reason: `file card reservation conflict: ${key}` };
      }
      fileReservations.push(reservation);
    }
  }
  if (
    !validateCommittedStreamReservations(reservations)
    || !validateFileChangeCardReservations(fileReservations)
  ) {
    releaseReservations();
    return { status: "stale", reason: "owner reservation validation failed" };
  }

  try {
    let result: RenderTranscriptResult = {
      status: "stale",
      reason: "transcript reconciliation did not run",
    };
    flushTranscriptReconciliationNow(() => {
      const applyResult = applyTranscriptReconciliation(root, plan, detached.blocks);
      if (applyResult.status !== "applied") {
        result = applyResult;
        return;
      }
      if (
        !validateCommittedStreamReservations(reservations)
        || !validateFileChangeCardReservations(fileReservations)
      ) {
        for (const child of Array.from(root.childNodes)) child.remove();
        root.append(...plan.sourceChildren);
        result = { status: "stale", reason: "final owner reservation validation failed" };
        return;
      }
      for (const block of plan.keep) {
        if (!claimByKey.has(block.key) && !pendingHandoffByKey.has(block.key)) continue;
        block.primary.dataset.reconcileKey = block.key;
        block.primary.dataset.reconcileFingerprint = block.fingerprint;
        block.primary.dataset.reconcileShape = block.rendererShapeVersion;
        block.primary.dataset.reconcileRootCount = String(block.roots.length);
        block.primary.dataset.reconcileMemberNodeIds = JSON.stringify([...block.memberNodeIds]);
        block.primary.dataset.reconcileToolCallIds = "[]";
        block.primary.dataset.reconcileFileChangeKeys = "[]";
        if (block.turnId) block.primary.dataset.reconcileTurnId = block.turnId;
        if (pendingHandoffByKey.has(block.key)) {
          const descriptor = descriptors.find((candidate) => candidate.key === block.key);
          const node = descriptor?.memberNodes[0];
          const payload = node?.payload as Record<string, unknown> | undefined;
          const style = node?.node_type === "turn"
            ? "user"
            : String(payload?.style ?? payload?.role ?? "user").toLowerCase();
          block.primary.classList.remove("message-text", "message-user", "message-guidance");
          block.primary.classList.add(style === "guidance" ? "message-guidance" : "message-user");
        }
      }
      commitFileChangeCardReservationsNoFail(fileReservations);
      commitCommittedStreamReservationsNoFail(reservations);
      result = { status: "applied" };
    });
    if ((result as RenderTranscriptResult).status !== "applied") {
      releaseReservations();
    }
    return result;
  } catch (error) {
    releaseReservations();
    throw error;
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


export interface TranscriptRenderContext {
  root: DocumentFragment;
  toolGroups: Map<string, HTMLElement>;
  tools: Map<string, HTMLElement>;
  fileChanges: HistoricalFileChangeContext;
  follow: "none";
}

export interface DetachedTranscriptBlocks {
  fragment: DocumentFragment;
  blocks: TranscriptLogicalBlock[];
  context: TranscriptRenderContext;
}

function renderDetachedMessage(context: TranscriptRenderContext, node: TranscriptNode): void {
  const classification = classifyProductionMessage(node);
  if (classification.suppressed) return;
  const payload = node.payload as Record<string, unknown> | undefined;
  switch (classification.shape) {
    case "message-stats": {
      const stats = parseTurnStats(classification.text);
      if (!stats) return;
      const element = document.createElement("div");
      element.className = "message-item message-stats";
      element.dataset.itemId = node.id;
      element.append(renderTurnStats(stats.duration, stats.calls, stats.input, stats.output));
      context.root.append(element);
      return;
    }
    case "message-file-summary":
      renderHistoricalFileChangeSummary(context.fileChanges, node.id, classification.text);
      return;
    case "message-thought":
      appendHistoricalThought(context.root, node.id, classification.text, node.elapsed);
      return;
    case "message-diff":
      appendHistoricalMessage(context.root, node.id, "diff", classification.text);
      return;
    default:
      appendHistoricalMessage(
        context.root,
        node.id,
        String(payload?.style ?? payload?.role ?? "text"),
        classification.text,
      );
  }
}

function renderDetachedNode(
  context: TranscriptRenderContext,
  node: TranscriptNode,
  turnId: string,
): void {
  const payload = node.payload as Record<string, unknown> | undefined;
  switch (node.node_type) {
    case "turn": {
      const text = snapshotTurnText(node);
      if (text) appendHistoricalMessage(context.root, node.id, "user", text);
      return;
    }
    case "message":
    case "error":
    case "warn":
      renderDetachedMessage(context, node);
      return;
    case "assistant": {
      const thinking = String(payload?.thinking_text ?? "");
      if (thinking) appendHistoricalThought(context.root, `${node.id}-thought`, thinking, node.elapsed);
      const text = String(payload?.raw_text
        ?? stripRichMarkup((node.body_lines ?? []).join("\n")));
      const stream = document.createElement("div");
      stream.className = "stream-buffer";
      stream.dataset.streamId = node.id;
      const body = document.createElement("div");
      body.className = "markdown-body";
      body.append(renderMarkdown(text));
      stream.append(body);
      context.root.append(stream);
      return;
    }
    case "thought":
      appendHistoricalThought(
        context.root,
        node.id,
        String(payload?.raw_text ?? stripRichMarkup((node.body_lines ?? []).join("\n"))),
        node.elapsed,
      );
      return;
    case "tool_call":
    case "tool_result": {
      const toolData = {
        tool_call_id: node.tool_call_id ?? node.id,
        tool_name: String(payload?.tool_name ?? payload?.label ?? "tool"),
        label: typeof payload?.label === "string" ? payload.label : undefined,
        args: payload?.args as string | Record<string, unknown> | undefined,
        raw_args: payload?.raw_args as Record<string, unknown> | undefined,
        detail: String(payload?.raw_text ?? payload?.summary ?? payload?.detail ?? ""),
        ok: payload?.success === undefined ? node.status !== "error" : Boolean(payload.success),
        elapsed: node.elapsed,
      };
      const tool = renderProductionToolItemDetached(
        context.root,
        context.toolGroups,
        context.tools,
        node.id,
        toolData,
        turnId,
      );
      if (payload?.diff_text) {
        const sourceId = node.tool_call_id || node.id;
        if (renderHistoricalFileChanges(
          context.fileChanges,
          turnId,
          String(payload.diff_text),
          sourceId,
        )) {
          tool.dataset.fileCard = "true";
        }
      }
      return;
    }
    case "diff":
      appendHistoricalMessage(
        context.root,
        node.id,
        "diff",
        String(payload?.diff_text ?? (node.body_lines ?? []).join("\n")),
      );
      return;
    case "status":
      if (payload?.outcome === "compacted") {
        const divider = document.createElement("div");
        divider.className = "compaction-divider";
        divider.dataset.compactionItemId = node.id;
        divider.setAttribute("role", "note");
        const label = document.createElement("span");
        label.className = "compaction-divider-label";
        label.textContent = "上下文已压缩";
        divider.append(label);
        const detailText = String(payload.detail ?? payload.label ?? "");
        if (detailText) {
          const detail = document.createElement("span");
          detail.className = "compaction-divider-detail";
          detail.textContent = detailText;
          divider.append(detail);
        }
        context.root.append(divider);
      }
      return;
    case "checkpoint": {
      const row = document.createElement("details");
      row.className = "checkpoint-row";
      row.dataset.itemId = node.id;
      const summary = document.createElement("summary");
      summary.textContent = stripRichMarkup(node.header || "voidx plan");
      const body = document.createElement("div");
      body.className = "checkpoint-row-body";
      body.textContent = (node.body_lines ?? []).map(stripRichMarkup).join("\n");
      row.append(summary, body);
      context.root.append(row);
      return;
    }
  }
}

function installDetachedBlockMetadata(
  descriptor: TranscriptNodeDescriptor,
  roots: HTMLElement[],
  ownedToolCallIds: Set<string>,
  ownedFileChangeKeys: Set<string>,
): TranscriptLogicalBlock {
  if (roots.length === 0) throw new Error(`descriptor rendered no roots: ${descriptor.key}`);
  const primary = roots[0];
  primary.dataset.reconcileKey = descriptor.key;
  primary.dataset.reconcileFingerprint = descriptor.fingerprint;
  primary.dataset.reconcileShape = descriptor.rendererShapeVersion;
  primary.dataset.reconcileRootCount = String(roots.length);
  primary.dataset.reconcileMemberNodeIds = JSON.stringify(descriptor.memberNodeIds);
  primary.dataset.reconcileToolCallIds = JSON.stringify([...ownedToolCallIds]);
  primary.dataset.reconcileFileChangeKeys = JSON.stringify([...ownedFileChangeKeys]);
  if (descriptor.turnId) primary.dataset.reconcileTurnId = descriptor.turnId;
  return {
    key: descriptor.key,
    fingerprint: descriptor.fingerprint,
    rendererShapeVersion: descriptor.rendererShapeVersion,
    turnId: descriptor.turnId,
    roots,
    primary,
    memberNodeIds: new Set(descriptor.memberNodeIds),
    ownedToolCallIds,
    ownedFileChangeKeys,
  };
}

export function renderTranscriptBlocksDetached(
  descriptors: TranscriptNodeDescriptor[],
): DetachedTranscriptBlocks {
  const fragment = document.createDocumentFragment();
  const context: TranscriptRenderContext = {
    root: fragment,
    toolGroups: new Map(),
    tools: new Map(),
    fileChanges: createHistoricalFileChangeContext(fragment),
    follow: "none",
  };
  const blocks: TranscriptLogicalBlock[] = [];

  for (const descriptor of descriptors) {
    const before = fragment.childElementCount;
    const fileKeysBefore = new Set(context.fileChanges.cards.keys());
    for (const member of descriptor.memberNodes) {
      renderDetachedNode(context, member, descriptor.turnId ?? "");
    }
    const roots = Array.from(fragment.children).slice(before) as HTMLElement[];
    const ownedToolCallIds = new Set(
      descriptor.memberNodes
        .filter((member) => member.node_type === "tool_call" || member.node_type === "tool_result")
        .map((member) => member.tool_call_id)
        .filter((toolCallId): toolCallId is string => Boolean(toolCallId)),
    );
    const ownedFileChangeKeys = new Set(
      [...context.fileChanges.cards.keys()].filter((key) => !fileKeysBefore.has(key)),
    );
    blocks.push(installDetachedBlockMetadata(
      descriptor,
      roots,
      ownedToolCallIds,
      ownedFileChangeKeys,
    ));
  }

  return { fragment, blocks, context };
}
