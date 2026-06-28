import { describe, it, expect } from "vitest";
import { renderMarkdown, highlightCode } from "../src/markdown.js";

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
