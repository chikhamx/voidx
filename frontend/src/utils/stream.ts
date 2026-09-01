import {
  createStreamingMarkdownProjection,
  type StreamUpdateOperation,
} from "./markdown";
import {
  createCanonicalMarkdownCoordinator,
  type CanonicalMarkdownCoordinator,
} from "./markdown-worker-client";
import type { StreamState } from "./types";
import {
  createTranscriptViewportController,
  validateBlockedQuiesceToken,
  type BlockedViewportQuiesceToken,
  type TranscriptViewportController,
} from "./transcript-viewport";

export const STREAM_RENDER_THROTTLE_MS = 100;
const THINKING_MAX_LINES = 5;
const REPLACEMENT_ERROR = "cannot replace transcript viewport while stream or canonical work is active";

const streams = new Map<string, StreamState>();
const committedEls: HTMLElement[] = [];
const committedCanonicalText = new WeakMap<HTMLElement, string>();
const canonicalOwners = new Map<string, {
  revision: number;
  generation: number;
  target: HTMLElement;
  viewportController: TranscriptViewportController | null;
  viewportGeneration: number;
}>();

export type TranscriptLiveOwnerKind =
  | "active-stream"
  | "pending-canonical"
  | "retained-committed";

export interface TranscriptLiveOwnerToken {
  kind: TranscriptLiveOwnerKind;
  streamId: string;
  element: HTMLElement;
  streamGeneration: number;
  canonicalRevision: number;
  viewportGeneration: number;
}

export interface CommittedStreamClaim {
  element: HTMLElement;
  streamId: string;
  streamGeneration: number;
  canonicalRevision: number;
}

export interface CommittedStreamReservation {
  claim: CommittedStreamClaim;
  originalIndex: number;
}

const committedClaims = new Map<HTMLElement, CommittedStreamClaim>();
const committedReservations = new Map<HTMLElement, CommittedStreamReservation>();
let canonicalMarkdownCoordinator = createCanonicalMarkdownCoordinator();
let nextStreamGeneration = 1;
let transcriptEl: HTMLElement | null = null;
let viewportController: TranscriptViewportController | null = null;
let transcriptViewportGeneration = 0;

function assertViewportReplacementAllowed(): void {
  if (streams.size || committedEls.length || canonicalOwners.size) {
    throw new Error(REPLACEMENT_ERROR);
  }
}

export function setTranscriptElement(
  el: HTMLElement,
  returnToBottomButton?: HTMLButtonElement | null,
): void {
  assertViewportReplacementAllowed();
  const candidate = createTranscriptViewportController({
    transcript: el,
    returnToBottomButton,
  });
  viewportController?.dispose();
  transcriptViewportGeneration += 1;
  viewportController = candidate;
  transcriptEl = el;
}

export function getTranscriptElement(): HTMLElement | null {
  return transcriptEl;
}

const transcriptReconciliationMutationKey = {};

export function flushTranscriptReconciliationNow(mutate: () => void): void {
  if (!viewportController) {
    mutate();
    return;
  }
  viewportController.flushMutationNow(
    transcriptReconciliationMutationKey,
    mutate,
    { followAfterMutation: true },
  );
}

export function requestTranscriptFollowAfterMutation(): void {
  viewportController?.requestFollowAfterExternalMutation();
}

export function forceTranscriptScrollToBottom(): void {
  viewportController?.forceScrollToBottom();
}

export function isTranscriptFollowing(): boolean {
  return viewportController?.isFollowing() ?? true;
}

export function getTranscriptInteractionGeneration(): number {
  return viewportController?.getInteractionGeneration() ?? 0;
}

export function prepareTranscriptForSynchronousPrepend(
  expectedInteractionGeneration: number,
): boolean {
  return viewportController?.prepareForSynchronousPrepend(
    expectedInteractionGeneration,
  ) ?? false;
}


export function quiesceTranscriptViewportForBlockedInstallNoDom(): BlockedViewportQuiesceToken | null {
  return viewportController?.quiesceForBlockedInstallNoDom() ?? null;
}

export function validateTranscriptViewportBlockedInstallReady(
  proof: BlockedViewportQuiesceToken,
): boolean {
  return validateBlockedQuiesceToken(proof);
}
export function resetTranscriptViewport(): void {
  viewportController?.reset();
}

export function _setTranscriptViewportControllerForTest(
  controller: TranscriptViewportController,
): void {
  assertViewportReplacementAllowed();
  if (controller === viewportController) return;
  viewportController?.dispose();
  transcriptViewportGeneration += 1;
  viewportController = controller;
  transcriptEl = controller.transcript;
}

export function getOrCreateStream(streamId: string, phase: string): StreamState {
  let stream = streams.get(streamId);
  if (stream) return stream;

  invalidateCanonicalOwner(streamId);
  const el = document.createElement("div");
  el.className = "stream-buffer";
  el.dataset.streamId = streamId;
  const thinkingEl = document.createElement("div");
  thinkingEl.className = "stream-thinking";
  thinkingEl.hidden = true;
  thinkingEl.setAttribute("role", "status");
  thinkingEl.setAttribute("aria-live", "polite");
  const thinkingLabel = document.createElement("div");
  thinkingLabel.className = "stream-thinking-label";
  const brainSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="vx-icon"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"></path><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"></path><path d="M12 5v14"></path><path d="M12 9h4"></path><path d="M12 14h-4"></path><path d="M12 14h4"></path><path d="M12 9h-4"></path></svg>`;
  thinkingLabel.innerHTML = `${brainSvg}<span>thought</span><span style="display: none;">Thinking</span>`;
  const thinkingBody = document.createElement("div");
  thinkingBody.className = "stream-thinking-body";
  thinkingEl.append(thinkingLabel, thinkingBody);
  const textEl = document.createElement("div");
  textEl.className = "markdown-body";
  el.append(thinkingEl, textEl);

  stream = {
    text: "",
    thinking: "",
    phase,
    el,
    thinkingEl,
    thinkingLabel,
    thinkingBody,
    textEl,
    renderTimer: null,
    renderQueued: false,
    attached: false,
    markdownProjection: createStreamingMarkdownProjection(textEl),
    pendingProjectionUpdate: null,
    canonicalRevision: 0,
    streamGeneration: nextStreamGeneration++,
  };
  streams.set(streamId, stream);
  return stream;
}

export function appendStreamText(
  streamId: string,
  text: string,
  phase: string,
  operation?: StreamUpdateOperation,
): void {
  const stream = getOrCreateStream(streamId, phase);
  const incoming = String(text ?? "");
  const previousPhase = stream.phase;
  if (previousPhase !== phase && operation !== undefined) {
    resetTextProjection(stream);
  }
  stream.phase = phase;

  if (phase === "thinking") {
    stream.thinking = operation === "append" && previousPhase === phase
      ? stream.thinking + incoming
      : incoming;
  } else {
    const previousText = stream.text;
    const nextText = stripAssistantPrefixBullet(
      operation === "append" ? previousText + incoming : incoming,
    );
    stream.text = nextText;
    stream.canonicalRevision += 1;
    if (operation === "append") {
      const extendsPrevious = nextText.startsWith(previousText);
      queueProjectionUpdate(
        stream,
        extendsPrevious ? nextText.slice(previousText.length) : nextText,
        extendsPrevious ? "append" : "replace",
      );
    } else if (
      operation === undefined
      && previousPhase === phase
      && nextText.startsWith(previousText)
    ) {
      queueProjectionUpdate(stream, nextText.slice(previousText.length), "append");
    } else {
      queueProjectionUpdate(stream, nextText, "replace");
    }
  }
  scheduleRender(stream);
}

export function commitStream(streamId: string, retain = true): {
  text: string;
  thinking: string;
  el: HTMLElement;
} | null {
  const stream = streams.get(streamId);
  if (!stream) return null;

  cancelStreamTimer(stream);
  stream.committed = true;
  const render = () => renderLatestStreamState(stream, true);
  if (viewportController) viewportController.flushMutationNow(stream, render);
  else render();
  stream.renderQueued = false;
  stream.canonicalRevision += 1;

  const result = { text: stream.text, thinking: stream.thinking, el: stream.el };
  streams.delete(streamId);
  committedCanonicalText.set(stream.el, stream.text);
  if (retain) committedEls.push(stream.el);

  const owner = {
    revision: stream.canonicalRevision,
    generation: stream.streamGeneration,
    target: stream.textEl,
    viewportController,
    viewportGeneration: transcriptViewportGeneration,
  };
  canonicalOwners.set(streamId, owner);
  if (retain) {
    committedClaims.set(stream.el, {
      element: stream.el,
      streamId,
      streamGeneration: owner.generation,
      canonicalRevision: owner.revision,
    });
  }
  canonicalMarkdownCoordinator.start({
    itemId: streamId,
    revision: owner.revision,
    generation: owner.generation,
    canonicalText: stream.text,
    target: stream.textEl,
    isCurrent: () => canonicalOwners.get(streamId) === owner,
    onSettled: (outcome) => {
      const isOwner = canonicalOwners.get(streamId) === owner;
      if (
        isOwner
        && (outcome.status === "installed" || outcome.status === "fallback")
        && owner.viewportController !== null
        && owner.viewportController === viewportController
        && owner.viewportGeneration === transcriptViewportGeneration
        && owner.target.isConnected
        && owner.viewportController.transcript.contains(owner.target)
      ) {
        owner.viewportController.requestFollowAfterExternalMutation();
      }
      if (isOwner) canonicalOwners.delete(streamId);
    },
  });
  return result;
}

export function takeCommittedStreams(): HTMLElement[] {
  return committedEls.splice(0);
}

function pendingCanonicalElement(streamId: string): HTMLElement | null {
  return canonicalOwners.get(streamId)?.target.closest<HTMLElement>(".stream-buffer") ?? null;
}

function latestRetainedClaim(streamId: string): CommittedStreamClaim | null {
  for (let index = committedEls.length - 1; index >= 0; index -= 1) {
    const claim = committedClaims.get(committedEls[index]);
    if (claim?.streamId === streamId) return claim;
  }
  return null;
}

export function peekTranscriptLiveOwners(): readonly TranscriptLiveOwnerToken[] {
  const tokens: TranscriptLiveOwnerToken[] = [];
  const represented = new Set<string>();
  for (const [streamId, owner] of canonicalOwners) {
    const element = pendingCanonicalElement(streamId);
    if (!element) continue;
    tokens.push({
      kind: "pending-canonical",
      streamId,
      element,
      streamGeneration: owner.generation,
      canonicalRevision: owner.revision,
      viewportGeneration: owner.viewportGeneration,
    });
    represented.add(streamId);
  }
  for (const [streamId, stream] of streams) {
    if (represented.has(streamId)) continue;
    tokens.push({
      kind: "active-stream",
      streamId,
      element: stream.el,
      streamGeneration: stream.streamGeneration,
      canonicalRevision: stream.canonicalRevision,
      viewportGeneration: transcriptViewportGeneration,
    });
    represented.add(streamId);
  }
  for (let index = committedEls.length - 1; index >= 0; index -= 1) {
    const claim = committedClaims.get(committedEls[index]);
    if (!claim || represented.has(claim.streamId)) continue;
    tokens.push({
      kind: "retained-committed",
      streamId: claim.streamId,
      element: claim.element,
      streamGeneration: claim.streamGeneration,
      canonicalRevision: claim.canonicalRevision,
      viewportGeneration: transcriptViewportGeneration,
    });
    represented.add(claim.streamId);
  }
  return tokens;
}

function liveOwnerTokenMatchesCurrent(token: TranscriptLiveOwnerToken): boolean {
  const owner = canonicalOwners.get(token.streamId);
  const pendingElement = pendingCanonicalElement(token.streamId);
  if (owner && pendingElement) {
    return token.kind === "pending-canonical"
      && token.element === pendingElement
      && token.streamGeneration === owner.generation
      && token.canonicalRevision === owner.revision
      && token.viewportGeneration === owner.viewportGeneration;
  }
  const stream = streams.get(token.streamId);
  if (stream) {
    return token.kind === "active-stream"
      && token.element === stream.el
      && token.streamGeneration === stream.streamGeneration
      && token.canonicalRevision === stream.canonicalRevision
      && token.viewportGeneration === transcriptViewportGeneration;
  }
  const claim = latestRetainedClaim(token.streamId);
  return claim !== null
    && token.kind === "retained-committed"
    && token.element === claim.element
    && token.streamGeneration === claim.streamGeneration
    && token.canonicalRevision === claim.canonicalRevision
    && token.viewportGeneration === transcriptViewportGeneration;
}

function currentLiveOwnerCount(): number {
  let count = 0;
  for (const streamId of canonicalOwners.keys()) {
    if (pendingCanonicalElement(streamId)) count += 1;
  }
  for (const streamId of streams.keys()) {
    if (!pendingCanonicalElement(streamId)) count += 1;
  }
  for (let index = committedEls.length - 1; index >= 0; index -= 1) {
    const claim = committedClaims.get(committedEls[index]);
    if (!claim
      || pendingCanonicalElement(claim.streamId)
      || streams.has(claim.streamId)
      || latestRetainedClaim(claim.streamId)?.element !== claim.element) continue;
    count += 1;
  }
  return count;
}

export function validateTranscriptLiveOwnerTokens(
  tokens: readonly TranscriptLiveOwnerToken[],
): boolean {
  if (tokens.length !== currentLiveOwnerCount()) return false;
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    for (let earlier = 0; earlier < index; earlier += 1) {
      if (tokens[earlier].streamId === token.streamId) return false;
    }
    if (!liveOwnerTokenMatchesCurrent(token)) return false;
  }
  return true;
}

export function peekCommittedStreamsForSnapshot(): readonly CommittedStreamClaim[] {
  return committedEls.map((element) => committedClaims.get(element)).filter(
    (claim): claim is CommittedStreamClaim => claim !== undefined,
  );
}

function committedClaimMatches(
  current: CommittedStreamClaim | undefined,
  expected: CommittedStreamClaim,
): boolean {
  return current === expected
    && current.element === expected.element
    && current.streamId === expected.streamId
    && current.streamGeneration === expected.streamGeneration
    && current.canonicalRevision === expected.canonicalRevision;
}

export function reserveCommittedStream(
  expected: CommittedStreamClaim,
): CommittedStreamReservation | null {
  if (committedReservations.has(expected.element)) return null;
  const originalIndex = committedEls.indexOf(expected.element);
  const owner = canonicalOwners.get(expected.streamId);
  if (
    originalIndex < 0
    || !committedClaimMatches(committedClaims.get(expected.element), expected)
    || (owner !== undefined && (
      owner.generation !== expected.streamGeneration
      || owner.revision !== expected.canonicalRevision
    ))
  ) {
    return null;
  }
  const reservation = { claim: expected, originalIndex };
  committedReservations.set(expected.element, reservation);
  return reservation;
}

export function validateCommittedStreamReservations(
  reservations: readonly CommittedStreamReservation[],
): boolean {
  const elements = new Set<HTMLElement>();
  return reservations.every((reservation) => {
    const expected = reservation.claim;
    const owner = canonicalOwners.get(expected.streamId);
    if (elements.has(expected.element)) return false;
    elements.add(expected.element);
    return committedReservations.get(expected.element) === reservation
      && committedEls[reservation.originalIndex] === expected.element
      && committedClaimMatches(committedClaims.get(expected.element), expected)
      && (owner === undefined || (
        owner.generation === expected.streamGeneration
        && owner.revision === expected.canonicalRevision
      ));
  });
}

export function commitCommittedStreamReservationsNoFail(
  reservations: readonly CommittedStreamReservation[],
): void {
  const ordered = [...reservations].sort((left, right) => right.originalIndex - left.originalIndex);
  for (const reservation of ordered) {
    const { claim, originalIndex } = reservation;
    committedEls.splice(originalIndex, 1);
    const owner = canonicalOwners.get(claim.streamId);
    if (
      owner?.generation === claim.streamGeneration
      && owner.revision === claim.canonicalRevision
    ) {
      canonicalOwners.delete(claim.streamId);
    }
    committedCanonicalText.delete(claim.element);
    committedClaims.delete(claim.element);
    committedReservations.delete(claim.element);
  }
}

export function releaseCommittedStreamReservations(
  reservations: readonly CommittedStreamReservation[],
): void {
  for (const reservation of reservations) {
    if (committedReservations.get(reservation.claim.element) === reservation) {
      committedReservations.delete(reservation.claim.element);
    }
  }
}

export function getCommittedStreamCanonicalText(element: HTMLElement): string | null {
  return committedCanonicalText.get(element) ?? null;
}

export function invalidateCommittedStreamElement(element: HTMLElement): void {
  const streamId = element.dataset.streamId;
  if (streamId) {
    const owner = canonicalOwners.get(streamId);
    if (owner?.target.closest(".stream-buffer") === element) {
      invalidateCanonicalOwner(streamId);
    }
  }
  committedCanonicalText.delete(element);
}

export function clearCommittedStreams(): void {
  for (const el of committedEls) {
    invalidateCommittedStreamElement(el);
    committedClaims.delete(el);
    committedReservations.delete(el);
    el.remove();
  }
  committedEls.length = 0;
}


export function quiesceStreamsForBlockedInstallNoCallback(): void {
  for (const stream of streams.values()) cancelStreamWork(stream);
  streams.clear();
  for (const element of committedEls) {
    committedCanonicalText.delete(element);
    committedClaims.delete(element);
    committedReservations.delete(element);
  }
  committedEls.length = 0;
  committedReservations.clear();
  canonicalOwners.clear();
  nextStreamGeneration += 1;
  transcriptViewportGeneration += 1;
}
export function clearActiveStreams(
  options: { preserveCanonicalCommits?: boolean } = {},
): void {
  for (const [streamId, stream] of streams) {
    cancelStreamWork(stream);
    stream.el.remove();
    canonicalMarkdownCoordinator.invalidate(streamId);
  }
  streams.clear();
  if (!options.preserveCanonicalCommits) invalidateAllCanonicalOwners();
}

export function discardStream(streamId: string): void {
  const stream = streams.get(streamId);
  if (!stream) {
    invalidateCanonicalOwner(streamId);
    return;
  }
  cancelStreamWork(stream);
  stream.el.remove();
  streams.delete(streamId);
  invalidateCanonicalOwner(streamId);
}

function scheduleRender(stream: StreamState): void {
  if (stream.renderTimer !== null || stream.renderQueued || stream.committed) return;
  stream.renderTimer = setTimeout(() => {
    stream.renderTimer = null;
    if (stream.committed || streams.get(stream.el.dataset.streamId ?? "") !== stream) {
      return;
    }
    stream.renderQueued = true;
    const render = () => {
      stream.renderQueued = false;
      if (stream.committed) return;
      renderLatestStreamState(stream, false);
    };
    if (viewportController) viewportController.enqueueMutation(stream, render);
    else render();
  }, STREAM_RENDER_THROTTLE_MS);
}

function cancelStreamTimer(stream: StreamState): void {
  if (stream.renderTimer === null) return;
  clearTimeout(stream.renderTimer);
  stream.renderTimer = null;
}

function cancelStreamWork(stream: StreamState): void {
  cancelStreamTimer(stream);
  viewportController?.cancelMutation(stream);
  stream.renderQueued = false;
}

function queueProjectionUpdate(
  stream: StreamState,
  text: string,
  operation: StreamUpdateOperation,
): void {
  const pending = stream.pendingProjectionUpdate;
  if (operation === "replace") {
    stream.pendingProjectionUpdate = { text, operation };
  } else if (!text) {
    return;
  } else if (!pending) {
    stream.pendingProjectionUpdate = { text, operation };
  } else if (pending.operation === "append") {
    pending.text += text;
  } else {
    pending.text += text;
  }
}

function resetTextProjection(stream: StreamState): void {
  stream.text = "";
  stream.canonicalRevision += 1;
  stream.pendingProjectionUpdate = { text: "", operation: "replace" };
}

function applyPendingTextProjection(stream: StreamState): void {
  stream.textEl.querySelector(".stream-cursor")?.remove();
  const update = stream.pendingProjectionUpdate;
  stream.pendingProjectionUpdate = null;
  if (stream.markdownProjection && update) {
    stream.markdownProjection.update(update.text, update.operation);
  } else if (!stream.markdownProjection) {
    stream.textEl.replaceChildren(document.createTextNode(stream.text));
  }
}

function renderLatestStreamState(stream: StreamState, committed: boolean): void {
  if (!stream.attached && transcriptEl) {
    transcriptEl.append(stream.el);
    stream.attached = true;
  }
  applyPendingTextProjection(stream);
  const hasThinking = !committed && stream.phase === "thinking" && Boolean(stream.thinking);
  stream.thinkingEl.hidden = !hasThinking;
  stream.thinkingBody.textContent = hasThinking
    ? visibleThinkingLines(stream.thinking)
    : "";
  if (!committed && stream.phase === "text") {
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    stream.textEl.append(cursor);
  }
  if (committed && !stream.text) stream.el.style.display = "none";
}

function visibleThinkingLines(text: string): string {
  return String(text || "")
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .slice(-THINKING_MAX_LINES)
    .join("\n");
}

function stripAssistantPrefixBullet(text: string): string {
  return String(text || "").replace(/^\s*●\s+/, "");
}

function invalidateCanonicalOwner(streamId: string): void {
  if (!canonicalOwners.has(streamId)) return;
  canonicalOwners.delete(streamId);
  canonicalMarkdownCoordinator.invalidate(streamId);
}

function invalidateAllCanonicalOwners(): void {
  canonicalOwners.clear();
  canonicalMarkdownCoordinator.invalidateAll();
}

export function _setCanonicalMarkdownCoordinatorForTest(
  coordinator: CanonicalMarkdownCoordinator,
): void {
  invalidateAllCanonicalOwners();
  canonicalMarkdownCoordinator = coordinator;
}

export function _resetForTest(): void {
  for (const stream of streams.values()) cancelStreamWork(stream);
  streams.clear();
  for (const el of committedEls) el.remove();
  committedEls.length = 0;
  committedClaims.clear();
  committedReservations.clear();
  invalidateAllCanonicalOwners();
  canonicalMarkdownCoordinator = createCanonicalMarkdownCoordinator();
  viewportController?.dispose();
  viewportController = null;
  transcriptViewportGeneration += 1;
  nextStreamGeneration += 1;
  transcriptEl = null;
}
