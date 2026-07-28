import { renderMarkdown } from './markdown';
import { iconSvg } from './icons';
import { takeCommittedStreams, getTranscriptElement } from './stream';
import type { ThoughtItemData } from './render-types';
import { formatElapsed } from './render';

function formatThoughtMeta(meta: string | null | undefined, elapsed?: number | null): string {
  let seconds = elapsed;

  if ((seconds === undefined || seconds === null) && meta) {
    const match = meta.match(/Thinking for ([\d.]+)s/);
    if (match) {
      seconds = parseFloat(match[1]);
    } else {
      const num = parseFloat(meta);
      if (!isNaN(num)) {
        seconds = num;
      }
    }
  }

  if (seconds !== undefined && seconds !== null) {
    if (seconds < 1) {
      const ms = Math.round(seconds * 1000);
      return `thought for ${ms}ms`;
    } else if (seconds < 60) {
      return `thought for ${seconds.toFixed(1)}s`;
    } else {
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return `thought for ${m}m ${s}s`;
    }
  }

  if (meta) {
    if (meta.toLowerCase() === "thinking") {
      return "thought";
    }
    return meta.replace(/^thinking/i, "thought");
  }

  return "thought";
}

function findMergeableThoughtTarget(
  insertBeforeEl: HTMLElement | null,
  transcriptEl: HTMLElement
): HTMLElement | null {
  const curr = insertBeforeEl
    ? (insertBeforeEl.previousElementSibling as HTMLElement | null)
    : (transcriptEl.lastElementChild as HTMLElement | null);

  if (curr?.classList.contains("thought-item")) {
    return curr;
  }
  return null;
}

export function appendThoughtItem(
  itemId: string,
  data: ThoughtItemData,
  insertBeforeEl?: HTMLElement | null
): void {
  const transcriptEl = getTranscriptElement();
  if (!transcriptEl) return;

  const mergeTarget = findMergeableThoughtTarget(insertBeforeEl || null, transcriptEl);

  if (mergeTarget && mergeTarget.classList.contains("thought-item")) {
    const body = mergeTarget.querySelector<HTMLElement>(".thought-body");
    const label = mergeTarget.querySelector<HTMLElement>(".thought-label");

    const prevText = mergeTarget.dataset.text || "";
    const prevElapsed = mergeTarget.dataset.elapsed ? parseInt(mergeTarget.dataset.elapsed, 10) : 0;

    const combinedText = prevText ? (prevText + "\n\n" + (data.text || "")) : (data.text || "");
    const combinedElapsed = prevElapsed + (typeof data.elapsed === "number" ? data.elapsed : 0);

    mergeTarget.dataset.text = combinedText;
    mergeTarget.dataset.elapsed = String(combinedElapsed);

    const chevron = mergeTarget.querySelector<HTMLElement>(".thought-chevron");

    if (label) {
      const formatted = formatThoughtMeta(data.meta, combinedElapsed);
      label.innerHTML = `${iconSvg("brain", 14, 2)}${formatted}`;
    }

    if (chevron && body) {
      chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
    }

    if (body && data.text) {
      if (body.firstElementChild) {
        const divider = document.createElement("div");
        divider.className = "thought-divider";
        body.append(divider);
      }
      const md = renderMarkdown(data.text);
      md.className = "markdown-body";
      body.append(md);
    }
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return;
  }

  const el = document.createElement("div");
  el.className = "thought-item";
  el.dataset.itemId = itemId;
  el.dataset.text = data.text || "";
  el.dataset.elapsed = String(typeof data.elapsed === "number" ? data.elapsed : 0);

  const header = document.createElement("div");
  header.className = "thought-header";

  const label = document.createElement("span");
  label.className = "thought-label";
  const formatted = formatThoughtMeta(data.meta, data.elapsed);
  label.innerHTML = `${iconSvg("brain", 14, 2)}${formatted}`;

  const chevron = document.createElement("span");
  chevron.className = "thought-chevron";
  chevron.innerHTML = iconSvg("chevron-right", 12, 2);

  header.addEventListener("click", () => {
    const body = el.querySelector<HTMLElement>(".thought-body");
    if (body) {
      body.hidden = !body.hidden;
      chevron.innerHTML = iconSvg(body.hidden ? "chevron-right" : "chevron-down", 12, 2);
    }
  });

  header.append(label, chevron);
  el.append(header);

  const body = document.createElement("div");
  body.className = "thought-body";
  body.hidden = true;
  if (data.text) {
    const md = renderMarkdown(data.text);
    md.className = "markdown-body";
    body.append(md);
  }
  el.append(body);

  if (insertBeforeEl) {
    insertBeforeEl.parentNode?.insertBefore(el, insertBeforeEl);
  } else {
    transcriptEl.append(el);
  }
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

