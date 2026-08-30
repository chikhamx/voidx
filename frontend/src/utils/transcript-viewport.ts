export const TRANSCRIPT_NEAR_BOTTOM_PX = 48;

export type TranscriptFrameHandle = number | ReturnType<typeof setTimeout>;

export interface TranscriptViewportGeometry {
  scrollTop: number;
  clientHeight: number;
  scrollHeight: number;
}

export interface TranscriptViewportOptions {
  transcript: HTMLElement;
  returnToBottomButton?: HTMLButtonElement | null;
  scheduleFrame?: (callback: FrameRequestCallback) => TranscriptFrameHandle;
  cancelFrame?: (handle: TranscriptFrameHandle) => void;
  readGeometry?: () => TranscriptViewportGeometry;
  writeScrollTop?: (value: number) => void;
}

export interface TranscriptViewportController {
  readonly transcript: HTMLElement;
  enqueueMutation(key: object, mutate: () => void): void;
  cancelMutation(key: object): void;
  flushMutationNow(key: object, mutate: () => void): void;
  getInteractionGeneration(): number;
  prepareForSynchronousPrepend(expectedInteractionGeneration: number): boolean;
  requestFollowAfterExternalMutation(): void;
  forceScrollToBottom(): void;
  isFollowing(): boolean;
  reset(): void;
  dispose(): void;
}

interface TransactionFlags {
  scrollGeometryDirty: boolean;
  forceFollowRequested: boolean;
  externalFollowRequested: boolean;
}

function defaultFramePair(): {
  scheduleFrame: (callback: FrameRequestCallback) => TranscriptFrameHandle;
  cancelFrame: (handle: TranscriptFrameHandle) => void;
} {
  if (
    typeof globalThis.requestAnimationFrame === "function"
    && typeof globalThis.cancelAnimationFrame === "function"
  ) {
    return {
      scheduleFrame: (callback) => globalThis.requestAnimationFrame(callback),
      cancelFrame: (handle) => globalThis.cancelAnimationFrame(Number(handle)),
    };
  }
  return {
    scheduleFrame: (callback) => setTimeout(
      () => callback(typeof performance === "undefined" ? Date.now() : performance.now()),
      16,
    ),
    cancelFrame: (handle) => clearTimeout(handle),
  };
}

export function createTranscriptViewportController(
  options: TranscriptViewportOptions,
): TranscriptViewportController {
  const hasScheduleFrame = options.scheduleFrame !== undefined;
  const hasCancelFrame = options.cancelFrame !== undefined;
  if (hasScheduleFrame !== hasCancelFrame) {
    throw new Error("scheduleFrame and cancelFrame must be provided together");
  }

  const framePair = hasScheduleFrame
    ? {
        scheduleFrame: options.scheduleFrame!,
        cancelFrame: options.cancelFrame!,
      }
    : defaultFramePair();
  const transcript = options.transcript;
  const button = options.returnToBottomButton ?? null;
  const readGeometry = options.readGeometry ?? (() => ({
    scrollTop: transcript.scrollTop,
    clientHeight: transcript.clientHeight,
    scrollHeight: transcript.scrollHeight,
  }));
  const writeScrollTop = options.writeScrollTop ?? ((value: number) => {
    transcript.scrollTop = value;
  });

  let pendingMutations = new Map<object, () => void>();
  let frameHandle: TranscriptFrameHandle | null = null;
  let following = true;
  let scrollGeometryDirty = false;
  let forceFollowRequested = false;
  let externalFollowRequested = false;
  let interactionGeneration = 0;
  let callbackGeneration = 0;
  let transactionActive = false;
  let disposed = false;

  const updateButton = (): void => {
    if (button) button.hidden = following;
  };

  const hasPendingWork = (): boolean => (
    pendingMutations.size > 0
    || scrollGeometryDirty
    || forceFollowRequested
    || externalFollowRequested
  );

  const cancelPendingFrame = (): void => {
    if (frameHandle === null) return;
    framePair.cancelFrame(frameHandle);
    frameHandle = null;
  };

  const takeFlags = (): TransactionFlags => {
    const flags = {
      scrollGeometryDirty,
      forceFollowRequested,
      externalFollowRequested,
    };
    scrollGeometryDirty = false;
    forceFollowRequested = false;
    externalFollowRequested = false;
    return flags;
  };

  const runTransaction = (
    mutations: Array<() => void>,
    flags: TransactionFlags,
  ): void => {
    const geometry = readGeometry();
    const bottomGap = Math.max(
      0,
      geometry.scrollHeight - geometry.clientHeight - geometry.scrollTop,
    );
    const wasNearBottom = bottomGap <= TRANSCRIPT_NEAR_BOTTOM_PX;
    const hasContentMutation = mutations.length > 0;

    if (flags.scrollGeometryDirty) {
      following = wasNearBottom;
    } else if (
      hasContentMutation
      && !flags.forceFollowRequested
      && !flags.externalFollowRequested
      && following
      && !wasNearBottom
    ) {
      following = false;
    }

    transactionActive = true;
    try {
      for (const mutate of mutations) mutate();
    } finally {
      transactionActive = false;
    }

    let shouldScroll = false;
    if (flags.forceFollowRequested) {
      following = true;
      shouldScroll = true;
    } else if (flags.scrollGeometryDirty && !wasNearBottom) {
      shouldScroll = false;
    } else if (flags.externalFollowRequested && following) {
      shouldScroll = true;
    } else if (hasContentMutation && following && wasNearBottom) {
      shouldScroll = true;
    }

    if (shouldScroll) writeScrollTop(Number.MAX_SAFE_INTEGER);
    updateButton();
  };

  const scheduleIfNeeded = (): void => {
    if (disposed || frameHandle !== null || !hasPendingWork()) return;
    const scheduledGeneration = callbackGeneration;
    frameHandle = framePair.scheduleFrame(() => {
      if (disposed || scheduledGeneration !== callbackGeneration) return;
      frameHandle = null;
      const mutations = [...pendingMutations.values()];
      pendingMutations = new Map();
      const flags = takeFlags();
      runTransaction(mutations, flags);
      scheduleIfNeeded();
    });
  };

  const onScroll = (): void => {
    if (disposed) return;
    interactionGeneration += 1;
    scrollGeometryDirty = true;
    scheduleIfNeeded();
  };

  const forceScrollToBottom = (): void => {
    if (disposed) return;
    interactionGeneration += 1;
    forceFollowRequested = true;
    scheduleIfNeeded();
  };

  const onButtonClick = (): void => forceScrollToBottom();

  transcript.addEventListener("scroll", onScroll);
  button?.addEventListener("click", onButtonClick);
  updateButton();

  const reset = (): void => {
    cancelPendingFrame();
    pendingMutations.clear();
    scrollGeometryDirty = false;
    forceFollowRequested = false;
    externalFollowRequested = false;
    transactionActive = false;
    callbackGeneration += 1;
    interactionGeneration += 1;
    following = true;
    updateButton();
  };

  return {
    transcript,
    enqueueMutation(key, mutate) {
      if (disposed) return;
      pendingMutations.set(key, mutate);
      scheduleIfNeeded();
    },
    cancelMutation(key) {
      if (disposed) return;
      pendingMutations.delete(key);
      if (!hasPendingWork()) cancelPendingFrame();
    },
    flushMutationNow(key, mutate) {
      if (disposed) return;
      if (transactionActive) {
        pendingMutations.set(key, mutate);
        scheduleIfNeeded();
        return;
      }
      pendingMutations.delete(key);
      const flags = takeFlags();
      runTransaction([mutate], flags);
      if (hasPendingWork()) scheduleIfNeeded();
      else cancelPendingFrame();
    },
    getInteractionGeneration: () => interactionGeneration,
    prepareForSynchronousPrepend(expectedInteractionGeneration) {
      if (disposed || expectedInteractionGeneration !== interactionGeneration) {
        return false;
      }
      scrollGeometryDirty = false;
      forceFollowRequested = false;
      externalFollowRequested = false;
      following = false;
      updateButton();
      if (pendingMutations.size === 0) cancelPendingFrame();
      return true;
    },
    requestFollowAfterExternalMutation() {
      if (disposed || !following) return;
      externalFollowRequested = true;
      scheduleIfNeeded();
    },
    forceScrollToBottom,
    isFollowing: () => following,
    reset,
    dispose() {
      if (disposed) return;
      reset();
      transcript.removeEventListener("scroll", onScroll);
      button?.removeEventListener("click", onButtonClick);
      disposed = true;
    },
  };
}
