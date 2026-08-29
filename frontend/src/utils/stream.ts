import { createStreamingMarkdownProjection, type StreamingMarkdownProjection } from "./markdown";
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
  const brainSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="vx-icon"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"></path><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"></path><path d="M12 5v14"></path><path d="M12 9h4"></path><path d="M12 14h-4"></path><path d="M12 14h4"></path><path d="M12 9h-4"></path></svg>`;
  thinkingLabel.innerHTML = `${brainSvg}<span>thought</span><span style="display: none;">Thinking</span>`;
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
    markdownProjection: createStreamingMarkdownProjection(textEl),
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
  stream.phase = phase;
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

export function commitStream(streamId: string, retain = true): {
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
  if (!stream.text) {
    stream.el.style.display = "none";
  }
  const result = {
    text: stream.text,
    thinking: stream.thinking,
    el: stream.el,
  };
  streams.delete(streamId);
  if (retain) {
    committedEls.push(stream.el);
  }
  return result;
}

export function takeCommittedStreams(): HTMLElement[] {
  const els = committedEls.splice(0);
  return els;
}

export function clearCommittedStreams(): void {
  for (const el of committedEls) {
    el.remove();
  }
  committedEls.length = 0;
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

const pendingRenders = new Map<StreamState, string | undefined>();
let renderFrame: number | ReturnType<typeof setTimeout> | null = null;

function scheduleRender(stream: StreamState, target?: string): void {
  pendingRenders.set(stream, target);
  if (renderFrame !== null) return;
  const flush = () => {
    renderFrame = null;
    const pending = [...pendingRenders.entries()];
    pendingRenders.clear();
    for (const [queued, queuedTarget] of pending) {
      if (!queued.committed) {
        if (queuedTarget === "thinking") renderStreamThinking(queued);
        else renderStreamText(queued);
      }
    }
  };
  if (typeof requestAnimationFrame === "function") {
    renderFrame = requestAnimationFrame(flush);
  } else {
    renderFrame = setTimeout(flush, 16);
  }
}

function renderStreamText(stream: StreamState): void {
  stream.textEl.querySelector(".stream-cursor")?.remove();
  if (stream.markdownProjection) {
    if (stream.committed) {
      stream.markdownProjection.update(stream.text);
      stream.markdownProjection.commit();
    } else {
      stream.markdownProjection.update(stream.text);
    }
  } else {
    stream.textEl.replaceChildren(renderMarkdown(stream.text));
  }
  if (!stream.committed && stream.phase === "text") {
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    stream.textEl.append(cursor);
  }
}

function renderStreamThinking(stream: StreamState): void {
  const hasThinking = Boolean(stream.thinking) || stream.phase === "thinking";
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
