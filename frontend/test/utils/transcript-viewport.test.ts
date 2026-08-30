import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  TRANSCRIPT_NEAR_BOTTOM_PX,
  createTranscriptViewportController,
  type TranscriptFrameHandle,
  type TranscriptViewportGeometry,
} from "../../src/utils/transcript-viewport";

function createHarness(initial: TranscriptViewportGeometry = {
  scrollTop: 852,
  clientHeight: 100,
  scrollHeight: 1000,
}) {
  const transcript = document.createElement("div");
  const button = document.createElement("button");
  const frames = new Map<number, FrameRequestCallback>();
  const canceled: TranscriptFrameHandle[] = [];
  const writes: number[] = [];
  const order: string[] = [];
  let nextHandle = 1;
  let geometry = { ...initial };
  let readCount = 0;

  const controller = createTranscriptViewportController({
    transcript,
    returnToBottomButton: button,
    scheduleFrame(callback) {
      const handle = nextHandle++;
      frames.set(handle, callback);
      return handle;
    },
    cancelFrame(handle) {
      canceled.push(handle);
      frames.delete(Number(handle));
    },
    readGeometry() {
      readCount += 1;
      order.push("read");
      return { ...geometry };
    },
    writeScrollTop(value) {
      order.push(`write:${value}`);
      writes.push(value);
    },
  });

  return {
    transcript,
    button,
    controller,
    writes,
    order,
    canceled,
    pendingFrames: () => frames.size,
    readCount: () => readCount,
    setGeometry(next: TranscriptViewportGeometry) {
      geometry = { ...next };
    },
    flushFrame() {
      const entry = frames.entries().next().value as
        | [number, FrameRequestCallback]
        | undefined;
      if (!entry) throw new Error("no frame pending");
      frames.delete(entry[0]);
      entry[1](0);
    },
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("createTranscriptViewportController", () => {
  it("requires custom scheduler and canceler as a pair before adding listeners", () => {
    const transcript = document.createElement("div");
    const listener = vi.spyOn(transcript, "addEventListener");

    expect(() => createTranscriptViewportController({
      transcript,
      scheduleFrame: () => 1,
    })).toThrowError("scheduleFrame and cancelFrame must be provided together");
    expect(() => createTranscriptViewportController({
      transcript,
      cancelFrame: () => undefined,
    })).toThrowError("scheduleFrame and cancelFrame must be provided together");
    expect(listener).not.toHaveBeenCalled();
  });

  it("coalesces keyed mutations into one read-before-write transaction", () => {
    const h = createHarness();
    const first = {};
    const second = {};
    const calls: string[] = [];

    h.controller.enqueueMutation(first, () => calls.push("old"));
    h.controller.enqueueMutation(first, () => {
      calls.push("first");
      h.order.push("mutate:first");
    });
    h.controller.enqueueMutation(second, () => {
      calls.push("second");
      h.order.push("mutate:second");
    });

    expect(h.pendingFrames()).toBe(1);
    h.flushFrame();

    expect(calls).toEqual(["first", "second"]);
    expect(h.readCount()).toBe(1);
    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.order).toEqual([
      "read",
      "mutate:first",
      "mutate:second",
      `write:${Number.MAX_SAFE_INTEGER}`,
    ]);
    expect(h.pendingFrames()).toBe(0);
  });

  it("demotes following when content geometry is away without a scroll event", () => {
    const h = createHarness({ scrollTop: 200, clientHeight: 100, scrollHeight: 1000 });
    h.controller.enqueueMutation({}, () => h.order.push("mutate"));

    h.flushFrame();

    expect(h.controller.isFollowing()).toBe(false);
    expect(h.button.hidden).toBe(false);
    expect(h.writes).toEqual([]);
  });

  it("tracks scroll geometry and restores following near bottom", () => {
    const h = createHarness({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });

    h.transcript.dispatchEvent(new Event("scroll"));
    expect(h.controller.getInteractionGeneration()).toBe(1);
    h.flushFrame();
    expect(h.controller.isFollowing()).toBe(false);
    expect(h.button.hidden).toBe(false);

    h.setGeometry({
      scrollTop: 1000 - 100 - TRANSCRIPT_NEAR_BOTTOM_PX,
      clientHeight: 100,
      scrollHeight: 1000,
    });
    h.transcript.dispatchEvent(new Event("scroll"));
    h.flushFrame();

    expect(h.controller.isFollowing()).toBe(true);
    expect(h.button.hidden).toBe(true);
    expect(h.writes).toEqual([]);
  });

  it("force wins over dirty away geometry and increments interaction ownership", () => {
    const h = createHarness({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });
    h.transcript.dispatchEvent(new Event("scroll"));
    const beforeForce = h.controller.getInteractionGeneration();

    h.controller.forceScrollToBottom();
    expect(h.controller.getInteractionGeneration()).toBe(beforeForce + 1);
    h.flushFrame();

    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.controller.isFollowing()).toBe(true);
    expect(h.button.hidden).toBe(true);
  });

  it("drops external follow when a later scroll event moves away", () => {
    const h = createHarness();
    h.controller.requestFollowAfterExternalMutation();
    h.setGeometry({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });
    h.transcript.dispatchEvent(new Event("scroll"));

    h.flushFrame();

    expect(h.writes).toEqual([]);
    expect(h.controller.isFollowing()).toBe(false);
  });

  it("defers callback re-entry to a second frame", () => {
    const h = createHarness();
    const key = {};
    const calls: string[] = [];

    h.controller.enqueueMutation(key, () => {
      calls.push("first");
      h.controller.enqueueMutation(key, () => calls.push("second"));
      h.controller.requestFollowAfterExternalMutation();
    });
    h.flushFrame();

    expect(calls).toEqual(["first"]);
    expect(h.pendingFrames()).toBe(1);
    h.flushFrame();
    expect(calls).toEqual(["first", "second"]);
  });

  it("flushes only the target mutation and preserves other keyed work", () => {
    const h = createHarness();
    const target = {};
    const other = {};
    const calls: string[] = [];

    h.controller.enqueueMutation(target, () => calls.push("stale-target"));
    h.controller.enqueueMutation(other, () => calls.push("other"));
    h.controller.forceScrollToBottom();
    h.controller.flushMutationNow(target, () => calls.push("target-now"));

    expect(calls).toEqual(["target-now"]);
    expect(h.readCount()).toBe(1);
    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.pendingFrames()).toBe(1);

    h.flushFrame();
    expect(calls).toEqual(["target-now", "other"]);
    expect(calls).not.toContain("stale-target");
    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]);
  });

  it("keeps flags created during flush for the next frame", () => {
    const h = createHarness();
    const key = {};

    h.controller.flushMutationNow(key, () => {
      h.controller.forceScrollToBottom();
    });

    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.pendingFrames()).toBe(1);
    h.flushFrame();
    expect(h.writes).toEqual([
      Number.MAX_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
    ]);
  });

  it("prepares prepend only for the current interaction generation", () => {
    const h = createHarness();
    const token = h.controller.getInteractionGeneration();
    h.controller.forceScrollToBottom();

    expect(h.controller.prepareForSynchronousPrepend(token)).toBe(false);
    expect(h.pendingFrames()).toBe(1);
    h.flushFrame();

    const current = h.controller.getInteractionGeneration();
    h.controller.requestFollowAfterExternalMutation();
    expect(h.controller.prepareForSynchronousPrepend(current)).toBe(true);
    expect(h.controller.isFollowing()).toBe(false);
    expect(h.button.hidden).toBe(false);
    expect(h.pendingFrames()).toBe(0);
  });

  it("preserves keyed work while preparing a synchronous prepend", () => {
    const h = createHarness();
    const calls: string[] = [];
    h.controller.enqueueMutation({}, () => calls.push("keyed"));

    expect(h.controller.prepareForSynchronousPrepend(
      h.controller.getInteractionGeneration(),
    )).toBe(true);
    expect(h.pendingFrames()).toBe(1);
    h.flushFrame();

    expect(calls).toEqual(["keyed"]);
    expect(h.writes).toEqual([]);
    expect(h.controller.isFollowing()).toBe(false);
  });

  it("reset and dispose retire callbacks and remove listeners", () => {
    const h = createHarness();
    const calls: string[] = [];
    const initialGeneration = h.controller.getInteractionGeneration();
    h.controller.enqueueMutation({}, () => calls.push("stale"));

    h.controller.reset();
    expect(h.canceled).toHaveLength(1);
    expect(h.pendingFrames()).toBe(0);
    expect(h.controller.getInteractionGeneration()).toBe(initialGeneration + 1);
    expect(h.controller.isFollowing()).toBe(true);
    expect(h.button.hidden).toBe(true);

    h.controller.dispose();
    h.transcript.dispatchEvent(new Event("scroll"));
    h.button.click();
    expect(h.pendingFrames()).toBe(0);
    expect(calls).toEqual([]);
  });
});
