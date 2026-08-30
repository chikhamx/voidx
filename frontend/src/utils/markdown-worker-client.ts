import DOMPurify from "dompurify";
import type {
  CanonicalBlockDescriptor,
  CanonicalRenderRequest,
  CanonicalRenderResponse,
} from "./markdown-worker-protocol";

export const CANONICAL_INSTALL_BUDGET_MS = 8;

export type CanonicalFallbackReason =
  | "worker_unavailable"
  | "worker_error"
  | "worker_parse"
  | "worker_protocol"
  | "sanitize_error"
  | "install_error"
  | "html_block_budget";

export interface CanonicalWorkerLike {
  postMessage(message: CanonicalRenderRequest): void;
  addEventListener(
    type: "message" | "error",
    listener:
      | ((event: MessageEvent<CanonicalRenderResponse>) => void)
      | ((event: Event) => void),
  ): void;
  terminate?(): void;
}

export interface CanonicalCommitWork {
  itemId: string;
  revision: number;
  generation: number;
  canonicalText: string;
  target: HTMLElement;
  isCurrent(): boolean;
  onSettled?(outcome: CanonicalCommitOutcome): void;
}

export interface CanonicalCommitOutcome {
  status: "installed" | "fallback" | "stale";
  reason?: CanonicalFallbackReason;
}

interface CoordinatorOptions {
  workerFactory?: () => CanonicalWorkerLike;
  scheduleFrame?: (callback: FrameRequestCallback) => number;
  now?: () => number;
  sanitize?: (html: string) => string;
}

interface PendingCommit {
  request: CanonicalRenderRequest;
  work: CanonicalCommitWork;
  blocks: CanonicalBlockDescriptor[] | null;
  blockIndex: number;
  staging: DocumentFragment;
  fallbackReason: CanonicalFallbackReason | null;
  frameScheduled: boolean;
}

export interface CanonicalMarkdownCoordinator {
  start(work: CanonicalCommitWork): number;
  invalidate(itemId: string): void;
  invalidateAll(): void;
}

export function createCanonicalMarkdownCoordinator(
  options: CoordinatorOptions = {},
): CanonicalMarkdownCoordinator {
  const workerFactory = options.workerFactory ?? defaultWorkerFactory;
  const scheduleFrame = options.scheduleFrame ?? defaultScheduleFrame;
  const now = options.now ?? defaultNow;
  const sanitize = options.sanitize ?? ((html: string) => (
    DOMPurify.sanitize(html) as unknown as string
  ));
  const pending = new Map<number, PendingCommit>();
  const jobsByItem = new Map<string, number>();
  let worker: CanonicalWorkerLike | null = null;
  let nextJobId = 1;

  const settle = (
    job: PendingCommit,
    outcome: CanonicalCommitOutcome,
  ): void => {
    if (pending.get(job.request.jobId) !== job) return;
    pending.delete(job.request.jobId);
    if (jobsByItem.get(job.work.itemId) === job.request.jobId) {
      jobsByItem.delete(job.work.itemId);
    }
    delete job.work.target.dataset.renderPending;
    job.work.onSettled?.(outcome);
  };

  const isCurrent = (job: PendingCommit): boolean => (
    pending.get(job.request.jobId) === job
    && jobsByItem.get(job.work.itemId) === job.request.jobId
    && job.work.isCurrent()
  );

  const discardStale = (job: PendingCommit): void => {
    settle(job, { status: "stale" });
  };

  const installFallback = (
    job: PendingCommit,
    reason: CanonicalFallbackReason,
  ): void => {
    if (!isCurrent(job)) {
      discardStale(job);
      return;
    }
    try {
      try {
        job.work.target.replaceChildren(
          document.createTextNode(job.work.canonicalText),
        );
      } catch {
        job.work.target.textContent = job.work.canonicalText;
      }
      job.work.target.dataset.canonicalFallback = reason;
    } finally {
      settle(job, { status: "fallback", reason });
    }
  };

  const installCanonical = (job: PendingCommit): void => {
    if (!isCurrent(job)) {
      discardStale(job);
      return;
    }
    try {
      job.work.target.replaceChildren(...Array.from(job.staging.childNodes));
      if (job.fallbackReason) {
        job.work.target.dataset.canonicalFallback = job.fallbackReason;
      } else {
        delete job.work.target.dataset.canonicalFallback;
      }
      settle(
        job,
        job.fallbackReason
          ? { status: "fallback", reason: job.fallbackReason }
          : { status: "installed" },
      );
    } catch {
      installFallback(job, "install_error");
    }
  };

  const appendBlock = (
    job: PendingCommit,
    block: CanonicalBlockDescriptor,
  ): boolean => {
    try {
      if (block.kind === "text") {
        job.staging.append(document.createTextNode(block.text));
        if (block.reason === "html_block_budget") {
          job.fallbackReason = "html_block_budget";
        }
        return true;
      }
      const template = document.createElement("template");
      template.innerHTML = sanitize(block.html);
      job.staging.append(template.content);
      return true;
    } catch {
      installFallback(job, "sanitize_error");
      return false;
    }
  };

  const scheduleInstallFrame = (job: PendingCommit): void => {
    if (job.frameScheduled || !isCurrent(job)) return;
    job.frameScheduled = true;
    scheduleFrame(() => {
      job.frameScheduled = false;
      if (!isCurrent(job)) {
        discardStale(job);
        return;
      }
      const blocks = job.blocks;
      if (!blocks) return;
      const startedAt = now();
      let budgetReached = false;
      while (job.blockIndex < blocks.length) {
        if (!appendBlock(job, blocks[job.blockIndex])) return;
        job.blockIndex += 1;
        if (now() - startedAt >= CANONICAL_INSTALL_BUDGET_MS) {
          budgetReached = true;
          break;
        }
      }
      if (job.blockIndex < blocks.length || budgetReached) {
        scheduleInstallFrame(job);
        return;
      }
      installCanonical(job);
    });
  };

  const failPendingProtocol = (sourceWorker: CanonicalWorkerLike): void => {
    if (worker !== sourceWorker) return;
    const jobs = [...pending.values()];
    sourceWorker.terminate?.();
    worker = null;
    for (const job of jobs) {
      if (pending.get(job.request.jobId) === job) {
        installFallback(job, "worker_protocol");
      }
    }
  };

  const handleMessage = (
    sourceWorker: CanonicalWorkerLike,
    event: MessageEvent<CanonicalRenderResponse>,
  ): void => {
    if (worker !== sourceWorker) return;
    const response = event.data;
    if (!response || typeof response !== "object") {
      failPendingProtocol(sourceWorker);
      return;
    }
    const responseJobId = (response as Partial<CanonicalRenderResponse>).jobId;
    const validJobId = Number.isInteger(responseJobId)
      ? responseJobId as number
      : null;
    const job = validJobId === null ? undefined : pending.get(validJobId);
    if (!job) {
      const wasAllocated = validJobId !== null
        && validJobId >= 1
        && validJobId < nextJobId;
      const matchesPendingIdentity = [...pending.values()].some((candidate) => (
        response.itemId === candidate.request.itemId
        && response.revision === candidate.request.revision
        && response.generation === candidate.request.generation
      ));
      if (!wasAllocated || matchesPendingIdentity) {
        failPendingProtocol(sourceWorker);
      }
      return;
    }
    if (
      response.itemId !== job.request.itemId
      || response.revision !== job.request.revision
      || response.generation !== job.request.generation
    ) {
      failPendingProtocol(sourceWorker);
      return;
    }
    if (response.type === "failed") {
      installFallback(
        job,
        response.reason === "worker_parse" || response.reason === "worker_protocol"
          ? response.reason
          : "worker_protocol",
      );
      return;
    }
    if (
      response.type !== "rendered"
      || !Array.isArray(response.blocks)
      || !response.blocks.every(isCanonicalBlockDescriptor)
      || !hasCompleteSourceCoverage(response.blocks, job.work.canonicalText)
    ) {
      failPendingProtocol(sourceWorker);
      return;
    }
    job.blocks = response.blocks;
    scheduleInstallFrame(job);
  };

  const handleWorkerError = (sourceWorker: CanonicalWorkerLike): void => {
    if (worker !== sourceWorker) return;
    const jobs = [...pending.values()];
    sourceWorker.terminate?.();
    worker = null;
    for (const job of jobs) {
      if (pending.get(job.request.jobId) === job) {
        installFallback(job, "worker_error");
      }
    }
  };

  const ensureWorker = (): CanonicalWorkerLike | null => {
    if (worker) return worker;
    try {
      const createdWorker = workerFactory();
      worker = createdWorker;
      createdWorker.addEventListener(
        "message",
        (event: MessageEvent<CanonicalRenderResponse>) => (
          handleMessage(createdWorker, event)
        ),
      );
      createdWorker.addEventListener(
        "error",
        () => handleWorkerError(createdWorker),
      );
      return createdWorker;
    } catch {
      worker = null;
      return null;
    }
  };

  const invalidateJob = (jobId: number): void => {
    const job = pending.get(jobId);
    if (!job) return;
    settle(job, { status: "stale" });
  };

  return {
    start(work): number {
      const existing = jobsByItem.get(work.itemId);
      if (existing !== undefined) invalidateJob(existing);

      const request: CanonicalRenderRequest = {
        type: "render",
        jobId: nextJobId++,
        itemId: work.itemId,
        revision: work.revision,
        generation: work.generation,
        canonicalText: work.canonicalText,
      };
      const job: PendingCommit = {
        request,
        work,
        blocks: null,
        blockIndex: 0,
        staging: document.createDocumentFragment(),
        fallbackReason: null,
        frameScheduled: false,
      };
      pending.set(request.jobId, job);
      jobsByItem.set(work.itemId, request.jobId);
      work.target.dataset.renderPending = "true";
      delete work.target.dataset.canonicalFallback;

      const activeWorker = ensureWorker();
      if (!activeWorker) {
        installFallback(job, "worker_unavailable");
        return request.jobId;
      }
      try {
        activeWorker.postMessage(request);
      } catch {
        installFallback(job, "worker_error");
      }
      return request.jobId;
    },

    invalidate(itemId): void {
      const jobId = jobsByItem.get(itemId);
      if (jobId !== undefined) invalidateJob(jobId);
    },

    invalidateAll(): void {
      for (const jobId of [...pending.keys()]) invalidateJob(jobId);
    },
  };
}

function isCanonicalBlockDescriptor(
  value: unknown,
): value is CanonicalBlockDescriptor {
  if (!value || typeof value !== "object") return false;
  const block = value as Partial<CanonicalBlockDescriptor>;
  if (block.kind === "html") {
    return typeof block.html === "string"
      && Number.isInteger(block.sourceLength)
      && (block.sourceLength as number) > 0;
  }
  if (block.kind === "text") {
    return typeof block.text === "string"
      && block.reason === "html_block_budget";
  }
  return false;
}


function hasCompleteSourceCoverage(
  blocks: CanonicalBlockDescriptor[],
  canonicalText: string,
): boolean {
  let coveredLength = 0;
  for (const block of blocks) {
    coveredLength += block.kind === "html"
      ? block.sourceLength
      : block.text.length;
    if (coveredLength > canonicalText.length) return false;
  }
  return coveredLength === canonicalText.length;
}
function defaultWorkerFactory(): CanonicalWorkerLike {
  return new Worker(new URL("./markdown.worker.ts", import.meta.url), {
    type: "module",
  }) as unknown as CanonicalWorkerLike;
}

function defaultScheduleFrame(callback: FrameRequestCallback): number {
  if (typeof requestAnimationFrame === "function") {
    return requestAnimationFrame(callback);
  }
  return globalThis.setTimeout(() => callback(defaultNow()), 16) as unknown as number;
}

function defaultNow(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}
