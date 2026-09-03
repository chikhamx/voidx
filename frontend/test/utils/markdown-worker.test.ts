import { describe, expect, it } from "vitest";
import { sha256 } from "../../src/utils/sha256";
import {
  CANONICAL_RAW_HTML_BLOCK_MAX_CHARS,
  renderCanonicalMarkdownBlocks,
} from "../../src/utils/markdown-renderer";
import { handleCanonicalRenderRequest } from "../../src/utils/markdown.worker";
import { renderMarkdown } from "../../src/utils/markdown";
import {
  createCanonicalMarkdownCoordinator,
  type CanonicalWorkerLike,
} from "../../src/utils/markdown-worker-client";
import type {
  CanonicalRenderRequest,
  CanonicalRenderResponse,
} from "../../src/utils/markdown-worker-protocol";

describe("synchronous UTF-8 SHA-256", () => {
    it("matches known ASCII and Unicode vectors", () => {
        expect(sha256("abc")).toBe(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        );
        expect(sha256("你好🌍")).toBe(
            "25feb68e8651a1e87d13b2a93c080d75e40174800e19388820c27be17009cd66",
        );
    });
});

function htmlFrom(response: CanonicalRenderResponse): string {
  if (response.type !== "rendered") {
    throw new Error(`expected rendered response, got ${response.type}`);
  }
  return response.blocks
    .map((block) => block.kind === "html" ? block.html : block.text)
    .join("");
}

describe("canonical Markdown Worker renderer", () => {
  it("renders top-level blocks while preserving cross-block reference links", () => {
    const source = [
      "# Result",
      "",
      "Read [the guide][guide].",
      "",
      "[guide]: https://example.com/guide \"Guide\"",
      "",
    ].join("\n");

    const blocks = renderCanonicalMarkdownBlocks(source);
    const html = blocks
      .map((block) => block.kind === "html" ? block.html : block.text)
      .join("");

    expect(blocks.filter((block) => block.kind === "html").length)
      .toBeGreaterThan(1);
    expect(html).toContain("<h1>Result</h1>");
    expect(html).toContain(
      '<a href="https://example.com/guide" title="Guide">the guide</a>',
    );
  });

  it("highlights fenced code before returning the Worker block", () => {
    const blocks = renderCanonicalMarkdownBlocks(
      "```python\nprint('hello')\n```\n",
    );
    const html = blocks
      .map((block) => block.kind === "html" ? block.html : block.text)
      .join("");

    expect(html).toContain("language-python");
    expect(html).toContain("hljs-");
    expect(html).toContain("print");
  });

  it("returns an escaped-text descriptor for an oversized raw HTML block", () => {
    const source = `<div>${"x".repeat(CANONICAL_RAW_HTML_BLOCK_MAX_CHARS)}</div>\n`;

    const blocks = renderCanonicalMarkdownBlocks(source);

    expect(blocks).toEqual([
      {
        kind: "text",
        text: source,
        reason: "html_block_budget",
            sourceStart: 0,
            sourceEnd: source.length,
            sourceHash: sha256(source),
      },
    ]);
  });
});

describe("canonical Markdown Worker protocol", () => {
  it("echoes request identity on a successful render", () => {
    const request: CanonicalRenderRequest = {
      type: "render",
      jobId: 7,
      itemId: "assistant-1",
      revision: 3,
      generation: 2,
      canonicalText: "safe **bold**",
    };

    const response = handleCanonicalRenderRequest(request);

    expect(response).toMatchObject({
      type: "rendered",
      jobId: 7,
      itemId: "assistant-1",
      revision: 3,
      generation: 2,
    });
    expect(htmlFrom(response)).toContain("<strong>bold</strong>");
  });

  it("returns an identity-preserving failure for malformed requests", () => {
    const response = handleCanonicalRenderRequest({
      type: "render",
      jobId: 9,
      itemId: "assistant-2",
      revision: 4,
      generation: 5,
      canonicalText: null,
    } as unknown as CanonicalRenderRequest);

    expect(response).toEqual({
      type: "failed",
      jobId: 9,
      itemId: "assistant-2",
      revision: 4,
      generation: 5,
      reason: "worker_protocol",
    });
  });
});


class FakeCanonicalWorker implements CanonicalWorkerLike {
  readonly sent: CanonicalRenderRequest[] = [];
  private readonly messageListeners = new Set<
    (event: MessageEvent<CanonicalRenderResponse>) => void
  >();
  private readonly errorListeners = new Set<(event: Event) => void>();

  postMessage(message: CanonicalRenderRequest): void {
    this.sent.push(message);
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
    } else {
      this.errorListeners.add(listener as (event: Event) => void);
    }
  }

  emit(response: CanonicalRenderResponse): void {
    const event = { data: response } as MessageEvent<CanonicalRenderResponse>;
    for (const listener of this.messageListeners) listener(event);
  }

  fail(): void {
    for (const listener of this.errorListeners) listener(new Event("error"));
  }
}

function createFrameHarness(times: number[] = []): {
  scheduleFrame: (callback: FrameRequestCallback) => number;
  now: () => number;
  flushFrame: () => void;
  pendingFrames: () => number;
} {
  const frames: FrameRequestCallback[] = [];
  let fallbackTime = 0;
  return {
    scheduleFrame(callback) {
      frames.push(callback);
      return frames.length;
    },
    now() {
      return times.shift() ?? fallbackTime++;
    },
    flushFrame() {
      const callback = frames.shift();
      if (!callback) throw new Error("no pending frame");
      callback(0);
    },
    pendingFrames() {
      return frames.length;
    },
  };
}

function renderedResponse(
  request: CanonicalRenderRequest,
    blocks: Array<
        | {
            kind: "html";
            html: string;
            sourceLength: number;
            sourceStart?: number;
            sourceEnd?: number;
            sourceHash?: string;
        }
        | {
            kind: "text";
            text: string;
            reason: "html_block_budget";
            sourceStart?: number;
            sourceEnd?: number;
            sourceHash?: string;
        }
    >,
): CanonicalRenderResponse {
    let sourceOffset = 0;
    const sourceBoundBlocks = blocks.map((block) => {
        const sourceLength = block.kind === "html"
            ? block.sourceLength
            : block.text.length;
        const sourceStart = sourceOffset;
        const sourceEnd = sourceStart + sourceLength;
        sourceOffset = sourceEnd;
        return {
            sourceStart,
            sourceEnd,
            sourceHash: sha256(request.canonicalText.slice(sourceStart, sourceEnd)),
            ...block,
        };
    });
    return {
        type: "rendered",
        jobId: request.jobId,
        itemId: request.itemId,
        revision: request.revision,
        generation: request.generation,
        blocks: sourceBoundBlocks,
  };
}

type SourceBoundBlockFixture =
    | {
        kind: "html";
        html: string;
        sourceLength: number;
        sourceStart: number;
        sourceEnd: number;
        sourceHash: string;
    }
    | {
        kind: "text";
        text: string;
        reason: "html_block_budget";
        sourceStart: number;
        sourceEnd: number;
        sourceHash: string;
    };

function sourceBoundRenderedResponse(
    request: CanonicalRenderRequest,
    blocks: SourceBoundBlockFixture[],
): CanonicalRenderResponse {
    return renderedResponse(
        request,
        blocks as unknown as Parameters<typeof renderedResponse>[1],
    );
}

const SOURCE_BINDING_CANONICAL_TEXT = "first\n\nsecond";
const SOURCE_BINDING_CASES: Array<{
    name: string;
    blocks: SourceBoundBlockFixture[];
}> = [
        {
            name: "out-of-order ranges",
            blocks: [
                {
                    kind: "html",
                    html: "<p>second</p>",
                    sourceLength: 6,
                    sourceStart: 7,
                    sourceEnd: 13,
                    sourceHash: "hash-of-second",
                },
                {
                    kind: "html",
                    html: "<p>first</p>",
                    sourceLength: 7,
                    sourceStart: 0,
                    sourceEnd: 7,
                    sourceHash: "hash-of-first-separator",
                },
            ],
        },
        {
            name: "repeated source ranges",
            blocks: [
                {
                    kind: "html",
                    html: "<p>first</p>",
                    sourceLength: 7,
                    sourceStart: 0,
                    sourceEnd: 7,
                    sourceHash: "hash-of-first-separator",
                },
                {
                    kind: "html",
                    html: "<p>first</p>",
                    sourceLength: 6,
                    sourceStart: 0,
                    sourceEnd: 6,
                    sourceHash: "hash-of-repeated-first",
                },
            ],
        },
        {
            name: "HTML bound to different source",
            blocks: [
                {
                    kind: "html",
                    html: "<p>tampered</p>",
                    sourceLength: 7,
                    sourceStart: 0,
                    sourceEnd: 7,
                    sourceHash: "hash-of-tampered-html-source",
                },
                {
                    kind: "html",
                    html: "<p>second</p>",
                    sourceLength: 6,
                    sourceStart: 7,
                    sourceEnd: 13,
                    sourceHash: "hash-of-second",
                },
            ],
        },
        {
            name: "text bound to different source",
            blocks: [
                {
                    kind: "html",
                    html: "<p>first</p>",
                    sourceLength: 7,
                    sourceStart: 0,
                    sourceEnd: 7,
                    sourceHash: "hash-of-first-separator",
                },
                {
                    kind: "text",
                    text: "xxxxxx",
                    reason: "html_block_budget",
                    sourceStart: 7,
                    sourceEnd: 13,
                    sourceHash: "hash-of-xxxxxx",
                },
            ],
        },
    ];

describe("canonical Markdown main-thread coordinator", () => {
  it("keeps the preview visible while staging blocks across budgeted frames", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness([0, 9, 10, 19, 20, 21]);
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    target.textContent = "provisional preview";
    const preview = target.firstChild;

    coordinator.start({
      itemId: "assistant-budget",
      revision: 1,
      generation: 1,
      canonicalText: "first\n\nsecond",
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [
      { kind: "html", html: "<p>first</p>", sourceLength: 7 },
      { kind: "html", html: "<p>second</p>", sourceLength: 6 },
    ]));

    expect(target.firstChild).toBe(preview);
    expect(target.dataset.renderPending).toBe("true");
    expect(frames.pendingFrames()).toBe(1);

    frames.flushFrame();
    expect(target.firstChild).toBe(preview);
    expect(target.textContent).toBe("provisional preview");
    expect(frames.pendingFrames()).toBe(1);

    frames.flushFrame();
    expect(target.firstChild).toBe(preview);
    expect(target.textContent).toBe("provisional preview");
    expect(frames.pendingFrames()).toBe(1);

    frames.flushFrame();
    expect(target.firstChild).not.toBe(preview);
    expect(target.innerHTML).toBe("<p>first</p><p>second</p>");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("sanitizes every Worker HTML block before atomic installation", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    target.textContent = "preview";

    coordinator.start({
      itemId: "assistant-safe",
      revision: 2,
      generation: 1,
      canonicalText: "safe",
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [{
      kind: "html",
      html: '<p>safe<img src="x" onerror="window.pwned=true"></p><script>bad()</script>',
      sourceLength: 4,
    }]));
    frames.flushFrame();

    expect(target.textContent).toBe("safe");
    expect(target.querySelector("script")).toBeNull();
    expect(target.querySelector("img[onerror]")).toBeNull();
  });

  it("installs oversized raw HTML descriptors only as escaped text", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    const source = '<div onclick="bad()">canonical</div>\n';

    coordinator.start({
      itemId: "assistant-html-budget",
      revision: 3,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [{
      kind: "text",
      text: source,
      reason: "html_block_budget",
    }]));
    frames.flushFrame();

    expect(target.textContent).toBe(source);
    expect(target.querySelector("div")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("html_block_budget");
  });

  it("falls back to the complete escaped canonical source on Worker failure", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
    });
    const target = document.createElement("div");
    target.innerHTML = "<strong>preview</strong>";
    const source = "safe **bold** <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-failure",
      revision: 4,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    worker.fail();

    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("worker_error");
    expect(target.dataset.renderPending).toBeUndefined();
  });


  it("falls back to complete escaped source when the Worker factory is unavailable", () => {
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => {
        throw new Error("Worker unavailable");
      },
    });
    const target = document.createElement("div");
    target.innerHTML = "<strong>preview</strong>";
    const source = "safe **bold** <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-unavailable",
      revision: 5,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });

    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("worker_unavailable");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("treats a response identity mismatch as a protocol failure", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
    });
    const target = document.createElement("div");
    const source = "canonical **source**";

    coordinator.start({
      itemId: "assistant-identity",
      revision: 6,
      generation: 3,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit({
      ...renderedResponse(request, []),
      generation: request.generation + 1,
    });

    expect(target.textContent).toBe(source);
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("rejects malformed block descriptors before staging them", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
    });
    const target = document.createElement("div");
    const source = "canonical <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-malformed-block",
      revision: 7,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit({
      type: "rendered",
      jobId: request.jobId,
      itemId: request.itemId,
      revision: request.revision,
      generation: request.generation,
      blocks: [{ kind: "html", html: 42, sourceLength: "invalid" }],
    } as unknown as CanonicalRenderResponse);

    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("falls back to complete escaped source when sanitization throws", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
      sanitize: () => {
        throw new Error("sanitize failed");
      },
    });
    const target = document.createElement("div");
    target.textContent = "preview";
    const source = "safe **bold** <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-sanitize-failure",
      revision: 8,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [{
      kind: "html",
      html: "<p>unsafe</p>",
      sourceLength: source.length,
    }]));
    frames.flushFrame();

    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("sanitize_error");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("uses a plain-text install path when canonical replacement throws", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    target.textContent = "preview";
    Object.defineProperty(target, "replaceChildren", {
      value: () => {
        throw new Error("replace failed");
      },
    });
    const source = "safe **bold** <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-install-failure",
      revision: 9,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [{
      kind: "html",
      html: "<p>canonical</p>",
      sourceLength: source.length,
    }]));

    expect(() => frames.flushFrame()).not.toThrow();
    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("install_error");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("installs a 50k+ canonical response equivalent to synchronous Markdown", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    target.textContent = "provisional preview";
    const preview = target.firstChild;
    const source = Array.from(
      { length: 700 },
      (_, index) => `Paragraph ${index}: ${"x".repeat(72)}`,
    ).join("\n\n");
    expect(source.length).toBeGreaterThan(50_000);

    coordinator.start({
      itemId: "assistant-large",
      revision: 10,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    expect(request.canonicalText).toBe(source);
    expect(target.firstChild).toBe(preview);

    worker.emit(handleCanonicalRenderRequest(request));
    expect(target.firstChild).toBe(preview);
    while (frames.pendingFrames() > 0) frames.flushFrame();

    expect(target.innerHTML).toBe(renderMarkdown(source).innerHTML);
    expect(target.dataset.renderPending).toBeUndefined();
    expect(target.dataset.canonicalFallback).toBeUndefined();
  });


  it("falls back a pending job when the Worker response has no valid job id", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
    });
    const target = document.createElement("div");
    target.textContent = "preview";
    const source = "canonical **source** <script>bad()</script>";

    coordinator.start({
      itemId: "assistant-missing-job",
      revision: 11,
      generation: 2,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit({
      type: "rendered",
      itemId: request.itemId,
      revision: request.revision,
      generation: request.generation,
      blocks: [],
    } as unknown as CanonicalRenderResponse);

    expect(target.textContent).toBe(source);
    expect(target.querySelector("script")).toBeNull();
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("does not let an invalidated queued frame settle twice or clear a new pending job", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    target.textContent = "preview";
    let oldSettles = 0;

    coordinator.start({
      itemId: "assistant-reused-target",
      revision: 1,
      generation: 1,
      canonicalText: "old",
      target,
      isCurrent: () => true,
      onSettled: () => {
        oldSettles += 1;
      },
    });
    worker.emit(renderedResponse(worker.sent[0], [{
      kind: "html",
      html: "<p>old</p>",
      sourceLength: 3,
    }]));

    coordinator.start({
      itemId: "assistant-reused-target",
      revision: 2,
      generation: 2,
      canonicalText: "new",
      target,
      isCurrent: () => true,
    });
    expect(oldSettles).toBe(1);
    expect(target.dataset.renderPending).toBe("true");

    frames.flushFrame();

    expect(oldSettles).toBe(1);
    expect(target.dataset.renderPending).toBe("true");

    worker.emit(renderedResponse(worker.sent[1], [{
      kind: "html",
      html: "<p>new</p>",
      sourceLength: 3,
    }]));
    frames.flushFrame();
    expect(target.innerHTML).toBe("<p>new</p>");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("rejects worker_block_fallback as a whole-item protocol failure", () => {
    const worker = new FakeCanonicalWorker();
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const target = document.createElement("div");
    const source = "complete **canonical** source";

    coordinator.start({
      itemId: "assistant-worker-block-fallback",
      revision: 12,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit({
      ...renderedResponse(request, []),
      blocks: [{
        kind: "text",
        text: "partial",
        reason: "worker_block_fallback",
      }],
    } as unknown as CanonicalRenderResponse);
    if (frames.pendingFrames() > 0) frames.flushFrame();

    expect(target.textContent).toBe(source);
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });


  it("treats a never-allocated integer job id from the current Worker as a protocol failure", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => worker,
    });
    const target = document.createElement("div");
    target.textContent = "preview";
    const source = "complete **canonical** source";

    coordinator.start({
      itemId: "assistant-unknown-job",
      revision: 13,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit({
      type: "rendered",
      jobId: request.jobId + 100,
      itemId: "wrong-item",
      revision: request.revision + 100,
      generation: request.generation + 100,
      blocks: [],
    });

    expect(target.textContent).toBe(source);
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("ignores late message and error events from a retired Worker", () => {
    const workers: FakeCanonicalWorker[] = [];
    const frames = createFrameHarness();
    const coordinator = createCanonicalMarkdownCoordinator({
      workerFactory: () => {
        const worker = new FakeCanonicalWorker();
        workers.push(worker);
        return worker;
      },
      scheduleFrame: frames.scheduleFrame,
      now: frames.now,
    });
    const oldTarget = document.createElement("div");
    const newTarget = document.createElement("div");
    oldTarget.textContent = "old preview";
    newTarget.textContent = "new preview";

    coordinator.start({
      itemId: "assistant-old-worker",
      revision: 1,
      generation: 1,
      canonicalText: "old source",
      target: oldTarget,
      isCurrent: () => true,
    });
    workers[0].emit(null as unknown as CanonicalRenderResponse);
    expect(oldTarget.dataset.canonicalFallback).toBe("worker_protocol");

    coordinator.start({
      itemId: "assistant-new-worker",
      revision: 1,
      generation: 2,
      canonicalText: "new source",
      target: newTarget,
      isCurrent: () => true,
    });
    expect(workers).toHaveLength(2);
    expect(newTarget.dataset.renderPending).toBe("true");

    workers[0].emit(null as unknown as CanonicalRenderResponse);
    workers[0].fail();

    expect(newTarget.textContent).toBe("new preview");
    expect(newTarget.dataset.canonicalFallback).toBeUndefined();
    expect(newTarget.dataset.renderPending).toBe("true");

    workers[1].emit(renderedResponse(workers[1].sent[0], [{
      kind: "html",
      html: "<p>new</p>",
      sourceLength: 10,
    }]));
    frames.flushFrame();

    expect(newTarget.innerHTML).toBe("<p>new</p>");
    expect(newTarget.dataset.renderPending).toBeUndefined();
  });


  it("falls back when a rendered response omits canonical source coverage", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({ workerFactory: () => worker });
    const target = document.createElement("div");
    const source = "first\n\nsecond";

    coordinator.start({
      itemId: "assistant-missing-block",
      revision: 14,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [{
      kind: "html",
      html: "<p>first</p>",
      sourceLength: 7,
    }]));

    expect(target.textContent).toBe(source);
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

  it("falls back when rendered blocks repeat canonical source coverage", () => {
    const worker = new FakeCanonicalWorker();
    const coordinator = createCanonicalMarkdownCoordinator({ workerFactory: () => worker });
    const target = document.createElement("div");
    const source = "first";

    coordinator.start({
      itemId: "assistant-repeated-block",
      revision: 15,
      generation: 1,
      canonicalText: source,
      target,
      isCurrent: () => true,
    });
    const request = worker.sent[0];
    worker.emit(renderedResponse(request, [
      { kind: "html", html: "<p>first</p>", sourceLength: 5 },
      { kind: "html", html: "<p>first</p>", sourceLength: 5 },
    ]));

    expect(target.textContent).toBe(source);
    expect(target.dataset.canonicalFallback).toBe("worker_protocol");
    expect(target.dataset.renderPending).toBeUndefined();
  });

    it.each(SOURCE_BINDING_CASES)(
        "falls back when source-bound descriptors have $name",
        ({ blocks }) => {
            const worker = new FakeCanonicalWorker();
            const frames = createFrameHarness();
            const coordinator = createCanonicalMarkdownCoordinator({
                workerFactory: () => worker,
                scheduleFrame: frames.scheduleFrame,
                now: frames.now,
            });
            const target = document.createElement("div");

            coordinator.start({
                itemId: "assistant-source-binding",
                revision: 16,
                generation: 1,
                canonicalText: SOURCE_BINDING_CANONICAL_TEXT,
                target,
                isCurrent: () => true,
            });
            worker.emit(sourceBoundRenderedResponse(worker.sent[0], blocks));
            while (frames.pendingFrames() > 0) frames.flushFrame();

            expect(target.textContent).toBe(SOURCE_BINDING_CANONICAL_TEXT);
            expect(target.innerHTML).not.toContain("<p>");
            expect(target.dataset.canonicalFallback).toBe("worker_protocol");
            expect(target.dataset.renderPending).toBeUndefined();
        },
    );
});
