import { takeCommittedStreams, getTranscriptElement } from './stream';
import type { TranscriptNode } from '../rpc/protocol';
import { iconSvg } from './icons';
import { TOOL_GROUP_PREVIEW_LIMIT, type ToolInfo, type ToolItemData } from './render-types';
import { formatElapsed, truncateText, renderDiffBlock } from './render';

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

