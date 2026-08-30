// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import {
  setTranscriptElement,
  getOrCreateStream,
  appendStreamText,
  commitStream,
  discardStream,
  takeCommittedStreams,
  clearCommittedStreams,
  clearActiveStreams,
  _setCanonicalMarkdownCoordinatorForTest,
  _resetForTest,
} from "../../src/utils/stream";
import { renderMarkdown } from "../../src/utils/markdown";
import {
  createCanonicalMarkdownCoordinator,
} from "../../src/utils/markdown-worker-client";
import { handleCanonicalRenderRequest } from "../../src/utils/markdown.worker";


type WorkerMessageListener = (event: { data: unknown }) => void;

class ControlledCanonicalWorker {
  sent = [];
  messageListeners = [];
  errorListeners = [];

  postMessage(message) {
    this.sent.push(message);
  }

  addEventListener(type, listener) {
    if (type === "message") this.messageListeners.push(listener);
    else this.errorListeners.push(listener);
  }

  respond(index = 0) {
    const response = handleCanonicalRenderRequest(this.sent[index]);
    for (const listener of this.messageListeners) {
      listener({ data: response });
    }
  }

  fail() {
    for (const listener of this.errorListeners) listener(new Event("error"));
  }
}

function immediateCanonicalCoordinator() {
  const worker = new ControlledCanonicalWorker();
  const coordinator = createCanonicalMarkdownCoordinator({
    workerFactory: () => ({
      postMessage(message) {
        worker.postMessage(message);
        worker.respond(worker.sent.length - 1);
      },
      addEventListener(type, listener) {
        worker.addEventListener(type, listener);
      },
    }),
    scheduleFrame(callback) {
      callback(0);
      return 1;
    },
    now: () => 0,
  });
  return coordinator;
}

function controlledCanonicalCoordinator() {
  const worker = new ControlledCanonicalWorker();
  const frames = [];
  const coordinator = createCanonicalMarkdownCoordinator({
    workerFactory: () => worker,
    scheduleFrame(callback) {
      frames.push(callback);
      return frames.length;
    },
    now: () => 0,
  });
  return {
    worker,
    coordinator,
    flushFrame() {
      const callback = frames.shift();
      if (!callback) throw new Error("no canonical frame pending");
      callback(0);
    },
    pendingFrames: () => frames.length,
  };
}

beforeEach(() => {
  _resetForTest();
  _setCanonicalMarkdownCoordinatorForTest(immediateCanonicalCoordinator());
  const transcript = document.querySelector("#transcript");
  setTranscriptElement(transcript);
});

describe("getOrCreateStream", () => {
  it("creates a new stream with DOM elements", () => {
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("");
    expect(stream.thinking).toBe("");
    expect(stream.phase).toBe("text");
    expect(stream.el.className).toBe("stream-buffer");
    expect(stream.el.dataset.streamId).toBe("s1");
    expect(stream.thinkingEl.className).toBe("stream-thinking");
    expect(stream.textEl.className).toBe("markdown-body");
  });

  it("returns existing stream for same id", () => {
    const s1 = getOrCreateStream("s1", "text");
    const s2 = getOrCreateStream("s1", "text");
    expect(s1).toBe(s2);
  });

  it("appends stream element to transcript", () => {
    getOrCreateStream("s1", "text");
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).not.toBeNull();
  });
});

describe("appendStreamText", () => {
  it("appends text to stream.text", () => {
    appendStreamText("s1", "hello", "text");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("hello");
  });

  it("appends thinking to stream.thinking", () => {
    appendStreamText("s1", "analyzing", "thinking");
    const stream = getOrCreateStream("s1", "thinking");
    expect(stream.thinking).toBe("analyzing");
  });

  it("replaces thinking phase content from full stream snapshots", () => {
    appendStreamText("s1", "part1", "thinking");
    appendStreamText("s1", "part1 part2", "thinking");
    const stream = getOrCreateStream("s1", "thinking");
    expect(stream.thinking).toBe("part1 part2");
  });

  it("shows thinking as a transient widget while streaming", async () => {
    appendStreamText("s1", "long internal thought", "thinking");
    const stream = getOrCreateStream("s1", "thinking");

    await new Promise((r) => setTimeout(r, 150));
    expect(stream.thinkingEl.tagName).toBe("DIV");
    expect(stream.thinkingEl.hidden).toBe(false);
    expect(stream.thinkingEl.textContent).toContain("Thinking");
    expect(stream.thinkingEl.textContent).toContain("long internal thought");
  });

  it("shows only the last five thinking lines", async () => {
    appendStreamText("s1", "one\ntwo\nthree\nfour\nfive\nsix", "thinking");
    const stream = getOrCreateStream("s1", "thinking");

    await new Promise((r) => setTimeout(r, 150));
    expect(stream.thinkingBody.textContent).toBe("two\nthree\nfour\nfive\nsix");
  });

  it("hides thinking when answer text starts", async () => {
    appendStreamText("s1", "thinking line", "thinking");
    await new Promise((r) => setTimeout(r, 150));
    appendStreamText("s1", "final answer", "text");
    const stream = getOrCreateStream("s1", "text");

    expect(stream.thinkingEl.hidden).toBe(true);
    expect(stream.thinkingBody.textContent).toBe("");
  });

  it("strips terminal assistant prefix bullet from text stream", () => {
    appendStreamText("s1", "● final answer", "text");
    const stream = getOrCreateStream("s1", "text");

    expect(stream.text).toBe("final answer");
  });

  it("replaces text phase content (not accumulate)", () => {
    appendStreamText("s1", "v1", "text");
    appendStreamText("s1", "v2", "text");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("v2");
  });
});

describe("commitStream", () => {
  it("returns committed stream data", () => {
    appendStreamText("s1", "final text", "text");
    appendStreamText("s1", "thoughts", "thinking");
    const result = commitStream("s1");
    expect(result).not.toBeNull();
    expect(result.text).toBe("final text");
    expect(result.thinking).toBe("thoughts");
    expect(result.el).toBeDefined();
    expect(result.el.querySelector(".stream-thinking").hidden).toBe(true);
  });

  it("removes stream from active streams", () => {
    appendStreamText("s1", "text", "text");
    commitStream("s1");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("");
  });

  it("returns null for non-existent stream", () => {
    expect(commitStream("nonexistent")).toBeNull();
  });

  it("adds element to committed list", () => {
    appendStreamText("s1", "text", "text");
    commitStream("s1");
    const committed = takeCommittedStreams();
    expect(committed).toHaveLength(1);
  });
});

describe("takeCommittedStreams", () => {
  it("returns empty array when nothing committed", () => {
    expect(takeCommittedStreams()).toHaveLength(0);
  });

  it("returns committed elements and clears list", () => {
    appendStreamText("s1", "a", "text");
    commitStream("s1");
    appendStreamText("s2", "b", "text");
    commitStream("s2");
    const first = takeCommittedStreams();
    expect(first).toHaveLength(2);
    const second = takeCommittedStreams();
    expect(second).toHaveLength(0);
  });
});

describe("discardStream", () => {
  it("removes stream without committing", () => {
    appendStreamText("s1", "text", "text");
    discardStream("s1");
    const committed = takeCommittedStreams();
    expect(committed).toHaveLength(0);
  });

  it("does nothing for non-existent stream", () => {
    expect(() => discardStream("nonexistent")).not.toThrow();
  });

  it("removes stream element from transcript", () => {
    appendStreamText("s1", "text", "text");
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).not.toBeNull();
    discardStream("s1");
    expect(transcript.querySelector(".stream-buffer")).toBeNull();
  });
});

describe("stream cursor", () => {
  it("shows cursor while streaming (not committed)", async () => {
    appendStreamText("s1", "hello", "text");
    await new Promise((r) => setTimeout(r, 150));
    const cursor = document.querySelector(".stream-cursor");
    expect(cursor).not.toBeNull();
  });

  it("removes cursor after commit", async () => {
    appendStreamText("s1", "hello", "text");
    await new Promise((r) => setTimeout(r, 150));
    commitStream("s1");
    const cursor = document.querySelector(".stream-cursor");
    expect(cursor).toBeNull();
  });
});


describe("explicit stream update operations", () => {
  it("accumulates append deltas without requiring cumulative snapshots", () => {
    appendStreamText("s1", "hello", "text", "append");
    appendStreamText("s1", " world", "text", "append");

    expect(getOrCreateStream("s1", "text").text).toBe("hello world");
  });

  it("replaces canonical text and clears the old projection", async () => {
    appendStreamText("s1", "# old\n\n", "text", "append");
    await new Promise((r) => setTimeout(r, 150));
    const stream = getOrCreateStream("s1", "text");
    const oldHeading = stream.textEl.querySelector("h1");

    appendStreamText("s1", "new answer", "text", "replace");
    await new Promise((r) => setTimeout(r, 150));

    expect(stream.text).toBe("new answer");
    expect(stream.textEl.textContent).toContain("new answer");
    expect(stream.textEl.textContent).not.toContain("old");
    expect(oldHeading?.isConnected).toBe(false);
  });

  it("resets the Markdown projection when the stream phase changes", async () => {
    appendStreamText("s1", "old answer", "text", "append");
    await new Promise((r) => setTimeout(r, 150));
    const stream = getOrCreateStream("s1", "text");
    expect(stream.textEl.textContent).toContain("old answer");

    appendStreamText("s1", "internal thought", "thinking", "replace");
    await new Promise((r) => setTimeout(r, 150));

    expect(stream.phase).toBe("thinking");
    expect(stream.text).toBe("");
    expect(stream.textEl.textContent).not.toContain("old answer");
  });


  it("resets prior thinking when an explicit append switches back to thinking", () => {
    appendStreamText("s1", "old thought", "thinking", "append");
    appendStreamText("s1", "answer", "text", "append");
    appendStreamText("s1", "new thought", "thinking", "append");

    const stream = getOrCreateStream("s1", "thinking");
    expect(stream.text).toBe("");
    expect(stream.thinking).toBe("new thought");
  });


  it("uses the canonical renderer when the streaming projection is unavailable", () => {
    const stream = getOrCreateStream("s1", "text");
    stream.markdownProjection = null;
    appendStreamText("s1", "safe **bold**", "text", "replace");

    const result = commitStream("s1");

    expect(result.el.querySelector("strong")?.textContent).toBe("bold");
  });

  it("commits appended deltas as the canonical full Markdown render", () => {
    const first = "safe **bold** ";
    const second = "<script>window.pwned = true</script>";
    appendStreamText("s1", first, "text", "append");
    appendStreamText("s1", second, "text", "append");

    const result = commitStream("s1");

    expect(result.text).toBe(first + second);
    expect(result.el.querySelector(".markdown-body").innerHTML)
      .toBe(renderMarkdown(first + second).innerHTML);
  });
});


describe("async canonical stream commit", () => {
  it("returns immediately and keeps the live preview until canonical install", async () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    const source = "safe **bold** answer";
    appendStreamText("async-1", source, "text", "append");
    await new Promise((resolve) => setTimeout(resolve, 30));
    const stream = getOrCreateStream("async-1", "text");
    const preview = stream.textEl.firstChild;

    const result = commitStream("async-1");

    expect(result?.text).toBe(source);
    expect(harness.worker.sent).toHaveLength(1);
    expect(stream.textEl.firstChild).toBe(preview);
    expect(stream.textEl.dataset.renderPending).toBe("true");

    harness.worker.respond();
    expect(harness.pendingFrames()).toBe(1);
    expect(stream.textEl.firstChild).toBe(preview);

    harness.flushFrame();
    expect(stream.textEl.firstChild).not.toBe(preview);
    expect(stream.textEl.innerHTML).toBe(renderMarkdown(source).innerHTML);
    expect(stream.textEl.dataset.renderPending).toBeUndefined();
  });

  it("drops an old result when the same stream id is reused and discarded", async () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("reused", "old **answer**", "text", "append");
    await new Promise((resolve) => setTimeout(resolve, 30));
    const oldStream = getOrCreateStream("reused", "text");
    const oldPreview = oldStream.textEl.firstChild;
    commitStream("reused");

    appendStreamText("reused", "new answer", "text", "replace");
    discardStream("reused");
    harness.worker.respond(0);

    expect(harness.pendingFrames()).toBe(0);
    expect(oldStream.textEl.firstChild).toBe(oldPreview);
    expect(oldStream.textEl.textContent).toContain("old answer");
  });

  it("does not install a pending result after committed streams are cleared", async () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("cleared", "old **answer**", "text", "append");
    await new Promise((resolve) => setTimeout(resolve, 30));
    const stream = getOrCreateStream("cleared", "text");
    commitStream("cleared");

    clearCommittedStreams();
    harness.worker.respond();

    expect(stream.el.isConnected).toBe(false);
    expect(harness.pendingFrames()).toBe(0);
  });

  it("uses the complete escaped canonical source when the Worker fails", () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    const source = "safe **bold** <script>window.pwned=true</script>";
    appendStreamText("failed", source, "text", "append");

    const result = commitStream("failed");
    harness.worker.fail();

    const target = result?.el.querySelector<HTMLElement>(".markdown-body");
    expect(target?.textContent).toBe(source);
    expect(target?.querySelector("script")).toBeNull();
    expect(target?.dataset.canonicalFallback).toBe("worker_error");
    expect(target?.dataset.renderPending).toBeUndefined();
  });

  it("invalidates pending commits when active stream state is cleared", async () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("generation", "canonical", "text", "append");
    await new Promise((resolve) => setTimeout(resolve, 30));
    commitStream("generation");

    clearActiveStreams();
    harness.worker.respond();

    expect(harness.pendingFrames()).toBe(0);
  });
});
