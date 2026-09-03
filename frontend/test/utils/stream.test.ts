// @ts-nocheck
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  setTranscriptElement,
  getOrCreateStream,
  appendStreamText,
  commitStream,
  discardStream,
  takeCommittedStreams,
  peekCommittedStreamsForSnapshot,
  reserveCommittedStream,
  validateCommittedStreamReservations,
  commitCommittedStreamReservationsNoFail,
  releaseCommittedStreamReservations,
  clearCommittedStreams,
  clearActiveStreams,
  _setCanonicalMarkdownCoordinatorForTest,
  _setTranscriptViewportControllerForTest,
  _resetForTest,
  flushTranscriptReconciliationNow,
  quiesceStreamsForBlockedInstallNoCallback,
  peekTranscriptLiveOwners,
  validateTranscriptLiveOwnerTokens,
} from "../../src/utils/stream";
import { renderMarkdown } from "../../src/utils/markdown";
import { createTranscriptViewportController } from "../../src/utils/transcript-viewport";
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

  it("attaches the stream element in the render transaction", async () => {
    appendStreamText("s1", "text", "text");
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).toBeNull();

    await new Promise((resolve) => setTimeout(resolve, 150));
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

  it("hides thinking when the answer transaction renders", async () => {
    appendStreamText("s1", "thinking line", "thinking");
    await new Promise((resolve) => setTimeout(resolve, 150));
    appendStreamText("s1", "final answer", "text");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.thinkingEl.hidden).toBe(false);

    await new Promise((resolve) => setTimeout(resolve, 150));
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

  it("removes an attached stream element from transcript", async () => {
    appendStreamText("s1", "text", "text");
    const transcript = document.querySelector("#transcript");
    await new Promise((resolve) => setTimeout(resolve, 150));
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

    const result = commitStream("async-1");
    const preview = stream.textEl.firstChild;

    expect(result?.text).toBe(source);
    expect(harness.worker.sent).toHaveLength(1);
    expect(preview).not.toBeNull();
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
    commitStream("reused");
    const oldPreview = oldStream.textEl.firstChild;

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


class ControlledViewportController {
  transcript;
  pending = new Map();
  flushCalls = [];
  flushOptions = [];
  disposed = false;
  resetCalls = 0;
  interactionGeneration = 0;
  following = true;

  constructor(transcript) {
    this.transcript = transcript;
  }

  enqueueMutation(key, mutate) {
    this.pending.set(key, mutate);
  }

  cancelMutation(key) {
    this.pending.delete(key);
  }

  flushMutationNow(key, mutate, options = {}) {
    this.pending.delete(key);
    this.flushCalls.push(key);
    this.flushOptions.push(options);
    mutate();
  }

  flushFrame() {
    const pending = [...this.pending.values()];
    this.pending.clear();
    for (const mutate of pending) mutate();
  }

  getInteractionGeneration() {
    return this.interactionGeneration;
  }

  prepareForSynchronousPrepend(expected) {
    if (expected !== this.interactionGeneration) return false;
    this.following = false;
    return true;
  }

  requestFollowAfterExternalMutation() {}

  forceScrollToBottom() {
    this.interactionGeneration += 1;
    this.following = true;
  }

  isFollowing() {
    return this.following;
  }

  reset() {
    this.pending.clear();
    this.resetCalls += 1;
    this.interactionGeneration += 1;
    this.following = true;
  }

  dispose() {
    this.disposed = true;
    this.reset();
  }
}

describe("frame-owned stream scheduling", () => {
  let controller;

  beforeEach(async () => {
    vi.useFakeTimers();
    const streamModule = await import("../../src/utils/stream");
    _resetForTest();
    _setCanonicalMarkdownCoordinatorForTest(immediateCanonicalCoordinator());
    const transcript = document.querySelector("#transcript");
    controller = new ControlledViewportController(transcript);
    streamModule._setTranscriptViewportControllerForTest(controller);
  });

  afterEach(() => {
    vi.useRealTimers();
  });
  it("flushes transcript reconciliation synchronously with follow intent", () => {
    const calls = [];

    flushTranscriptReconciliationNow(() => calls.push("mutate"));

    expect(calls).toEqual(["mutate"]);
    expect(controller.flushCalls).toHaveLength(1);
    expect(controller.flushOptions).toEqual([{ followAfterMutation: true }]);
  });


  it("coalesces 100 updates into one real viewport transaction without following an away user", async () => {
    const transcript = document.querySelector<HTMLElement>("#transcript")!;
    transcript.scrollTop = 100;
    const frames = new Map<number, FrameRequestCallback>();
    const writes: number[] = [];
    let nextFrame = 1;
    let geometryReads = 0;
    let renderTransactions = 0;
    const viewport = createTranscriptViewportController({
        transcript,
        scheduleFrame(callback) {
            const handle = nextFrame++;
            frames.set(handle, callback);
            return handle;
        },
        cancelFrame(handle) {
            frames.delete(Number(handle));
        },
        readGeometry() {
            geometryReads += 1;
            renderTransactions += 1;
            return { scrollTop: 100, clientHeight: 100, scrollHeight: 1000 };
        },
        writeScrollTop(value) {
            writes.push(value);
            transcript.scrollTop = value;
        },
    });
    _setTranscriptViewportControllerForTest(viewport);

    appendStreamText("burst", "0", "text", "append");
    const stream = getOrCreateStream("burst", "text");
    const projectionUpdate = vi.spyOn(stream.markdownProjection, "update");
    const transcriptAppend = vi.spyOn(transcript, "append");
    for (let update = 1; update < 100; update += 1) {
        appendStreamText("burst", String(update), "text", "append");
    }

    await vi.advanceTimersByTimeAsync(100);
    expect(frames.size).toBe(1);
    const frame = frames.entries().next().value as [number, FrameRequestCallback];
    frames.delete(frame[0]);
    frame[1](0);

    expect(projectionUpdate).toHaveBeenCalledTimes(1);
    expect(transcriptAppend).toHaveBeenCalledTimes(1);
    expect(renderTransactions).toBe(1);
    expect(geometryReads).toBeLessThanOrEqual(1);
    expect(writes).toHaveLength(0);
    expect(transcript.scrollTop).toBe(100);
    expect(frames.size).toBe(0);
  });

  it("uses a non-resetting 100 ms trailing throttle and one frame transaction", async () => {
    appendStreamText("throttle", "a", "text", "append");
    const stream = getOrCreateStream("throttle", "text");
    expect(stream.el.isConnected).toBe(false);
    expect(controller.pending.size).toBe(0);

    await vi.advanceTimersByTimeAsync(99);
    appendStreamText("throttle", "b", "text", "append");
    expect(controller.pending.size).toBe(0);

    await vi.advanceTimersByTimeAsync(1);
    expect(controller.pending.size).toBe(1);
    expect(stream.el.isConnected).toBe(false);

    controller.flushFrame();
    expect(stream.el.isConnected).toBe(true);
    expect(stream.textEl.textContent).toContain("ab");
    expect(stream.textEl.querySelectorAll(".stream-cursor")).toHaveLength(1);
  });

  it("folds append and replace updates into one latest projection operation", () => {
    appendStreamText("fold", "a", "text", "append");
    appendStreamText("fold", "b", "text", "append");
    let stream = getOrCreateStream("fold", "text");
    expect(stream.pendingProjectionUpdate).toEqual({ text: "ab", operation: "append" });

    appendStreamText("fold", "canonical", "text", "replace");
    appendStreamText("fold", " tail", "text", "append");
    stream = getOrCreateStream("fold", "text");
    expect(stream.pendingProjectionUpdate).toEqual({
      text: "canonical tail",
      operation: "replace",
    });
    expect(stream.pendingProjectionUpdates).toBeUndefined();
  });

  it("commit synchronously drains the latest state and cancels queued frame work", async () => {
    appendStreamText("commit-barrier", "first", "text", "append");
    await vi.advanceTimersByTimeAsync(100);
    appendStreamText("commit-barrier", " second", "text", "append");
    const stream = getOrCreateStream("commit-barrier", "text");
    expect(controller.pending.has(stream)).toBe(true);

    const result = commitStream("commit-barrier");

    expect(result.text).toBe("first second");
    expect(controller.flushCalls).toEqual([stream]);
    expect(controller.pending.has(stream)).toBe(false);
    expect(result.el.isConnected).toBe(true);
    expect(result.el.querySelector(".markdown-body").innerHTML)
      .toBe(renderMarkdown("first second").innerHTML);
    expect(result.el.querySelector(".stream-cursor")).toBeNull();
  });

  it("rejects controller replacement while active or retained work exists", async () => {
    const streamModule = await import("../../src/utils/stream");
    appendStreamText("active-owner", "text", "text", "append");
    const candidate = new ControlledViewportController(document.createElement("div"));

    expect(() => streamModule._setTranscriptViewportControllerForTest(candidate))
      .toThrowError("cannot replace transcript viewport while stream or canonical work is active");
    expect(candidate.disposed).toBe(false);

    discardStream("active-owner");
    streamModule._setTranscriptViewportControllerForTest(candidate);
    expect(controller.disposed).toBe(true);
    expect(streamModule.getTranscriptElement()).toBe(candidate.transcript);
  });

  it("exposes force, interaction generation, prepend prepare, and reset through stream ownership", async () => {
    const streamModule = await import("../../src/utils/stream");
    const token = streamModule.getTranscriptInteractionGeneration();
    streamModule.forceTranscriptScrollToBottom();
    expect(streamModule.getTranscriptInteractionGeneration()).toBe(token + 1);
    expect(streamModule.prepareTranscriptForSynchronousPrepend(token)).toBe(false);

    const current = streamModule.getTranscriptInteractionGeneration();
    expect(streamModule.prepareTranscriptForSynchronousPrepend(current)).toBe(true);
    streamModule.resetTranscriptViewport();
    expect(controller.resetCalls).toBe(1);
  });
});


describe("blocked stream quiesce", () => {
  it("invalidates stream production ownership without removing attached DOM", () => {
    appendStreamText("blocked-active", "active", "text");
    const active = getOrCreateStream("blocked-active", "text").el;
    document.querySelector("#transcript")?.append(active);
    appendStreamText("blocked-committed", "committed", "text");
    const committed = commitStream("blocked-committed")!.el;
    document.querySelector("#transcript")?.append(committed);

    quiesceStreamsForBlockedInstallNoCallback();

    expect(active.isConnected).toBe(true);
    expect(committed.isConnected).toBe(true);
    expect(peekCommittedStreamsForSnapshot()).toEqual([]);
    expect(getOrCreateStream("blocked-active", "text").el).not.toBe(active);
  });
});


describe("committed stream reservations", () => {
  it("peeks and reserves without consuming or invalidating committed ownership", () => {
    appendStreamText("reserved", "answer", "text");
    const committed = commitStream("reserved")!;

    const claims = peekCommittedStreamsForSnapshot();
    expect(claims).toHaveLength(1);
    expect(claims[0]).toMatchObject({
      element: committed.el,
      streamId: "reserved",
    });

    const reservation = reserveCommittedStream(claims[0]);
    expect(reservation).not.toBeNull();
    expect(reserveCommittedStream(claims[0])).toBeNull();
    expect(peekCommittedStreamsForSnapshot()).toEqual(claims);
    expect(validateCommittedStreamReservations([reservation])).toBe(true);

    releaseCommittedStreamReservations([reservation]);
    const again = reserveCommittedStream(claims[0]);
    expect(again).not.toBeNull();
    releaseCommittedStreamReservations([again]);
    expect(takeCommittedStreams()).toEqual([committed.el]);
  });

  it("commits a validated batch without coordinator invalidation and rejects stale settle", () => {
    const harness = controlledCanonicalCoordinator();
    const invalidate = vi.spyOn(harness.coordinator, "invalidate");
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("async-reserved", "safe **answer**", "text");
    const stream = getOrCreateStream("async-reserved", "text");
    const committed = commitStream("async-reserved")!;
    const preview = stream.textEl.firstChild;
    const claim = peekCommittedStreamsForSnapshot()[0];
    const reservation = reserveCommittedStream(claim)!;

    expect(validateCommittedStreamReservations([reservation])).toBe(true);
    commitCommittedStreamReservationsNoFail([reservation]);

    expect(invalidate).not.toHaveBeenCalled();
    expect(peekCommittedStreamsForSnapshot()).toEqual([]);
    expect(takeCommittedStreams()).toEqual([]);
    expect(committed.el.isConnected).toBe(true);

    harness.worker.respond();
    expect(harness.pendingFrames()).toBe(0);
    expect(stream.textEl.firstChild).toBe(preview);
  });

  it("fails validation after committed collection ownership changes", () => {
    appendStreamText("stale", "answer", "text");
    commitStream("stale");
    const claim = peekCommittedStreamsForSnapshot()[0];
    const reservation = reserveCommittedStream(claim)!;

    takeCommittedStreams();

    expect(validateCommittedStreamReservations([reservation])).toBe(false);
    releaseCommittedStreamReservations([reservation]);
  });
});


describe("transcript live-owner tokens", () => {
  it("peeks and validates active stream ownership without exposing private maps", () => {
    const stream = getOrCreateStream("active-owner", "text");

    const tokens = peekTranscriptLiveOwners();

    expect(tokens).toEqual([{
      kind: "active-stream",
      streamId: "active-owner",
      element: stream.el,
      streamGeneration: stream.streamGeneration,
      canonicalRevision: stream.canonicalRevision,
      viewportGeneration: expect.any(Number),
    }]);
    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(true);
  });

  it("prefers pending canonical ownership over the retained committed claim", () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("pending-owner", "answer", "text", "append");
    const committed = commitStream("pending-owner")!;

    const tokens = peekTranscriptLiveOwners();

    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toMatchObject({
      kind: "pending-canonical",
      streamId: "pending-owner",
      element: committed.el,
    });
    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(true);
  });

  it("prefers a new active stream over an older retained claim with the same stream id", () => {
    appendStreamText("reused-owner", "old", "text", "append");
    const retained = commitStream("reused-owner")!;
    const active = getOrCreateStream("reused-owner", "text");

    const tokens = peekTranscriptLiveOwners();

    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toMatchObject({
      kind: "active-stream",
      streamId: "reused-owner",
      element: active.el,
    });
    expect(tokens[0].element).not.toBe(retained.el);
    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(true);
  });

  it("invalidates an active token after canonical revision changes", () => {
    appendStreamText("revision-owner", "a", "text", "append");
    const tokens = peekTranscriptLiveOwners();

    appendStreamText("revision-owner", "b", "text", "append");

    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(false);
    expect(validateTranscriptLiveOwnerTokens(peekTranscriptLiveOwners())).toBe(true);
  });

  it("invalidates pending and retained tokens after settle or blocked quiesce", () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("settled-owner", "answer", "text", "append");
    commitStream("settled-owner");
    const pending = peekTranscriptLiveOwners();

    harness.worker.respond();
    harness.flushFrame();

    expect(validateTranscriptLiveOwnerTokens(pending)).toBe(false);
    const retained = peekTranscriptLiveOwners();
    expect(retained).toHaveLength(1);
    expect(retained[0].kind).toBe("retained-committed");

    quiesceStreamsForBlockedInstallNoCallback();

    expect(validateTranscriptLiveOwnerTokens(retained)).toBe(false);
    expect(peekTranscriptLiveOwners()).toEqual([]);
  });
});


describe("transcript live-owner token set validation", () => {
  it("accepts an empty proof only when there are no live owners", () => {
    expect(validateTranscriptLiveOwnerTokens([])).toBe(true);

    getOrCreateStream("present-owner", "text");

    expect(validateTranscriptLiveOwnerTokens([])).toBe(false);
  });

  it("rejects a proof that omits one current owner", () => {
    getOrCreateStream("owner-a", "text");
    getOrCreateStream("owner-b", "text");
    const tokens = peekTranscriptLiveOwners();

    expect(tokens).toHaveLength(2);
    expect(validateTranscriptLiveOwnerTokens(tokens.slice(0, 1))).toBe(false);
  });

  it("rejects a proof after a new owner appears", () => {
    getOrCreateStream("captured-owner", "text");
    const tokens = peekTranscriptLiveOwners();

    getOrCreateStream("new-owner", "text");

    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(false);
  });

  it("uses the nearest stream buffer identity for pending canonical ownership", () => {
    const harness = controlledCanonicalCoordinator();
    _setCanonicalMarkdownCoordinatorForTest(harness.coordinator);
    appendStreamText("metadata-owner", "answer", "text", "append");
    const committed = commitStream("metadata-owner")!;
    committed.el.dataset.streamId = "changed-metadata";

    const tokens = peekTranscriptLiveOwners();

    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toMatchObject({
      kind: "pending-canonical",
      streamId: "metadata-owner",
      element: committed.el,
    });
    expect(validateTranscriptLiveOwnerTokens(tokens)).toBe(true);
  });
});
