// @ts-nocheck
import { describe, it, expect } from "vitest";
import { renderMarkdown, highlightCode, stripPastedTags, renderUserMessage } from "../src/markdown";

describe("renderMarkdown", () => {
  it("renders bold text", () => {
    const el = renderMarkdown("**bold**");
    expect(el.className).toBe("markdown-body");
    expect(el.innerHTML).toContain("<strong>bold</strong>");
  });

  it("renders code block with language", () => {
    const el = renderMarkdown("```python\nprint('hi')\n```");
    const codeBlock = el.querySelector("pre code");
    expect(codeBlock).not.toBeNull();
    expect(codeBlock.className).toContain("language-python");
  });

  it("renders inline code", () => {
    const el = renderMarkdown("use `npm test`");
    expect(el.innerHTML).toContain("<code>");
    expect(el.textContent).toContain("npm test");
  });

  it("renders links", () => {
    const el = renderMarkdown("[example](https://example.com)");
    const link = el.querySelector("a");
    expect(link).not.toBeNull();
    expect(link.textContent).toBe("example");
  });

  it("renders empty string for null input", () => {
    const el = renderMarkdown(null);
    expect(el.className).toBe("markdown-body");
    expect(el.innerHTML).toBe("");
  });

  it("renders empty string for empty input", () => {
    const el = renderMarkdown("");
    expect(el.className).toBe("markdown-body");
  });

  it("sanitizes dangerous HTML", () => {
    const el = renderMarkdown('<img src=x onerror="alert(1)">');
    const img = el.querySelector("img[onerror]");
    expect(img).toBeNull();
  });

  it("renders heading", () => {
    const el = renderMarkdown("# Title");
    expect(el.querySelector("h1")).not.toBeNull();
  });
});

describe("highlightCode", () => {
  it("highlights with specified language", () => {
    const html = highlightCode("print('hi')", "python");
    expect(html).toContain("hljs");
    expect(html).toContain("print");
  });

  it("auto-detects language when not specified", () => {
    const html = highlightCode("const x = 1;", null);
    expect(html).toContain("hljs");
  });

  it("falls back to escaped text when language is unknown", () => {
    const html = highlightCode("some code", "unknown-lang");
    expect(html).toBe("some code");
  });

  it("escapes HTML in code", () => {
    const html = highlightCode("<script>alert(1)</script>", "javascript");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});


describe("stripPastedTags", () => {
  it("returns original text when no tags present", () => {
    const text = "hello world";
    expect(stripPastedTags(text)).toBe(text);
  });

  it("converts single block to blockquote", () => {
    const text = "fix\n<pasted>\ncode line\n</pasted>\npls";
    const result = stripPastedTags(text);
    expect(result).not.toContain("<pasted>");
    expect(result).not.toContain("</pasted>");
    expect(result).toContain("> code line");
    expect(result).toContain("fix");
    expect(result).toContain("pls");
  });

  it("converts multiple blocks independently", () => {
    const text = "a\n<pasted>\nb\n</pasted>\nc\n<pasted>\nd\n</pasted>\ne";
    const result = stripPastedTags(text);
    expect(result).not.toContain("<pasted>");
    expect(result).toContain("> b");
    expect(result).toContain("> d");
    expect(result).toContain("a");
    expect(result).toContain("c");
    expect(result).toContain("e");
  });

  it("handles empty pasted content", () => {
    const text = "<pasted>\n\n</pasted>";
    const result = stripPastedTags(text);
    expect(result).not.toContain("<pasted>");
    expect(result).toContain("> ");
  });

  it("returns original text for unclosed tag", () => {
    const text = "fix\n<pasted>\ncode\npls";
    expect(stripPastedTags(text)).toBe(text);
  });

  it("handles lines starting with > (nested blockquote)", () => {
    const text = "<pasted>\n> existing quote\n</pasted>";
    const result = stripPastedTags(text);
    expect(result).not.toContain("<pasted>");
    expect(result).toContain("> > existing quote");
  });
});


describe("renderUserMessage", () => {
  it("returns a markdown-body element", () => {
    const el = renderUserMessage("hello");
    expect(el.className).toBe("markdown-body");
  });

  it("renders pasted block as blockquote", () => {
    const text = "fix\n<pasted>\ncode line\n</pasted>\npls";
    const el = renderUserMessage(text);
    expect(el.querySelector("blockquote")).not.toBeNull();
    expect(el.textContent).not.toContain("<pasted>");
    expect(el.textContent).toContain("code line");
  });

  it("renders plain text without pasted tags as normal markdown", () => {
    const el = renderUserMessage("just plain text");
    expect(el.querySelector("blockquote")).toBeNull();
    expect(el.textContent).toContain("just plain text");
  });
});
