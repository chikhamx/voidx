import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  TRANSCRIPT_NEAR_BOTTOM_PX,
  createTranscriptViewportController,
  validateBlockedQuiesceToken,
  type TranscriptFrameHandle,
  type TranscriptViewportGeometry,
} from "../../src/utils/transcript-viewport";

function createHarness(initial: TranscriptViewportGeometry = {
  scrollTop: 852,
  clientHeight: 100,
  scrollHeight: 1000,
}) {
  const transcript = document.createElement("div");
  transcript.scrollTop = initial.scrollTop;
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
      transcript.scrollTop = value;
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

  it("merges synchronous follow intent into the same transaction", () => {
    const h = createHarness({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });
    const calls: string[] = [];

    h.controller.flushMutationNow(
      {},
      () => calls.push("mutate"),
      { followAfterMutation: true },
    );

    expect(calls).toEqual(["mutate"]);
    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.pendingFrames()).toBe(0);
  });

  it("does not follow or schedule another frame when a synchronous mutation throws", () => {
    const h = createHarness();
    const failure = new Error("mutation failed");

    expect(() => h.controller.flushMutationNow(
      {},
      () => { throw failure; },
      { followAfterMutation: true },
    )).toThrow(failure);

    expect(h.writes).toEqual([]);
    expect(h.pendingFrames()).toBe(0);
  });

  it("rolls back following, button, and consumed flags when flushMutationNow throws", () => {
    const h = createHarness({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });
    const failure = new Error("measurement failed");
    h.controller.requestFollowAfterExternalMutation();

    expect(() => h.controller.flushMutationNow(
      {},
      () => { throw failure; },
      { followAfterMutation: true },
    )).toThrow(failure);

    expect(h.controller.isFollowing()).toBe(true);
    expect(h.button.hidden).toBe(true);
    expect(h.writes).toEqual([]);
    expect(h.pendingFrames()).toBe(0);
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


describe("blocked install quiesce", () => {
  const quiet = (controller: unknown) => {
    const method = "quiesceForBlocked" + "InstallNoDom";
    return (controller as Record<string, () => unknown>)[method]();
  };

  it("cancels pending work without geometry or DOM writes and returns a valid proof", () => {
    const h = createHarness();
    h.controller.enqueueMutation({}, () => h.order.push("mutate"));
    h.controller.requestFollowAfterExternalMutation();
    const readsBefore = h.readCount();
    const writesBefore = h.writes.length;

    const proof = quiet(h.controller);

    expect(proof).not.toBeNull();
    expect(validateBlockedQuiesceToken(proof as never)).toBe(true);
    expect(h.pendingFrames()).toBe(0);
    expect(h.readCount()).toBe(readsBefore);
    expect(h.writes).toHaveLength(writesBefore);
    expect(h.order).not.toContain("mutate");
  });

  it("keeps old proofs invalid after accepted work is consumed and the controller is idle again", () => {
    const h = createHarness();
    const proof = quiet(h.controller);
    expect(validateBlockedQuiesceToken(proof as never)).toBe(true);

    h.controller.enqueueMutation({}, () => undefined);
    h.flushFrame();

    expect(h.pendingFrames()).toBe(0);
    expect(validateBlockedQuiesceToken(proof as never)).toBe(false);
    const next = quiet(h.controller);
    expect(validateBlockedQuiesceToken(next as never)).toBe(true);
    h.controller.forceScrollToBottom();
    h.flushFrame();
    expect(validateBlockedQuiesceToken(next as never)).toBe(false);
  });

  it("returns null inside an active transaction and succeeds only after it returns", () => {
    const h = createHarness();
    let insideProof: unknown;
    let insideValid: boolean | undefined;

    h.controller.flushMutationNow({}, () => {
      insideProof = quiet(h.controller);
      insideValid = insideProof === null
        ? false
        : validateBlockedQuiesceToken(insideProof as never);
      h.controller.flushMutationNow({}, () => h.order.push("reentry"));
    });

    expect(insideProof).toBeNull();
    expect(insideValid).toBe(false);
    expect(h.pendingFrames()).toBe(1);
    const after = quiet(h.controller);
    expect(after).not.toBeNull();
    expect(validateBlockedQuiesceToken(after as never)).toBe(true);
    expect(h.pendingFrames()).toBe(0);
    expect(h.order).not.toContain("reentry");
  });
});


describe("Task 3 window transaction viewport integration", () => {
  type WindowTransactionResult =
    | { status: "applied"; state: unknown }
    | { status: "deferred"; reason: string };
  type ApplyWindowTransaction = (input: {
    viewportController: ReturnType<typeof createTranscriptViewportController>;
    transactionKey: object;
    root: HTMLElement;
    sourceChildren: readonly Element[];
    externalSourceChildren: ReadonlySet<Element>;
    nextChildren: readonly Element[];
    expectedNextChildren: readonly Element[];
    existingBlocks: ReadonlyMap<string, {
      key: string;
      roots: readonly HTMLElement[];
      primary: HTMLElement;
    }>;
    plan: {
      materializeKeys: string[];
      trimKeys: string[];
      nextAttachedKeys: Set<string>;
      spacerSegments: unknown[];
      anchorKey: string | null;
      overBudgetReason: string | null;
    };
    stagedBlocks: ReadonlyMap<string, { key: string; roots: HTMLElement[]; primary: HTMLElement }>;
    spacers: readonly unknown[];
    anchorJournal: null;
    expectedInteractionGeneration: number;
    expectedWindowGeneration: number;
    validateInteractionGeneration: (generation: number) => boolean;
    validateWindowGeneration: (generation: number) => boolean;
    measureBlock: (block: { roots: HTMLElement[] }) => number;
    writeScrollTop: (value: number) => void;
    resolvePrimaryByKey: (key: string) => HTMLElement | null;
    currentState: { generation: number; attachedKeys: Set<string>; heights: Map<string, number> };
    nextState: { generation: number; attachedKeys: Set<string>; heights: Map<string, number> };
    following: boolean;
    beforeMutation?: () => void;
  }) => WindowTransactionResult;

  const loadApply = async (): Promise<ApplyWindowTransaction> => {
    const module = await import("../../src/utils/transcript-dom-window") as unknown as {
      applyTranscriptDomWindowTransaction: ApplyWindowTransaction;
    };
    return module.applyTranscriptDomWindowTransaction;
  };

  const inputFor = (
    h: ReturnType<typeof createHarness>,
    overrides: Partial<Parameters<ApplyWindowTransaction>[0]> = {},
  ): Parameters<ApplyWindowTransaction>[0] => {
    const tail = document.createElement("div");
    tail.dataset.reconcileKey = "tail";
    tail.dataset.reconcileFingerprint = "fingerprint:tail";
    tail.dataset.reconcileShape = "test-shape-v1";
    tail.dataset.reconcileRootCount = "1";
    tail.dataset.reconcileMemberNodeIds = "[\"tail\"]";
    tail.dataset.reconcileToolCallIds = "[]";
    tail.dataset.reconcileFileChangeKeys = "[]";
    const state = { generation: 1, attachedKeys: new Set<string>(), heights: new Map<string, number>() };
    return {
      viewportController: h.controller,
      transactionKey: {},
      root: h.transcript,
      sourceChildren: Array.from(h.transcript.children),
      externalSourceChildren: new Set<Element>(),
      nextChildren: [tail],
      expectedNextChildren: [tail],
      existingBlocks: new Map(),
      plan: {
        materializeKeys: ["tail"], trimKeys: [], nextAttachedKeys: new Set(["tail"]),
        spacerSegments: [], anchorKey: null, overBudgetReason: null,
      },
      stagedBlocks: new Map([["tail", { key: "tail", roots: [tail], primary: tail }]]),
      spacers: [],
      anchorJournal: null,
      expectedInteractionGeneration: h.controller.getInteractionGeneration(),
      expectedWindowGeneration: 1,
      validateInteractionGeneration: (value) => value === h.controller.getInteractionGeneration(),
      validateWindowGeneration: (value) => value === 1,
      measureBlock: () => 40,
      writeScrollTop: (value) => { h.transcript.scrollTop = value; },
      resolvePrimaryByKey: (key) => Array.from(h.transcript.children).find(
        (child) => (child as HTMLElement).dataset.reconcileKey === key,
      ) as HTMLElement | undefined ?? null,
      currentState: state,
      nextState: { generation: 2, attachedKeys: new Set(["tail"]), heights: new Map() },
      following: true,
      ...overrides,
    };
  };

  it("keeps a following viewport at the bottom after a tail append", async () => {
    const apply = await loadApply();
    const h = createHarness({ scrollTop: 900, clientHeight: 100, scrollHeight: 1000 });

    expect(apply(inputFor(h))).toMatchObject({ status: "applied" });

    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
    expect(h.controller.isFollowing()).toBe(true);
    expect(h.pendingFrames()).toBe(0);
  });

  it("defers same-root synchronous reentry without partial state or another frame", async () => {
    const apply = await loadApply();
    const h = createHarness();
    const outer = inputFor(h);
    const innerState = { generation: 10, attachedKeys: new Set<string>(), heights: new Map<string, number>() };
    const inner = inputFor(h, {
      sourceChildren: outer.sourceChildren,
      nextChildren: outer.nextChildren,
      expectedNextChildren: outer.expectedNextChildren,
      currentState: innerState,
      nextState: { generation: 11, attachedKeys: new Set(["tail"]), heights: new Map() },
      following: false,
    });
    let nested: WindowTransactionResult | undefined;
    outer.beforeMutation = () => { nested = apply(inner); };

    expect(apply(outer)).toMatchObject({ status: "applied" });
    expect(nested).toMatchObject({ status: "deferred" });
    expect(innerState.generation).toBe(10);
    expect(h.pendingFrames()).toBe(0);
    expect(h.writes).toEqual([Number.MAX_SAFE_INTEGER]);
  });

  it("defers a transaction whose root differs from its viewport controller before mutation", async () => {
    const apply = await loadApply();
    const h = createHarness();
    const foreignRoot = document.createElement("div");
    const tail = document.createElement("div");
    tail.dataset.reconcileKey = "tail";
    foreignRoot.append(tail);
    const sourceChildren = Array.from(foreignRoot.children);
    const replacement = document.createElement("div");
    replacement.dataset.reconcileKey = "replacement";
    const currentState = {
      generation: 1, attachedKeys: new Set(["tail"]), heights: new Map([["tail", 40]]),
    };
    const nextState = {
      generation: 2, attachedKeys: new Set(["replacement"]), heights: new Map(currentState.heights),
    };
    const input = inputFor(h, {
      root: foreignRoot,
      sourceChildren,
      nextChildren: [replacement],
      expectedNextChildren: [replacement],
      existingBlocks: new Map([["tail", { key: "tail", roots: [tail], primary: tail }]]),
      plan: {
        materializeKeys: ["replacement"], trimKeys: ["tail"],
        nextAttachedKeys: new Set(["replacement"]), spacerSegments: [],
        anchorKey: null, overBudgetReason: null,
      },
      stagedBlocks: new Map([[
        "replacement", { key: "replacement", roots: [replacement], primary: replacement },
      ]]),
      currentState,
      nextState,
      following: false,
    });
    const beforeHeights = new Map(nextState.heights);

    expect.soft(apply(input)).toMatchObject({ status: "deferred" });
    expect.soft(Array.from(foreignRoot.children)).toEqual(sourceChildren);
    expect.soft(foreignRoot.children[0]).toBe(tail);
    expect.soft(currentState).toEqual({
      generation: 1, attachedKeys: new Set(["tail"]), heights: new Map([["tail", 40]]),
    });
    expect.soft(nextState.heights).toEqual(beforeHeights);
    expect.soft(h.pendingFrames()).toBe(0);
    expect.soft(h.writes).toEqual([]);
  });

  it.each(["measurement", "anchor write"] as const)(
    "rolls back a following window transaction and viewport ownership after a %s exception away from bottom",
    async (failurePoint) => {
      const apply = await loadApply();
      const h = createHarness({ scrollTop: 100, clientHeight: 100, scrollHeight: 1000 });
      const input = inputFor(h);
      const originalChildren = Array.from(h.transcript.children);
      const originalState = {
        generation: input.currentState.generation,
        attachedKeys: new Set(input.currentState.attachedKeys),
        heights: new Map(input.currentState.heights),
      };
      const nextHeights = new Map(input.nextState.heights);
      if (failurePoint === "measurement") {
        input.measureBlock = () => { throw new Error("measurement failed"); };
      } else {
        const anchor = document.createElement("div");
        anchor.dataset.reconcileKey = "anchor";
        anchor.dataset.reconcileFingerprint = "fingerprint:anchor";
        anchor.dataset.reconcileShape = "test-shape-v1";
        anchor.dataset.reconcileRootCount = "1";
        anchor.dataset.reconcileMemberNodeIds = "[\"anchor\"]";
        anchor.dataset.reconcileToolCallIds = "[]";
        anchor.dataset.reconcileFileChangeKeys = "[]";
        anchor.getBoundingClientRect = () => ({ top: 20, bottom: 60 } as DOMRect);
        h.transcript.append(anchor);
        input.sourceChildren = [anchor];
        input.existingBlocks = new Map([["anchor", { key: "anchor", roots: [anchor], primary: anchor }]]);
        input.nextChildren = [anchor, ...input.nextChildren];
        input.expectedNextChildren = [anchor, ...input.expectedNextChildren];
        input.plan.nextAttachedKeys = new Set(["anchor", "tail"]);
        input.currentState.attachedKeys = new Set(["anchor"]);
        input.currentState.heights = new Map([["anchor", 40]]);
        input.nextState.attachedKeys = new Set(["anchor", "tail"]);
        input.anchorJournal = {
          key: "anchor",
          oldRoot: anchor,
          offsetFromViewportTop: 20,
          scrollTop: 100,
          scrollHeight: 1000,
          interactionGeneration: input.expectedInteractionGeneration,
        } as never;
        input.resolvePrimaryByKey = (key) => key === "anchor" ? anchor : null;
        input.writeScrollTop = () => { throw new Error("anchor write failed"); };
        originalChildren.push(anchor);
        originalState.attachedKeys.add("anchor");
        originalState.heights.set("anchor", 40);
      }

      expect(() => apply(input)).toThrow(`${failurePoint} failed`);
      expect(Array.from(h.transcript.children)).toEqual(originalChildren);
      originalChildren.forEach((child, index) => expect(h.transcript.children[index]).toBe(child));
      expect(input.currentState).toEqual(originalState);
      expect(input.nextState.heights).toEqual(nextHeights);
      expect(h.transcript.scrollTop).toBe(100);
      expect(h.controller.isFollowing()).toBe(true);
      expect(h.button.hidden).toBe(true);
      expect(h.pendingFrames()).toBe(0);
    },
  );

  it("does not return applied early for same-controller different-root synchronous nesting", async () => {
    const apply = await loadApply();
    const h = createHarness();
    const outer = inputFor(h);
    const innerRoot = document.createElement("div");
    const innerTail = document.createElement("div");
    innerTail.dataset.reconcileKey = "inner-tail";
    const innerState = {
      generation: 10, attachedKeys: new Set<string>(), heights: new Map<string, number>(),
    };
    const innerNextState = {
      generation: 11, attachedKeys: new Set(["inner-tail"]), heights: new Map<string, number>(),
    };
    const inner = inputFor(h, {
      root: innerRoot,
      sourceChildren: [],
      nextChildren: [innerTail],
      expectedNextChildren: [innerTail],
      plan: {
        materializeKeys: ["inner-tail"], trimKeys: [], nextAttachedKeys: new Set(["inner-tail"]),
        spacerSegments: [], anchorKey: null, overBudgetReason: null,
      },
      stagedBlocks: new Map([[
        "inner-tail", { key: "inner-tail", roots: [innerTail], primary: innerTail },
      ]]),
      expectedWindowGeneration: 10,
      validateWindowGeneration: (value) => value === 10,
      currentState: innerState,
      nextState: innerNextState,
      following: false,
    });
    let nested: WindowTransactionResult | undefined;
    outer.beforeMutation = () => { nested = apply(inner); };

    expect(apply(outer)).toMatchObject({ status: "applied" });
    expect.soft(nested).toMatchObject({ status: "deferred" });
    expect.soft(Array.from(innerRoot.children)).toEqual([]);
    expect.soft(innerNextState.heights).toEqual(new Map());
    expect.soft(h.pendingFrames()).toBe(0);
  });

    it("ignores internal scroll events during active transaction to avoid generational rollback", async () => {
        const apply = await loadApply();
        const h = createHarness({ scrollTop: 900, clientHeight: 100, scrollHeight: 1000 });
        const initialGeneration = h.controller.getInteractionGeneration();

        const input = inputFor(h);
        // Simulate browser firing synchronous scroll during mutate
        input.beforeMutation = () => {
            h.transcript.dispatchEvent(new Event("scroll"));
        };

        const result = apply(input);
        expect(result).toMatchObject({ status: "applied" });
        expect(h.controller.getInteractionGeneration()).toBe(initialGeneration);
    });
});
