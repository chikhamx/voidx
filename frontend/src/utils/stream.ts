import {
  createStreamingMarkdownProjection,
  type StreamUpdateOperation,
  type StreamingMarkdownProjection,
} from "./markdown";
import {
  createCanonicalMarkdownCoordinator,
  type CanonicalMarkdownCoordinator,
} from "./markdown-worker-client";
import type { StreamState } from "./types";

const DEBOUNCE_MS = 100;
const THINKING_MAX_LINES = 5;

const streams = new Map<string, StreamState>();
const committedEls: HTMLElement[] = [];
const committedCanonicalText = new WeakMap<HTMLElement, string>();
const canonicalOwners = new Map<
  string,
  { revision: number; generation: number; target: HTMLElement }
>();
let canonicalMarkdownCoordinator = createCanonicalMarkdownCoordinator();
let nextStreamGeneration = 1;
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
  invalidateCanonicalOwner(streamId);
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
    pendingProjectionUpdates: [],
    canonicalRevision: 0,
    streamGeneration: nextStreamGeneration++,
  };
  streams.set(streamId, stream);
  return stream;
}

export function appendStreamText(
  streamId: string,
  text: string,
  phase: string,
  operation?: StreamUpdateOperation,
): void {
  const stream = getOrCreateStream(streamId, phase);
  const incoming = String(text ?? "");
  const previousPhase = stream.phase;
  if (previousPhase !== phase && operation !== undefined) {
    resetTextProjection(stream);
  }
  stream.phase = phase;

  if (phase === "thinking") {
    stream.thinking = operation === "append" && previousPhase === phase
      ? stream.thinking + incoming
      : incoming;
    scheduleRender(stream, "thinking");
  } else {
    hideThinking(stream);
    const previousText = stream.text;
    const nextText = stripAssistantPrefixBullet(
      operation === "append" ? previousText + incoming : incoming,
    );
    stream.text = nextText;
    stream.canonicalRevision += 1;
    if (operation === "append") {
      const delta = nextText.startsWith(previousText)
        ? nextText.slice(previousText.length)
        : nextText;
      queueProjectionUpdate(
        stream,
        delta,
        nextText.startsWith(previousText) ? "append" : "replace",
      );
    } else if (
      operation === undefined
      && previousPhase === phase
      && nextText.startsWith(previousText)
    ) {
      queueProjectionUpdate(
        stream,
        nextText.slice(previousText.length),
        "append",
      );
    } else {
      queueProjectionUpdate(stream, nextText, "replace");
    }
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
  pendingRenders.delete(stream);
  applyPendingTextProjection(stream);
  stream.committed = true;
  stream.canonicalRevision += 1;
  stream.textEl.querySelector(".stream-cursor")?.remove();
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
  committedCanonicalText.set(stream.el, stream.text);
  if (retain) {
    committedEls.push(stream.el);
  }

  const owner = {
    revision: stream.canonicalRevision,
    generation: stream.streamGeneration,
    target: stream.textEl,
  };
  canonicalOwners.set(streamId, owner);
  canonicalMarkdownCoordinator.start({
    itemId: streamId,
    revision: owner.revision,
    generation: owner.generation,
    canonicalText: stream.text,
    target: stream.textEl,
    isCurrent: () => canonicalOwners.get(streamId) === owner,
    onSettled: () => {
      if (canonicalOwners.get(streamId) === owner) {
        canonicalOwners.delete(streamId);
      }
    },
  });
  return result;
}

export function takeCommittedStreams(): HTMLElement[] {
  return committedEls.splice(0);
}

export function getCommittedStreamCanonicalText(
  element: HTMLElement,
): string | null {
  return committedCanonicalText.get(element) ?? null;
}

export function invalidateCommittedStreamElement(element: HTMLElement): void {
  const streamId = element.dataset.streamId;
  if (streamId) {
    const owner = canonicalOwners.get(streamId);
    if (owner?.target.closest(".stream-buffer") === element) {
      invalidateCanonicalOwner(streamId);
    }
  }
  committedCanonicalText.delete(element);
}

export function clearCommittedStreams(): void {
  for (const el of committedEls) {
    invalidateCommittedStreamElement(el);
    el.remove();
  }
  committedEls.length = 0;
}

export function clearActiveStreams(
  options: { preserveCanonicalCommits?: boolean } = {},
): void {
  for (const [streamId, stream] of streams) {
    if (stream.debounceTimer) {
      clearTimeout(stream.debounceTimer);
    }
    pendingRenders.delete(stream);
    stream.el.remove();
    canonicalMarkdownCoordinator.invalidate(streamId);
  }
  streams.clear();
  if (!options.preserveCanonicalCommits) {
    invalidateAllCanonicalOwners();
  }
}

export function discardStream(streamId: string): void {
  const stream = streams.get(streamId);
  if (!stream) {
    invalidateCanonicalOwner(streamId);
    return;
  }
  if (stream.debounceTimer) {
    clearTimeout(stream.debounceTimer);
  }
  pendingRenders.delete(stream);
  stream.el.remove();
  streams.delete(streamId);
  invalidateCanonicalOwner(streamId);
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

function queueProjectionUpdate(
  stream: StreamState,
  text: string,
  operation: StreamUpdateOperation,
): void {
  if (operation === "replace") {
    stream.pendingProjectionUpdates = [{ text, operation }];
    return;
  }
  const last = stream.pendingProjectionUpdates[
    stream.pendingProjectionUpdates.length - 1
  ];
  if (last?.operation === "append") {
    last.text += text;
  } else if (text) {
    stream.pendingProjectionUpdates.push({ text, operation });
  }
}

function resetTextProjection(stream: StreamState): void {
  stream.text = "";
  stream.canonicalRevision += 1;
  stream.pendingProjectionUpdates = [];
  stream.markdownProjection?.reset();
  stream.textEl.querySelector(".stream-cursor")?.remove();
}

function applyPendingTextProjection(stream: StreamState): void {
  stream.textEl.querySelector(".stream-cursor")?.remove();
  if (stream.markdownProjection) {
    const updates = stream.pendingProjectionUpdates.splice(0);
    for (const update of updates) {
      stream.markdownProjection.update(update.text, update.operation);
    }
  } else {
    stream.pendingProjectionUpdates = [];
    stream.textEl.replaceChildren(document.createTextNode(stream.text));
  }
}

function renderStreamText(stream: StreamState): void {
  applyPendingTextProjection(stream);
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

function invalidateCanonicalOwner(streamId: string): void {
  if (!canonicalOwners.has(streamId)) return;
  canonicalOwners.delete(streamId);
  canonicalMarkdownCoordinator.invalidate(streamId);
}

function invalidateAllCanonicalOwners(): void {
  canonicalOwners.clear();
  canonicalMarkdownCoordinator.invalidateAll();
}

export function _setCanonicalMarkdownCoordinatorForTest(
  coordinator: CanonicalMarkdownCoordinator,
): void {
  invalidateAllCanonicalOwners();
  canonicalMarkdownCoordinator = coordinator;
}

export function _resetForTest(): void {
  for (const stream of streams.values()) {
    if (stream.debounceTimer) clearTimeout(stream.debounceTimer);
  }
  streams.clear();
  committedEls.length = 0;
  pendingRenders.clear();
  invalidateAllCanonicalOwners();
  canonicalMarkdownCoordinator = createCanonicalMarkdownCoordinator();
  nextStreamGeneration += 1;
  transcriptEl = null;
}
