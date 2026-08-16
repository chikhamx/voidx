import { iconSvg } from './icons';
import { getTranscriptElement } from './stream';
import type { NoticeItemData, DiffItemData, StatusItemData } from './render-types';
import { stripRichMarkup, renderDiffBlock } from './render';

export function appendNoticeItem(itemId: string, data: NoticeItemData): void {
  const el = document.createElement("div");
  el.className = `notice-item notice-${data.style || "error"}`;
  el.dataset.itemId = itemId;

  const icon = document.createElement("span");
  icon.className = "notice-icon";
  icon.textContent = data.style === "warning" ? "!" : data.style === "info" ? "i" : "\u2717";

  const text = document.createElement("span");
  text.className = "notice-text";
  text.textContent = stripRichMarkup(data.text || "");

  el.append(icon, text);

  const region = getOrCreateNoticeToastRegion();
  region.append(el);
  setTimeout(() => {
    el.classList.add("notice-toast-exiting");
    setTimeout(() => {
      el.remove();
      if (!region.childElementCount) {
        region.remove();
      }
    }, 250);
  }, 4000);
}

function getOrCreateNoticeToastRegion(): Element {
  let region: Element | null = document.querySelector(".notice-toast-region");
  if (region) {
    return region;
  }
  region = document.createElement("div");
  region.className = "notice-toast-region";
  region.setAttribute("role", "status");
  region.setAttribute("aria-live", "polite");
  document.body.append(region);
  return region;
}

export function appendDiffItem(itemId: string, data: DiffItemData): void {
  const el = document.createElement("div");
  el.className = "diff-item";
  el.dataset.itemId = itemId;

  const header = document.createElement("div");
  header.className = "diff-header";

  const chevron = document.createElement("span");
  chevron.className = "diff-chevron";
  chevron.innerHTML = iconSvg("chevron-right", 12, 2);

  const title = document.createElement("span");
  title.className = "diff-title";
  title.textContent = data.title || "diff";

  header.addEventListener("click", () => {
    const body = el.querySelector<HTMLElement>(".diff-body");
    if (body) {
      body.hidden = !body.hidden;
      chevron.classList.toggle("open", !body.hidden);
    }
  });

  header.append(chevron, title);
  el.append(header);

  const body = document.createElement("div");
  body.className = "diff-body";
  body.hidden = true;
  if (data.text) {
    body.append(renderDiffBlock(data.text));
  }
  el.append(body);

  const transcriptEl = getTranscriptElement();
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

const statusElapsedTimers = new Map<string, ReturnType<typeof setInterval>>();

function formatElapsedSeconds(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

function updateStatusElapsed(el: HTMLElement): void {
  const startTs = Number(el.dataset.startTs || Date.now());
  const seconds = Math.max(0, Math.floor((Date.now() - startTs) / 1000));
  const elapsedEl = el.querySelector<HTMLElement>(".status-elapsed");
  if (elapsedEl) elapsedEl.textContent = formatElapsedSeconds(seconds);
}

export function appendCompactionDivider(itemId: string, data: StatusItemData): void {
  const transcriptEl = getTranscriptElement();
  if (!transcriptEl) return;
  const existing = transcriptEl.querySelector<HTMLElement>(`[data-compaction-item-id="${itemId}"]`);
  if (existing) return;

  const divider = document.createElement("div");
  divider.className = "compaction-divider";
  divider.dataset.compactionItemId = itemId;
  divider.setAttribute("role", "note");

  const label = document.createElement("span");
  label.className = "compaction-divider-label";
  label.textContent = "上下文已压缩";
  divider.append(label);

  if (data.detail) {
    const detail = document.createElement("span");
    detail.className = "compaction-divider-detail";
    detail.textContent = data.detail;
    divider.append(detail);
  }

  transcriptEl.append(divider);
}

export function handleStatusItem(method: string, itemId: string, data: StatusItemData): void {
  if (itemId === "turn:analyzing" || data.status_id === "turn:analyzing") {
    return;
  }
  const transcriptEl = getTranscriptElement();
  if (method === "item.completed" && data.outcome === "compacted") {
    const timer = statusElapsedTimers.get(itemId);
    if (timer) {
      clearInterval(timer);
      statusElapsedTimers.delete(itemId);
    }
    document.querySelector<HTMLElement>(`[data-status-item-id="${itemId}"]`)?.remove();
    appendCompactionDivider(itemId, data);
    return;
  }
  let el = document.querySelector<HTMLElement>(`[data-status-item-id="${itemId}"]`);
  if (method === "item.started") {
    if (!el) {
      el = document.createElement("div");
      el.className = "status-item running";
      el.dataset.statusItemId = itemId;
      el.dataset.statusId = data.status_id || itemId;
      el.dataset.startTs = String(Date.now());
      const label = document.createElement("span");
      label.className = "status-label";
      el.append(label);
      const elapsed = document.createElement("span");
      elapsed.className = "status-elapsed";
      elapsed.textContent = "0s";
      el.append(elapsed);
      const detail = document.createElement("div");
      detail.className = "status-detail";
      el.append(detail);
      transcriptEl?.append(el);
      const target = el;
      statusElapsedTimers.set(
        itemId,
        setInterval(() => updateStatusElapsed(target), 1000),
      );
    }
    const label = el.querySelector<HTMLElement>(".status-label");
    const detail = el.querySelector<HTMLElement>(".status-detail");
    if (label) label.textContent = data.label || "Working";
    if (detail) {
      detail.textContent = data.detail || "";
      detail.hidden = !data.detail;
    }
    return;
  }
  if (method === "item.completed") {
    if (!el) return;
    const timer = statusElapsedTimers.get(itemId);
    if (timer) {
      clearInterval(timer);
      statusElapsedTimers.delete(itemId);
    }
    updateStatusElapsed(el);
    el.classList.remove("running");
    el.classList.add(data.ok === false ? "failed" : "completed");
    const label = el.querySelector<HTMLElement>(".status-label");
    const detail = el.querySelector<HTMLElement>(".status-detail");
    if (label && data.label) label.textContent = data.label;
    if (detail && data.detail) {
      detail.textContent = data.detail;
      detail.hidden = false;
    }
    return;
  }
}

