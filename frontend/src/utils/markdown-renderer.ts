import {
  marked,
  Renderer,
  type Links,
  type MarkedOptions,
  type Token,
  type Tokens,
} from "marked";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import rust from "highlight.js/lib/languages/rust";
import diff from "highlight.js/lib/languages/diff";
import type { CanonicalBlockDescriptor } from "./markdown-worker-protocol";

export const CANONICAL_RAW_HTML_BLOCK_MAX_CHARS = 16 * 1024;

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

const MARKDOWN_OPTIONS: MarkedOptions<string, string> = {
  async: false,
  breaks: true,
  gfm: true,
};

class CanonicalRenderer extends Renderer {
  override code({ text, lang }: Tokens.Code): string {
    const language = (lang || "").match(/^\S*/)?.[0] || null;
    const code = `${text.replace(/\n$/, "")}\n`;
    const highlighted = highlightCanonicalCode(code, language);
    const className = language
      ? ` class="language-${escapeHtmlAttribute(language)}"`
      : "";
    return `<pre><code${className}>${highlighted}</code></pre>\n`;
  }
}

type LinkedTokenList = Token[] & { links: Links };

export function highlightCanonicalCode(
  code: string,
  language: string | null,
): string {
  try {
    if (language && hljs.getLanguage(language)) {
      return hljs.highlight(code, { language }).value;
    }
    return hljs.highlightAuto(code).value;
  } catch {
    return escapeHtml(code);
  }
}

export function renderMarkdownHtml(text: string): string {
  const source = String(text ?? "");
  const tokens = marked.lexer(source, MARKDOWN_OPTIONS);
  return marked.parser(tokens, parserOptions());
}

export function renderCanonicalMarkdownBlocks(
  text: string,
): CanonicalBlockDescriptor[] {
  const source = String(text ?? "");
  const tokens = marked.lexer(source, MARKDOWN_OPTIONS);
  const blocks: CanonicalBlockDescriptor[] = [];

  for (const token of tokens) {
    if (
      token.type === "html"
      && token.raw.length > CANONICAL_RAW_HTML_BLOCK_MAX_CHARS
    ) {
      blocks.push({
        kind: "text",
        text: token.raw,
        reason: "html_block_budget",
      });
      continue;
    }

    const blockTokens = [token] as LinkedTokenList;
    blockTokens.links = tokens.links;
    const html = marked.parser(blockTokens, parserOptions());
    blocks.push({
      kind: "html",
      html,
      sourceLength: token.raw.length,
    });
  }

  return blocks;
}

function parserOptions(): MarkedOptions<string, string> {
  return {
    ...MARKDOWN_OPTIONS,
    renderer: new CanonicalRenderer(),
  };
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeHtmlAttribute(text: string): string {
  return escapeHtml(text);
}
