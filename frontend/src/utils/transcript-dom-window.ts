import type { TranscriptSnapshot } from "./render-types";
import {
  buildTranscriptDescriptors,
  type TranscriptNodeDescriptor,
} from "./transcript-reconciliation";

export const DEFAULT_BLOCK_ESTIMATE_PX = 96;
export const DEFAULT_TRANSCRIPT_DOM_WINDOW_BUDGET: TranscriptDomWindowBudget = {
  overscanViewportsBefore: 3,
  overscanViewportsAfter: 3,
  maxAttachedUnpinnedBlocks: 240,
};

export type CanonicalMergeMode = "full" | "windowed" | "earlier-page";

export type CanonicalMergeResult =
  | { status: "merged"; snapshot: TranscriptSnapshot; descriptors: TranscriptNodeDescriptor[] }
  | { status: "stale"; reason: string; recovery: "ordinary" | "blocked" };

export interface TranscriptBlockExtent {
  key: string;
  shape: string;
  extentPx: number;
}

export interface TranscriptDomWindowBudget {
  overscanViewportsBefore: number;
  overscanViewportsAfter: number;
  maxAttachedUnpinnedBlocks: number;
}

export interface TranscriptDomWindowViewport {
  scrollTop: number;
  clientHeight: number;
  following: boolean;
}

export type TranscriptExternalInsertion =
  | { edge: "before-all" | "after-all" }
  | { beforeKey: string }
  | { afterKey: string };

export type TranscriptLayoutEntry =
  | { kind: "canonical"; key: string; descriptorIndex: number; extentPx: number }
  | { kind: "external"; id: string; element: HTMLElement; extentPx: number; insertion: TranscriptExternalInsertion };

export interface TranscriptSpacerSegment {
  startIndex: number;
  endIndex: number;
  omittedKeys: string[];
  canonicalStartPx: number;
  canonicalEndPx: number;
  cssHeightPx: number;
}

export interface TranscriptDomWindowState {
  threadId: string;
  snapshot: TranscriptSnapshot;
  descriptors: TranscriptNodeDescriptor[];
  heights: Map<string, number>;
  estimates: Map<string, number>;
  attachedKeys: Set<string>;
  pinnedKeys: Set<string>;
  spacerSegments: TranscriptSpacerSegment[];
  generation: number;
  loadingEarlier: boolean;
}

export function measureTranscriptLogicalBlockExtent(
  roots: readonly HTMLElement[],
): number | null {
  if (roots.length === 0) return null;

  let firstTop: number | null = null;
  let previousTop = Number.NEGATIVE_INFINITY;
  let previousBottom = Number.NEGATIVE_INFINITY;
  let lastBottom: number | null = null;
  for (const root of roots) {
    const { top, bottom } = root.getBoundingClientRect();
    if (!Number.isFinite(top) || !Number.isFinite(bottom) || bottom <= top) return null;
    if (top < previousTop || bottom < previousBottom) return null;
    firstTop ??= top;
    previousTop = top;
    previousBottom = bottom;
    lastBottom = bottom;
  }

  const extent = lastBottom! - firstTop!;
  return Number.isFinite(extent) && extent > 0 ? extent : null;
}

export interface TranscriptWindowAnchorCandidate {
  key: string;
  primary: HTMLElement;
  roots?: readonly HTMLElement[];
}

export interface TranscriptWindowAnchorSelectionInput {
  blocks: readonly TranscriptWindowAnchorCandidate[];
  viewportTop: number;
  viewportBottom: number;
  following: boolean;
  pendingRoots?: ReadonlySet<HTMLElement>;
}

function anchorRect(element: HTMLElement): { top: number; bottom: number } | null {
  const rect = element.getBoundingClientRect();
  if (!Number.isFinite(rect.top) || !Number.isFinite(rect.bottom) || rect.bottom <= rect.top) {
    return null;
  }
  return { top: rect.top, bottom: rect.bottom };
}

function isAnchorCandidateUsable(
  block: TranscriptWindowAnchorCandidate,
  pendingRoots: ReadonlySet<HTMLElement>,
): boolean {
  return !(block.roots ?? [block.primary]).some((root) => pendingRoots.has(root));
}

export function selectTranscriptWindowAnchor(
  input: TranscriptWindowAnchorSelectionInput,
): TranscriptWindowAnchorCandidate | null {
  if (!Number.isFinite(input.viewportTop)
    || !Number.isFinite(input.viewportBottom)
    || input.viewportBottom <= input.viewportTop) return null;

  const pendingRoots = input.pendingRoots ?? new Set<HTMLElement>();
  const usable = input.blocks
    .map((block) => ({ block, rect: anchorRect(block.primary) }))
    .filter((entry): entry is { block: TranscriptWindowAnchorCandidate; rect: { top: number; bottom: number } } => (
      entry.rect !== null && isAnchorCandidateUsable(entry.block, pendingRoots)
    ));
  const fullyVisible = usable.find(({ rect }) => (
    rect.top >= input.viewportTop && rect.bottom <= input.viewportBottom
  ));
  if (fullyVisible) return fullyVisible.block;

  const partiallyVisible = usable.find(({ rect }) => (
    rect.bottom > input.viewportTop && rect.top < input.viewportBottom
  ));
  if (partiallyVisible) return partiallyVisible.block;

  return input.following ? usable[usable.length - 1]?.block ?? null : null;
}

export interface TranscriptDomWindowPlannerInput {
  entries: readonly TranscriptLayoutEntry[];
  attachedKeys: ReadonlySet<string>;
  pinnedKeys: ReadonlySet<string>;
  rowGapPx: number;
  budget: TranscriptDomWindowBudget;
  viewport: TranscriptDomWindowViewport;
  activation: "initial" | "replan";
  anchorKey: string | null;
}

export interface TranscriptDomWindowPlan {
  materializeKeys: string[];
  trimKeys: string[];
  nextAttachedKeys: Set<string>;
  spacerSegments: TranscriptSpacerSegment[];
  anchorKey: string | null;
  overBudgetReason: string | null;
}

function validDescriptors(snapshot: TranscriptSnapshot): TranscriptNodeDescriptor[] | null {
  try {
    const descriptors = buildTranscriptDescriptors(snapshot.nodes);
    const keys = new Set<string>();
    for (const descriptor of descriptors) {
      if (!descriptor.key || keys.has(descriptor.key)) return null;
      keys.add(descriptor.key);
    }
    return descriptors;
  } catch {
    return null;
  }
}

function sameMembers(a: TranscriptNodeDescriptor, b: TranscriptNodeDescriptor): boolean {
  return a.memberNodeIds.length === b.memberNodeIds.length
    && a.memberNodeIds.every((id, index) => id === b.memberNodeIds[index]);
}

function hasPartialOverlap(
  current: readonly TranscriptNodeDescriptor[],
  incoming: readonly TranscriptNodeDescriptor[],
): boolean {
  const owner = new Map<string, TranscriptNodeDescriptor>();
  for (const descriptor of current) {
    for (const id of descriptor.memberNodeIds) owner.set(id, descriptor);
  }
  for (const descriptor of incoming) {
    const overlaps = new Set(descriptor.memberNodeIds.map((id) => owner.get(id)).filter(Boolean));
    if (overlaps.size === 0) continue;
    if (overlaps.size !== 1) return true;
    const match = [...overlaps][0] as TranscriptNodeDescriptor;
    if (match.key !== descriptor.key || !sameMembers(match, descriptor)) return true;
  }
  return false;
}

function mergedSnapshot(
  current: TranscriptSnapshot,
  incoming: TranscriptSnapshot,
  descriptors: TranscriptNodeDescriptor[],
): TranscriptSnapshot {
  const currentBefore = current.before_turn_id;
  const incomingBefore = incoming.before_turn_id;
  const incomingIsEarlier = incomingBefore != null
    && (currentBefore == null || incomingBefore < currentBefore);
  const beforeSource = incomingIsEarlier ? incoming : current;

  const currentAfter = current.after_turn_id;
  const incomingAfter = incoming.after_turn_id;
  const incomingIsLater = incomingAfter != null
    && (currentAfter == null || incomingAfter > currentAfter);
  const afterSource = incomingIsLater ? incoming : current;

  return {
    ...current,
    ...incoming,
    before_turn_id: beforeSource.before_turn_id,
    after_turn_id: afterSource.after_turn_id,
    has_earlier: beforeSource.has_earlier,
    has_later: afterSource.has_later,
    nodes: descriptors.flatMap((descriptor) => descriptor.memberNodes),
  };
}

function successful(snapshot: TranscriptSnapshot, descriptors: TranscriptNodeDescriptor[]): CanonicalMergeResult {
  return {
    status: "merged",
    snapshot: { ...snapshot, nodes: descriptors.flatMap((descriptor) => descriptor.memberNodes) },
    descriptors,
  };
}

export function mergeCanonicalTranscript(
  current: TranscriptSnapshot | null,
  incoming: TranscriptSnapshot,
  mode: CanonicalMergeMode,
): CanonicalMergeResult {
  const incomingDescriptors = validDescriptors(incoming);
  if (!incomingDescriptors) {
    return {
      status: "stale",
      reason: "invalid incoming transcript",
      recovery: mode === "full" ? "blocked" : "ordinary",
    };
  }

  if (current === null) {
    return successful(incoming, incomingDescriptors);
  }

  const currentDescriptors = validDescriptors(current);
  if (!currentDescriptors) {
    return {
      status: "stale",
      reason: "invalid current transcript",
      recovery: mode === "full" ? "blocked" : "ordinary",
    };
  }
  if (mode === "full" || current.thread_id !== incoming.thread_id) {
    return successful(incoming, incomingDescriptors);
  }
  if (hasPartialOverlap(currentDescriptors, incomingDescriptors)) {
    return { status: "stale", reason: "partial descriptor overlap", recovery: "ordinary" };
  }

  const currentByKey = new Map(currentDescriptors.map((descriptor, index) => [descriptor.key, { descriptor, index }]));

  if (mode === "earlier-page") {
    const firstOverlap = incomingDescriptors.findIndex((descriptor) => currentByKey.has(descriptor.key));
    const incomingOnly = firstOverlap < 0 ? incomingDescriptors : incomingDescriptors.slice(0, firstOverlap);
    const overlap = firstOverlap < 0 ? [] : incomingDescriptors.slice(firstOverlap);
    const anchoredOverlap = overlap.length > 0 && overlap.every((descriptor, index) => (
      currentDescriptors[index]?.key === descriptor.key
    ));
    if (overlap.length > 0 && !anchoredOverlap) {
      return { status: "stale", reason: "earlier page is not anchored", recovery: "ordinary" };
    }
    if (overlap.some((descriptor) => !currentByKey.has(descriptor.key))) {
      return { status: "stale", reason: "earlier page is not anchored", recovery: "ordinary" };
    }
    if (overlap.length === 0 && incoming.after_turn_id != null && current.before_turn_id != null
      && incoming.after_turn_id >= current.before_turn_id) {
      return { status: "stale", reason: "earlier page is not anchored", recovery: "ordinary" };
    }
    const descriptors = [...incomingOnly, ...currentDescriptors];
    return successful(mergedSnapshot(current, incoming, descriptors), descriptors);
  }

  const matches = incomingDescriptors
    .map((descriptor, incomingIndex) => {
      const currentMatch = currentByKey.get(descriptor.key);
      return currentMatch ? { incomingIndex, currentIndex: currentMatch.index } : null;
    })
    .filter((match): match is { incomingIndex: number; currentIndex: number } => match !== null);

  if (matches.length === 0) {
    const before = incoming.after_turn_id != null && current.before_turn_id != null
      && incoming.after_turn_id <= current.before_turn_id;
    const after = incoming.before_turn_id != null && current.after_turn_id != null
      && incoming.before_turn_id >= current.after_turn_id;
    if (!before && !after) {
      return { status: "stale", reason: "window order is not anchored", recovery: "ordinary" };
    }
    const descriptors = before
      ? [...incomingDescriptors, ...currentDescriptors]
      : [...currentDescriptors, ...incomingDescriptors];
    return successful(mergedSnapshot(current, incoming, descriptors), descriptors);
  }

  for (let index = 1; index < matches.length; index += 1) {
    if (matches[index].currentIndex !== matches[index - 1].currentIndex + 1) {
      return { status: "stale", reason: "window order is not anchored", recovery: "ordinary" };
    }
  }

  const result = [...currentDescriptors];
  let offset = 0;
  let previousIncoming = -1;
  for (const match of matches) {
    const additions = incomingDescriptors.slice(previousIncoming + 1, match.incomingIndex)
      .filter((descriptor) => !currentByKey.has(descriptor.key));
    const target = match.currentIndex + offset;
    result.splice(target, 0, ...additions);
    offset += additions.length;
    result[target + additions.length] = incomingDescriptors[match.incomingIndex];
    previousIncoming = match.incomingIndex;
  }
  const tail = incomingDescriptors.slice(previousIncoming + 1)
    .filter((descriptor) => !currentByKey.has(descriptor.key));
  result.splice(matches[matches.length - 1].currentIndex + offset + 1, 0, ...tail);
  return successful(mergedSnapshot(current, incoming, result), result);
}

export function normalizeTranscriptBlockExtents(
  extents: readonly TranscriptBlockExtent[],
): Map<string, number> {
  const samples = new Map<string, number[]>();
  for (const item of extents) {
    if (!Number.isFinite(item.extentPx) || item.extentPx <= 0) continue;
    const values = samples.get(item.shape) ?? [];
    values.push(item.extentPx);
    samples.set(item.shape, values);
  }
  const medians = new Map<string, number>();
  for (const [shape, values] of samples) {
    values.sort((a, b) => a - b);
    const middle = Math.floor(values.length / 2);
    medians.set(shape, values.length % 2 === 0
      ? (values[middle - 1] + values[middle]) / 2
      : values[middle]);
  }
  return new Map(extents.map((item) => [
    item.key,
    Number.isFinite(item.extentPx) && item.extentPx > 0
      ? item.extentPx
      : (medians.get(item.shape) ?? DEFAULT_BLOCK_ESTIMATE_PX),
  ]));
}

interface PositionedEntry {
  entry: TranscriptLayoutEntry;
  start: number;
  end: number;
}

function orderedKeys(keys: Iterable<string>, indices: ReadonlyMap<string, number>): string[] {
  return [...keys].sort((a, b) => (indices.get(a) ?? Number.MAX_SAFE_INTEGER) - (indices.get(b) ?? Number.MAX_SAFE_INTEGER));
}

function unavailableLayoutPlan(input: TranscriptDomWindowPlannerInput): TranscriptDomWindowPlan {
  const canonical = input.entries.filter(
    (entry): entry is Extract<TranscriptLayoutEntry, { kind: "canonical" }> => entry.kind === "canonical",
  );
  const indices = new Map(canonical.map((entry) => [entry.key, entry.descriptorIndex]));
  const nextAttachedKeys = new Set(canonical.map((entry) => entry.key));
  return {
    materializeKeys: orderedKeys(
      [...nextAttachedKeys].filter((key) => !input.attachedKeys.has(key)),
      indices,
    ),
    trimKeys: [],
    nextAttachedKeys,
    spacerSegments: [],
    anchorKey: input.anchorKey,
    overBudgetReason: "layout extent unavailable",
  };
}

export function planTranscriptDomWindow(input: TranscriptDomWindowPlannerInput): TranscriptDomWindowPlan {
  const gap = Number.isFinite(input.rowGapPx) && input.rowGapPx >= 0 ? input.rowGapPx : 0;
  let cursor = 0;
  const positioned: PositionedEntry[] = [];
  for (const [index, entry] of input.entries.entries()) {
    if (index > 0) {
      cursor += gap;
      if (!Number.isFinite(cursor)) return unavailableLayoutPlan(input);
    }
    const start = cursor;
    cursor += entry.extentPx;
    if (!Number.isFinite(cursor) || cursor <= start) return unavailableLayoutPlan(input);
    positioned.push({ entry, start, end: cursor });
  }
  const canonical = positioned.filter(
    (item): item is PositionedEntry & { entry: Extract<TranscriptLayoutEntry, { kind: "canonical" }> } => item.entry.kind === "canonical",
  );
  let canonicalCursor = 0;
  const canonicalCoordinates = new Map<string, { start: number; end: number }>();
  for (const [index, item] of canonical.entries()) {
    if (index > 0) {
      canonicalCursor += gap;
      if (!Number.isFinite(canonicalCursor)) return unavailableLayoutPlan(input);
    }
    const start = canonicalCursor;
    canonicalCursor += item.entry.extentPx;
    if (!Number.isFinite(canonicalCursor) || canonicalCursor <= start) {
      return unavailableLayoutPlan(input);
    }
    canonicalCoordinates.set(item.entry.key, { start, end: canonicalCursor });
  }
  const indices = new Map(canonical.map((item) => [item.entry.key, item.entry.descriptorIndex]));
  const knownKeys = new Set(indices.keys());
  const pinned = new Set([...input.pinnedKeys].filter((key) => knownKeys.has(key)));
  const target = new Set<string>(pinned);
  const height = input.viewport.clientHeight;

  if (height <= 0) {
    for (const key of input.attachedKeys) if (knownKeys.has(key)) target.add(key);
    if (input.attachedKeys.size === 0 && canonical.length > 0) {
      target.add(canonical[canonical.length - 1].entry.key);
    }
  } else {
    const bottom = input.activation === "initial" && input.viewport.following
      ? cursor
      : input.viewport.scrollTop + height;
    const top = input.activation === "initial" && input.viewport.following
      ? Math.max(0, bottom - height)
      : input.viewport.scrollTop;
    const rangeStart = Math.max(0, top - input.budget.overscanViewportsBefore * height);
    const rangeEnd = Math.min(cursor, bottom + input.budget.overscanViewportsAfter * height);
    const candidates = canonical.filter((item) => item.end > rangeStart && item.start < rangeEnd);
    const viewportItems = candidates.filter((item) => item.end > top && item.start < bottom);
    for (const item of viewportItems) target.add(item.entry.key);

    const cap = Math.max(0, Math.floor(input.budget.maxAttachedUnpinnedBlocks));
    let unpinnedCount = viewportItems.filter((item) => !pinned.has(item.entry.key)).length;
    const extras = candidates
      .filter((item) => !target.has(item.entry.key) && !pinned.has(item.entry.key))
      .sort((a, b) => {
        const distanceA = Math.max(0, top - a.end, a.start - bottom);
        const distanceB = Math.max(0, top - b.end, b.start - bottom);
        return distanceA - distanceB || a.entry.descriptorIndex - b.entry.descriptorIndex;
      });
    for (const item of extras) {
      if (unpinnedCount >= cap) break;
      target.add(item.entry.key);
      unpinnedCount += 1;
    }
  }

  if (input.anchorKey && input.attachedKeys.has(input.anchorKey) && knownKeys.has(input.anchorKey)) {
    target.add(input.anchorKey);
  }
  const materializeKeys = orderedKeys([...target].filter((key) => !input.attachedKeys.has(key)), indices);
  let trimKeys = orderedKeys(
    [...input.attachedKeys].filter((key) => knownKeys.has(key) && !target.has(key)),
    indices,
  );
  let overBudgetReason: string | null = null;
  if (height > 0 && trimKeys.length > 0 && input.anchorKey === null) {
    overBudgetReason = "anchor unavailable";
    for (const key of trimKeys) target.add(key);
    trimKeys = [];
  }

  const nextAttachedKeys = new Set(orderedKeys(target, indices));
  const spacerSegments: TranscriptSpacerSegment[] = [];
  let segment: Array<PositionedEntry & { entry: Extract<TranscriptLayoutEntry, { kind: "canonical" }> }> = [];
  const flush = () => {
    if (segment.length === 0) return;
    const first = segment[0];
    const last = segment[segment.length - 1];
    spacerSegments.push({
      startIndex: first.entry.descriptorIndex,
      endIndex: last.entry.descriptorIndex + 1,
      omittedKeys: segment.map((item) => item.entry.key),
      canonicalStartPx: canonicalCoordinates.get(first.entry.key)!.start,
      canonicalEndPx: canonicalCoordinates.get(last.entry.key)!.end,
      cssHeightPx: segment.reduce((sum, item) => sum + item.entry.extentPx, 0) + gap * Math.max(0, segment.length - 1),
    });
    segment = [];
  };
  for (const item of positioned) {
    if (item.entry.kind === "external" || nextAttachedKeys.has(item.entry.key)) {
      flush();
    } else {
      segment.push(item as PositionedEntry & { entry: Extract<TranscriptLayoutEntry, { kind: "canonical" }> });
    }
  }
  flush();

  return {
    materializeKeys,
    trimKeys,
    nextAttachedKeys,
    spacerSegments,
    anchorKey: input.anchorKey,
    overBudgetReason,
  };
}

export interface TranscriptDomWindowStagedBlock {
  key: string;
  roots: HTMLElement[];
  primary: HTMLElement;
}

export interface TranscriptDomWindowAnchorJournal {
  key: string;
  oldRoot: HTMLElement;
  offsetFromViewportTop: number;
  scrollTop: number;
  scrollHeight: number;
  interactionGeneration: number;
}

export interface TranscriptDomWindowTransactionState {
  generation: number;
  attachedKeys: Set<string>;
  heights: Map<string, number>;
}

export interface TranscriptDomWindowTransactionInput<
  State extends TranscriptDomWindowTransactionState = TranscriptDomWindowTransactionState,
> {
  root: HTMLElement;
  sourceChildren: readonly Element[];
  externalSourceChildren: ReadonlySet<Element>;
  nextChildren: readonly Element[];
  expectedNextChildren: readonly Element[];
  existingBlocks: ReadonlyMap<string, TranscriptDomWindowStagedBlock>;
  promotedExistingKeys?: ReadonlySet<string>;
  plan: TranscriptDomWindowPlan;
  stagedBlocks: ReadonlyMap<string, TranscriptDomWindowStagedBlock>;
  spacers: readonly { element: HTMLElement; segment: TranscriptSpacerSegment }[];
  anchorJournal: TranscriptDomWindowAnchorJournal | null;
  expectedInteractionGeneration: number;
  expectedWindowGeneration: number;
  validateInteractionGeneration: (generation: number) => boolean;
  validateWindowGeneration: (generation: number) => boolean;
  measureBlock: (block: TranscriptDomWindowStagedBlock) => number;
  writeScrollTop: (value: number) => void;
  resolvePrimaryByKey: (key: string) => HTMLElement | null;
  currentState: State;
  nextState: State;
  viewportController?: import("./transcript-viewport").TranscriptViewportController;
  transactionKey?: object;
  following?: boolean;
  beforeMutation?: () => void;
  afterDomMutation?: () => void;
  beforeFinalValidation?: () => void;
  promoteExistingBlocks?: () => void;
  rollbackPromotedBlocks?: () => void;
  reserveOwnerReservations?: () => boolean;
  validateOwnerReservations?: () => boolean;
  commitOwnerReservationsNoFail?: () => void;
  releaseOwnerReservations?: () => void;
  ownerPinnedKeys?: ReadonlySet<string>;
}

export type TranscriptDomWindowTransactionResult<
  State extends TranscriptDomWindowTransactionState = TranscriptDomWindowTransactionState,
> = { status: "applied"; state: State } | { status: "deferred"; reason: string };

const activeTranscriptWindowRoots = new WeakSet<HTMLElement>();

class DeferredTranscriptWindowTransaction extends Error {}

function sameChildIdentity(root: HTMLElement, expected: readonly Element[]): boolean {
  const actual = root.children;
  if (actual.length !== expected.length) return false;
  return expected.every((child, index) => actual[index] === child);
}

function isValidSpacerSegment(segment: TranscriptSpacerSegment): boolean {
  const { startIndex, endIndex, omittedKeys, canonicalStartPx, canonicalEndPx, cssHeightPx } = segment;
  return Number.isSafeInteger(startIndex)
    && Number.isSafeInteger(endIndex)
    && startIndex >= 0
    && endIndex > startIndex
    && omittedKeys.length === endIndex - startIndex
    && omittedKeys.length > 0
    && new Set(omittedKeys).size === omittedKeys.length
    && omittedKeys.every((key) => typeof key === "string" && key.length > 0)
    && Number.isFinite(canonicalStartPx)
    && canonicalStartPx >= 0
    && Number.isFinite(canonicalEndPx)
    && canonicalEndPx > canonicalStartPx
    && Number.isFinite(cssHeightPx)
    && cssHeightPx > 0;
}

function sameSpacerSegment(a: TranscriptSpacerSegment, b: TranscriptSpacerSegment): boolean {
  return isValidSpacerSegment(a)
    && isValidSpacerSegment(b)
    && a.startIndex === b.startIndex
    && a.endIndex === b.endIndex
    && a.canonicalStartPx === b.canonicalStartPx
    && a.canonicalEndPx === b.canonicalEndPx
    && a.cssHeightPx === b.cssHeightPx
    && a.omittedKeys.length === b.omittedKeys.length
    && a.omittedKeys.every((key, index) => key === b.omittedKeys[index]);
}

function isValidTranscriptWindowSpacer(element: HTMLElement): boolean {
  const hasReconciliationMetadata = [
    element.dataset.reconcileKey,
    element.dataset.reconcileFingerprint,
    element.dataset.reconcileShape,
    element.dataset.reconcileTurnId,
    element.dataset.reconcileRootCount,
    element.dataset.reconcileMemberNodeIds,
    element.dataset.reconcileToolCallIds,
    element.dataset.reconcileFileChangeKeys,
  ].some((value) => value !== undefined);
  const nativelyFocusable = element.matches(
    "button, input, select, textarea, summary, a[href], [contenteditable]",
  );
  return element.classList.contains("transcript-window-spacer")
    && element.getAttribute("aria-hidden") === "true"
    && element.tabIndex < 0
    && !nativelyFocusable
    && element.dataset.itemId === undefined
    && !hasReconciliationMetadata;
}

function hasValidStringArrayMetadata(value: string | undefined, allowEmpty: boolean): boolean {
  if (value === undefined) return false;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed)
      && (allowEmpty || parsed.length > 0)
      && parsed.every((item) => typeof item === "string" && item.length > 0);
  } catch {
    return false;
  }
}

function isValidTranscriptWindowBlock(
  key: string,
  block: TranscriptDomWindowStagedBlock,
): boolean {
  const { primary, roots } = block;
  const rootCount = Number(primary.dataset.reconcileRootCount);
  return block.key === key
    && roots.length > 0
    && primary === roots[0]
    && primary.dataset.reconcileKey === key
    && Boolean(primary.dataset.reconcileFingerprint)
    && Boolean(primary.dataset.reconcileShape)
    && Number.isSafeInteger(rootCount)
    && rootCount === roots.length
    && hasValidStringArrayMetadata(primary.dataset.reconcileMemberNodeIds, false)
    && hasValidStringArrayMetadata(primary.dataset.reconcileToolCallIds, true)
    && hasValidStringArrayMetadata(primary.dataset.reconcileFileChangeKeys, true);
}

function isValidPromotedTranscriptWindowBlock(
  key: string,
  block: TranscriptDomWindowStagedBlock,
): boolean {
  return block.key === key
    && block.roots.length > 0
    && block.primary === block.roots[0];
}

function parseStrictTranscriptWindowSpacer(element: HTMLElement): TranscriptSpacerSegment | null {
  if (!isValidTranscriptWindowSpacer(element)) return null;
  const value = element.dataset.transcriptSpacer;
  if (value === undefined) return null;
  try {
    const parsed = JSON.parse(value) as TranscriptSpacerSegment;
    return parsed && typeof parsed === "object" && isValidSpacerSegment(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function sameStringSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  return a.size === b.size && [...a].every((value) => b.has(value));
}

function validateTranscriptWindowNextChildren(
  input: TranscriptDomWindowTransactionInput,
): boolean {
  const source = new Set(input.sourceChildren);
  if (source.size !== input.sourceChildren.length) return false;
  if (input.promotedExistingKeys) {
    for (const key of input.promotedExistingKeys) {
      if (!input.existingBlocks.has(key)) return false;
    }
  }

  const ownedSourceRoots = new Set<Element>();
  for (const [key, block] of input.existingBlocks) {
    const promoted = input.promotedExistingKeys?.has(key) === true;
    if (promoted
      ? !isValidPromotedTranscriptWindowBlock(key, block)
      : !isValidTranscriptWindowBlock(key, block)) return false;
    const rootCount = Number(
      block.primary.dataset.reconcileRootCount
      ?? (promoted ? String(block.roots.length) : "1"),
    );
    if (!Number.isSafeInteger(rootCount) || rootCount !== block.roots.length) return false;
    const start = input.sourceChildren.indexOf(block.primary);
    if (start < 0 || start + block.roots.length > input.sourceChildren.length) return false;
    for (const [offset, root] of block.roots.entries()) {
      if (input.sourceChildren[start + offset] !== root
        || ownedSourceRoots.has(root)
        || input.externalSourceChildren.has(root)) return false;
      ownedSourceRoots.add(root);
    }
  }

  const sourceSpacers = new Set<Element>();
  for (const child of input.sourceChildren) {
    const element = child as HTMLElement;
    if (element.dataset.transcriptSpacer === undefined) continue;
    if (!parseStrictTranscriptWindowSpacer(element)) return false;
    sourceSpacers.add(element);
  }
  for (const external of input.externalSourceChildren) {
    if (!source.has(external) || ownedSourceRoots.has(external) || sourceSpacers.has(external)) return false;
  }
  for (const child of input.sourceChildren) {
    const classifications = Number(ownedSourceRoots.has(child))
      + Number(sourceSpacers.has(child))
      + Number(input.externalSourceChildren.has(child));
    if (classifications !== 1) return false;
  }

  if (input.spacers.length !== input.plan.spacerSegments.length) return false;
  const spacerElements = new Set<Element>();
  for (const [index, spacer] of input.spacers.entries()) {
    if (!isValidTranscriptWindowSpacer(spacer.element)
      || spacerElements.has(spacer.element)
      || !sameSpacerSegment(spacer.segment, input.plan.spacerSegments[index])) return false;
    spacerElements.add(spacer.element);
  }

  const removed = new Set<Element>(input.sourceChildren.filter(
    (child) => (child as HTMLElement).dataset.transcriptSpacer !== undefined,
  ));
  for (const key of input.plan.trimKeys) {
    const block = input.existingBlocks.get(key);
    if (!block) return false;
    for (const root of block.roots) removed.add(root);
  }

  const expected = new Set<Element>();
  for (const child of input.sourceChildren) {
    if (!removed.has(child)) expected.add(child);
  }
  for (const key of input.plan.materializeKeys) {
    const block = input.stagedBlocks.get(key);
    if (!block || !isValidTranscriptWindowBlock(key, block)) {
      return false;
    }
    for (const root of block.roots) {
      if (root.parentNode !== null || source.has(root) || expected.has(root)) return false;
      expected.add(root);
    }
  }
  for (const { element } of input.spacers) {
    if (source.has(element) || expected.has(element)) return false;
    expected.add(element);
  }

  if (input.nextChildren.length !== expected.size
    || input.expectedNextChildren.length !== input.nextChildren.length) return false;
  const observed = new Set<Element>();
  for (const [index, child] of input.nextChildren.entries()) {
    if (!expected.has(child)
      || observed.has(child)
      || input.expectedNextChildren[index] !== child) return false;
    observed.add(child);
  }
  return observed.size === expected.size
    && sameStringSet(input.nextState.attachedKeys, input.plan.nextAttachedKeys)
    && input.nextState.generation === input.expectedWindowGeneration + 1;
}


/** Apply a complete DOM-window plan synchronously or leave the observed state untouched. */
export function applyTranscriptDomWindowTransaction<
  State extends TranscriptDomWindowTransactionState,
>(input: TranscriptDomWindowTransactionInput<State>): TranscriptDomWindowTransactionResult<State> {
  if (input.viewportController && input.viewportController.transcript !== input.root) {
    return { status: "deferred", reason: "viewport controller root mismatch" };
  }
  if (activeTranscriptWindowRoots.has(input.root)) {
    return { status: "deferred", reason: "window transaction reentry" };
  }
  if (!sameChildIdentity(input.root, input.sourceChildren)) {
    return { status: "deferred", reason: "source children changed" };
  }
  if (!validateTranscriptWindowNextChildren(input)) {
    return { status: "deferred", reason: "invalid next transcript children" };
  }
  if (
    input.currentState.generation !== input.expectedWindowGeneration
    || (input.anchorJournal !== null
      && input.anchorJournal.interactionGeneration !== input.expectedInteractionGeneration)
    || !input.validateInteractionGeneration(input.expectedInteractionGeneration)
    || !input.validateWindowGeneration(input.expectedWindowGeneration)
  ) {
    return { status: "deferred", reason: "transaction generation changed" };
  }

  const anchor = input.anchorJournal;
  if (anchor && input.plan.trimKeys.includes(anchor.key)) {
    const replacement = input.stagedBlocks.get(anchor.key);
    if (!input.plan.materializeKeys.includes(anchor.key) || !replacement
      || replacement.primary === anchor.oldRoot) {
      return { status: "deferred", reason: "anchor cannot be trimmed" };
    }
  }

  if (input.ownerPinnedKeys
    && input.plan.trimKeys.some((key) => input.ownerPinnedKeys!.has(key))) {
    return { status: "deferred", reason: "owner-pinned key cannot be trimmed" };
  }

  if (input.reserveOwnerReservations
    && (!input.validateOwnerReservations
      || !input.commitOwnerReservationsNoFail
      || !input.releaseOwnerReservations)) {
    return { status: "deferred", reason: "incomplete owner reservation hooks" };
  }

  let ownerReservationsHeld = false;
  const releaseOwnerReservations = (): void => {
    if (!ownerReservationsHeld) return;
    ownerReservationsHeld = false;
    input.releaseOwnerReservations?.();
  };
  try {
    if (input.reserveOwnerReservations) {
      ownerReservationsHeld = true;
      if (!input.reserveOwnerReservations()) {
        releaseOwnerReservations();
        return { status: "deferred", reason: "owner reservation conflict" };
      }
    }
    if (input.validateOwnerReservations && !input.validateOwnerReservations()) {
      releaseOwnerReservations();
      return { status: "deferred", reason: "owner reservation validation failed" };
    }
  } catch (error) {
    releaseOwnerReservations();
    throw error;
  }

  const originalChildren = [...input.sourceChildren];
  const nextChildren = [...input.nextChildren];
  const originalScrollTop = anchor?.scrollTop ?? input.root.scrollTop;
  const originalNextHeights = new Map(input.nextState.heights);
  const spacerElements = new Set<HTMLElement>([
    ...originalChildren.filter(
      (child): child is HTMLElement => (
        child instanceof HTMLElement && child.dataset.transcriptSpacer !== undefined
      ),
    ),
    ...input.spacers.map((item) => item.element),
  ]);
  const originalSpacerAttributes = [...spacerElements].map((element) => ({
    element,
    metadata: element.getAttribute("data-transcript-spacer"),
    style: element.getAttribute("style"),
  }));
  let deferredReason: string | null = null;

  const rollback = (): void => {
    input.rollbackPromotedBlocks?.();
    input.root.replaceChildren(...originalChildren);
    for (const saved of originalSpacerAttributes) {
      if (saved.metadata === null) saved.element.removeAttribute("data-transcript-spacer");
      else saved.element.setAttribute("data-transcript-spacer", saved.metadata);
      if (saved.style === null) saved.element.removeAttribute("style");
      else saved.element.setAttribute("style", saved.style);
    }
    input.root.scrollTop = originalScrollTop;
    input.nextState.heights = new Map(originalNextHeights);
  };

  let measuredHeights: Map<string, number> | null = null;
  const mutate = (): void => {
    input.beforeMutation?.();
    for (const { element, segment } of input.spacers) {
      element.dataset.transcriptSpacer = JSON.stringify(segment);
      element.style.height = `${segment.cssHeightPx}px`;
    }
    input.root.replaceChildren(...nextChildren);
    input.afterDomMutation?.();
    input.promoteExistingBlocks?.();

    input.beforeFinalValidation?.();
    if (!sameChildIdentity(input.root, nextChildren)
      || !input.validateInteractionGeneration(input.expectedInteractionGeneration)
      || !input.validateWindowGeneration(input.expectedWindowGeneration)
      || (input.validateOwnerReservations && !input.validateOwnerReservations())) {
      throw new DeferredTranscriptWindowTransaction("final transaction validation failed");
    }

    const heights = new Map(input.nextState.heights);
    for (const key of input.plan.materializeKeys) {
      const block = input.stagedBlocks.get(key)!;
      const extent = input.measureBlock(block);
      if (!Number.isFinite(extent) || extent <= 0) {
        throw new Error(`invalid measured extent for ${key}`);
      }
      heights.set(key, extent);
    }

    if (anchor) {
      const primary = input.resolvePrimaryByKey(anchor.key);
      if (!primary || primary.dataset.reconcileKey !== anchor.key) {
        throw new DeferredTranscriptWindowTransaction("anchor primary unavailable");
      }
      const matches = [...input.root.children].filter(
        (child) => (child as HTMLElement).dataset.reconcileKey === anchor.key,
      );
      if (matches.length !== 1 || matches[0] !== primary) {
        throw new DeferredTranscriptWindowTransaction("anchor primary ambiguous");
      }
      const nextOffset = primary.getBoundingClientRect().top
        - input.root.getBoundingClientRect().top;
      if (!Number.isFinite(nextOffset)) throw new Error("invalid anchor geometry");
      input.writeScrollTop(anchor.scrollTop + nextOffset - anchor.offsetFromViewportTop);
    }

    measuredHeights = heights;
  };

  activeTranscriptWindowRoots.add(input.root);
  try {
    try {
      if (input.viewportController) {
        const transactionKey = input.transactionKey ?? input;
        input.viewportController.flushMutationNow(
          transactionKey,
          mutate,
          { followAfterMutation: input.following === true },
        );
        if (measuredHeights === null) {
          input.viewportController.cancelMutation(transactionKey);
          throw new DeferredTranscriptWindowTransaction(
            "viewport transaction did not execute synchronously",
          );
        }
      } else {
        mutate();
      }

      if (measuredHeights === null) {
        throw new DeferredTranscriptWindowTransaction("window mutation did not complete");
      }
      if (ownerReservationsHeld) {
        input.commitOwnerReservationsNoFail!();
        ownerReservationsHeld = false;
      }
      input.nextState.heights = measuredHeights;
    } catch (error) {
      rollback();
      releaseOwnerReservations();
      if (error instanceof DeferredTranscriptWindowTransaction) {
        deferredReason = error.message;
      } else {
        throw error;
      }
    }
  } finally {
    activeTranscriptWindowRoots.delete(input.root);
  }

  return deferredReason
    ? { status: "deferred", reason: deferredReason }
    : { status: "applied", state: input.nextState };
}
