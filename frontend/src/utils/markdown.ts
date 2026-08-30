import { marked } from "marked";
import DOMPurify from "dompurify";
import {
  highlightCanonicalCode,
  renderMarkdownHtml,
} from "./markdown-renderer";

marked.setOptions({
  breaks: true,
  gfm: true,
});

export function renderMarkdown(text: string): HTMLElement {
  const container = document.createElement("div");
  container.className = "markdown-body";
  try {
    const html = renderMarkdownHtml(text || "");
    container.innerHTML = DOMPurify.sanitize(html) as unknown as string;
  } catch {
    container.textContent = text || "";
  }
  return container;
}



type StreamingToken = {
  type?: string;
  raw?: string;
  [key: string]: unknown;
};

export type StreamUpdateOperation = "append" | "replace";

export interface StreamingMarkdownDebugSnapshot {
  rawTextLength: number;
  stableNodeCount: number;
  provisionalNodeCount: number;
  mutableNodeCount: number;
  mutableTailLength: number;
  lastUpdateOperation: StreamUpdateOperation;
  lastUpdateInputLength: number;
}

const STREAMING_TAIL_SOFT_LIMIT = 8 * 1024;
const STREAMING_TAIL_HARD_LIMIT = 16 * 1024;

export interface StreamingMarkdownProjection {
  update(text: string, operation?: StreamUpdateOperation): void;
  commit(canonicalText?: string): void;
  reset(): void;
  _debugSnapshotForTest?(): StreamingMarkdownDebugSnapshot;
}

/**
 * Maintains an append-only live preview with three regions:
 * rendered stable blocks, escaped provisional prefixes, and one bounded
 * mutable tail. The complete raw source is retained separately for commit.
 */
export class StreamingMarkdownProjectionImpl implements StreamingMarkdownProjection {
  private rawText = "";
  private mutableTail = "";
  private stableNodes: Node[] = [];
  private provisionalNodes: Node[] = [];
  private mutableNodes: Node[] = [];
  private committed = false;
  private lastUpdateOperation: StreamUpdateOperation = "replace";
  private lastUpdateInputLength = 0;

  constructor(private readonly target: HTMLElement) {}

  update(text: string, operation?: StreamUpdateOperation): void {
    const incoming = String(text ?? "");
    if (operation !== undefined && operation !== "append" && operation !== "replace") {
      throw new Error("stream projection operation must be append or replace");
    }

    this.lastUpdateOperation = operation ?? "replace";
    this.lastUpdateInputLength = incoming.length;

    // The old one-argument API receives cumulative snapshots. Preserve stable
    // identity when that snapshot is a prefix extension, otherwise replace it.
    if (operation === undefined) {
      if (!this.committed && incoming.startsWith(this.rawText)) {
        this.appendDelta(incoming.slice(this.rawText.length));
      } else {
        this.replaceCanonical(incoming);
      }
      return;
    }

    if (operation === "replace") {
      this.replaceCanonical(incoming);
      return;
    }

    if (this.committed) {
      this.replaceCanonical(this.rawText + incoming);
      this.lastUpdateOperation = "append";
      this.lastUpdateInputLength = incoming.length;
      return;
    }
    this.appendDelta(incoming);
  }

  commit(canonicalText?: string): void {
    if (canonicalText !== undefined) {
      this.rawText = String(canonicalText ?? "");
    }
    const canonical = renderMarkdown(this.rawText);
    this.target.replaceChildren(...Array.from(canonical.childNodes));
    this.stableNodes = Array.from(this.target.childNodes);
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.mutableTail = "";
    this.committed = true;
  }

  reset(): void {
    this.clearProjection();
  }

  _debugSnapshotForTest(): StreamingMarkdownDebugSnapshot {
    return {
      rawTextLength: this.rawText.length,
      stableNodeCount: this.stableNodes.length,
      provisionalNodeCount: this.provisionalNodes.length,
      mutableNodeCount: this.mutableNodes.length,
      mutableTailLength: this.mutableTail.length,
      lastUpdateOperation: this.lastUpdateOperation,
      lastUpdateInputLength: this.lastUpdateInputLength,
    };
  }

  private appendDelta(delta: string): void {
    removeNodes(this.mutableNodes);
    this.mutableNodes = [];
    this.rawText += delta;
    this.feed(delta);
    this.renderMutableTail();
    this.committed = false;
  }

  private replaceCanonical(text: string): void {
    this.clearProjection();
    this.rawText = text;
    this.feed(text);
    this.renderMutableTail();
  }

  private clearProjection(): void {
    this.rawText = "";
    this.mutableTail = "";
    this.stableNodes = [];
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.committed = false;
    this.target.replaceChildren();
  }

  private feed(text: string): void {
    let offset = 0;
    while (offset < text.length) {
      if (this.mutableTail.length >= STREAMING_TAIL_HARD_LIMIT) {
        this.freezeProvisionalPrefix();
      }
      const capacity = STREAMING_TAIL_HARD_LIMIT - this.mutableTail.length;
      const take = Math.min(text.length - offset, Math.max(capacity, 1));
      this.mutableTail += text.slice(offset, offset + take);
      offset += take;
      this.freezeStablePrefix();
      if (
        offset < text.length &&
        this.mutableTail.length >= STREAMING_TAIL_HARD_LIMIT
      ) {
        this.freezeProvisionalPrefix();
      }
    }
  }

  private freezeStablePrefix(): void {
    // Once parser-uncertain text has been frozen, the truncated parser context
    // is no longer sufficient to prove later Markdown stable. Keep it escaped.
    if (this.provisionalNodes.length > 0) {
      return;
    }
    const stableLength = stablePrefixLength(this.mutableTail);
    if (stableLength <= 0) {
      return;
    }
    const stableText = this.mutableTail.slice(0, stableLength);
    this.mutableTail = this.mutableTail.slice(stableLength);
    const nodes = renderChildren(stableText);
    this.stableNodes.push(...nodes);
    this.target.append(...nodes);
  }

  private freezeProvisionalPrefix(): void {
    if (this.mutableTail.length <= STREAMING_TAIL_SOFT_LIMIT) {
      return;
    }
    const requested = this.mutableTail.length - STREAMING_TAIL_SOFT_LIMIT;
    const boundary = safeProvisionalBoundary(
      this.mutableTail,
      requested,
    );
    const provisionalText = this.mutableTail.slice(0, boundary);
    this.mutableTail = this.mutableTail.slice(boundary);
    if (!provisionalText) {
      return;
    }
    const node = document.createTextNode(provisionalText);
    this.provisionalNodes.push(node);
    this.target.append(node);
  }

  private renderMutableTail(): void {
    removeNodes(this.mutableNodes);
    this.mutableNodes = [];
    if (!this.mutableTail) {
      return;
    }
    this.mutableNodes =
      this.provisionalNodes.length > 0
        ? [document.createTextNode(this.mutableTail)]
        : renderStreamingTail(this.mutableTail);
    this.target.append(...this.mutableNodes);
  }
}

export function createStreamingMarkdownProjection(
  target: HTMLElement,
): StreamingMarkdownProjection {
  return new StreamingMarkdownProjectionImpl(target);
}

function renderChildren(text: string): Node[] {
  if (!text) {
    return [];
  }
  const rendered = renderMarkdown(text);
  return Array.from(rendered.childNodes);
}

function renderStreamingTail(text: string): Node[] {
  if (!text) {
    return [];
  }
  if (isParserUncertain(text)) {
    return [document.createTextNode(text)];
  }
  const nodes = renderChildren(text);
  if (!/\r?\n$/.test(text)) {
    while (
      nodes.length > 0
      && nodes[nodes.length - 1].nodeType === Node.TEXT_NODE
      && !nodes[nodes.length - 1].textContent?.trim()
    ) {
      nodes.pop();
    }
  }
  return nodes;
}

function stablePrefixLength(text: string): number {
  if (!text || containsCrossBlockSyntax(text)) {
    return 0;
  }

  let tokens: StreamingToken[];
  try {
    tokens = marked.lexer(text) as StreamingToken[];
  } catch {
    return 0;
  }

  let offset = 0;
  let stableOffset = 0;
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    const raw = typeof token.raw === "string" ? token.raw : "";
    if (!raw) {
      break;
    }
    if (token.type === "space") {
      if (stableOffset !== offset) {
        break;
      }
      offset += raw.length;
      stableOffset = offset;
      continue;
    }

    const next = tokens[index + 1];
    const nextRaw = typeof next?.raw === "string" ? next.raw : "";
    if (!isStableToken(token, raw, next, nextRaw)) {
      break;
    }
    offset += raw.length;
    stableOffset = offset;
  }
  return stableOffset;
}

function isStableToken(
  token: StreamingToken,
  raw: string,
  next: StreamingToken | undefined,
  nextRaw: string,
): boolean {
  if (containsCrossBlockSyntax(raw)) {
    return false;
  }
  switch (token.type) {
    case "heading":
    case "hr":
      return /\r?\n$/.test(raw) ||
        (next?.type === "space" && /\r?\n/.test(nextRaw));
    case "code":
      return hasClosedFence(raw);
    case "paragraph":
      return /\r?\n\r?\n$/.test(raw) ||
        (next?.type === "space" && /\r?\n/.test(nextRaw));
    default:
      return false;
  }
}

function isParserUncertain(text: string): boolean {
  return (
    hasUnclosedFence(text) ||
    /<\/?[A-Za-z!][^>]*(?:>|$)/s.test(text) ||
    /^\s{0,3}\[[^\]]+\]:/m.test(text) ||
    /\[[^\]]+\]\s*\[[^\]]*\]/.test(text) ||
    /^\s{0,3}(?:[-+*]|\d+[.)]|>)\s/m.test(text)
  );
}

function containsCrossBlockSyntax(raw: string): boolean {
  return (
    /<\/?[A-Za-z!][^>]*>/s.test(raw) ||
    /^\s{0,3}\[[^\]]+\]:/m.test(raw) ||
    /\[[^\]]+\]\s*\[[^\]]*\]/.test(raw) ||
    /^\s{0,3}(?:[-+*]|\d+[.)]|>)\s/m.test(raw)
  );
}

function hasClosedFence(raw: string): boolean {
  const opening = raw.match(/^\s{0,3}(`{3,}|~{3,})[^\n]*(?:\r?\n|$)/);
  if (!opening || !/\r?\n$/.test(raw)) {
    return false;
  }
  const marker = opening[1][0];
  const size = opening[1].length;
  const closing = new RegExp(
    `^\\s{0,3}${marker}{${size},}\\s*$`,
    "m",
  );
  return closing.test(raw.slice(opening[0].length));
}

function hasUnclosedFence(text: string): boolean {
  const fences = text.match(/^\s{0,3}(`{3,}|~{3,})/gm) || [];
  return fences.length % 2 === 1;
}

function safeProvisionalBoundary(text: string, requested: number): number {
  const bounded = Math.max(0, Math.min(requested, text.length));
  const lineBoundary = text.lastIndexOf("\n", bounded);
  if (lineBoundary >= 0) {
    return lineBoundary + 1;
  }
  if (
    bounded > 0
    && bounded < text.length
    && isHighSurrogate(text.charCodeAt(bounded - 1))
    && isLowSurrogate(text.charCodeAt(bounded))
  ) {
    return bounded - 1;
  }
  return bounded;
}

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}

function removeNodes(nodes: Node[]): void {
  for (const node of nodes) {
    if (node.parentNode) {
      node.parentNode.removeChild(node);
    }
  }
}

const PASTED_RE = /<pasted>\n([\s\S]*?)\n<\/pasted>/g;

export function stripPastedTags(text: string): string {
  if (!text || !text.includes("<pasted>")) {
    return text;
  }
  return text.replace(PASTED_RE, (_match: string, content: string) => {
    const quoted = content
      .split("\n")
      .map((line) => `> ${line}`)
      .join("\n");
    return `\n${quoted}\n`;
  });
}

export function renderUserMessage(text: string): HTMLElement {
  return renderMarkdown(stripPastedTags(text));
}

export function highlightCode(code: string, lang: string | null): string {
  return highlightCanonicalCode(code, lang);
}
