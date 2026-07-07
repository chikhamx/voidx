import { renderMarkdown } from "./markdown";
import type { StreamState } from "./types";

const DEBOUNCE_MS = 100;
const THINKING_MAX_LINES = 5;

const streams = new Map<string, StreamState>();
const committedEls: HTMLElement[] = [];
let transcriptEl: HTMLElement | null = null;

export function setTranscriptElement(el: HTMLElement): void {
  transcriptEl = el;
}

export function getTranscriptElement(): HTMLElement | null {
  return transcriptEl;
}

export function getOrCreateStream(streamId: string, phase: string): StreamState {
  let stream = streams.get(streamId);
  if (stream) {
    return stream;
  }
  const el = document.createElement("div");
  el.className = "stream-buffer";
  el.dataset.streamId = streamId;
  const thinkingEl = document.createElement("div");
  thinkingEl.className = "stream-thinking";
  thinkingEl.hidden = true;
  thinkingEl.setAttribute("role", "status");
  thinkingEl.setAttribute("aria-live", "polite");
  const thinkingLabel = document.createElement("div");
  thinkingLabel.className = "stream-thinking-label";
  thinkingLabel.textContent = "Thinking";
  const thinkingBody = document.createElement("div");
  thinkingBody.className = "stream-thinking-body";
  thinkingEl.append(thinkingLabel, thinkingBody);
  const textEl = document.createElement("div");
  textEl.className = "markdown-body";
  el.append(thinkingEl, textEl);
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  stream = {
    text: "",
    thinking: "",
    phase,
    el,
    thinkingEl,
    thinkingLabel,
    thinkingBody,
    textEl,
    debounceTimer: null,
  };
  streams.set(streamId, stream);
  return stream;
}

export function appendStreamText(
  streamId: string,
  text: string,
  phase: string,
): void {
  const stream = getOrCreateStream(streamId, phase);
  if (phase === "thinking") {
    stream.thinking = text;
    scheduleRender(stream, "thinking");
  } else {
    hideThinking(stream);
    stream.text = stripAssistantPrefixBullet(text);
    scheduleRender(stream);
  }
  if (transcriptEl) {
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

export function commitStream(streamId: string): {
  text: string;
  thinking: string;
  el: HTMLElement;
} | null {
  const stream = streams.get(streamId);
  if (!stream) {
    return null;
  }
  if (stream.debounceTimer) {
    clearTimeout(stream.debounceTimer);
    stream.debounceTimer = null;
  }
  stream.committed = true;
  renderStreamText(stream);
  hideThinking(stream);
  const result = {
    text: stream.text,
    thinking: stream.thinking,
    el: stream.el,
  };
  streams.delete(streamId);
  committedEls.push(stream.el);
  return result;
}

export function takeCommittedStreams(): HTMLElement[] {
  const els = committedEls.splice(0);
  return els;
}

export function clearActiveStreams(): void {
  for (const [, stream] of streams) {
    if (stream.debounceTimer) {
      clearTimeout(stream.debounceTimer);
    }
    stream.el.remove();
  }
  streams.clear();
}

export function discardStream(streamId: string): void {
  const stream = streams.get(streamId);
  if (!stream) {
    return;
  }
  if (stream.debounceTimer) {
    clearTimeout(stream.debounceTimer);
  }
  stream.el.remove();
  streams.delete(streamId);
}

function scheduleRender(stream: StreamState, target?: string): void {
  if (stream.debounceTimer) {
    clearTimeout(stream.debounceTimer);
  }
  stream.debounceTimer = setTimeout(() => {
    stream.debounceTimer = null;
    if (target === "thinking") {
      renderStreamThinking(stream);
    } else {
      renderStreamText(stream);
    }
  }, DEBOUNCE_MS);
}

function renderStreamText(stream: StreamState): void {
  stream.textEl.replaceChildren(renderMarkdown(stream.text));
  if (!stream.committed) {
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    stream.textEl.append(cursor);
  }
}

function renderStreamThinking(stream: StreamState): void {
  const hasThinking = Boolean(stream.thinking);
  stream.thinkingEl.hidden = !hasThinking;
  stream.thinkingBody.textContent = visibleThinkingLines(stream.thinking);
}

function hideThinking(stream: StreamState): void {
  stream.thinkingEl.hidden = true;
  stream.thinkingBody.textContent = "";
}

function visibleThinkingLines(text: string): string {
  const lines = String(text || "")
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0);
  return lines.slice(-THINKING_MAX_LINES).join("\n");
}

function stripAssistantPrefixBullet(text: string): string {
  return String(text || "").replace(/^\s*●\s+/, "");
}

export function _resetForTest(): void {
  streams.clear();
  committedEls.length = 0;
  transcriptEl = null;
}
