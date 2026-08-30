import { renderCanonicalMarkdownBlocks } from "./markdown-renderer";
import type {
  CanonicalRenderFailure,
  CanonicalRenderRequest,
  CanonicalRenderResponse,
} from "./markdown-worker-protocol";

type WorkerScope = {
  addEventListener(
    type: "message",
    listener: (event: MessageEvent<unknown>) => void,
  ): void;
  postMessage(message: CanonicalRenderResponse): void;
  document?: unknown;
};

export function handleCanonicalRenderRequest(
  value: unknown,
): CanonicalRenderResponse {
  const identity = readIdentity(value);
  if (!isCanonicalRenderRequest(value)) {
    return { type: "failed", ...identity, reason: "worker_protocol" };
  }
  try {
    return {
      type: "rendered",
      ...identity,
      blocks: renderCanonicalMarkdownBlocks(value.canonicalText),
    };
  } catch {
    return { type: "failed", ...identity, reason: "worker_parse" };
  }
}

export function installCanonicalMarkdownWorker(scope: WorkerScope): void {
  scope.addEventListener("message", (event) => {
    scope.postMessage(handleCanonicalRenderRequest(event.data));
  });
}

function isCanonicalRenderRequest(
  value: unknown,
): value is CanonicalRenderRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<CanonicalRenderRequest>;
  return request.type === "render"
    && Number.isInteger(request.jobId)
    && typeof request.itemId === "string"
    && Number.isInteger(request.revision)
    && Number.isInteger(request.generation)
    && typeof request.canonicalText === "string";
}

function readIdentity(value: unknown): Omit<CanonicalRenderFailure, "type" | "reason"> {
  const request = value && typeof value === "object"
    ? value as Partial<CanonicalRenderRequest>
    : {};
  return {
    jobId: Number.isInteger(request.jobId) ? request.jobId as number : -1,
    itemId: typeof request.itemId === "string" ? request.itemId : "",
    revision: Number.isInteger(request.revision) ? request.revision as number : -1,
    generation: Number.isInteger(request.generation)
      ? request.generation as number
      : -1,
  };
}

const scope = globalThis as unknown as WorkerScope;
if (typeof scope.document === "undefined") {
  installCanonicalMarkdownWorker(scope);
}

export {};
