import { getTranscriptElement } from './stream';
import { iconSvg } from './icons';
import { TOOL_GROUP_PREVIEW_LIMIT, type ToolInfo, type ToolItemData } from './render-types';
import { formatElapsed, truncateText, renderDiffBlock } from './render';
import { renderFileChanges } from './render-file-changes';

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
  let searches = 0;
  let writes = 0;
  let commands = 0;
  let others = 0;

  for (const tool of tools) {
    const name = (tool.tool_name || "").toLowerCase();
    if (name.includes("search") || name.includes("find") || name.includes("locate")) {
      searches += 1;
    } else if (name.includes("read") || name.includes("view") || name.includes("list")) {
      reads += 1;
    } else if (name.includes("write") || name.includes("replace") || name.includes("edit")) {
      writes += 1;
    } else if (name.includes("command") || name.includes("run") || name.includes("bash") || name.includes("cmd") || name.includes("terminal") || name.includes("powershell")) {
      commands += 1;
    } else {
      others += 1;
    }
  }

  const parts: string[] = [];
  if (writes) parts.push(`edited ${writes} ${writes > 1 ? "files" : "file"}`);
  if (commands) parts.push(`ran ${commands} ${commands > 1 ? "commands" : "command"}`);
  if (reads) parts.push(`read ${reads} ${reads > 1 ? "files" : "file"}`);
  if (searches) parts.push(`searched ${searches} ${searches > 1 ? "times" : "time"}`);
  if (others) parts.push(`ran ${others} ${others > 1 ? "tools" : "tool"}`);

  const icon = writes
    ? SVG_ICONS.write
    : commands
      ? SVG_ICONS.command
      : searches
        ? SVG_ICONS.search
        : reads
          ? SVG_ICONS.read
          : SVG_ICONS.tool;
  return { icon, text: parts.join(", ") };
}

interface ToolHeaderInfo {
  icon: string;
  verb: string;
  target?: string;
}

const FORMATTED_ARG_TAG = /\\?\[\/?(?:bold|dim|italic|underline|strike|red|green|yellow|blue|magenta|cyan|white|black|#[0-9a-f]{6})\]/gi;

type ToolArgs = Record<string, unknown>;

function cleanFormattedArg(value: string): string {
  return value
    .replace(FORMATTED_ARG_TAG, "")
    .replace(/\\([\[\]])/g, "$1")
    .trim();
}

function unquoteArg(value: string): string {
  const trimmed = cleanFormattedArg(value);
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1).replace(/\\([\\"'])/g, "$1");
    }
  }
  return trimmed;
}

function parseFormattedArgs(value: string): ToolArgs {
  const normalized = cleanFormattedArg(value);
  const parsed: ToolArgs = {};
  const pairPattern = /([A-Za-z_][\w-]*)\s*=\s*(?:"((?:\\.|[^"])*)"|'((?:\\.|[^'])*)'|([^,\s]+))/g;
  let matched = false;
  let match: RegExpExecArray | null;
  while ((match = pairPattern.exec(normalized)) !== null) {
    matched = true;
    parsed[match[1]] = unquoteArg(match[2] ?? match[3] ?? match[4] ?? "");
  }
  return matched ? parsed : (normalized ? { args: normalized } : {});
}

function normalizedToolArgs(data: ToolItemData): ToolArgs {
  if (typeof data.raw_args === "object" && data.raw_args !== null && Object.keys(data.raw_args).length > 0) {
    return data.raw_args as ToolArgs;
  }
  if (typeof data.args === "object" && data.args !== null) {
    return data.args as ToolArgs;
  }
  if (typeof data.args === "string") {
    return parseFormattedArgs(data.args);
  }
  return {};
}

function argValue(args: ToolArgs, names: string[]): string {
  const lowerNames = new Set(names.map((name) => name.toLowerCase()));
  for (const [key, value] of Object.entries(args)) {
    if (!lowerNames.has(key.toLowerCase()) || value == null || value === "") continue;
    return typeof value === "string" ? cleanFormattedArg(value) : String(value);
  }
  return "";
}

function getToolItemHeaderInfo(data: ToolItemData): ToolHeaderInfo {
  const toolName = (data.tool_name || "").toLowerCase();
  const args = normalizedToolArgs(data);
  const path = argValue(args, [
    "path",
    "file_path",
    "TargetFile",
    "target_file",
    "AbsolutePath",
    "absolute_path",
    "DirectoryPath",
    "directory_path",
    "SearchPath",
    "search_path",
  ]);
  const filename = path ? path.substring(path.lastIndexOf("/") + 1) : "";

  if (toolName.includes("read") || toolName.includes("view")) {
    return { icon: SVG_ICONS.read, verb: "read", target: filename || "file" };
  }

  if (toolName.includes("search")) {
    const query = argValue(args, ["query", "Query", "pattern", "args"]);
    const searchPath = argValue(args, ["path", "Path", "SearchPath", "search_path"]);
    const dirname = searchPath ? searchPath.substring(searchPath.lastIndexOf("/") + 1) : "";
    const queryTruncated = query.length > 20 ? query.slice(0, 20) + "..." : query;
    const location = dirname ? ` in ${dirname}` : "";
    return {
      icon: SVG_ICONS.search,
      verb: query ? `searched "${queryTruncated}"${location}` : "searched",
    };
  }

  if (toolName.includes("find")) {
    const query = argValue(args, ["query", "Query", "pattern", "args"]);
    const searchPath = argValue(args, ["path", "Path", "SearchPath", "search_path"]);
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

  if (toolName.includes("command") || toolName.includes("run") || toolName.includes("bash") || toolName.includes("cmd") || toolName.includes("terminal") || toolName.includes("powershell")) {
    const cmd = argValue(args, ["command", "CommandLine", "command_line", "cmd", "args"]);
    return cmd
      ? { icon: SVG_ICONS.command, verb: "", target: cmd }
      : { icon: SVG_ICONS.command, verb: "command" };
  }

  const argument = summarizeArgs(data);
  return {
    icon: SVG_ICONS.tool,
    verb: data.tool_name || data.label || "tool",
    target: argument || undefined,
  };
}

function createToolGroup(turnId = ""): HTMLElement {
  const group = document.createElement("div");
  group.className = "tool-group";
  group.dataset.visibleCount = String(TOOL_GROUP_PREVIEW_LIMIT);
  if (turnId) group.dataset.turnId = turnId;

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

function latestToolGroup(transcriptEl: HTMLElement, turnId = ""): HTMLElement {
  let curr = transcriptEl.lastElementChild as HTMLElement | null;
  while (curr) {
    if (curr.classList.contains("tool-group")) {
      if (!turnId || !curr.dataset.turnId || curr.dataset.turnId === turnId) {
        return curr;
      }
      break;
    }
    if (
      curr.classList.contains("message-item") ||
      curr.classList.contains("stream-buffer") ||
      curr.classList.contains("thought-item")
    ) {
      break;
    }
    if (curr.classList.contains("file-change-card")) {
      curr = curr.previousElementSibling as HTMLElement | null;
      continue;
    }
    curr = curr.previousElementSibling as HTMLElement | null;
  }
  const group = createToolGroup(turnId);
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


export function handleToolItem(
  method: string,
  itemId: string,
  data: ToolItemData,
  turnId = "",
): void {
  const transcriptEl = getTranscriptElement();
  let el: HTMLElement | null = transcriptEl?.querySelector<HTMLElement>(`[data-tool-id="${data.tool_call_id}"]`) || null;
  if (method === "item.started") {
    el = document.createElement("div");
    el.className = "tool-item";
    el.dataset.toolId = data.tool_call_id ?? "";

    el.dataset.itemId = itemId;

    const header = document.createElement("div");
    header.className = "tool-header";

    const chevron = document.createElement("span");
    chevron.className = "tool-chevron";
    chevron.innerHTML = iconSvg("dot", 12, 2);

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
      target.title = summaryInfo.target;
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


    if (transcriptEl) {
      const group = latestToolGroup(transcriptEl, turnId);
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
        const sourceId = data.tool_call_id || itemId;
        const renderedAsFileCard = renderFileChanges(
          turnId,
          String(data.diff_text),
          String(sourceId || ""),
        );
        if (renderedAsFileCard) {
          body.querySelectorAll(".diff-content").forEach((diff) => diff.remove());
        } else {
          const diff = renderDiffBlock(data.diff_text);
          body.append(diff);
        }
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


function summarizeArgs(data: ToolItemData): string {
  const args = normalizedToolArgs(data);
  const primary = argValue(args, [
    "command",
    "CommandLine",
    "command_line",
    "cmd",
    "path",
    "file_path",
    "query",
    "pattern",
    "target",
    "url",
    "name",
    "args",
  ]);
  const fallback = primary || Object.values(args).find((value) => value != null && value !== "");
  const text = typeof fallback === "string" ? fallback : fallback == null ? "" : String(fallback);
  return text.length > 60 ? text.slice(0, 60) + "..." : text;
}

