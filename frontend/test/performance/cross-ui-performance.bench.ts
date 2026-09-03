import { bench, describe, expect } from "vitest";
import type { TranscriptNode } from "../../src/rpc/protocol";
import {
    createStreamingMarkdownProjection,
    renderMarkdown,
} from "../../src/utils/markdown";
import {
    createCanonicalMarkdownCoordinator,
    type CanonicalCommitOutcome,
    type CanonicalWorkerLike,
} from "../../src/utils/markdown-worker-client";
import type {
    CanonicalRenderRequest,
    CanonicalRenderResponse,
} from "../../src/utils/markdown-worker-protocol";
import { handleCanonicalRenderRequest } from "../../src/utils/markdown.worker";
import type { TranscriptSnapshot } from "../../src/utils/render-types";
import {
    DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET,
    mergeCanonicalTranscript,
    planTranscriptDomWindow,
    type TranscriptLayoutEntry,
} from "../../src/utils/transcript-dom-window";
import { buildTranscriptDescriptors } from "../../src/utils/transcript-reconciliation";

const BENCH_OPTIONS = {
    iterations: 2,
    time: 100,
    warmupIterations: 1,
    warmupTime: 10,
};
const PAYLOAD_BYTES = 50_000;
const STREAM_CHUNK_SIZE = 1_000;
const STREAM_TEXT = "streaming markdown payload ".repeat(2_000).slice(0, PAYLOAD_BYTES);
const STREAM_CHUNKS = Array.from(
    { length: PAYLOAD_BYTES / STREAM_CHUNK_SIZE },
    (_, index) => STREAM_TEXT.slice(index * STREAM_CHUNK_SIZE, (index + 1) * STREAM_CHUNK_SIZE),
);
const CANONICAL_TEXT = "canonical worker payload ".repeat(2_000).slice(0, PAYLOAD_BYTES);
const WORKER_REQUEST: CanonicalRenderRequest = {
    type: "render",
    jobId: 1,
    itemId: "bench-canonical",
    revision: 1,
    generation: 1,
    canonicalText: CANONICAL_TEXT,
};

function transcriptNode(id: string, text = id): TranscriptNode {
    return {
        id,
        node_type: "turn",
        payload: { text },
    } as TranscriptNode;
}

const TRANSCRIPT_NODES = Array.from(
    { length: 10_000 },
    (_, index) => transcriptNode(`turn-${index}`),
);
const PINNED_KEY = "node:turn-9000";
const PLANNER_VIEWPORT = {
    scrollTop: 500_000,
    clientHeight: 1_000,
    following: false,
};

function planTenThousandTranscripts() {
    const descriptors = buildTranscriptDescriptors(TRANSCRIPT_NODES);
    const entries: TranscriptLayoutEntry[] = descriptors.map((descriptor, descriptorIndex) => ({
        kind: "canonical",
        key: descriptor.key,
        descriptorIndex,
        extentPx: 100,
    }));
    return planTranscriptDomWindow({
        entries,
        attachedKeys: new Set<string>(),
        pinnedKeys: new Set([PINNED_KEY]),
        rowGapPx: 0,
        budget: DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET,
        viewport: PLANNER_VIEWPORT,
        activation: "initial",
        anchorKey: null,
    });
}

const ALL_TURNS: TranscriptSnapshot = {
    thread_id: "bench-thread",
    revision: 1,
    windowed: true,
    before_turn_id: 0,
    after_turn_id: 199,
    has_earlier: false,
    has_later: false,
    nodes: Array.from({ length: 200 }, (_, index) => transcriptNode(`merge-turn-${index}`)),
};
const RECENT_FORTY_TURNS: TranscriptSnapshot = {
    ...ALL_TURNS,
    revision: 2,
    before_turn_id: 160,
    nodes: Array.from(
        { length: 40 },
        (_, index) => transcriptNode(`merge-turn-${index + 160}`, `updated-${index + 160}`),
    ),
};

function mergeRecentFortyTurns() {
    return mergeCanonicalTranscript(ALL_TURNS, RECENT_FORTY_TURNS, "windowed");
}

class ProductionHandlerWorker implements CanonicalWorkerLike {
    private readonly messageListeners = new Set<
        (event: MessageEvent<CanonicalRenderResponse>) => void
    >();

    postMessage(message: CanonicalRenderRequest): void {
        const event = {
            data: handleCanonicalRenderRequest(message),
        } as MessageEvent<CanonicalRenderResponse>;
        for (const listener of this.messageListeners) listener(event);
    }

    addEventListener(
        type: "message" | "error",
        listener:
            | ((event: MessageEvent<CanonicalRenderResponse>) => void)
            | ((event: Event) => void),
    ): void {
        if (type === "message") {
            this.messageListeners.add(
                listener as (event: MessageEvent<CanonicalRenderResponse>) => void,
            );
        }
    }
}

function installCanonicalOnMainThread() {
    const worker = new ProductionHandlerWorker();
    const frames: FrameRequestCallback[] = [];
    const target = document.createElement("div");
    let outcome: CanonicalCommitOutcome | undefined;
    const coordinator = createCanonicalMarkdownCoordinator({
        workerFactory: () => worker,
        scheduleFrame(callback) {
            frames.push(callback);
            return frames.length;
        },
        now: () => 0,
    });

    coordinator.start({
        itemId: "bench-install",
        revision: 1,
        generation: 1,
        canonicalText: CANONICAL_TEXT,
        target,
        isCurrent: () => true,
        onSettled(value) {
            outcome = value;
        },
    });
    while (frames.length > 0) {
        frames.shift()!(0);
    }
    return { target, outcome, pendingFrames: frames.length };
}

function appendStreamingProjection() {
    const target = document.createElement("div");
    const projection = createStreamingMarkdownProjection(target);
    for (const chunk of STREAM_CHUNKS) projection.update(chunk, "append");
    return { target, snapshot: projection._debugSnapshotForTest?.() };
}

// Keep semantic checks out of timed loops while proving each benchmark fixture
// exercises the intended production path.
const streamingCheck = appendStreamingProjection();
expect(streamingCheck.snapshot?.rawTextLength).toBe(PAYLOAD_BYTES);
expect(streamingCheck.snapshot?.mutableTailLength).toBeLessThanOrEqual(16_384);

const workerCheck = handleCanonicalRenderRequest(WORKER_REQUEST);
expect(workerCheck.type).toBe("rendered");
if (workerCheck.type !== "rendered") throw new Error("canonical worker preflight failed");
expect(workerCheck.blocks.length).toBeGreaterThan(0);
expect(workerCheck.blocks[workerCheck.blocks.length - 1]?.sourceEnd).toBe(PAYLOAD_BYTES);

const installCheck = installCanonicalOnMainThread();
expect(installCheck.outcome).toEqual({ status: "installed" });
expect(installCheck.target.innerHTML).toBe(renderMarkdown(CANONICAL_TEXT).innerHTML);
expect(installCheck.target.dataset.renderPending).toBeUndefined();
expect(installCheck.pendingFrames).toBe(0);

const plannerCheck = planTenThousandTranscripts();
expect(plannerCheck.nextAttachedKeys.size).toBeLessThanOrEqual(
    DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET.maxAttachedUnpinnedBlocks,
);
expect(plannerCheck.nextAttachedKeys.has(PINNED_KEY)).toBe(true);

const mergeCheck = mergeRecentFortyTurns();
expect(mergeCheck.status).toBe("merged");
if (mergeCheck.status !== "merged") throw new Error("recent merge preflight failed");
expect(mergeCheck.descriptors).toHaveLength(200);
expect(mergeCheck.snapshot.nodes[mergeCheck.snapshot.nodes.length - 1]?.payload)
    .toEqual({ text: "updated-199" });

console.log(`VOIDX_BENCH_META=${JSON.stringify({
    node_version: (globalThis as { process?: { version?: string } }).process?.version ?? "unknown",
    payload_bytes: PAYLOAD_BYTES,
    dom_count: installCheck.target.childNodes.length,
    mutable_tail: streamingCheck.snapshot?.mutableTailLength ?? -1,
    worker: workerCheck.type === "rendered",
    install: installCheck.outcome?.status === "installed",
})}`);

describe("cross-ui production composition", () => {
    bench("stream: 50k incremental projection (mutable tail <= 16KiB)", () => {
        appendStreamingProjection();
    }, BENCH_OPTIONS);

    bench("canonical: worker render production path", () => {
        handleCanonicalRenderRequest(WORKER_REQUEST);
    }, BENCH_OPTIONS);

    bench("canonical: main-thread install production path", () => {
        installCanonicalOnMainThread();
    }, BENCH_OPTIONS);

    bench("window: 10k transcript planner mounted count + pinned", () => {
        planTenThousandTranscripts();
    }, BENCH_OPTIONS);

    bench("window: recent 40-turn canonical merge", () => {
        mergeRecentFortyTurns();
    }, BENCH_OPTIONS);
});
