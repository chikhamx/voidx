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

export function renderMarkdown(text) {
  const container = document.createElement("div");
  container.className = "markdown-body";
  try {
    const html = marked.parse(text || "", { async: false });
    if (typeof html === "string") {
      container.innerHTML = DOMPurify.sanitize(html);
    } else {
      container.textContent = text || "";
    }
    container.querySelectorAll("pre code").forEach((block) => {
      try {
        const lang = detectLang(block);
        if (lang && hljs.getLanguage(lang)) {
          block.innerHTML = hljs.highlight(block.textContent, { language: lang }).value;
        } else {
          block.innerHTML = hljs.highlightAuto(block.textContent).value;
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

export function highlightCode(code, lang) {
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  } catch {
    return escapeHtml(code);
  }
}

function detectLang(block) {
  const classes = block.className || "";
  const match = classes.match(/language-([\w-]+)/);
  return match ? match[1] : null;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
