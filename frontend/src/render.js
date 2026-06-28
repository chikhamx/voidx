import { renderMarkdown, highlightCode } from "./markdown.js";
import { takeCommittedStreams } from "./stream.js";

const RICH_TAG = /\[(\/)?(?:bold|dim|italic|underline|strike|red|green|yellow|blue|magenta|cyan|white|black|#[0-9A-Fa-f]{6})\]/g;

export function stripRichMarkup(text) {
  return String(text || "").replace(RICH_TAG, "");
}

export function nodeClassName(node) {
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

export function renderNodeElement(node, byId) {
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
    const diffText = node.payload?.diff_text || node.body_lines?.join("\n") || "";
    item.append(renderDiffBlock(diffText));
  } else if (type === "assistant") {
    const text = node.body_lines.map(stripRichMarkup).join("\n");
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

function renderBodyLines(node) {
  const body = document.createElement("div");
  body.className = "node-body";
  const text = node.body_lines.map(stripRichMarkup).join("\n");
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

function renderDiffBlock(diffText) {
  const block = document.createElement("pre");
  block.className = "node-diff";
  for (const line of String(diffText).split("\n")) {
    const row = document.createElement("div");
    row.className = diffLineClass(line);
    row.textContent = line;
    block.append(row);
  }
  return block;
}

export function diffLineClass(line) {
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

function renderSubagentCard(node) {
  const card = document.createElement("div");
  card.className = "subagent-card";
  const header = document.createElement("div");
  header.className = "subagent-header";
  const name = document.createElement("span");
  name.className = "subagent-name";
  name.textContent = node.payload?.name || node.agent_name || "subagent";
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
    desc.textContent = node.payload.description;
    card.append(desc);
  }
  return card;
}

export function formatToolMeta(payload) {
  const name = payload.tool_name || "tool";
  const args = payload.args ? ` ${payload.args}` : "";
  return `${name}${args}`.trim();
}

export function formatElapsed(seconds) {
  if (seconds == null || Number.isNaN(seconds)) {
    return "";
  }
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)}ms`;
  }
  return `${seconds.toFixed(1)}s`;
}

function depthFor(node, byId) {
  let depth = 0;
  let cursor = node;
  while (cursor.parent_id && byId.has(cursor.parent_id)) {
    depth += 1;
    cursor = byId.get(cursor.parent_id);
  }
  return depth;
}

export function renderTranscript(root, snapshot) {
  const committed = takeCommittedStreams();
  const byId = new Map(snapshot.nodes.map((n) => [n.id, n]));
  root.replaceChildren(
    ...snapshot.nodes
      .map((node) => renderNodeElement(node, byId))
      .filter((el) => el !== null),
  );
  for (const el of committed) {
    if (el.isConnected) {
      continue;
    }
    root.append(el);
  }
}

export function renderTodoPanel(panel, items, summary) {
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
