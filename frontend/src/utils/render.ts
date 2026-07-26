import { renderMarkdown, renderUserMessage, highlightCode } from './markdown';
import { takeCommittedStreams, clearActiveStreams, appendStreamText, commitStream, getTranscriptElement } from './stream';
import type { TranscriptNode, Payload } from '../rpc/protocol';
import { iconSvg } from './icons';

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
  elapsed?: number | null;
}

interface NoticeItemData {
  style?: string;
  text?: string;
}

interface DiffItemData {
  text?: string;
  title?: string;
}

interface StatusItemData {
  status_id?: string;
  label?: string;
  detail?: string;
  ok?: boolean;
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
const TOOL_GROUP_PREVIEW_LIMIT = 3;

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

interface ToolInfo {
  tool_name: string;
}

const SVG_ICONS: Record<string, string> = {
  read: iconSvg("book", 14, 2),
  write: iconSvg("pencil", 14, 2),
  command: iconSvg("terminal", 14, 2),
  search: iconSvg("search", 14, 2),
  folder: iconSvg("folder", 14, 2),
  tool: iconSvg("wrench", 14, 2),
};

function getToolGroupSummary(tools: ToolInfo[]): { icon: string; text: string } {
  if (tools.length === 0) {
    return { icon: SVG_ICONS.tool, text: "tool" };
  }

  let reads = 0;
  let writes = 0;
  let commands = 0;
  let others = 0;

  for (const tool of tools) {
    const name = (tool.tool_name || "").toLowerCase();
    if (name.includes("read") || name.includes("view") || name.includes("search") || name.includes("find") || name.includes("list") || name.includes("locate")) {
      reads += 1;
    } else if (name.includes("write") || name.includes("replace") || name.includes("edit")) {
      writes += 1;
    } else if (name.includes("command") || name.includes("run") || name.includes("bash") || name.includes("cmd") || name.includes("terminal")) {
      commands += 1;
    } else {
      others += 1;
    }
  }

  const parts: string[] = [];
  if (writes) parts.push(`edited ${writes} ${writes > 1 ? "files" : "file"}`);
  if (commands) parts.push(`ran ${commands} ${commands > 1 ? "commands" : "command"}`);
  if (reads) parts.push(`read ${reads} ${reads > 1 ? "files" : "file"}`);
  if (others) parts.push(`ran ${others} ${others > 1 ? "tools" : "tool"}`);

  const icon = writes ? SVG_ICONS.write : commands ? SVG_ICONS.command : reads ? SVG_ICONS.read : SVG_ICONS.tool;
  return { icon, text: parts.join(", ") };
}

interface ToolHeaderInfo {
  icon: string;
  verb: string;
  target?: string;
}

function getToolItemHeaderInfo(data: ToolItemData): ToolHeaderInfo {
  const toolName = (data.tool_name || "").toLowerCase();
  const args: Record<string, any> = typeof data.args === "object" && data.args !== null ? data.args : {};
  const rawArgs: Record<string, any> = typeof data.raw_args === "object" && data.raw_args !== null ? data.raw_args : {};
  const path = String(args.path || args.file_path || args.TargetFile || args.target_file || args.AbsolutePath || args.absolute_path || args.DirectoryPath || args.directory_path || args.SearchPath || args.search_path || "");
  const filename = path ? path.substring(path.lastIndexOf("/") + 1) : "";

  if (toolName.includes("read") || toolName.includes("view")) {
    return { icon: SVG_ICONS.read, verb: "read", target: filename || "file" };
  }

  if (toolName.includes("search")) {
    const query = String(args.query || args.Query || "");
    const searchPath = String(args.path || args.Path || "");
    const dirname = searchPath ? searchPath.substring(searchPath.lastIndexOf("/") + 1) : "";
    const queryTruncated = query.length > 20 ? query.slice(0, 20) + "..." : query;
    const location = dirname ? ` in ${dirname}` : "";
    return {
      icon: SVG_ICONS.search,
      verb: query ? `searched "${queryTruncated}"${location}` : "searched",
    };
  }

  if (toolName.includes("find")) {
    const query = String(args.query || args.Query || "");
    const searchPath = String(args.path || args.Path || "");
    const dirname = searchPath ? searchPath.substring(searchPath.lastIndexOf("/") + 1) : "";
    const queryTruncated = query.length > 20 ? query.slice(0, 20) + "..." : query;
    const location = dirname ? ` in ${dirname}` : "";
    return {
      icon: SVG_ICONS.search,
      verb: query ? `found "${queryTruncated}"${location}` : "found files",
    };
  }

  if (toolName.includes("list_dir") || toolName.includes("list")) {
    return { icon: SVG_ICONS.folder, verb: "listed", target: filename || "directory" };
  }

  if (toolName.includes("write")) {
    return { icon: SVG_ICONS.write, verb: "edited", target: filename || "file" };
  }

  if (toolName.includes("replace") || toolName.includes("edit")) {
    return { icon: SVG_ICONS.write, verb: "edited", target: filename || "file" };
  }

  if (toolName.includes("command") || toolName.includes("run") || toolName.includes("bash") || toolName.includes("cmd") || toolName.includes("terminal")) {
    const cmd = String(args.command || args.CommandLine || args.command_line || rawArgs.command || "");
    return cmd
      ? { icon: SVG_ICONS.command, verb: "ran", target: cmd }
      : { icon: SVG_ICONS.command, verb: "ran command" };
  }

  return { icon: SVG_ICONS.tool, verb: data.tool_name || data.label || "tool" };
}

function createToolGroup(): HTMLElement {
  const group = document.createElement("div");
  group.className = "tool-group";
  group.dataset.visibleCount = String(TOOL_GROUP_PREVIEW_LIMIT);

  const header = document.createElement("div");
  header.className = "tool-group-header";

  const name = document.createElement("span");
  name.className = "tool-group-name";
  name.textContent = "tool";

  const chevron = document.createElement("span");
  chevron.className = "tool-group-chevron";
  chevron.innerHTML = iconSvg("chevron-right", 12, 2);

  const args = document.createElement("span");
  args.className = "tool-group-args";

  header.addEventListener("click", () => {
    const body = group.querySelector<HTMLElement>(".tool-group-body");
    if (!body) return;
    body.hidden = !body.hidden;
    chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
    renderToolGroupVisibility(group);
  });

  header.append(name, chevron, args);
  group.append(header);

  const body = document.createElement("div");
  body.className = "tool-group-body";
  body.hidden = true;
  group.append(body);

  return group;
}

function latestToolGroup(transcriptEl: HTMLElement): HTMLElement {
  let curr = transcriptEl.lastElementChild as HTMLElement | null;
  while (curr) {
    if (curr.classList.contains("tool-group")) {
      return curr;
    }
    if (
      curr.classList.contains("message-item") ||
      curr.classList.contains("stream-buffer") ||
      curr.classList.contains("thought-item")
    ) {
      break;
    }
    curr = curr.previousElementSibling as HTMLElement | null;
  }
  const group = createToolGroup();
  transcriptEl.append(group);
  return group;
}

function updateToolGroupSummary(group: HTMLElement, data: ToolItemData): void {
  const name = group.querySelector<HTMLElement>(".tool-group-name");
  const args = group.querySelector<HTMLElement>(".tool-group-args");

  const toolItems = [...group.querySelectorAll<HTMLElement>(".tool-item")];
  const tools: ToolInfo[] = toolItems.map(el => {
    const toolName = el.querySelector<HTMLElement>(".tool-name")?.textContent || "";
    return { tool_name: toolName };
  });

  if (tools.length === 0 && data.tool_name) {
    tools.push({ tool_name: data.tool_name });
  }

  const summary = getToolGroupSummary(tools);

  if (name) {
    name.innerHTML = `<span class="tool-group-icon">${summary.icon}</span> ${summary.text}`;
  }
  if (args) {
    args.textContent = "";
  }
}

function renderToolGroupVisibility(group: HTMLElement): void {
  const body = group.querySelector<HTMLElement>(".tool-group-body");
  if (!body) return;
  const items = [...body.querySelectorAll<HTMLElement>(".tool-item")];
  const visibleCount = Math.min(
    Number(group.dataset.visibleCount || TOOL_GROUP_PREVIEW_LIMIT),
    items.length,
  );

  for (const [index, item] of items.entries()) {
    item.hidden = index >= visibleCount;
  }

  group.querySelector(".tool-group-expand-controls")?.remove();
  if (body.hidden || visibleCount >= items.length) return;

  const controls = document.createElement("div");
  controls.className = "tool-group-expand-controls";

  const expand = document.createElement("button");
  expand.type = "button";
  expand.className = "tool-group-expand tool-group-expand-more";
  expand.textContent = "展开显示";
  expand.addEventListener("click", (event) => {
    event.stopPropagation();
    group.dataset.visibleCount = String(
      Math.min(visibleCount + TOOL_GROUP_PREVIEW_LIMIT, items.length),
    );
    renderToolGroupVisibility(group);
  });

  controls.append(expand);
  body.append(controls);
}

function updateToolStats(el: HTMLElement): void {
  const adds = Number(el.dataset.diffAdds || 0);
  const dels = Number(el.dataset.diffDels || 0);
  if (!adds && !dels) return;
  let stats = el.querySelector<HTMLElement>(".tool-stats");
  if (!stats) {
    stats = document.createElement("span");
    stats.className = "tool-stats";
    el.querySelector(".tool-summary")?.after(stats);
  }
  stats.innerHTML = `<span class="tool-stat-add">+${adds}</span> <span class="tool-stat-del">-${dels}</span>`;
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
    const hasArgs = !!data.args && (typeof data.args === "string" ? data.args.trim() !== "" : Object.keys(data.args).length > 0);
    const isCmd = data.tool_name === "run_command" || data.tool_name === "command" || data.tool_name === "bash";
    const expandable = hasArgs || isCmd;
    chevron.innerHTML = iconSvg(expandable ? "chevron-right" : "dot", 12, 2);

    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = data.tool_name || data.label || "tool";

    const summaryInfo = getToolItemHeaderInfo(data);
    const summary = document.createElement("span");
    summary.className = "tool-summary";
    summary.innerHTML = `<span class="tool-icon">${summaryInfo.icon}</span> `;
    summary.append(document.createTextNode(summaryInfo.verb));
    if (summaryInfo.target) {
      summary.append(" ");
      const target = document.createElement("span");
      target.className = "tool-target";
      target.textContent = summaryInfo.target;
      summary.append(target);
    }

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
        if (body.children.length > 0) {
          chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
        }
        chevron.classList.toggle("open", !body.hidden);
      }
    });

    header.append(chevron, name, summary, argsSummary, spinner);
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
      const group = latestToolGroup(transcriptEl);
      group.querySelector(".tool-group-body")?.append(el);
      updateToolGroupSummary(group, data);
      renderToolGroupVisibility(group);
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } else if (el) {
    const body = el.querySelector<HTMLElement>(".tool-body");
    if (!body) return;
    const chev = el.querySelector<HTMLElement>(".tool-chevron");
    if (method === "item.delta") {
      if (data.diff_text) {
        const diff = renderDiffBlock(data.diff_text);
        body.append(diff);
        let adds = Number(el.dataset.diffAdds || 0);
        let dels = Number(el.dataset.diffDels || 0);
        for (const line of String(data.diff_text).split("\n")) {
          if (line.startsWith("+++") || line.startsWith("---")) continue;
          if (line.startsWith("+")) adds += 1;
          else if (line.startsWith("-")) dels += 1;
        }
        el.dataset.diffAdds = String(adds);
        el.dataset.diffDels = String(dels);
        updateToolStats(el);
      } else if (data.detail) {
        const detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = truncateText(data.detail);
        body.append(detail);
      }
      if (chev && body.children.length > 0) {
        chev.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
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
        detail.textContent = truncateText(data.detail);
        body.append(detail);
      }
      if (body && chev) {
        chev.innerHTML = iconSvg(
          body.children.length === 0 ? "dot" : body.hidden ? "chevron-right" : "chevron-down",
          12,
          2,
        );
      }
    }
    if (transcriptEl) {
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } else if (method === "item.delta") {
    console.warn(`voidx: tool delta for unknown tool_call_id: ${data.tool_call_id}`);
  }
}

function formatThoughtMeta(meta: string | null | undefined, elapsed?: number | null): string {
  let seconds = elapsed;

  if ((seconds === undefined || seconds === null) && meta) {
    const match = meta.match(/Thinking for ([\d.]+)s/);
    if (match) {
      seconds = parseFloat(match[1]);
    } else {
      const num = parseFloat(meta);
      if (!isNaN(num)) {
        seconds = num;
      }
    }
  }

  if (seconds !== undefined && seconds !== null) {
    if (seconds < 1) {
      const ms = Math.round(seconds * 1000);
      return `thought for ${ms}ms`;
    } else if (seconds < 60) {
      return `thought for ${seconds.toFixed(1)}s`;
    } else {
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return `thought for ${m}m ${s}s`;
    }
  }

  if (meta) {
    if (meta.toLowerCase() === "thinking") {
      return "thought";
    }
    return meta.replace(/^thinking/i, "thought");
  }

  return "thought";
}

function findMergeableThoughtTarget(
  insertBeforeEl: HTMLElement | null,
  transcriptEl: HTMLElement
): HTMLElement | null {
  const curr = insertBeforeEl
    ? (insertBeforeEl.previousElementSibling as HTMLElement | null)
    : (transcriptEl.lastElementChild as HTMLElement | null);

  if (curr?.classList.contains("thought-item")) {
    return curr;
  }
  return null;
}

export function appendThoughtItem(
  itemId: string,
  data: ThoughtItemData,
  insertBeforeEl?: HTMLElement | null
): void {
  const transcriptEl = getTranscriptElement();
  if (!transcriptEl) return;

  const mergeTarget = findMergeableThoughtTarget(insertBeforeEl || null, transcriptEl);

  if (mergeTarget && mergeTarget.classList.contains("thought-item")) {
    const body = mergeTarget.querySelector<HTMLElement>(".thought-body");
    const label = mergeTarget.querySelector<HTMLElement>(".thought-label");

    const prevText = mergeTarget.dataset.text || "";
    const prevElapsed = mergeTarget.dataset.elapsed ? parseInt(mergeTarget.dataset.elapsed, 10) : 0;

    const combinedText = prevText ? (prevText + "\n\n" + (data.text || "")) : (data.text || "");
    const combinedElapsed = prevElapsed + (typeof data.elapsed === "number" ? data.elapsed : 0);

    mergeTarget.dataset.text = combinedText;
    mergeTarget.dataset.elapsed = String(combinedElapsed);

    const chevron = mergeTarget.querySelector<HTMLElement>(".thought-chevron");

    if (label) {
      const formatted = formatThoughtMeta(data.meta, combinedElapsed);
      label.innerHTML = `${iconSvg("brain", 14, 2)}${formatted}`;
    }

    if (chevron && body) {
      chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
    }

    if (body && data.text) {
      if (body.firstElementChild) {
        const divider = document.createElement("div");
        divider.className = "thought-divider";
        body.append(divider);
      }
      const md = renderMarkdown(data.text);
      md.className = "markdown-body";
      body.append(md);
    }
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return;
  }

  const el = document.createElement("div");
  el.className = "thought-item";
  el.dataset.itemId = itemId;
  el.dataset.text = data.text || "";
  el.dataset.elapsed = String(typeof data.elapsed === "number" ? data.elapsed : 0);

  const header = document.createElement("div");
  header.className = "thought-header";

  const label = document.createElement("span");
  label.className = "thought-label";
  const formatted = formatThoughtMeta(data.meta, data.elapsed);
  label.innerHTML = `${iconSvg("brain", 14, 2)}${formatted}`;

  const chevron = document.createElement("span");
  chevron.className = "thought-chevron";
  chevron.innerHTML = iconSvg("chevron-right", 12, 2);

  header.addEventListener("click", () => {
    const body = el.querySelector<HTMLElement>(".thought-body");
    if (body) {
      body.hidden = !body.hidden;
      chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
    }
  });

  header.append(label, chevron);
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

  if (insertBeforeEl) {
    insertBeforeEl.parentNode?.insertBefore(el, insertBeforeEl);
  } else {
    transcriptEl.append(el);
  }
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

export function appendNoticeItem(itemId: string, data: NoticeItemData): void {
  const el = document.createElement("div");
  el.className = `notice-item notice-${data.style || "error"}`;
  el.dataset.itemId = itemId;

  const icon = document.createElement("span");
  icon.className = "notice-icon";
  icon.textContent = data.style === "warning" ? "!" : data.style === "info" ? "i" : "\u2717";

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
  chevron.innerHTML = iconSvg("chevron-right", 12, 2);

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

const statusElapsedTimers = new Map<string, ReturnType<typeof setInterval>>();

function formatElapsedSeconds(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

function updateStatusElapsed(el: HTMLElement): void {
  const startTs = Number(el.dataset.startTs || Date.now());
  const seconds = Math.max(0, Math.floor((Date.now() - startTs) / 1000));
  const elapsedEl = el.querySelector<HTMLElement>(".status-elapsed");
  if (elapsedEl) elapsedEl.textContent = formatElapsedSeconds(seconds);
}

export function handleStatusItem(method: string, itemId: string, data: StatusItemData): void {
  if (itemId === "turn:analyzing" || data.status_id === "turn:analyzing") {
    return;
  }
  const transcriptEl = getTranscriptElement();
  let el = document.querySelector<HTMLElement>(`[data-status-item-id="${itemId}"]`);
  if (method === "item.started") {
    if (!el) {
      el = document.createElement("div");
      el.className = "status-item running";
      el.dataset.statusItemId = itemId;
      el.dataset.statusId = data.status_id || itemId;
      el.dataset.startTs = String(Date.now());
      const label = document.createElement("span");
      label.className = "status-label";
      el.append(label);
      const elapsed = document.createElement("span");
      elapsed.className = "status-elapsed";
      elapsed.textContent = "0s";
      el.append(elapsed);
      const detail = document.createElement("div");
      detail.className = "status-detail";
      el.append(detail);
      transcriptEl?.append(el);
      const target = el;
      statusElapsedTimers.set(
        itemId,
        setInterval(() => updateStatusElapsed(target), 1000),
      );
    }
    const label = el.querySelector<HTMLElement>(".status-label");
    const detail = el.querySelector<HTMLElement>(".status-detail");
    if (label) label.textContent = data.label || "Working";
    if (detail) {
      detail.textContent = data.detail || "";
      detail.hidden = !data.detail;
    }
    return;
  }
  if (method === "item.completed") {
    if (!el) return;
    const timer = statusElapsedTimers.get(itemId);
    if (timer) {
      clearInterval(timer);
      statusElapsedTimers.delete(itemId);
    }
    updateStatusElapsed(el);
    el.classList.remove("running");
    el.classList.add(data.ok === false ? "failed" : "completed");
    const label = el.querySelector<HTMLElement>(".status-label");
    const detail = el.querySelector<HTMLElement>(".status-detail");
    if (label && data.label) label.textContent = data.label;
    if (detail && data.detail) {
      detail.textContent = data.detail;
      detail.hidden = false;
    }
    return;
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
