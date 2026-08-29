import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import rust from "highlight.js/lib/languages/rust";
import diff from "highlight.js/lib/languages/diff";

hljs.registerLanguage("python", python);
hljs.registerLanguage("py", python);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("js", javascript);
hljs.registerLanguage("typescript", javascript);
hljs.registerLanguage("ts", javascript);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("sh", bash);
hljs.registerLanguage("shell", bash);
hljs.registerLanguage("json", json);
hljs.registerLanguage("rust", rust);
hljs.registerLanguage("rs", rust);
hljs.registerLanguage("diff", diff);

marked.setOptions({
  breaks: true,
  gfm: true,
});

export function renderMarkdown(text: string): HTMLElement {
  const container = document.createElement("div");
  container.className = "markdown-body";
  try {
    const html = marked.parse(text || "", { async: false }) as string;
    if (typeof html === "string") {
      container.innerHTML = DOMPurify.sanitize(html) as unknown as string;
    } else {
      container.textContent = text || "";
    }
    container
      .querySelectorAll<HTMLElement>("pre code")
      .forEach((block) => {
        try {
          const lang = detectLang(block);
          if (lang && hljs.getLanguage(lang)) {
            block.innerHTML = hljs.highlight(block.textContent ?? "", {
              language: lang,
            }).value;
          } else {
            block.innerHTML = hljs.highlightAuto(
              block.textContent ?? "",
            ).value;
          }
        } catch {
          // leave block as-is on highlight failure
        }
      });
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

const STREAMING_TAIL_SOFT_LIMIT = 8 * 1024;
const STREAMING_TAIL_HARD_LIMIT = 16 * 1024;

export interface StreamingMarkdownProjection {
  update(text: string): void;
  commit(): void;
  reset(): void;
}

/**
 * Keeps a conservative, append-only Markdown preview while a stream is live.
 * The projection is deliberately provisional: commit() always installs the
 * canonical full render so parser/highlight differences cannot leak into the
 * final transcript.
 */
export class StreamingMarkdownProjectionImpl implements StreamingMarkdownProjection {
  private rawText = "";
  private stableText = "";
  private mutableTail = "";
  private provisionalText = "";
  private stableNodes: Node[] = [];
  private provisionalNodes: Node[] = [];
  private mutableNodes: Node[] = [];
  private committed = false;

  constructor(private readonly target: HTMLElement) {}

  update(text: string): void {
    const nextText = String(text || "");
    if (
      this.committed ||
      (this.rawText.length > 0 && !nextText.startsWith(this.rawText))
    ) {
      this.rebuild(nextText);
      return;
    }

    if (
      this.rawText.length > 0
      && nextText.startsWith(this.rawText)
      && this.rawText.endsWith("\n\n")
    ) {
      const suffix = nextText.slice(this.rawText.length);
      this.stableNodes = Array.from(this.target.childNodes);
      this.provisionalNodes = [];
      this.mutableNodes = [];
      this.stableText = nextText;
      this.mutableTail = "";
      this.provisionalText = "";
      this.rawText = nextText;
      if (suffix) {
        this.target.append(...renderChildren(suffix));
      }
      this.committed = false;
      return;
    }

    const nextStableLength = stablePrefixLength(nextText);
    if (nextStableLength < this.stableText.length) {
      this.rebuild(nextText);
      return;
    }

    if (nextStableLength > this.stableText.length) {
      const stablePrefix = nextText.slice(0, nextStableLength);
      if (
        this.stableText.length === 0
        && this.rawText.length > 0
        && stablePrefix.startsWith(this.rawText)
        && this.provisionalNodes.length === 0
      ) {
        // The previous snapshot already rendered this prefix. Promote those
        // nodes instead of reparsing and replacing their DOM identity.
        this.stableNodes = this.mutableNodes;
        this.mutableNodes = [];
        this.stableText = stablePrefix;
      } else {
        // A non-empty mutable block may have changed shape while it became
        // closed. Rebuild only in that ambiguous case; whitespace is inert.
        if (this.mutableTail.trim() || this.provisionalText) {
          this.rebuild(nextText);
          return;
        }
        removeNodes(this.mutableNodes);
        const stableAppend = nextText.slice(
          this.stableText.length,
          nextStableLength,
        );
        this.stableNodes.push(...renderChildren(stableAppend));
        this.stableText = stablePrefix;
      }
    }

    this.rawText = nextText;
    this.renderMutableTail(nextText.slice(nextStableLength));
    this.committed = false;
  }

  commit(): void {
    const canonical = renderMarkdown(this.rawText);
    this.target.replaceChildren(...Array.from(canonical.childNodes));
    this.stableNodes = Array.from(this.target.childNodes);
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.stableText = this.rawText;
    this.mutableTail = "";
    this.provisionalText = "";
    this.committed = true;
  }

  reset(): void {
    this.rawText = "";
    this.stableText = "";
    this.mutableTail = "";
    this.provisionalText = "";
    this.stableNodes = [];
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.committed = false;
    this.target.replaceChildren();
  }

  private rebuild(text: string): void {
    this.target.replaceChildren();
    this.rawText = text;
    this.stableText = "";
    this.mutableTail = "";
    this.provisionalText = "";
    this.stableNodes = [];
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.committed = false;

    const stableLength = stablePrefixLength(text);
    this.stableText = text.slice(0, stableLength);
    this.stableNodes = renderChildren(this.stableText);
    this.target.append(...this.stableNodes);
    this.renderMutableTail(text.slice(stableLength));
  }

  private renderMutableTail(text: string): void {
    removeNodes(this.provisionalNodes);
    removeNodes(this.mutableNodes);
    this.provisionalNodes = [];
    this.mutableNodes = [];
    this.mutableTail = text;

    const provisionalLength =
      text.length > STREAMING_TAIL_HARD_LIMIT
        ? safeProvisionalBoundary(text, text.length - STREAMING_TAIL_SOFT_LIMIT)
        : 0;
    this.provisionalText = text.slice(0, provisionalLength);
    const tail = text.slice(provisionalLength);

    if (this.provisionalText) {
      const provisional = document.createTextNode(this.provisionalText);
      this.provisionalNodes = [provisional];
      this.target.append(provisional);
    }

    this.mutableNodes = renderStreamingTail(tail);
    this.target.append(...this.mutableNodes);
  }
}

export function createStreamingMarkdownProjection(
  target: HTMLElement,
): StreamingMarkdownProjection {
  return new StreamingMarkdownProjectionImpl(target);
}

function renderChildren(text: string): Node[] {
  const rendered = renderMarkdown(text);
  return Array.from(rendered.childNodes);
}

function renderStreamingTail(text: string): Node[] {
  if (!text) {
    return [];
  }
  // An open fence or HTML block is parser-uncertain. Show escaped text until
  // the block closes; commit() will replace this with the highlighted render.
  if (hasUnclosedFence(text) || /<\/?[A-Za-z!][^>]*$|<\/?[A-Za-z!][^>]*>/s.test(text)) {
    return [document.createTextNode(text)];
  }
  return renderChildren(text);
}

function stablePrefixLength(text: string): number {
  let offset = 0;
  let foundStableBlock = false;
  const tokens = marked.lexer(text) as StreamingToken[];
  for (const token of tokens) {
    const raw = typeof token.raw === "string" ? token.raw : "";
    if (!raw) {
      break;
    }
    if (token.type === "space") {
      if (!foundStableBlock) {
        break;
      }
      offset += raw.length;
      continue;
    }
    if (!isStableToken(token, raw)) {
      break;
    }
    foundStableBlock = true;
    offset += raw.length;
  }
  return foundStableBlock ? offset : 0;
}

function isStableToken(token: StreamingToken, raw: string): boolean {
  if (!/\r?\n$/.test(raw) || containsCrossBlockSyntax(raw)) {
    return false;
  }
  switch (token.type) {
    case "heading":
    case "hr":
      return true;
    case "code":
      return hasClosedFence(raw);
    case "paragraph":
      return /\r?\n\r?\n$/.test(raw);
    default:
      return false;
  }
}

function containsCrossBlockSyntax(raw: string): boolean {
  return (
    /<\/?[A-Za-z!][^>]*>/s.test(raw) ||
    /^\s{0,3}\[[^\]]+\]:/m.test(raw) ||
    /\[[^\]]+\]\s*\[[^\]]*\]/.test(raw)
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
  if (lineBoundary > 0) {
    return lineBoundary + 1;
  }
  return bounded;
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

export function highlightCode(code: string, lang: string): string {
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  } catch {
    return escapeHtml(code);
  }
}

function detectLang(block: HTMLElement): string | null {
  const classes = block.className || "";
  const match = classes.match(/language-([\w-]+)/);
  return match ? match[1] : null;
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
