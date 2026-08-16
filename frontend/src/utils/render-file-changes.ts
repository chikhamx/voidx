import { getTranscriptElement } from "./stream";

export const FILE_CHANGE_PREVIEW_LIMIT = 3;

type FileChangeOperation = "created" | "modified" | "deleted";

export interface FileChange {
  path: string;
  operation: FileChangeOperation;
  added: number;
  removed: number;
  diffText: string;
}

interface FileChangeCardState {
  card: HTMLElement;
  files: Map<string, FileChange>;
  legacyFiles: Map<string, FileChange>;
  sources: Map<string, string>;
  expanded: boolean;
}

const cards = new Map<string, FileChangeCardState>();

export function parseUnifiedDiff(diffText: string): FileChange[] {
  const files: FileChange[] = [];
  let current: {
    oldPath: string;
    newPath: string;
    path: string;
    operation: FileChangeOperation;
    added: number;
    removed: number;
    lines: string[];
    hasNewHeader: boolean;
  } | null = null;

  const finish = (): void => {
    if (!current || !current.hasNewHeader || !current.path) return;
    files.push({
      path: current.path,
      operation: current.operation,
      added: current.added,
      removed: current.removed,
      diffText: current.lines.join("\n"),
    });
  };

  for (const line of String(diffText || "").split("\n")) {
    if (line.startsWith("--- ")) {
      finish();
      current = {
        oldPath: cleanDiffPath(line.slice(4)),
        newPath: "",
        path: "",
        operation: "modified",
        added: 0,
        removed: 0,
        lines: [line],
        hasNewHeader: false,
      };
      continue;
    }

    if (!current) continue;
    current.lines.push(line);

    if (line.startsWith("+++ ")) {
      current.newPath = cleanDiffPath(line.slice(4));
      current.path = displayPath(current.oldPath, current.newPath);
      current.operation = operationFor(current.oldPath, current.newPath);
      current.hasNewHeader = true;
      continue;
    }

    if (line.startsWith("+") && !line.startsWith("+++")) {
      current.added += 1;
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.removed += 1;
    }
  }

  finish();
  return files;
}

export function renderFileChanges(
  turnId: string,
  diffText: string,
  sourceId = "",
): boolean {
  const key = turnId || "__unscoped__";
  let state = cards.get(key);
  if (!state) {
    const card = createCard();
    state = {
      card,
      files: new Map(),
      legacyFiles: new Map(),
      sources: new Map(),
      expanded: false,
    };
    cards.set(key, state);
  }

  let currentFiles: FileChange[];
  if (sourceId) {
    const previous = state.sources.get(sourceId) || "";
    const buffered = mergeSourceText(previous, String(diffText || ""));
    state.sources.set(sourceId, buffered);
    currentFiles = parseUnifiedDiff(buffered);
  } else {
    currentFiles = parseUnifiedDiff(diffText);
    if (currentFiles.length === 0) return false;
    mergeFiles(state.legacyFiles, currentFiles);
  }

  rebuildFiles(state);
  if (currentFiles.length === 0) {
    const buffered = sourceId ? state.sources.get(sourceId) || "" : "";
    return Boolean(sourceId && looksLikeUnifiedDiff(buffered));
  }

  const transcript = getTranscriptElement();
  if (transcript && !state.card.isConnected) {
    transcript.append(state.card);
  }
  renderCard(state);
  if (transcript) transcript.scrollTop = transcript.scrollHeight;
  return true;
}

function rebuildFiles(state: FileChangeCardState): void {
  const files = new Map<string, FileChange>();
  mergeFiles(files, [...state.legacyFiles.values()]);
  for (const diffText of state.sources.values()) {
    mergeFiles(files, parseUnifiedDiff(diffText));
  }
  state.files = files;
}

function mergeFiles(target: Map<string, FileChange>, files: FileChange[]): void {
  for (const file of files) {
    const existing = target.get(file.path);
    if (!existing) {
      target.set(file.path, { ...file });
      continue;
    }
    existing.added += file.added;
    existing.removed += file.removed;
    existing.diffText = mergeDiffText(existing.diffText, file.diffText);
    existing.operation = mergeOperation(existing.operation, file.operation);
  }
}

function mergeSourceText(previous: string, incoming: string): string {
  if (!previous) return incoming;
  if (!incoming || incoming === previous) return previous;
  if (incoming.startsWith(previous)) return incoming;
  if (previous.startsWith(incoming)) return previous;
  return `${previous}\n${incoming}`;
}

function looksLikeUnifiedDiff(value: string): boolean {
  return /^(?:--- |\+\+\+ |@@)/m.test(value);
}

export function resetFileChangeCards(): void {
  for (const state of cards.values()) {
    state.card.remove();
  }
  cards.clear();
}

function createCard(): HTMLElement {
  const card = document.createElement("section");
  card.className = "file-change-card";
  card.setAttribute("aria-label", "File changes");
  return card;
}

function renderCard(state: FileChangeCardState): void {
  const files = [...state.files.values()];
  const added = files.reduce((total, file) => total + file.added, 0);
  const removed = files.reduce((total, file) => total + file.removed, 0);

  state.card.replaceChildren();

  const header = document.createElement("div");
  header.className = "file-change-header";

  const title = document.createElement("span");
  title.className = "file-change-title";
  title.textContent = `${files.length} ${files.length === 1 ? "file" : "files"} changed`;

  const stats = document.createElement("span");
  stats.className = "file-change-stats";
  stats.append(
    statElement("file-change-added", `+${added}`),
    statElement("file-change-removed", `-${removed}`),
  );
  header.append(title, stats);
  state.card.append(header);

  const list = document.createElement("div");
  list.className = "file-change-list";
  const visibleFiles = state.expanded ? files : files.slice(0, FILE_CHANGE_PREVIEW_LIMIT);
  for (const file of visibleFiles) {
    list.append(renderFileRow(file));
  }
  state.card.append(list);

  if (files.length > FILE_CHANGE_PREVIEW_LIMIT) {
    const expand = document.createElement("button");
    expand.type = "button";
    expand.className = "file-change-expand";
    expand.setAttribute("aria-expanded", String(state.expanded));
    expand.textContent = state.expanded
      ? "Show fewer files"
      : `Show ${files.length - FILE_CHANGE_PREVIEW_LIMIT} more ${files.length - FILE_CHANGE_PREVIEW_LIMIT === 1 ? "file" : "files"}`;
    expand.addEventListener("click", () => {
      state.expanded = !state.expanded;
      renderCard(state);
    });
    state.card.append(expand);
  }
}

function renderFileRow(file: FileChange): HTMLElement {
  const entry = document.createElement("div");
  entry.className = "file-change-entry";

  const row = document.createElement("button");
  row.type = "button";
  row.className = "file-change-row";
  row.dataset.operation = file.operation;
  row.setAttribute("aria-expanded", "false");

  const operation = document.createElement("span");
  operation.className = `file-change-operation file-change-operation-${file.operation}`;
  operation.textContent = operationLabel(file.operation);

  const path = document.createElement("span");
  path.className = "file-change-path";
  path.textContent = file.path;
  path.title = file.path;

  const stats = document.createElement("span");
  stats.className = "file-change-row-stats";
  stats.append(
    statElement("file-change-added", `+${file.added}`),
    statElement("file-change-removed", `-${file.removed}`),
  );

  const chevron = document.createElement("span");
  chevron.className = "file-change-chevron";
  chevron.textContent = "›";
  row.append(operation, path, stats, chevron);

  const detail = document.createElement("div");
  detail.className = "file-change-detail";
  detail.hidden = true;
  detail.append(renderDiff(file.diffText));

  row.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    row.setAttribute("aria-expanded", String(!detail.hidden));
    chevron.textContent = detail.hidden ? "›" : "⌄";
  });

  entry.append(row, detail);
  return entry;
}

function renderDiff(diffText: string): HTMLPreElement {
  const block = document.createElement("pre");
  block.className = "file-change-diff";
  for (const line of String(diffText || "").split("\n")) {
    const row = document.createElement("div");
    row.className = diffLineClass(line);
    row.textContent = line;
    block.append(row);
  }
  return block;
}

function statElement(className: string, text: string): HTMLSpanElement {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

function operationLabel(operation: FileChangeOperation): string {
  if (operation === "created") return "A";
  if (operation === "deleted") return "D";
  return "M";
}

function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-meta";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-context";
}

function cleanDiffPath(value: string): string {
  const path = value.split("\t", 1)[0].trim().replace(/^"|"$/g, "");
  if (path === "/dev/null" || path === "dev/null") return "/dev/null";
  if (path.startsWith("a/") || path.startsWith("b/")) return path.slice(2);
  return path;
}

function displayPath(oldPath: string, newPath: string): string {
  return newPath && newPath !== "/dev/null" ? newPath : oldPath;
}

function operationFor(oldPath: string, newPath: string): FileChangeOperation {
  if (oldPath === "/dev/null") return "created";
  if (newPath === "/dev/null") return "deleted";
  return "modified";
}

function mergeOperation(
  previous: FileChangeOperation,
  next: FileChangeOperation,
): FileChangeOperation {
  if (next === "deleted") return "deleted";
  if (previous === "created") return "created";
  return next;
}

function mergeDiffText(previous: string, next: string): string {
  if (!previous) return next;
  if (!next) return previous;
  return `${previous}\n${next}`;
}
