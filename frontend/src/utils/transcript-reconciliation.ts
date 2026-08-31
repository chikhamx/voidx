import type { TranscriptNode } from "../rpc/protocol";

export const PRODUCTION_RENDERER_SHAPE_VERSION = "production-v1";

export interface ProductionMessageClassification {
  shape: string;
  suppressed: boolean;
  text: string;
}

export interface TranscriptNodeDescriptor {
  key: string;
  nodeId: string;
  nodeType: string;
  turnId: string | null;
  toolCallId: string | null;
  rendererShapeVersion: string;
  fingerprint: string;
  node: TranscriptNode;
  memberNodeIds: string[];
  memberNodes: TranscriptNode[];
}

const RICH_TAG = /\[(\/)?(?:bold|dim|italic|underline|strike|red|green|yellow|blue|magenta|cyan|white|black|#[0-9A-Fa-f]{6})\]/g;
const SESSION_CHANGE_LINE = /^\s*(Created|Modified|Deleted)\s+(.+?)\s+\+(\d+)\s+[−-](\d+)\s*$/i;
const TURN_STATS = /✻\s*([\w.]+s)\s*·\s*(\d+)\s*calls\s*·\s*([\w.]+)\s*in\s*([\w.]+)\s*out/;

function payloadOf(node: TranscriptNode): Record<string, unknown> {
  return (node.payload as Record<string, unknown> | undefined) ?? {};
}

function stripRichMarkup(value: unknown): string {
  return String(value ?? "").replace(RICH_TAG, "");
}

function productionText(node: TranscriptNode): string {
  const payload = payloadOf(node);
  if (typeof payload.raw_text === "string") return payload.raw_text;
  return stripRichMarkup([node.header ?? node.title ?? "", ...(node.body_lines ?? [])].join("\n"));
}

function isSessionChangeSummary(text: string): boolean {
  const lines = text.split("\n").map((line) => stripRichMarkup(line).trim()).filter(Boolean);
  return lines.length > 0 && lines.every((line) => SESSION_CHANGE_LINE.test(line));
}

function isRuntimeNoise(text: string): boolean {
  const trimmed = text.trim();
  return trimmed.includes("MCP connecting:")
    || trimmed.includes("LSP setup failed:")
    || trimmed.includes("LSP startup:")
    || trimmed.includes("LSP warmup:")
    || (trimmed.includes("→") && /(warming\.\.\.|ready|failed)/.test(trimmed));
}

function switchNotification(text: string): boolean {
  return stripRichMarkup(text).replace(/\x1B\[[0-9;]*[a-zA-Z]/g, "").includes("switched");
}

export function classifyProductionMessage(node: TranscriptNode): ProductionMessageClassification {
  const payload = payloadOf(node);
  const text = productionText(node);
  const style = String(payload.style ?? payload.role ?? "text").toLowerCase();

  if (TURN_STATS.test(stripRichMarkup(text))) {
    return { shape: "message-stats", suppressed: false, text };
  }
  if (switchNotification(text)) {
    return { shape: "suppressed-switch", suppressed: true, text };
  }
  if (isSessionChangeSummary(text)) {
    return { shape: "message-file-summary", suppressed: false, text };
  }
  if (isRuntimeNoise(text)) {
    return { shape: "suppressed-runtime-noise", suppressed: true, text };
  }
  if (style === "thought") {
    return { shape: "message-thought", suppressed: false, text };
  }
  if (style === "diff") {
    return { shape: "message-diff", suppressed: false, text };
  }
  if (node.node_type === "error" || node.node_type === "warn" || style === "error" || style === "warning") {
    return { shape: "suppressed-snapshot-notice", suppressed: true, text };
  }
  return { shape: `message-${style}`, suppressed: false, text };
}

export function deriveTurnOwners(nodes: readonly TranscriptNode[]): Array<string | null> {
  const ids = new Set<string>();
  let currentTurnId: string | null = null;
  return nodes.map((node) => {
    if (!node.id) throw new Error("empty transcript node id");
    if (ids.has(node.id)) throw new Error(`duplicate transcript node id: ${node.id}`);
    ids.add(node.id);
    if (node.node_type === "turn") currentTurnId = node.id;
    return currentTurnId;
  });
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  if (value && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item !== undefined) result[key] = canonicalize(item);
    }
    return result;
  }
  return value;
}

function commonFingerprintFields(node: TranscriptNode, turnId: string | null): Record<string, unknown> {
  return {
    rendererShapeVersion: PRODUCTION_RENDERER_SHAPE_VERSION,
    node_type: node.node_type,
    id: node.id,
    parent_id: node.parent_id ?? null,
    turnId,
    status: node.status,
    collapsed: node.collapsed,
    title: node.title,
    header: node.header,
    body_lines: node.body_lines,
  };
}

function fingerprintFields(node: TranscriptNode, turnId: string | null): Record<string, unknown> {
  const payload = payloadOf(node);
  const common = commonFingerprintFields(node, turnId);
  switch (node.node_type) {
    case "turn":
      return { ...common, payload: pick(payload, ["style", "text", "raw_text"]) };
    case "message": {
      const classification = classifyProductionMessage(node);
      return {
        ...common,
        messageShape: classification.shape,
        meta: node.meta,
        elapsed: node.elapsed,
        payload: pick(payload, ["role", "style", "raw_text", "title"]),
      };
    }
    case "assistant":
      return {
        ...common,
        meta: node.meta,
        elapsed: node.elapsed,
        payload: pick(payload, ["raw_text", "thinking_text", "phase"]),
      };
    case "thought":
      return { ...common, meta: node.meta, elapsed: node.elapsed, payload: pick(payload, ["raw_text"]) };
    case "tool_call":
    case "tool_result":
      return {
        ...common,
        tool_call_id: node.tool_call_id ?? null,
        elapsed: node.elapsed,
        payload: pick(payload, [
          "tool_name", "label", "args", "raw_args", "raw_text", "summary",
          "diff_text", "detail", "success", "error", "status",
        ]),
      };
    case "diff":
      return { ...common, payload: pick(payload, ["diff_text", "title"]) };
    case "status":
      return { ...common, payload: pick(payload, ["outcome", "detail", "label", "ok"]) };
    default:
      return common;
  }
}

function pick(source: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of keys) {
    if (source[key] !== undefined) result[key] = source[key];
  }
  return result;
}

function stableFingerprint(nodes: readonly TranscriptNode[], owners: readonly (string | null)[]): string {
  return JSON.stringify(canonicalize(nodes.map((node, index) => fingerprintFields(node, owners[index]))));
}

function isSkippedNode(node: TranscriptNode): boolean {
  return node.node_type === "root"
    || node.node_type === "startup"
    || node.node_type === "todo"
    || node.node_type === "permission"
    || node.node_type === "subagent"
    || node.node_type === "error"
    || node.node_type === "warn"
    || (node.node_type === "status" && payloadOf(node).outcome !== "compacted");
}

function hasVisibleTurnContent(node: TranscriptNode): boolean {
  const payload = payloadOf(node);
  return Boolean(
    String(payload.text ?? payload.raw_text ?? node.header ?? node.title ?? "").trim()
    || (node.body_lines ?? []).join("").trim(),
  );
}

function validateToolMembers(nodes: readonly TranscriptNode[]): void {
  const calls = new Set<string>();
  for (const node of nodes) {
    if (node.node_type === "tool_call") {
      if (!node.tool_call_id || calls.has(node.tool_call_id)) {
        throw new Error(`invalid or duplicate tool call id: ${node.tool_call_id ?? ""}`);
      }
      calls.add(node.tool_call_id);
    } else if (node.node_type === "tool_result" && (!node.tool_call_id || !calls.has(node.tool_call_id))) {
      throw new Error(`tool result without preceding call: ${node.tool_call_id ?? ""}`);
    }
  }
}

function descriptorFor(
  key: string,
  members: readonly TranscriptNode[],
  owners: readonly (string | null)[],
  turnId: string | null,
): TranscriptNodeDescriptor {
  const primary = members[0];
  return {
    key,
    nodeId: primary.id,
    nodeType: primary.node_type,
    turnId,
    toolCallId: primary.tool_call_id ?? null,
    rendererShapeVersion: PRODUCTION_RENDERER_SHAPE_VERSION,
    fingerprint: stableFingerprint(members, owners),
    node: primary,
    memberNodeIds: members.map((member) => member.id),
    memberNodes: [...members],
  };
}

export function buildTranscriptDescriptors(nodes: readonly TranscriptNode[]): TranscriptNodeDescriptor[] {
  const owners = deriveTurnOwners(nodes);
  const descriptors: TranscriptNodeDescriptor[] = [];

  for (let start = 0; start < nodes.length;) {
    const owner = owners[start];
    let end = start + 1;
    while (end < nodes.length && owners[end] === owner) end += 1;
    const group = nodes.slice(start, end);
    const groupOwners = owners.slice(start, end);
    const hasTools = group.some((node) => node.node_type === "tool_call" || node.node_type === "tool_result");

    if (hasTools) {
      validateToolMembers(group);
      const renderable = group.filter((node) => !isSkippedNode(node));
      if (renderable.length > 0) {
        const renderableOwners = renderable.map(() => owner);
        descriptors.push(descriptorFor(
          owner ? `turn-with-tools:${owner}` : "prefix-with-tools",
          renderable,
          renderableOwners,
          owner,
        ));
      }
      start = end;
      continue;
    }

    for (let index = 0; index < group.length;) {
      const current = group[index];
      if (isSkippedNode(current) || (current.node_type === "turn" && !hasVisibleTurnContent(current))) {
        index += 1;
        continue;
      }
      if (current.node_type === "message" && classifyProductionMessage(current).suppressed) {
        index += 1;
        continue;
      }
      if (current.node_type === "thought"
        || (current.node_type === "message" && classifyProductionMessage(current).shape === "message-thought")) {
        let thoughtEnd = index + 1;
        while (thoughtEnd < group.length) {
          const candidate = group[thoughtEnd];
          const thought = candidate.node_type === "thought"
            || (candidate.node_type === "message" && classifyProductionMessage(candidate).shape === "message-thought");
          if (!thought) break;
          thoughtEnd += 1;
        }
        const members = group.slice(index, thoughtEnd);
        descriptors.push(descriptorFor(
          `thoughts:${owner ?? "prefix"}:${current.id}`,
          members,
          groupOwners.slice(index, thoughtEnd),
          owner,
        ));
        index = thoughtEnd;
        continue;
      }
      descriptors.push(descriptorFor(
        `node:${current.id}`,
        [current],
        [groupOwners[index]],
        owner,
      ));
      index += 1;
    }
    start = end;
  }

  return descriptors;
}

export interface TranscriptLogicalBlock {
  key: string;
  fingerprint: string;
  rendererShapeVersion: string;
  turnId: string | null;
  roots: HTMLElement[];
  primary: HTMLElement;
  memberNodeIds: Set<string>;
  ownedToolCallIds: Set<string>;
  ownedFileChangeKeys: Set<string>;
}

export type ExistingTranscriptEntry =
  | { kind: "block"; key: string }
  | { kind: "retained"; root: HTMLElement };

export interface ExistingTranscriptIndex {
  blocks: TranscriptLogicalBlock[];
  byKey: Map<string, TranscriptLogicalBlock>;
  entries: ExistingTranscriptEntry[];
  sourceChildren: readonly ChildNode[];
}

export interface TranscriptReconciliationPlan {
  keep: TranscriptLogicalBlock[];
  replace: Array<{ current: TranscriptLogicalBlock; descriptor: TranscriptNodeDescriptor }>;
  insert: TranscriptNodeDescriptor[];
  remove: TranscriptLogicalBlock[];
  order: Array<string | { retained: HTMLElement }>;
  requiresFullRecovery: boolean;
  reason: string | null;
  sourceChildren: readonly ChildNode[];
}

function parseStringArrayMetadata(value: string | undefined, name: string): Set<string> {
  if (!value) return new Set();
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`malformed ${name} reconciliation metadata`);
  }
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string" || !item)) {
    throw new Error(`malformed ${name} reconciliation metadata`);
  }
  return new Set(parsed);
}

function hasOrphanReconciliationMetadata(element: HTMLElement): boolean {
  return !element.dataset.reconcileKey && [
    element.dataset.reconcileFingerprint,
    element.dataset.reconcileShape,
    element.dataset.reconcileTurnId,
    element.dataset.reconcileRootCount,
    element.dataset.reconcileMemberNodeIds,
    element.dataset.reconcileToolCallIds,
    element.dataset.reconcileFileChangeKeys,
  ].some((value) => value !== undefined);
}

export function collectExistingTranscriptBlocks(
  root: HTMLElement,
  descriptors: readonly TranscriptNodeDescriptor[] = [],
  syntheticBlocks: ReadonlyMap<HTMLElement, TranscriptLogicalBlock> = new Map(),
): ExistingTranscriptIndex {
  const children = Array.from(root.children) as HTMLElement[];
  const blocks: TranscriptLogicalBlock[] = [];
  const byKey = new Map<string, TranscriptLogicalBlock>();
  const entries: ExistingTranscriptEntry[] = [];
  const legacyToolDescriptors = descriptors.filter(
    (descriptor) => descriptor.key.startsWith("turn-with-tools:") && descriptor.turnId,
  );
  const legacyToolTurns = new Map(
    legacyToolDescriptors.map((descriptor) => [descriptor.turnId!, descriptor]),
  );

  for (let index = 0; index < children.length;) {
    const primary = children[index];
    if (hasOrphanReconciliationMetadata(primary)) {
      throw new Error("reconciliation metadata without key");
    }
    const key = primary.dataset.reconcileKey;
    const synthetic = syntheticBlocks.get(primary);
    if (!key && synthetic) {
      if (byKey.has(synthetic.key)) {
        throw new Error(`duplicate synthetic reconciliation key: ${synthetic.key}`);
      }
      blocks.push(synthetic);
      byKey.set(synthetic.key, synthetic);
      entries.push({ kind: "block", key: synthetic.key });
      index += synthetic.roots.length;
      continue;
    }
    if (!key && primary.classList.contains("tool-group")) {
      const turnId = primary.dataset.turnId || "";
      const groupToolCallIds = new Set(
        [...primary.querySelectorAll<HTMLElement>("[data-tool-id]")]
          .map((element) => element.dataset.toolId)
          .filter((toolCallId): toolCallId is string => Boolean(toolCallId)),
      );
      const toolMatches = legacyToolDescriptors.filter((candidate) => (
        candidate.memberNodes.some((member) => (
          Boolean(member.tool_call_id) && groupToolCallIds.has(member.tool_call_id!)
        ))
      ));
      const descriptor = legacyToolTurns.get(turnId)
        ?? (toolMatches.length === 1 ? toolMatches[0] : undefined);
      if (descriptor) {
        const memberIds = new Set(descriptor.memberNodeIds);
        const roots = [primary];
        let cursor = index + 1;
        while (cursor < children.length) {
          const candidate = children[cursor];
          const candidateId = candidate.dataset.itemId || candidate.dataset.streamId || "";
          const isOwnedFileCard = candidate.classList.contains("file-change-card")
            && candidate.dataset.turnId === turnId;
          const isLiveTurnTail = !candidate.dataset.reconcileKey && (
            candidate.classList.contains("stream-buffer")
            || candidate.classList.contains("thought-item")
          );
          const isOwnedMember = memberIds.has(candidateId)
            || (candidate.classList.contains("thought-item")
              && [...memberIds].some((id) => candidateId === `${id}-thought`))
            || isLiveTurnTail;
          if (!isOwnedFileCard && !isOwnedMember) break;
          roots.push(candidate);
          cursor += 1;
        }
        const ownedToolCallIds = new Set(
          descriptor.memberNodes
            .map((member) => member.tool_call_id)
            .filter((toolCallId): toolCallId is string => Boolean(toolCallId)),
        );
        const block: TranscriptLogicalBlock = {
          key: descriptor.key,
          fingerprint: `legacy-live:${turnId}`,
          rendererShapeVersion: "legacy-live-v1",
          turnId,
          roots,
          primary,
          memberNodeIds: memberIds,
          ownedToolCallIds,
          ownedFileChangeKeys: roots.some((candidate) => candidate.classList.contains("file-change-card"))
            ? new Set([turnId])
            : new Set(),
        };
        blocks.push(block);
        byKey.set(block.key, block);
        entries.push({ kind: "block", key: block.key });
        index = cursor;
        continue;
      }
    }
    if (!key) {
      entries.push({ kind: "retained", root: primary });
      index += 1;
      continue;
    }
    if (byKey.has(key)) throw new Error(`duplicate reconciliation key: ${key}`);

    const fingerprint = primary.dataset.reconcileFingerprint;
    const rendererShapeVersion = primary.dataset.reconcileShape;
    const rootCountValue = primary.dataset.reconcileRootCount ?? "1";
    const rootCount = Number(rootCountValue);
    if (!fingerprint || !rendererShapeVersion || !Number.isSafeInteger(rootCount) || rootCount < 1) {
      throw new Error(`malformed reconciliation metadata for ${key}`);
    }
    if (index + rootCount > children.length) {
      throw new Error(`reconciliation root range exceeds transcript for ${key}`);
    }

    const roots = children.slice(index, index + rootCount);
    for (let offset = 1; offset < roots.length; offset += 1) {
      if (roots[offset].dataset.reconcileKey || hasOrphanReconciliationMetadata(roots[offset])) {
        throw new Error(`overlapping primary in reconciliation root range for ${key}`);
      }
    }

    const block: TranscriptLogicalBlock = {
      key,
      fingerprint,
      rendererShapeVersion,
      turnId: primary.dataset.reconcileTurnId || null,
      roots,
      primary,
      memberNodeIds: parseStringArrayMetadata(primary.dataset.reconcileMemberNodeIds, "member node ids"),
      ownedToolCallIds: parseStringArrayMetadata(primary.dataset.reconcileToolCallIds, "tool call ids"),
      ownedFileChangeKeys: parseStringArrayMetadata(primary.dataset.reconcileFileChangeKeys, "file change keys"),
    };
    blocks.push(block);
    byKey.set(key, block);
    entries.push({ kind: "block", key });
    index += rootCount;
  }

  return { blocks, byKey, entries, sourceChildren: [...children] };
}

function stalePlan(
  reason: string,
  sourceChildren: readonly ChildNode[] = [],
): TranscriptReconciliationPlan {
  return {
    keep: [],
    replace: [],
    insert: [],
    remove: [],
    order: [],
    requiresFullRecovery: true,
    reason,
    sourceChildren,
  };
}

function validateDescriptorKeys(descriptors: readonly TranscriptNodeDescriptor[]): void {
  const keys = new Set<string>();
  for (const descriptor of descriptors) {
    if (!descriptor.key) throw new Error("empty reconciliation descriptor key");
    if (keys.has(descriptor.key)) throw new Error(`duplicate reconciliation descriptor key: ${descriptor.key}`);
    keys.add(descriptor.key);
  }
}

interface WindowSegment {
  entryStart: number;
  entryEnd: number;
  keys: string[];
}

function windowSegments(
  entries: readonly ExistingTranscriptEntry[],
  pageKeys: ReadonlySet<string>,
): { segments: WindowSegment[]; keyToSegment: Map<string, number> } {
  const segments: WindowSegment[] = [];
  const keyToSegment = new Map<string, number>();
  let current: WindowSegment | null = null;

  entries.forEach((entry, entryIndex) => {
    const pageBlock = entry.kind === "block" && pageKeys.has(entry.key);
    if (!pageBlock) {
      current = null;
      return;
    }
    if (!current) {
      current = { entryStart: entryIndex, entryEnd: entryIndex + 1, keys: [] };
      segments.push(current);
    }
    current.entryEnd = entryIndex + 1;
    current.keys.push(entry.key);
    keyToSegment.set(entry.key, segments.length - 1);
  });

  return { segments, keyToSegment };
}

function windowedOrder(
  index: ExistingTranscriptIndex,
  descriptors: readonly TranscriptNodeDescriptor[],
): { order: Array<string | { retained: HTMLElement }>; reason: string | null } {
  const pageKeys = new Set(descriptors.map((descriptor) => descriptor.key));
  const existingPageKeys = descriptors
    .map((descriptor) => descriptor.key)
    .filter((key) => index.byKey.has(key));
  const { segments, keyToSegment } = windowSegments(index.entries, pageKeys);

  let previousSegment = -1;
  for (const key of existingPageKeys) {
    const segment = keyToSegment.get(key);
    if (segment === undefined) continue;
    if (segment < previousSegment) {
      return { order: [], reason: "descriptor order crosses windowed segment boundary" };
    }
    previousSegment = segment;
  }

  const newKeys = descriptors.filter((descriptor) => !index.byKey.has(descriptor.key));
  const assignedNewKeys = new Map<number, string[]>();
  for (const descriptor of newKeys) {
    const descriptorIndex = descriptors.indexOf(descriptor);
    let beforeSegment: number | undefined;
    let afterSegment: number | undefined;
    for (let position = descriptorIndex - 1; position >= 0; position -= 1) {
      beforeSegment = keyToSegment.get(descriptors[position].key);
      if (beforeSegment !== undefined) break;
    }
    for (let position = descriptorIndex + 1; position < descriptors.length; position += 1) {
      afterSegment = keyToSegment.get(descriptors[position].key);
      if (afterSegment !== undefined) break;
    }
    if (beforeSegment !== undefined && afterSegment !== undefined && beforeSegment !== afterSegment) {
      return { order: [], reason: "new descriptor anchors cross windowed segment boundary" };
    }
    const segment = beforeSegment ?? afterSegment;
    if (segment === undefined) {
      if (index.blocks.length > 0 && existingPageKeys.length > 0) {
        return { order: [], reason: "new windowed descriptor has no canonical segment anchor" };
      }
      assignedNewKeys.set(-1, [...(assignedNewKeys.get(-1) ?? []), descriptor.key]);
    } else {
      assignedNewKeys.set(segment, [...(assignedNewKeys.get(segment) ?? []), descriptor.key]);
    }
  }

  if (index.entries.length === 0) {
    return { order: descriptors.map((descriptor) => descriptor.key), reason: null };
  }

  const orderedKeysBySegment = new Map<number, string[]>();
  segments.forEach((segment, segmentIndex) => {
    const keys = descriptors
      .filter((descriptor) => keyToSegment.get(descriptor.key) === segmentIndex)
      .map((descriptor) => descriptor.key);
    const additions = assignedNewKeys.get(segmentIndex) ?? [];
    const descriptorPositions = new Map(descriptors.map((descriptor, position) => [descriptor.key, position]));
    orderedKeysBySegment.set(
      segmentIndex,
      [...keys, ...additions].sort((left, right) => descriptorPositions.get(left)! - descriptorPositions.get(right)!),
    );
  });

  const order: Array<string | { retained: HTMLElement }> = [];
  let entryIndex = 0;
  while (entryIndex < index.entries.length) {
    const segmentIndex = segments.findIndex((segment) => segment.entryStart === entryIndex);
    if (segmentIndex >= 0) {
      order.push(...(orderedKeysBySegment.get(segmentIndex) ?? []));
      entryIndex = segments[segmentIndex].entryEnd;
      continue;
    }
    const entry = index.entries[entryIndex];
    order.push(entry.kind === "block" ? entry.key : { retained: entry.root });
    entryIndex += 1;
  }
  order.push(...(assignedNewKeys.get(-1) ?? []));
  return { order, reason: null };
}

export function planTranscriptReconciliation(
  existing: ExistingTranscriptIndex,
  descriptors: readonly TranscriptNodeDescriptor[],
  options: { windowed: boolean },
): TranscriptReconciliationPlan {
  validateDescriptorKeys(descriptors);
  const keep: TranscriptLogicalBlock[] = [];
  const replace: Array<{ current: TranscriptLogicalBlock; descriptor: TranscriptNodeDescriptor }> = [];
  const insert: TranscriptNodeDescriptor[] = [];
  const descriptorKeys = new Set(descriptors.map((descriptor) => descriptor.key));

  for (const descriptor of descriptors) {
    const current = existing.byKey.get(descriptor.key);
    if (!current) {
      insert.push(descriptor);
    } else if (
      current.fingerprint === descriptor.fingerprint
      && current.rendererShapeVersion === descriptor.rendererShapeVersion
    ) {
      keep.push(current);
    } else {
      replace.push({ current, descriptor });
    }
  }

  const remove = options.windowed
    ? []
    : existing.blocks.filter((block) => !descriptorKeys.has(block.key));

  if (options.windowed) {
    const keptKeys = new Set(keep.map((block) => block.key));
    for (const block of existing.blocks) {
      if (!descriptorKeys.has(block.key) && !keptKeys.has(block.key)) {
        keep.push(block);
        keptKeys.add(block.key);
      }
    }
    const window = windowedOrder(existing, descriptors);
    if (window.reason) return stalePlan(window.reason, existing.sourceChildren);
    return {
      keep,
      replace,
      insert,
      remove,
      order: window.order,
      requiresFullRecovery: false,
      reason: null,
      sourceChildren: existing.sourceChildren,
    };
  }

  const retained = existing.entries
    .filter((entry): entry is Extract<ExistingTranscriptEntry, { kind: "retained" }> => entry.kind === "retained")
    .map((entry) => ({ retained: entry.root }));
  return {
    keep,
    replace,
    insert,
    remove,
    order: [...descriptors.map((descriptor) => descriptor.key), ...retained],
    requiresFullRecovery: false,
    reason: null,
    sourceChildren: existing.sourceChildren,
  };
}

export type TranscriptApplyFailurePoint =
  | "after-remove"
  | "after-replace"
  | "after-insert"
  | "after-order";

export interface TranscriptApplyOptions {
  failAt?: (point: TranscriptApplyFailurePoint) => void;
}

export type TranscriptApplyResult =
  | { status: "applied" }
  | { status: "stale"; reason: string };

function sameChildIdentity(root: HTMLElement, expected: readonly ChildNode[]): boolean {
  const current = Array.from(root.childNodes);
  return current.length === expected.length
    && current.every((child, index) => child === expected[index]);
}

function restoreTranscriptChildren(root: HTMLElement, original: readonly ChildNode[]): void {
  for (const child of Array.from(root.childNodes)) child.remove();
  for (const child of original) root.append(child);
}

export function applyTranscriptReconciliation(
  root: HTMLElement,
  plan: TranscriptReconciliationPlan,
  detachedBlocks: readonly TranscriptLogicalBlock[],
  options: TranscriptApplyOptions = {},
): TranscriptApplyResult {
  if (plan.requiresFullRecovery) {
    return { status: "stale", reason: plan.reason ?? "full recovery required" };
  }
  if (!sameChildIdentity(root, plan.sourceChildren)) {
    return { status: "stale", reason: "transcript children changed after plan" };
  }

  const detachedByKey = new Map(detachedBlocks.map((block) => [block.key, block]));
  const currentByKey = new Map<string, TranscriptLogicalBlock>();
  for (const block of plan.keep) currentByKey.set(block.key, block);
  for (const entry of plan.replace) currentByKey.set(entry.current.key, entry.current);
  for (const block of plan.remove) currentByKey.set(block.key, block);

  for (const entry of plan.replace) {
    if (!detachedByKey.has(entry.descriptor.key)) {
      return { status: "stale", reason: `missing detached replacement: ${entry.descriptor.key}` };
    }
  }
  for (const descriptor of plan.insert) {
    if (!detachedByKey.has(descriptor.key)) {
      return { status: "stale", reason: `missing detached insertion: ${descriptor.key}` };
    }
  }

  for (const entry of plan.replace) {
    if (entry.current.ownedFileChangeKeys.size === 0) continue;
    const next = detachedByKey.get(entry.descriptor.key)!;
    const retainedCards = entry.current.roots.filter((element) => (
      element.classList.contains("file-change-card")
      && element.dataset.turnId === entry.current.turnId
    ));
    if (retainedCards.length === 0) continue;
    const groupIndex = next.roots.findIndex((element) => element.classList.contains("tool-group"));
    if (groupIndex < 0) {
      return { status: "stale", reason: `file card handoff missing tool group: ${entry.current.key}` };
    }
    next.roots.splice(groupIndex + 1, 0, ...retainedCards);
    for (const key of entry.current.ownedFileChangeKeys) next.ownedFileChangeKeys.add(key);
    next.primary.dataset.reconcileRootCount = String(next.roots.length);
    next.primary.dataset.reconcileFileChangeKeys = JSON.stringify([...next.ownedFileChangeKeys]);
  }

  try {
    for (const block of plan.remove) {
      for (const element of block.roots) element.remove();
    }
    options.failAt?.("after-remove");

    for (const entry of plan.replace) {
      const next = detachedByKey.get(entry.descriptor.key)!;
      const [first, ...rest] = entry.current.roots;
      first.replaceWith(...next.roots);
      for (const element of rest) {
        if (!next.roots.includes(element)) element.remove();
      }
      currentByKey.set(entry.current.key, next);
    }
    options.failAt?.("after-replace");

    for (const descriptor of plan.insert) {
      const next = detachedByKey.get(descriptor.key)!;
      root.append(...next.roots);
      currentByKey.set(descriptor.key, next);
    }
    options.failAt?.("after-insert");

    for (const item of plan.order) {
      if (typeof item === "string") {
        const block = currentByKey.get(item);
        if (!block) throw new Error(`missing ordered reconciliation block: ${item}`);
        root.append(...block.roots);
      } else {
        root.append(item.retained);
      }
    }
    options.failAt?.("after-order");
    return { status: "applied" };
  } catch (error) {
    restoreTranscriptChildren(root, plan.sourceChildren);
    throw error;
  }
}
