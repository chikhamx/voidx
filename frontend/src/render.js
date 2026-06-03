const RICH_TAG = /\[(\/)?(?:bold|dim|italic|underline|strike|red|green|yellow|blue|magenta|cyan|white|black|#[0-9A-Fa-f]{6}|[^\]]+)\]/g;

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

export function renderNodeElement(node, nodes) {
  const item = document.createElement("article");
  item.className = nodeClassName(node);
  item.dataset.nodeId = node.id;
  item.style.marginLeft = `${depthFor(node, nodes) * 18}px`;

  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = stripRichMarkup(node.title || node.header || node.node_type);
  item.append(title);

  if (node.meta && node.node_type === "thought") {
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

  if (node.payload?.diff_text) {
    item.append(renderDiffBlock(node.payload.diff_text));
  } else if (node.body_lines?.length) {
    item.append(renderBodyLines(node));
  }

  return item;
}

export function renderBodyLines(node) {
  const body = document.createElement("div");
  body.className = "node-body";
  const text = node.body_lines.map(stripRichMarkup).join("\n");
  if (node.node_type === "tool_call" || node.node_type === "tool_result") {
    const pre = document.createElement("pre");
    pre.className = "node-code";
    pre.textContent = text;
    body.append(pre);
  } else {
    body.textContent = text;
  }
  return body;
}

export function renderDiffBlock(diffText) {
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

function diffLineClass(line) {
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

function formatToolMeta(payload) {
  const name = payload.tool_name || "tool";
  const args = payload.args ? ` ${payload.args}` : "";
  return `${name}${args}`.trim();
}

function depthFor(node, nodes) {
  const byId = new Map(nodes.map((candidate) => [candidate.id, candidate]));
  let depth = 0;
  let cursor = node;
  while (cursor.parent_id && byId.has(cursor.parent_id)) {
    depth += 1;
    cursor = byId.get(cursor.parent_id);
  }
  return depth;
}

export function renderTranscript(root, snapshot) {
  root.replaceChildren(
    ...snapshot.nodes.map((node) => renderNodeElement(node, snapshot.nodes)),
  );
}

export function describeEvent(envelope) {
  const kind = envelope.payload?.kind || "unknown";
  const detail = eventDetail(envelope.payload);
  return detail ? `${envelope.seq} ${kind} ${detail}` : `${envelope.seq} ${kind}`;
}

function eventDetail(payload) {
  if (!payload) {
    return "";
  }
  if (payload.text) {
    return stripRichMarkup(payload.text).slice(0, 80);
  }
  if (payload.message) {
    return stripRichMarkup(payload.message).slice(0, 80);
  }
  if (payload.label) {
    return stripRichMarkup(payload.label).slice(0, 80);
  }
  return "";
}
