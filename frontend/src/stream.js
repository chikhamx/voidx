import { renderMarkdown } from "./markdown.js";

const DEBOUNCE_MS = 100;

const streams = new Map();
const committedEls = [];
let transcriptEl = null;

export function setTranscriptElement(el) {
  transcriptEl = el;
}

export function getOrCreateStream(streamId, phase) {
  let stream = streams.get(streamId);
  if (stream) {
    return stream;
  }
  const el = document.createElement("div");
  el.className = "stream-buffer";
  el.dataset.streamId = streamId;
  const thinkingEl = document.createElement("div");
  thinkingEl.className = "stream-thinking";
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
    textEl,
    debounceTimer: null,
  };
  streams.set(streamId, stream);
  return stream;
}

export function appendStreamText(streamId, text, phase) {
  const stream = getOrCreateStream(streamId, phase);
  if (phase === "thinking") {
    stream.thinking += text;
    scheduleRender(stream, "thinking");
  } else {
    stream.text = text;
    scheduleRender(stream);
  }
  if (transcriptEl) {
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

export function commitStream(streamId) {
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
  renderStreamThinking(stream);
  const result = {
    text: stream.text,
    thinking: stream.thinking,
    el: stream.el,
  };
  streams.delete(streamId);
  committedEls.push(stream.el);
  return result;
}

export function takeCommittedStreams() {
  const els = committedEls.splice(0);
  return els;
}

export function discardStream(streamId) {
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

function scheduleRender(stream, target) {
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

function renderStreamText(stream) {
  stream.textEl.replaceChildren(renderMarkdown(stream.text));
  if (!stream.committed) {
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    stream.textEl.append(cursor);
  }
}

function renderStreamThinking(stream) {
  stream.thinkingEl.textContent = stream.thinking;
}

export function _resetForTest() {
  streams.clear();
  committedEls.length = 0;
  transcriptEl = null;
}
