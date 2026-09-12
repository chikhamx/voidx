import { describe, expect, it, vi } from "vitest";
import type { TranscriptNode } from "../../src/rpc/protocol";
import type { TranscriptSnapshot } from "../../src/utils/render-types";
import {
  DEFAULT_BLOCK_ESTIMATE_PX,
  mergeCanonicalTranscript,
  normalizeTranscriptBlockExtents,
  planTranscriptDomWindow,
  type TranscriptLayoutEntry,
} from "../../src/utils/transcript-dom-window";

function node(
  id: string,
  nodeType: TranscriptNode["node_type"] = "assistant",
  payload: Record<string, unknown> = { raw_text: id },
  extra: Partial<TranscriptNode> = {},
): TranscriptNode {
  return { id, node_type: nodeType, payload, ...extra } as TranscriptNode;
}

function snapshot(
  ids: string[],
  metadata: Partial<TranscriptSnapshot> = {},
): TranscriptSnapshot {
  return {
    thread_id: "thread-1",
    revision: 1,
    windowed: true,
    before_turn_id: 10,
    after_turn_id: 20,
    has_earlier: true,
    has_later: true,
    nodes: ids.map((id) => node(id)),
    ...metadata,
  };
}

function toolTurn(turnId: string, suffix = ""): TranscriptNode[] {
  const proof = `proof-${turnId}${suffix}`;
  return [
    node(turnId, "turn", { text: turnId }),
    node(`call-${turnId}${suffix}`, "tool_call", { tool_name: "read" }, { tool_call_id: proof }),
    node(`result-${turnId}${suffix}`, "tool_result", { raw_text: "ok" }, { tool_call_id: proof }),
  ];
}

function canonicalEntries(count: number, extentPx = 100): TranscriptLayoutEntry[] {
  return Array.from({ length: count }, (_, descriptorIndex) => ({
    kind: "canonical" as const,
    key: `k${descriptorIndex}`,
    descriptorIndex,
    extentPx,
  }));
}

const budget = {
  overscanViewportsBefore: 1,
  overscanViewportsAfter: 1,
  maxAttachedUnpinnedBlocks: 240,
};

function planner(overrides: Partial<Parameters<typeof planTranscriptDomWindow>[0]> = {}) {
  return planTranscriptDomWindow({
    entries: canonicalEntries(5),
    attachedKeys: new Set<string>(),
    pinnedKeys: new Set<string>(),
    rowGapPx: 0,
    budget,
    viewport: { scrollTop: 100, clientHeight: 100, following: false },
    activation: "replan",
    anchorKey: null,
    ...overrides,
  });
}

describe("selectTranscriptWindowAnchor", () => {
  it("prefers the first fully visible non-pending block over an earlier offscreen block", async () => {
    const api = await import("../../src/utils/transcript-dom-window") as unknown as {
      selectTranscriptWindowAnchor: (input: {
        blocks: Array<{ key: string; primary: HTMLElement }>;
        viewportTop: number;
        viewportBottom: number;
        following: boolean;
        pendingRoots?: ReadonlySet<HTMLElement>;
      }) => { key: string; primary: HTMLElement } | null;
    };
    const block = (key: string, top: number, bottom: number) => {
      const primary = document.createElement("div");
      primary.getBoundingClientRect = () => ({ top, bottom } as DOMRect);
      return { key, primary };
    };
    const offscreen = block("offscreen", 0, 40);
    const visible = block("visible", 60, 100);

    expect(api.selectTranscriptWindowAnchor({
      blocks: [offscreen, visible],
      viewportTop: 50,
      viewportBottom: 110,
      following: false,
    })).toBe(visible);
  });

  it("prefers a fully visible block over an earlier partially visible block", async () => {
    const api = await import("../../src/utils/transcript-dom-window") as unknown as {
      selectTranscriptWindowAnchor: (input: {
        blocks: Array<{ key: string; primary: HTMLElement }>;
        viewportTop: number;
        viewportBottom: number;
        following: boolean;
        pendingRoots?: ReadonlySet<HTMLElement>;
      }) => { key: string; primary: HTMLElement } | null;
    };
    const block = (key: string, top: number, bottom: number) => {
      const primary = document.createElement("div");
      primary.getBoundingClientRect = () => ({ top, bottom } as DOMRect);
      return { key, primary };
    };
    const partial = block("partial", 40, 80);
    const complete = block("complete", 80, 100);

    expect(api.selectTranscriptWindowAnchor({
      blocks: [partial, complete],
      viewportTop: 50,
      viewportBottom: 110,
      following: false,
    })).toBe(complete);
  });

  it("skips pending-local roots and falls back to the last block while following", async () => {
    const api = await import("../../src/utils/transcript-dom-window") as unknown as {
      selectTranscriptWindowAnchor: (input: {
        blocks: Array<{ key: string; primary: HTMLElement }>;
        viewportTop: number;
        viewportBottom: number;
        following: boolean;
        pendingRoots?: ReadonlySet<HTMLElement>;
      }) => { key: string; primary: HTMLElement } | null;
    };
    const block = (key: string, top: number, bottom: number) => {
      const primary = document.createElement("div");
      primary.getBoundingClientRect = () => ({ top, bottom } as DOMRect);
      return { key, primary };
    };
    const pending = block("pending", 10, 40);
    const last = block("last", 500, 540);

    expect(api.selectTranscriptWindowAnchor({
      blocks: [pending, last],
      viewportTop: 100,
      viewportBottom: 200,
      following: true,
      pendingRoots: new Set([pending.primary]),
    })).toBe(last);
  });

    it("returns null when all usable blocks are outside the viewport", async () => {
        const api = await import("../../src/utils/transcript-dom-window") as unknown as {
            selectTranscriptWindowAnchor: (input: {
                blocks: Array<{ key: string; primary: HTMLElement }>;
                viewportTop: number;
                viewportBottom: number;
                following: boolean;
                pendingRoots?: ReadonlySet<HTMLElement>;
            }) => { key: string; primary: HTMLElement } | null;
        };
        const block = (key: string, top: number, bottom: number) => {
            const primary = document.createElement("div");
            primary.getBoundingClientRect = () => ({ top, bottom } as DOMRect);
            return { key, primary };
        };

        const farAbove = block("far-above", -1000, -800);
        const nearAbove = block("near-above", -200, -50);
        expect(api.selectTranscriptWindowAnchor({
            blocks: [farAbove, nearAbove],
            viewportTop: 100,
            viewportBottom: 400,
            following: false,
        })).toBeNull();
  });
});


describe("mergeCanonicalTranscript", () => {
  it("fully replaces canonical nodes and all metadata", () => {
    const current = snapshot(["old"], { revision: 2, before_turn_id: 1 });
    const incoming = snapshot(["new-a", "new-b"], {
      revision: 9,
      before_turn_id: 30,
      after_turn_id: 40,
      has_earlier: false,
      has_later: false,
    });

    const result = mergeCanonicalTranscript(current, incoming, "full");
    expect(result.status).toBe("merged");
    if (result.status === "merged") {
      expect(result.snapshot).toEqual(incoming);
      expect(result.descriptors.map((item) => item.key)).toEqual(["node:new-a", "node:new-b"]);
    }
  });

  it("installs an initial window and later replaces matches, retains absences, and inserts anchored incoming-only blocks", () => {
    const initial = snapshot(["a", "b", "d"], {
      revision: 1,
      before_turn_id: 10,
      after_turn_id: 20,
      has_earlier: true,
      has_later: true,
    });
    const installed = mergeCanonicalTranscript(null, initial, "windowed");
    expect(installed.status).toBe("merged");
    if (installed.status !== "merged") return;

    const replacementB = node("b", "assistant", { raw_text: "updated" });
    const incoming: TranscriptSnapshot = {
      ...snapshot([], {
        revision: 2,
        before_turn_id: 5,
        after_turn_id: 15,
        has_earlier: false,
        has_later: true,
      }),
      nodes: [replacementB, node("c"), node("d")],
    };
    const result = mergeCanonicalTranscript(installed.snapshot, incoming, "windowed");
    expect(result.status).toBe("merged");
    if (result.status === "merged") {
      expect(result.descriptors.map((item) => item.key)).toEqual(["node:a", "node:b", "node:c", "node:d"]);
      expect(result.snapshot.nodes).toEqual(result.descriptors.flatMap((item) => item.memberNodes));
      expect(result.snapshot.nodes[1]).toBe(replacementB);
      expect(result.snapshot).toMatchObject({
        revision: 2,
        before_turn_id: 5,
        after_turn_id: 20,
        has_earlier: false,
        has_later: true,
      });
    }
  });

  it("rejects earlier pages with incoming-only descriptors after or around overlap anchors", () => {
    const current = snapshot(["a", "b"], { before_turn_id: 20, after_turn_id: 40 });
    const afterAnchor: TranscriptSnapshot = {
      ...snapshot([], { before_turn_id: 10, after_turn_id: 20 }),
      nodes: [node("a"), node("new-after")],
    };
    const aroundAnchor: TranscriptSnapshot = {
      ...snapshot([], { before_turn_id: 10, after_turn_id: 20 }),
      nodes: [node("new-before"), node("a"), node("new-after")],
    };

    expect(mergeCanonicalTranscript(current, afterAnchor, "earlier-page")).toEqual({
      status: "stale", reason: "earlier page is not anchored", recovery: "ordinary",
    });
    expect(mergeCanonicalTranscript(current, aroundAnchor, "earlier-page")).toEqual({
      status: "stale", reason: "earlier page is not anchored", recovery: "ordinary",
    });
  });

  it("validates malformed current descriptors before an authoritative full replacement", () => {
    const malformedCurrent = snapshot(["same", "same"]);
    const incoming = snapshot(["replacement"], { windowed: false, revision: 9 });

    expect(mergeCanonicalTranscript(malformedCurrent, incoming, "full")).toEqual({
      status: "stale", reason: "invalid current transcript", recovery: "blocked",
    });
  });

  it("prepends earlier compound blocks and skips complete overlap blocks", () => {
    const current: TranscriptSnapshot = {
      ...snapshot([], { before_turn_id: 20, after_turn_id: 40 }),
      nodes: toolTurn("turn-20"),
    };
    const incoming: TranscriptSnapshot = {
      ...snapshot([], { revision: 3, before_turn_id: 1, after_turn_id: 20, has_earlier: false }),
      nodes: [...toolTurn("turn-1"), ...toolTurn("turn-20")],
    };
    const result = mergeCanonicalTranscript(current, incoming, "earlier-page");
    expect(result.status).toBe("merged");
    if (result.status === "merged") {
      expect(result.descriptors.map((item) => item.key)).toEqual([
        "turn-with-tools:turn-1", "turn-with-tools:turn-20",
      ]);
      expect(result.descriptors[0].memberNodeIds).toHaveLength(3);
      expect(result.snapshot.nodes).toEqual(result.descriptors.flatMap((item) => item.memberNodes));
    }
  });

  it("returns fixed stale results for partial overlap, duplicate ids, and unanchored order without mutating inputs", () => {
    const compoundCurrent: TranscriptSnapshot = { ...snapshot([]), nodes: toolTurn("turn-x") };
    const partial: TranscriptSnapshot = {
      ...snapshot([], { before_turn_id: 1, after_turn_id: 10 }),
      nodes: [compoundCurrent.nodes[0], ...toolTurn("alternate").slice(1)],
    };
    const duplicate = snapshot(["same", "same"]);
    const unanchored = snapshot(["unrelated"], { before_turn_id: 10, after_turn_id: 20 });
    const current = snapshot(["a", "b"], { before_turn_id: 10, after_turn_id: 20 });
    const before = JSON.stringify([compoundCurrent, partial, duplicate, unanchored, current]);

    expect(mergeCanonicalTranscript(compoundCurrent, partial, "earlier-page")).toEqual({
      status: "stale", reason: "partial descriptor overlap", recovery: "ordinary",
    });
    expect(mergeCanonicalTranscript(current, duplicate, "windowed")).toEqual({
      status: "stale", reason: "invalid incoming transcript", recovery: "ordinary",
    });
    expect(mergeCanonicalTranscript(current, unanchored, "windowed")).toEqual({
      status: "stale", reason: "window order is not anchored", recovery: "ordinary",
    });
    expect(JSON.stringify([compoundCurrent, partial, duplicate, unanchored, current])).toBe(before);
  });
});

describe("normalizeTranscriptBlockExtents", () => {
  it("uses a same-shape median and the fixed fallback for unknown or invalid extents", () => {
    const result = normalizeTranscriptBlockExtents([
      { key: "a", shape: "message", extentPx: 40 },
      { key: "b", shape: "message", extentPx: 100 },
      { key: "c", shape: "message", extentPx: 60 },
      { key: "d", shape: "message", extentPx: 0 },
      { key: "e", shape: "tool", extentPx: Number.NaN },
    ]);
    expect([...result]).toEqual([
      ["a", 40], ["b", 100], ["c", 60], ["d", 60], ["e", DEFAULT_BLOCK_ESTIMATE_PX],
    ]);
  });
});

describe("planTranscriptDomWindow", () => {
  it("selects only the initial tail around the viewport for 10,000 blocks", () => {
    const plan = planner({
      entries: canonicalEntries(10_000, 96),
      viewport: { scrollTop: 0, clientHeight: 600, following: true },
      activation: "initial",
      budget: { ...budget, overscanViewportsBefore: 3, overscanViewportsAfter: 3 },
    });
    expect(plan.nextAttachedKeys.size).toBe(25);
    expect([...plan.nextAttachedKeys][0]).toBe("k9975");
    const attached = [...plan.nextAttachedKeys];
    expect(attached[attached.length - 1]).toBe("k9999");
  });

  it("keeps pinned islands and splits omitted canonical ranges into multiple spacers", () => {
    const plan = planner({
      entries: canonicalEntries(8, 50),
      pinnedKeys: new Set(["k1", "k6"]),
      viewport: { scrollTop: 150, clientHeight: 50, following: false },
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
    });
    expect([...plan.nextAttachedKeys]).toEqual(["k1", "k3", "k6"]);
    expect(plan.spacerSegments.map((item) => [item.startIndex, item.endIndex, item.omittedKeys])).toEqual([
      [0, 1, ["k0"]], [2, 3, ["k2"]], [4, 6, ["k4", "k5"]], [7, 8, ["k7"]],
    ]);
  });

  it("uses the complete layout, and external extent changes alter canonical selection", () => {
    const external = (extentPx: number): TranscriptLayoutEntry[] => [
      { kind: "external", id: "leading", element: document.createElement("div"), extentPx: 20, insertion: { edge: "before-all" } },
      { kind: "canonical", key: "a", descriptorIndex: 0, extentPx: 50 },
      { kind: "external", id: "middle", element: document.createElement("div"), extentPx, insertion: { afterKey: "a" } },
      { kind: "canonical", key: "b", descriptorIndex: 1, extentPx: 50 },
      { kind: "external", id: "trailing", element: document.createElement("div"), extentPx: 30, insertion: { edge: "after-all" } },
      { kind: "canonical", key: "c", descriptorIndex: 2, extentPx: 50 },
    ];
    const input = {
      viewport: { scrollTop: 145, clientHeight: 40, following: false },
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
      rowGapPx: 5,
    };
    expect([...planner({ ...input, entries: external(20) }).nextAttachedKeys]).toEqual(["b"]);
    expect([...planner({ ...input, entries: external(100) }).nextAttachedKeys]).toEqual([]);
  });

  it("applies the safety cap by distance then descriptor index and never splits a compound key", () => {
    const plan = planner({
      entries: canonicalEntries(7, 40),
      viewport: { scrollTop: 120, clientHeight: 40, following: false },
      budget: { overscanViewportsBefore: 3, overscanViewportsAfter: 3, maxAttachedUnpinnedBlocks: 3 },
    });
    expect([...plan.nextAttachedKeys]).toEqual(["k2", "k3", "k4"]);
    expect(plan.materializeKeys).toEqual(["k2", "k3", "k4"]);
    expect(new Set(plan.materializeKeys).size).toBe(plan.materializeKeys.length);
  });

  it("retains a complete oversized block even when it exceeds the pixel budget", () => {
    const plan = planner({
      entries: [
        { kind: "canonical", key: "huge", descriptorIndex: 0, extentPx: 1000 },
        { kind: "canonical", key: "small", descriptorIndex: 1, extentPx: 20 },
      ],
      viewport: { scrollTop: 0, clientHeight: 100, following: false },
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0, maxAttachedUnpinnedBlocks: 1 },
    });
    expect([...plan.nextAttachedKeys]).toEqual(["huge"]);
  });

  it("does not trim for nonpositive client height and picks the tail when nothing is attached", () => {
    const retained = planner({
      attachedKeys: new Set(["k0", "k2"]),
      pinnedKeys: new Set(["k4"]),
      viewport: { scrollTop: 0, clientHeight: 0, following: false },
    });
    expect(retained.trimKeys).toEqual([]);
    expect([...retained.nextAttachedKeys]).toEqual(["k0", "k2", "k4"]);

    const empty = planner({ viewport: { scrollTop: 0, clientHeight: -1, following: false } });
    expect([...empty.nextAttachedKeys]).toEqual(["k4"]);
  });


  it("refuses destructive trim without an anchor but still materializes", () => {
    const plan = planner({
      attachedKeys: new Set(["k0"]),
      viewport: { scrollTop: 300, clientHeight: 100, following: false },
      anchorKey: null,
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
    });
    expect(plan.materializeKeys).toEqual(["k3"]);
    expect(plan.trimKeys).toEqual([]);
    expect(plan.nextAttachedKeys).toEqual(new Set(["k0", "k3"]));
    expect(plan.overBudgetReason).toBe("anchor unavailable");
  });

  it("computes spacer coordinates and heights with gaps and external boundaries", () => {
    const entries: TranscriptLayoutEntry[] = [
      { kind: "canonical", key: "a", descriptorIndex: 0, extentPx: 10 },
      { kind: "canonical", key: "b", descriptorIndex: 1, extentPx: 20 },
      { kind: "external", id: "proof", element: document.createElement("div"), extentPx: 30, insertion: { afterKey: "b" } },
      { kind: "canonical", key: "c", descriptorIndex: 2, extentPx: 40 },
    ];
    const plan = planner({
      entries,
      pinnedKeys: new Set(["c"]),
      viewport: { scrollTop: 500, clientHeight: 10, following: false },
      rowGapPx: 5,
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
    });
    expect(plan.spacerSegments).toEqual([{
      startIndex: 0,
      endIndex: 2,
      omittedKeys: ["a", "b"],
      canonicalStartPx: 0,
      canonicalEndPx: 35,
      cssHeightPx: 35,
    }]);
  });
});


describe("Task 2 spacer height and block measurement", () => {
  it.each([
    { name: "leading", attached: ["k2"], viewport: { scrollTop: 0, clientHeight: 0, following: false }, expected: [{ keys: ["k0", "k1"], height: 37 }] },
    { name: "trailing", attached: ["k0"], viewport: { scrollTop: 0, clientHeight: 0, following: false }, expected: [{ keys: ["k1", "k2"], height: 67 }] },
    { name: "interior", attached: ["k0", "k2"], viewport: { scrollTop: 0, clientHeight: 0, following: false }, expected: [{ keys: ["k1"], height: 20 }] },
    { name: "all omitted", attached: [], viewport: { scrollTop: 1_000, clientHeight: 1, following: false }, expected: [{ keys: ["k0", "k1", "k2"], height: 84 }] },
  ])("uses only omitted extents and internal row gaps for a $name spacer", ({ attached, viewport, expected }) => {
    const entries: TranscriptLayoutEntry[] = [
      { kind: "canonical", key: "k0", descriptorIndex: 0, extentPx: 10 },
      { kind: "canonical", key: "k1", descriptorIndex: 1, extentPx: 20 },
      { kind: "canonical", key: "k2", descriptorIndex: 2, extentPx: 40 },
    ];
    const plan = planner({
      entries,
      attachedKeys: new Set(attached),
      pinnedKeys: new Set(attached),
      rowGapPx: 7,
      viewport,
    });

    expect(plan.spacerSegments.map((segment) => ({
      keys: segment.omittedKeys,
      height: segment.cssHeightPx,
    }))).toEqual(expected);
  });

  it("keeps spacer canonical coordinates independent from preceding external layout entries", () => {
    const external = document.createElement("div");
    const plan = planner({
      entries: [
        { kind: "external", id: "prompt", element: external, extentPx: 30, insertion: { edge: "before-all" } },
        { kind: "canonical", key: "a", descriptorIndex: 0, extentPx: 10 },
        { kind: "canonical", key: "b", descriptorIndex: 1, extentPx: 20 },
        { kind: "canonical", key: "c", descriptorIndex: 2, extentPx: 40 },
      ],
      pinnedKeys: new Set(["c"]),
      rowGapPx: 5,
      viewport: { scrollTop: 500, clientHeight: 10, following: false },
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
    });

    expect(plan.spacerSegments).toEqual([{
      startIndex: 0,
      endIndex: 2,
      omittedKeys: ["a", "b"],
      canonicalStartPx: 0,
      canonicalEndPx: 35,
      cssHeightPx: 35,
    }]);
  });

  it("measures a multi-root block from first.top to last.bottom without adding its internal gap twice", async () => {
    const api = await import("../../src/utils/transcript-dom-window") as unknown as {
      measureTranscriptLogicalBlockExtent(roots: readonly HTMLElement[]): number | null;
    };
    const first = document.createElement("div");
    const middle = document.createElement("div");
    const last = document.createElement("div");
    first.getBoundingClientRect = () => ({ top: 100, bottom: 120 } as DOMRect);
    middle.getBoundingClientRect = () => ({ top: 127, bottom: 157 } as DOMRect);
    last.getBoundingClientRect = () => ({ top: 164, bottom: 190 } as DOMRect);

    expect(api.measureTranscriptLogicalBlockExtent([first, middle, last])).toBe(90);
  });

  it("uses single-root border-box extent and treats zero-height or invalid geometry as unknown", async () => {
    const api = await import("../../src/utils/transcript-dom-window") as unknown as {
      measureTranscriptLogicalBlockExtent(roots: readonly HTMLElement[]): number | null;
    };
    const root = (top: number, bottom: number) => {
      const element = document.createElement("div");
      element.getBoundingClientRect = () => ({ top, bottom } as DOMRect);
      return element;
    };

    expect(api.measureTranscriptLogicalBlockExtent([root(12, 48)])).toBe(36);
    expect(api.measureTranscriptLogicalBlockExtent([root(12, 12)])).toBeNull();
    expect(api.measureTranscriptLogicalBlockExtent([root(Number.NaN, 48)])).toBeNull();
    expect(api.measureTranscriptLogicalBlockExtent([root(60, 48)])).toBeNull();
    expect(api.measureTranscriptLogicalBlockExtent([])).toBeNull();
  });
});


describe("Task 3 anchor-preserving window transaction", () => {
  type WindowState = {
    generation: number;
    attachedKeys: Set<string>;
    heights: Map<string, number>;
  };
  type TransactionResult =
    | { status: "applied"; state: WindowState }
    | { status: "deferred"; reason: string };
  type LogicalBlock = {
    key: string;
    roots: readonly HTMLElement[];
    primary: HTMLElement;
  };
  type StagedBlock = LogicalBlock;
  type AnchorJournal = {
    key: string;
    oldRoot: HTMLElement;
    offsetFromViewportTop: number;
    scrollTop: number;
    scrollHeight: number;
    interactionGeneration: number;
  };
  type TransactionInput = {
    root: HTMLElement;
    sourceChildren: readonly Element[];
    externalSourceChildren: ReadonlySet<Element>;
    nextChildren: readonly Element[];
    expectedNextChildren: readonly Element[];
    existingBlocks: ReadonlyMap<string, LogicalBlock>;
    plan: ReturnType<typeof planTranscriptDomWindow>;
    stagedBlocks: ReadonlyMap<string, StagedBlock>;
    spacers: readonly { element: HTMLElement; segment: ReturnType<typeof planTranscriptDomWindow>["spacerSegments"][number] }[];
    anchorJournal: AnchorJournal | null;
    expectedInteractionGeneration: number;
    expectedWindowGeneration: number;
    validateInteractionGeneration: (generation: number) => boolean;
    validateWindowGeneration: (generation: number) => boolean;
    measureBlock: (block: StagedBlock) => number;
    writeScrollTop: (value: number) => void;
    resolvePrimaryByKey: (key: string) => HTMLElement | null;
    currentState: WindowState;
    nextState: WindowState;
    beforeMutation?: () => void;
    afterDomMutation?: () => void;
    beforeFinalValidation?: () => void;
    reserveOwnerReservations?: () => boolean;
    validateOwnerReservations?: () => boolean;
    commitOwnerReservationsNoFail?: () => void;
    releaseOwnerReservations?: () => void;
    ownerPinnedKeys?: ReadonlySet<string>;
    transactionKey?: object;
    following?: boolean;
    viewportController?: {
      transcript: HTMLElement;
      flushMutationNow: (
        key: object,
        mutate: () => void,
        options?: { followAfterMutation?: boolean },
      ) => void;
      cancelMutation: (key: object) => void;
    };
  };
  type TransactionApi = (input: TransactionInput) => TransactionResult;

  const transactionApi = async (): Promise<TransactionApi> => {
    const module = await import("../../src/utils/transcript-dom-window") as unknown as {
      applyTranscriptDomWindowTransaction: TransactionApi;
    };
    return module.applyTranscriptDomWindowTransaction;
  };

  const block = (key: string, top: () => number, height = 40): StagedBlock => {
    const primary = document.createElement("div");
    primary.dataset.reconcileKey = key;
    primary.dataset.reconcileFingerprint = `fingerprint:${key}`;
    primary.dataset.reconcileShape = "test-shape-v1";
    primary.dataset.reconcileRootCount = "1";
    primary.dataset.reconcileMemberNodeIds = JSON.stringify([key]);
    primary.dataset.reconcileToolCallIds = "[]";
    primary.dataset.reconcileFileChangeKeys = "[]";
    primary.getBoundingClientRect = () => ({
      top: top(),
      bottom: top() + height,
      height,
    } as DOMRect);
    return { key, roots: [primary], primary };
  };

  const segment = (keys: string[], height: number) => ({
    startIndex: 0,
    endIndex: keys.length,
    omittedKeys: keys,
    canonicalStartPx: 0,
    canonicalEndPx: height,
    cssHeightPx: height,
  });

  const spacer = (keys: string[], height: number) => {
    const element = document.createElement("div");
    element.className = "transcript-window-spacer";
    element.setAttribute("aria-hidden", "true");
    const metadata = segment(keys, height);
    element.dataset.transcriptSpacer = JSON.stringify(metadata);
    element.style.height = `${height}px`;
    return { element, segment: metadata };
  };

  const fixture = () => {
    const root = document.createElement("div");
    let anchorDocumentTop = 120;
    const before = block("before", () => 40 - root.scrollTop);
    const anchor = block("anchor", () => anchorDocumentTop - root.scrollTop);
    const after = block("after", () => 180 - root.scrollTop);
    const oldSpacer = spacer(["omitted"], 60);
    root.append(before.primary, oldSpacer.element, anchor.primary, after.primary);
    root.scrollTop = 80;
    const sourceChildren = Array.from(root.children);
    const currentState: WindowState = {
      generation: 7,
      attachedKeys: new Set(["before", "anchor", "after"]),
      heights: new Map([["before", 40], ["anchor", 40], ["after", 40]]),
    };
    const nextState: WindowState = {
      generation: 8,
      attachedKeys: new Set(["new", "anchor"]),
      heights: new Map(currentState.heights),
    };
    const newBlock = block("new", () => 20 - root.scrollTop);
    const nextSpacer = spacer(["before", "after"], 100);
    const plan = planner({
      entries: canonicalEntries(4, 40),
      attachedKeys: new Set(["k0", "k1", "k2"]),
      anchorKey: "k1",
    });
    plan.materializeKeys = ["new"];
    plan.trimKeys = ["before", "after"];
    plan.nextAttachedKeys = new Set(["new", "anchor"]);
    plan.anchorKey = "anchor";
    plan.spacerSegments = [nextSpacer.segment];
    const writes: number[] = [];
    const input: TransactionInput = {
      root,
      sourceChildren,
      externalSourceChildren: new Set<Element>(),
      nextChildren: [newBlock.primary, nextSpacer.element, anchor.primary],
      expectedNextChildren: [newBlock.primary, nextSpacer.element, anchor.primary],
      existingBlocks: new Map([
        ["before", before],
        ["anchor", anchor],
        ["after", after],
      ]),
      plan,
      stagedBlocks: new Map([["new", newBlock]]),
      spacers: [nextSpacer],
      anchorJournal: {
        key: "anchor",
        oldRoot: anchor.primary,
        offsetFromViewportTop: 40,
        scrollTop: 80,
        scrollHeight: 260,
        interactionGeneration: 3,
      },
      expectedInteractionGeneration: 3,
      expectedWindowGeneration: 7,
      validateInteractionGeneration: (value) => value === 3,
      validateWindowGeneration: (value) => value === 7,
      measureBlock: () => 40,
      writeScrollTop(value) {
        writes.push(value);
        root.scrollTop = value;
      },
      resolvePrimaryByKey(key) {
        return Array.from(root.children).find(
          (child) => (child as HTMLElement).dataset.reconcileKey === key,
        ) as HTMLElement | undefined ?? null;
      },
      currentState,
      nextState,
    };
    return {
      root, before, anchor, after, oldSpacer, nextSpacer, newBlock,
      sourceChildren, currentState, nextState, writes, input,
      setAnchorDocumentTop(value: number) { anchorDocumentTop = value; },
    };
  };

  const expectOriginal = (h: ReturnType<typeof fixture>) => {
    expect(Array.from(h.root.children)).toEqual(h.sourceChildren);
    h.sourceChildren.forEach((child, index) => expect(h.root.children[index]).toBe(child));
    expect(h.oldSpacer.element.dataset.transcriptSpacer).toBe(JSON.stringify(h.oldSpacer.segment));
    expect(h.oldSpacer.element.style.height).toBe("60px");
    expect(h.root.scrollTop).toBe(80);
    expect(h.currentState).toEqual({
      generation: 7,
      attachedKeys: new Set(["before", "anchor", "after"]),
      heights: new Map([["before", 40], ["anchor", 40], ["after", 40]]),
    });
  };

  it("places a staged interior block and spacer in the Phase 1 order while retaining external roots", async () => {
    const apply = await transactionApi();
    const root = document.createElement("div");
    const retainedBefore = document.createElement("aside");
    const retainedAfter = document.createElement("form");
    const a = block("a", () => 10);
    const c = block("c", () => 90);
    const b = block("b", () => 50);
    const interiorSpacer = spacer(["omitted-between-b-and-c"], 20);
    root.append(retainedBefore, a.primary, c.primary, retainedAfter);
    const sourceChildren = Array.from(root.children);
    const currentState: WindowState = {
      generation: 1,
      attachedKeys: new Set(["a", "c"]),
      heights: new Map([["a", 40], ["c", 40]]),
    };
    const nextState: WindowState = {
      generation: 2,
      attachedKeys: new Set(["a", "b", "c"]),
      heights: new Map(currentState.heights),
    };
    const plan = planner();
    plan.materializeKeys = ["b"];
    plan.trimKeys = [];
    plan.nextAttachedKeys = new Set(["a", "b", "c"]);
    plan.spacerSegments = [interiorSpacer.segment];
    const nextChildren = [
      retainedBefore, a.primary, b.primary, interiorSpacer.element, c.primary, retainedAfter,
    ];

    expect(apply({
      root,
      sourceChildren,
      externalSourceChildren: new Set([retainedBefore, retainedAfter]),
      nextChildren,
      expectedNextChildren: nextChildren,
      existingBlocks: new Map([["a", a], ["c", c]]),
      plan,
      stagedBlocks: new Map([["b", b]]),
      spacers: [interiorSpacer],
      anchorJournal: null,
      expectedInteractionGeneration: 0,
      expectedWindowGeneration: 1,
      validateInteractionGeneration: (value) => value === 0,
      validateWindowGeneration: (value) => value === 1,
      measureBlock: () => 40,
      writeScrollTop: () => undefined,
      resolvePrimaryByKey: () => null,
      currentState,
      nextState,
    })).toEqual({ status: "applied", state: nextState });
    expect(Array.from(root.children)).toEqual(nextChildren);
    expect([a.primary, b.primary, c.primary].map((element) => element.dataset.reconcileKey))
      .toEqual(["a", "b", "c"]);
  });

  it("trims and replaces only the exact roots declared for a multi-root block", async () => {
    const apply = await transactionApi();
    const createCase = (replace: boolean) => {
      const root = document.createElement("div");
      const a = block("a", () => 20);
      const aContinuation = document.createElement("div");
      a.primary.dataset.reconcileRootCount = "2";
      const pendingLocal = document.createElement("div");
      pendingLocal.dataset.pendingLocal = "true";
      const retained = document.createElement("section");
      root.append(a.primary, aContinuation, pendingLocal, retained);
      const sourceChildren = Array.from(root.children);
      const replacementPrimary = block("a", () => 60).primary;
      const replacementContinuation = document.createElement("div");
      replacementPrimary.dataset.reconcileRootCount = "2";
      const replacement: StagedBlock = {
        key: "a",
        roots: [replacementPrimary, replacementContinuation],
        primary: replacementPrimary,
      };
      const nextChildren = replace
        ? [replacement.primary, replacementContinuation, pendingLocal, retained]
        : [pendingLocal, retained];
      const currentState: WindowState = {
        generation: 1,
        attachedKeys: new Set(["a"]),
        heights: new Map([["a", 40]]),
      };
      const nextState: WindowState = {
        generation: 2,
        attachedKeys: new Set(replace ? ["a"] : []),
        heights: new Map(currentState.heights),
      };
      const plan = planner();
      plan.trimKeys = ["a"];
      plan.materializeKeys = replace ? ["a"] : [];
      plan.nextAttachedKeys = new Set(replace ? ["a"] : []);
      plan.spacerSegments = [];
      return {
        root,
        pendingLocal,
        retained,
        nextChildren,
        input: {
          root,
          sourceChildren,
          externalSourceChildren: new Set([pendingLocal, retained]),
          nextChildren,
          expectedNextChildren: nextChildren,
          existingBlocks: new Map([["a", { ...a, roots: [a.primary, aContinuation] }]]),
          plan,
          stagedBlocks: replace ? new Map([["a", replacement]]) : new Map<string, StagedBlock>(),
          spacers: [],
          anchorJournal: null,
          expectedInteractionGeneration: 0,
          expectedWindowGeneration: 1,
          validateInteractionGeneration: (value: number) => value === 0,
          validateWindowGeneration: (value: number) => value === 1,
          measureBlock: () => 40,
          writeScrollTop: () => undefined,
          resolvePrimaryByKey: () => null,
          currentState,
          nextState,
        } satisfies TransactionInput,
      };
    };

    for (const replace of [false, true]) {
      const h = createCase(replace);
      expect(apply(h.input)).toMatchObject({ status: "applied" });
      expect(Array.from(h.root.children)).toEqual(h.nextChildren);
      expect(h.root.contains(h.pendingLocal)).toBe(true);
      expect(h.root.contains(h.retained)).toBe(true);
    }
  });

  it.each([
    ["unknown child", (h: ReturnType<typeof fixture>) => [document.createElement("i"), ...h.input.nextChildren]],
    ["duplicate child", (h: ReturnType<typeof fixture>) => [...h.input.nextChildren, h.anchor.primary]],
    ["missing staged root", (h: ReturnType<typeof fixture>) => h.input.nextChildren.filter((child) => child !== h.newBlock.primary)],
    ["lookalike source child", (h: ReturnType<typeof fixture>) => h.input.nextChildren.map(
      (child) => child === h.anchor.primary ? child.cloneNode(true) as Element : child,
    )],
  ])("defers an invalid explicit nextChildren containing %s before mutation", async (_name, invalid) => {
    const apply = await transactionApi();
    const h = fixture();
    h.input.nextChildren = invalid(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it("keeps the exact anchor viewport offset after upward materialization", async () => {
    const apply = await transactionApi();
    const h = fixture();
    h.input.measureBlock = () => {
      h.setAnchorDocumentTop(200);
      return 40;
    };

    const result = apply(h.input);

    expect(result).toEqual({ status: "applied", state: h.nextState });
    expect(h.writes).toEqual([160]);
    expect(h.anchor.primary.getBoundingClientRect().top - h.root.getBoundingClientRect().top).toBe(40);
  });

  it("does not jump for downward materialization and remote trim", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const downward = block("new", () => 260);
    h.input.stagedBlocks = new Map([["new", downward]]);
    h.input.nextChildren = h.input.nextChildren.map(
      (child) => child === h.newBlock.primary ? downward.primary : child,
    );
    h.input.expectedNextChildren = [...h.input.nextChildren];
    h.input.measureBlock = () => 40;

    expect(apply(h.input).status).toBe("applied");
    expect(h.writes).toEqual([80]);
    expect(h.root.scrollTop).toBe(80);
  });

  it.each([
    ["interaction before mutation", (h: ReturnType<typeof fixture>) => {
      h.input.validateInteractionGeneration = () => false;
    }],
    ["window before mutation", (h: ReturnType<typeof fixture>) => {
      h.input.validateWindowGeneration = () => false;
    }],
    ["interaction at final validation", (h: ReturnType<typeof fixture>) => {
      let calls = 0;
      h.input.validateInteractionGeneration = () => ++calls === 1;
    }],
    ["window at final validation", (h: ReturnType<typeof fixture>) => {
      let calls = 0;
      h.input.validateWindowGeneration = () => ++calls === 1;
    }],
  ])("defers when %s changes and preserves DOM, state, and scroll", async (_name, invalidate) => {
    const apply = await transactionApi();
    const h = fixture();
    invalidate(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
    expect(h.writes[h.writes.length - 1] ?? 80).toBe(80);
  });

  it("rejects source children changed by identity at any position without touching the observed DOM", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const lookalike = h.oldSpacer.element.cloneNode(true) as HTMLElement;
    h.root.replaceChild(lookalike, h.oldSpacer.element);
    const observed = Array.from(h.root.children);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(Array.from(h.root.children)).toEqual(observed);
    observed.forEach((child, index) => expect(h.root.children[index]).toBe(child));
    expect(h.root.scrollTop).toBe(80);
    expect(h.currentState.generation).toBe(7);
  });

  it("rolls back when source child identity changes during final validation", async () => {
    const apply = await transactionApi();
    const h = fixture();
    h.input.beforeFinalValidation = () => {
      const currentAnchor = h.input.resolvePrimaryByKey("anchor");
      if (!currentAnchor) throw new Error("missing anchor during injected final validation");
      h.root.replaceChild(currentAnchor.cloneNode(true), currentAnchor);
    };

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it("rejects reservation hooks without owner validation before reserve", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const order: string[] = [];
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };
    h.input.releaseOwnerReservations = () => { order.push("release"); };

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(order).toEqual([]);
    expectOriginal(h);
  });

  it("cancels a viewport callback that did not execute synchronously and releases reservations", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const transactionKey = {};
    const order: string[] = [];
    h.input.transactionKey = transactionKey;
    h.input.viewportController = {
      transcript: h.root,
      flushMutationNow: (key) => { order.push(key === transactionKey ? "flush" : "wrong-key"); },
      cancelMutation: (key) => { order.push(key === transactionKey ? "cancel" : "wrong-key"); },
    };
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => { order.push("validate"); return true; };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };
    h.input.releaseOwnerReservations = () => { order.push("release"); };

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(order).toEqual(["reserve", "validate", "flush", "cancel", "release"]);
    expectOriginal(h);
  });

  it("does not commit owners when viewport follow fails after the mutation callback", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const failure = new Error("follow write failed");
    const order: string[] = [];
    h.input.following = true;
    h.input.viewportController = {
      transcript: h.root,
      flushMutationNow: (_key, mutate, options) => {
        order.push("flush");
        mutate();
        order.push(options?.followAfterMutation === true ? "follow" : "missing-follow");
        throw failure;
      },
      cancelMutation: () => { order.push("cancel"); },
    };
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => { order.push("validate"); return true; };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };
    h.input.releaseOwnerReservations = () => { order.push("release"); };

    expect(() => apply(h.input)).toThrow(failure);
    expect(order).toEqual([
      "reserve", "validate", "flush", "validate", "follow", "release",
    ]);
    expectOriginal(h);
  });

  it("defers before reserve or mutation when an owner-pinned key remains in trimKeys", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const order: string[] = [];
    h.input.ownerPinnedKeys = new Set(["before"]);
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => true;
    h.input.commitOwnerReservationsNoFail = () => undefined;
    h.input.releaseOwnerReservations = () => { order.push("release"); };
    h.input.beforeMutation = () => { order.push("mutation"); };

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(order).toEqual([]);
    expectOriginal(h);
  });

  it("releases owner reservations and defers before mutation on reserve conflict or initial validation failure", async () => {
    const apply = await transactionApi();
    for (const failure of ["reserve", "validation"] as const) {
      const h = fixture();
      const order: string[] = [];
      h.input.reserveOwnerReservations = () => {
        order.push("reserve");
        return failure !== "reserve";
      };
      h.input.validateOwnerReservations = () => {
        order.push("validate");
        return failure !== "validation";
      };
      h.input.releaseOwnerReservations = () => { order.push("release"); };
      h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };
      h.input.beforeMutation = () => { order.push("mutation"); };

      expect(apply(h.input)).toMatchObject({ status: "deferred" });
      expect(order).toEqual(failure === "reserve"
        ? ["reserve", "release"]
        : ["reserve", "validate", "release"]);
      expectOriginal(h);
    }
  });

  it("rolls back and releases owner reservations when final owner validation becomes stale", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const order: string[] = [];
    let ownerCurrent = true;
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => { order.push("validate"); return ownerCurrent; };
    h.input.beforeFinalValidation = () => { ownerCurrent = false; };
    h.input.releaseOwnerReservations = () => { order.push("release"); };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(order).toEqual(["reserve", "validate", "validate", "release"]);
    expectOriginal(h);
  });

  it("releases uncommitted owner reservations after a transaction exception", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const order: string[] = [];
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => { order.push("validate"); return true; };
    h.input.measureBlock = () => { throw new Error("owner measurement fault"); };
    h.input.releaseOwnerReservations = () => { order.push("release"); };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };

    expect(() => apply(h.input)).toThrow("owner measurement fault");
    expect(order).toEqual(["reserve", "validate", "validate", "release"]);
    expectOriginal(h);
  });

  it("commits owners only after final validation, measurement, and anchor restoration", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const order: string[] = [];
    h.input.reserveOwnerReservations = () => { order.push("reserve"); return true; };
    h.input.validateOwnerReservations = () => { order.push("validate"); return true; };
    h.input.afterDomMutation = () => { order.push("dom"); };
    h.input.measureBlock = () => { order.push("measure"); return 40; };
    h.input.writeScrollTop = (value) => {
      order.push("anchor");
      h.writes.push(value);
      h.root.scrollTop = value;
    };
    h.input.commitOwnerReservationsNoFail = () => { order.push("commit"); };
    h.input.releaseOwnerReservations = () => { order.push("release"); };

    expect(apply(h.input)).toEqual({ status: "applied", state: h.nextState });
    expect(order).toEqual([
      "reserve", "validate", "dom", "validate", "measure", "anchor", "commit",
    ]);
  });

  it.each(["mutation", "measurement", "final validation", "anchor write"] as const)(
    "synchronously rolls back identity, spacer metadata, scroll, and state after a %s failure",
    async (failurePoint) => {
      const apply = await transactionApi();
      const h = fixture();
      if (failurePoint === "mutation") {
        h.input.afterDomMutation = () => { throw new Error("mutation fault"); };
      } else if (failurePoint === "measurement") {
        h.input.measureBlock = () => { throw new Error("measurement fault"); };
      } else if (failurePoint === "final validation") {
        h.input.beforeFinalValidation = () => { throw new Error("validation fault"); };
      } else {
        h.input.writeScrollTop = () => { throw new Error("anchor fault"); };
      }

      expect(() => apply(h.input)).toThrow();
      expectOriginal(h);
      expect(h.nextState.heights).toEqual(new Map(h.currentState.heights));
    },
  );

  it("restores replacement anchors with real viewport-relative DOMRect geometry", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const replacementDocumentTop = 200;
    const replacement = block("anchor", () => replacementDocumentTop - h.root.scrollTop);
    h.input.plan.materializeKeys = ["anchor"];
    h.input.plan.trimKeys = ["anchor"];
    h.input.stagedBlocks = new Map([["anchor", replacement]]);
    h.input.plan.nextAttachedKeys = new Set(["anchor"]);
    h.nextState.attachedKeys = new Set(["anchor"]);
    h.input.nextChildren = [
      h.before.primary,
      h.nextSpacer.element,
      replacement.primary,
      h.after.primary,
    ];
    h.input.expectedNextChildren = [...h.input.nextChildren];

    expect(apply(h.input)).toEqual({ status: "applied", state: h.nextState });
    expect(h.root.contains(h.anchor.primary)).toBe(false);
    expect(h.root.contains(replacement.primary)).toBe(true);
    const newOffset = replacementDocumentTop - 80 - h.root.getBoundingClientRect().top;
    expect(h.writes).toEqual([80 + newOffset - 40]);
  });

  it("defers ambiguous anchor replacement and never permits ordinary trim to remove the anchor", async () => {
    const apply = await transactionApi();
    const replacement = fixture();
    replacement.input.plan.materializeKeys = ["anchor"];
    replacement.input.plan.trimKeys = ["anchor"];
    replacement.input.stagedBlocks = new Map([["anchor", block("anchor", () => 200)]]);
    replacement.input.resolvePrimaryByKey = () => null;
    expect(apply(replacement.input)).toMatchObject({ status: "deferred" });
    expectOriginal(replacement);

    const trim = fixture();
    trim.input.plan.materializeKeys = [];
    trim.input.plan.trimKeys = ["anchor"];
    trim.input.stagedBlocks = new Map();
    expect(apply(trim.input)).toMatchObject({ status: "deferred" });
    expectOriginal(trim);
  });


  it.each([
    ["map key differs from block key", (h: ReturnType<typeof fixture>) => {
      h.input.existingBlocks = new Map(h.input.existingBlocks).set(
        "anchor", { ...h.anchor, key: "wrong" },
      );
    }],
    ["primary is not the first root", (h: ReturnType<typeof fixture>) => {
      h.input.existingBlocks = new Map(h.input.existingBlocks).set("anchor", {
        ...h.anchor, roots: [h.anchor.primary, h.oldSpacer.element], primary: h.oldSpacer.element,
      });
    }],
    ["roots are duplicated", (h: ReturnType<typeof fixture>) => {
      h.input.existingBlocks = new Map(h.input.existingBlocks).set("anchor", {
        ...h.anchor, roots: [h.anchor.primary, h.anchor.primary],
      });
    }],
    ["roots are not in source order and contiguous", (h: ReturnType<typeof fixture>) => {
      h.input.existingBlocks = new Map(h.input.existingBlocks).set("anchor", {
        ...h.anchor, roots: [h.anchor.primary, h.before.primary],
      });
    }],
    ["two blocks share a root", (h: ReturnType<typeof fixture>) => {
      h.input.existingBlocks = new Map(h.input.existingBlocks).set("anchor", {
        ...h.anchor, roots: [h.anchor.primary, h.after.primary],
      });
    }],
  ])("validates every existing block: %s", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    corrupt(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  const continuationFixture = (continuation: HTMLElement, external: boolean) => {
    const root = document.createElement("div");
    const primary = block("primary", () => 20);
    primary.primary.dataset.reconcileRootCount = "2";
    root.append(primary.primary, continuation);
    const sourceChildren = Array.from(root.children);
    const currentState: WindowState = {
      generation: 1,
      attachedKeys: new Set(["primary"]),
      heights: new Map([["primary", 40]]),
    };
    const nextState: WindowState = {
      generation: 2,
      attachedKeys: new Set(["primary"]),
      heights: new Map(currentState.heights),
    };
    const plan = planner();
    plan.trimKeys = [];
    plan.materializeKeys = [];
    plan.nextAttachedKeys = new Set(["primary"]);
    plan.spacerSegments = [];
    const nextChildren = [...sourceChildren];
    const beforeMutation = vi.fn();
    const input: TransactionInput = {
      root,
      sourceChildren,
      externalSourceChildren: external ? new Set([continuation]) : new Set(),
      nextChildren,
      expectedNextChildren: nextChildren,
      existingBlocks: new Map([["primary", {
        ...primary, roots: [primary.primary, continuation],
      }]]),
      plan,
      stagedBlocks: new Map(),
      spacers: [],
      anchorJournal: null,
      expectedInteractionGeneration: 0,
      expectedWindowGeneration: 1,
      validateInteractionGeneration: (value) => value === 0,
      validateWindowGeneration: (value) => value === 1,
      measureBlock: () => 40,
      writeScrollTop: () => undefined,
      resolvePrimaryByKey: () => null,
      currentState,
      nextState,
      beforeMutation,
    };
    return { root, sourceChildren, nextChildren, input, nextState, beforeMutation };
  };

  it.each([
    ["pending-local", "div"],
    ["retained external", "section"],
  ])("rejects a Phase 1 %s identity falsely claimed as a continuation root", async (_name, tag) => {
    const apply = await transactionApi();
    const external = document.createElement(tag);
    const h = continuationFixture(external, true);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(h.beforeMutation).not.toHaveBeenCalled();
    expect(Array.from(h.root.children)).toEqual(h.sourceChildren);
    h.sourceChildren.forEach((child, index) => expect(h.root.children[index]).toBe(child));
  });

  it("allows a genuine contiguous multi-root continuation absent from external identities", async () => {
    const apply = await transactionApi();
    const h = continuationFixture(document.createElement("div"), false);

    expect(apply(h.input)).toEqual({ status: "applied", state: h.nextState });
    expect(Array.from(h.root.children)).toEqual(h.nextChildren);
  });

  it.each([
    ["missing", (h: ReturnType<typeof fixture>) => {
      h.input.spacers = [];
      h.input.nextChildren = h.input.nextChildren.filter((child) => child !== h.nextSpacer.element);
    }],
    ["extra", (h: ReturnType<typeof fixture>) => {
      const extra = spacer(["extra"], 10);
      h.input.spacers = [...h.input.spacers, extra];
      h.input.nextChildren = [...h.input.nextChildren, extra.element];
    }],
    ["duplicate", (h: ReturnType<typeof fixture>) => {
      const duplicate = spacer(["before", "after"], 100);
      h.input.spacers = [...h.input.spacers, duplicate];
      h.input.nextChildren = [...h.input.nextChildren, duplicate.element];
    }],
    ["tampered segment", (h: ReturnType<typeof fixture>) => {
      h.input.spacers = [{
        element: h.nextSpacer.element,
        segment: { ...h.nextSpacer.segment, cssHeightPx: 101 },
      }];
    }],
  ])("defers a %s spacer mapping instead of diverging from the plan", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    corrupt(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it.each([
    ["another connected parent", () => document.createElement("section")],
    ["a DocumentFragment staging parent", () => document.createDocumentFragment()],
  ])("defers a staged root owned by %s before mutation and preserves its parent order", async (_name, createParent) => {
    const apply = await transactionApi();
    const h = fixture();
    const parent = createParent() as HTMLElement | DocumentFragment;
    const before = document.createElement("i");
    const after = document.createElement("i");
    parent.append(before, h.newBlock.primary, after);
    if (parent instanceof HTMLElement) document.body.append(parent);
    const originalParentChildren = Array.from(parent.childNodes);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
    expect(h.newBlock.primary.parentNode).toBe(parent);
    expect(Array.from(parent.childNodes)).toEqual(originalParentChildren);
    originalParentChildren.forEach((child, index) => expect(parent.childNodes[index]).toBe(child));
    if (parent instanceof HTMLElement) parent.remove();
  });

  it("applies when every staged root has parentNode null", async () => {
    const apply = await transactionApi();
    const h = fixture();

    expect(h.newBlock.primary.parentNode).toBeNull();
    expect(apply(h.input)).toEqual({ status: "applied", state: h.nextState });
    expect(Array.from(h.root.children)).toEqual(h.input.nextChildren);
  });

  it("requires every staged root to have detached identity, including a root being trimmed", async () => {
    const apply = await transactionApi();
    const h = fixture();
    const reusedTrimmedRoot: StagedBlock = {
      key: "new", roots: [h.before.primary], primary: h.before.primary,
    };
    h.input.stagedBlocks = new Map([["new", reusedTrimmedRoot]]);
    h.input.nextChildren = h.input.nextChildren.map(
      (child) => child === h.newBlock.primary ? h.before.primary : child,
    );

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it.each([
    ["native button", (_element: HTMLElement) => {
      const button = document.createElement("button");
      button.className = "transcript-window-spacer";
      button.setAttribute("aria-hidden", "true");
      return button;
    }],
    ["missing class", (element: HTMLElement) => {
      element.classList.remove("transcript-window-spacer");
      return element;
    }],
    ["missing aria-hidden", (element: HTMLElement) => {
      element.removeAttribute("aria-hidden");
      return element;
    }],
    ["tabIndex=0", (element: HTMLElement) => {
      element.tabIndex = 0;
      return element;
    }],
    ["reconcile metadata", (element: HTMLElement) => {
      element.dataset.reconcileKey = "not-a-block";
      return element;
    }],
    ["itemId", (element: HTMLElement) => {
      element.dataset.itemId = "not-an-item";
      return element;
    }],
  ])("defers a spacer with %s before mutation", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    const malformed = corrupt(h.nextSpacer.element);
    h.input.spacers = [{ element: malformed, segment: h.nextSpacer.segment }];
    h.input.nextChildren = h.input.nextChildren.map(
      (child) => child === h.nextSpacer.element ? malformed : child,
    );

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it.each([
    ["canonical roots", (h: ReturnType<typeof fixture>) => [
      h.anchor.primary, h.nextSpacer.element, h.newBlock.primary,
    ]],
    ["external root and spacer", (h: ReturnType<typeof fixture>) => {
      const external = document.createElement("aside");
      h.root.append(external);
      h.input.sourceChildren = Array.from(h.root.children);
      h.input.externalSourceChildren = new Set([external]);
      h.input.nextChildren = [h.newBlock.primary, external, h.anchor.primary, h.nextSpacer.element];
      h.input.expectedNextChildren = [h.newBlock.primary, h.nextSpacer.element, h.anchor.primary, external];
      return h.input.nextChildren;
    }],
  ])("defers before mutation when nextChildren permutes %s against authoritative Phase 1 order", async (_name, permute) => {
    const apply = await transactionApi();
    const h = fixture();
    h.input.nextChildren = permute(h);
    const beforeMutation = vi.fn();
    h.input.beforeMutation = beforeMutation;

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(beforeMutation).not.toHaveBeenCalled();
    const observed = [...h.input.sourceChildren];
    expect(Array.from(h.root.children)).toEqual(observed);
    observed.forEach((child, index) => expect(h.root.children[index]).toBe(child));
    expect(h.root.scrollTop).toBe(80);
    expect(h.currentState.generation).toBe(7);
  });

  it("applies when nextChildren exactly matches the independent authoritative Phase 1 order", async () => {
    const apply = await transactionApi();
    const h = fixture();

    expect(h.input.nextChildren).not.toBe(h.input.expectedNextChildren);
    expect(h.input.nextChildren).toEqual(h.input.expectedNextChildren);
    expect(apply(h.input)).toEqual({ status: "applied", state: h.nextState });
  });

  it.each([
    ["existing map key differs from primary reconcileKey", (h: ReturnType<typeof fixture>) => {
      h.anchor.primary.dataset.reconcileKey = "dataset-wrong";
    }],
    ["existing rootCount is wrong", (h: ReturnType<typeof fixture>) => {
      h.anchor.primary.dataset.reconcileRootCount = "2";
    }],
    ["existing production fingerprint is missing", (h: ReturnType<typeof fixture>) => {
      delete h.anchor.primary.dataset.reconcileFingerprint;
    }],
    ["existing production shape is missing", (h: ReturnType<typeof fixture>) => {
      delete h.anchor.primary.dataset.reconcileShape;
    }],
    ["existing member metadata is malformed", (h: ReturnType<typeof fixture>) => {
      h.anchor.primary.dataset.reconcileMemberNodeIds = "not-json";
    }],
    ["staged primary reconcileKey differs from its map key", (h: ReturnType<typeof fixture>) => {
      h.newBlock.primary.dataset.reconcileKey = "staged-wrong";
    }],
    ["staged rootCount is wrong", (h: ReturnType<typeof fixture>) => {
      h.newBlock.primary.dataset.reconcileRootCount = "2";
    }],
  ])("defers incomplete logical block metadata: %s", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    const beforeMutation = vi.fn();
    h.input.beforeMutation = beforeMutation;
    corrupt(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expect(beforeMutation).not.toHaveBeenCalled();
    expectOriginal(h);
  });

  it.each([
    ["ghost attached key", (h: ReturnType<typeof fixture>) => {
      h.nextState.attachedKeys = new Set([...h.input.plan.nextAttachedKeys, "ghost"]);
    }],
    ["same generation", (h: ReturnType<typeof fixture>) => {
      h.nextState.generation = h.input.expectedWindowGeneration;
    }],
  ])("defers a next state that is not the exact plan successor: %s", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    corrupt(h);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it.each([
    ["malformed JSON", (element: HTMLElement) => { element.dataset.transcriptSpacer = "{"; }],
    ["missing class", (element: HTMLElement) => { element.classList.remove("transcript-window-spacer"); }],
    ["missing aria-hidden", (element: HTMLElement) => { element.removeAttribute("aria-hidden"); }],
    ["focusable", (element: HTMLElement) => { element.tabIndex = 0; }],
    ["reconciliation metadata", (element: HTMLElement) => { element.dataset.reconcileKey = "ghost"; }],
    ["item metadata", (element: HTMLElement) => { element.dataset.itemId = "ghost"; }],
    ["zero canonical extent and height", (element: HTMLElement) => {
      element.dataset.transcriptSpacer = JSON.stringify({
        startIndex: 0,
        endIndex: 1,
        omittedKeys: ["omitted"],
        canonicalStartPx: 0,
        canonicalEndPx: 0,
        cssHeightPx: 0,
      });
    }],
  ])("strictly validates a source spacer with %s", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    corrupt(h.oldSpacer.element);

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    const observed = [...h.sourceChildren];
    expect(Array.from(h.root.children)).toEqual(observed);
    observed.forEach((child, index) => expect(h.root.children[index]).toBe(child));
    expect(h.root.scrollTop).toBe(80);
    expect(h.currentState.generation).toBe(7);
  });

  it.each([
    ["negative startIndex", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, startIndex: -1 })],
    ["non-increasing range", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, endIndex: segment.startIndex })],
    ["key-count mismatch", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, endIndex: segment.endIndex + 1 })],
    ["duplicate omitted key", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, omittedKeys: ["before", "before"] })],
    ["non-finite coordinate", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, canonicalEndPx: Number.POSITIVE_INFINITY })],
    ["negative coordinate", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, canonicalStartPx: -1 })],
    ["negative height", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, cssHeightPx: -1 })],
    ["zero canonical extent", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({
      ...segment,
      canonicalEndPx: segment.canonicalStartPx,
    })],
    ["zero height", (segment: ReturnType<typeof fixture>["nextSpacer"]["segment"]) => ({ ...segment, cssHeightPx: 0 })],
  ])("defers an invalid new spacer/plan segment: %s", async (_name, corrupt) => {
    const apply = await transactionApi();
    const h = fixture();
    const invalid = corrupt(h.nextSpacer.segment);
    h.input.spacers = [{ element: h.nextSpacer.element, segment: invalid }];
    h.input.plan.spacerSegments = [invalid];

    expect(apply(h.input)).toMatchObject({ status: "deferred" });
    expectOriginal(h);
  });

  it.each([
    ["absorbed positive extent", Number.MAX_SAFE_INTEGER, 0, Number.MIN_VALUE],
    ["extent overflow", Number.MAX_VALUE, 0, Number.MAX_VALUE],
    ["row-gap overflow", Number.MAX_VALUE, Number.MAX_VALUE, 1],
  ])("safely attaches all canonical blocks when layout coordinates are unavailable: %s", (
    _name,
    firstExtent,
    rowGapPx,
    secondExtent,
  ) => {
    const extreme = planner({
      entries: [
        { kind: "canonical", key: "huge", descriptorIndex: 0, extentPx: firstExtent },
        { kind: "canonical", key: "tiny", descriptorIndex: 1, extentPx: secondExtent },
      ],
      attachedKeys: new Set(["huge"]),
      pinnedKeys: new Set(["huge"]),
      rowGapPx,
      viewport: { scrollTop: 0, clientHeight: 10, following: false },
      budget: { ...budget, overscanViewportsBefore: 0, overscanViewportsAfter: 0 },
    });

    expect(extreme).toMatchObject({
      materializeKeys: ["tiny"],
      trimKeys: [],
      nextAttachedKeys: new Set(["huge", "tiny"]),
      spacerSegments: [],
      overBudgetReason: "layout extent unavailable",
    });
  });
    describe("sameSpacerSegment / sameSpacerSegments", () => {
        it("recognizes identical spacer segments and tolerates subpixel height differences", async () => {
            const { sameSpacerSegment, sameSpacerSegments } = await import(
                "../../src/utils/transcript-dom-window"
            );
            const seg1 = {
                startIndex: 0,
                endIndex: 5,
                omittedKeys: ["a", "b", "c", "d", "e"],
                canonicalStartPx: 0,
                canonicalEndPx: 480,
                cssHeightPx: 480,
            };
            const seg2 = {
                startIndex: 0,
                endIndex: 5,
                omittedKeys: ["a", "b", "c", "d", "e"],
                canonicalStartPx: 0,
                canonicalEndPx: 480,
                cssHeightPx: 480.2, // subpixel difference < 0.5px
            };
            const seg3 = {
                startIndex: 0,
                endIndex: 5,
                omittedKeys: ["a", "b", "c", "d", "e"],
                canonicalStartPx: 0,
                canonicalEndPx: 480,
                cssHeightPx: 481, // >= 0.5px difference
            };

            expect(sameSpacerSegment(seg1, seg2)).toBe(true);
            expect(sameSpacerSegment(seg1, seg3)).toBe(false);
            expect(sameSpacerSegments([seg1], [seg2])).toBe(true);
            expect(sameSpacerSegments([seg1], [seg3])).toBe(false);
            expect(sameSpacerSegments([seg1], [])).toBe(false);
        });
    });
});
