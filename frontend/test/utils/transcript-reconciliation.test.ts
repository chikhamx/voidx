import { describe, expect, it } from "vitest";
import type { TranscriptNode } from "../../src/rpc/protocol";
import {
  buildTranscriptDescriptors,
  classifyProductionMessage,
  collectExistingTranscriptBlocks,
  deriveTurnOwners,
  planTranscriptReconciliation,
  applyTranscriptReconciliation,
} from "../../src/utils/transcript-reconciliation";

function node(
  id: string,
  nodeType: TranscriptNode["node_type"],
  payload: Record<string, unknown> = {},
  extra: Partial<TranscriptNode> = {},
): TranscriptNode {
  return {
    id,
    node_type: nodeType,
    payload,
    ...extra,
  } as TranscriptNode;
}

describe("deriveTurnOwners", () => {
  it("assigns the nearest preceding turn without using parent_id as ownership", () => {
    const nodes = [
      node("prefix", "message", { raw_text: "before" }),
      node("turn-a", "turn", { text: "hello" }),
      node("answer-a", "assistant", { raw_text: "one" }, { parent_id: "turn-a" }),
      node("turn-b", "turn", { text: "next" }),
      node("answer-b", "assistant", { raw_text: "two" }, { parent_id: "answer-a" }),
    ];

    expect(deriveTurnOwners(nodes)).toEqual([null, "turn-a", "turn-a", "turn-b", "turn-b"]);
  });

  it("rejects duplicate and empty node ids before descriptor construction", () => {
    expect(() => deriveTurnOwners([
      node("same", "turn"),
      node("same", "message"),
    ])).toThrow(/duplicate.*same/i);
    expect(() => deriveTurnOwners([node("", "message")])).toThrow(/empty.*id/i);
  });
});

describe("buildTranscriptDescriptors", () => {
  it("groups every renderable member of a tool turn under one compound key", () => {
    const descriptors = buildTranscriptDescriptors([
      node("turn-1", "turn", { text: "question" }),
      node("thought-1", "thought", { raw_text: "thinking" }),
      node("call-1", "tool_call", { tool_name: "read", args: { path: "a.ts" } }, { tool_call_id: "tool-1" }),
      node("message-1", "message", { style: "text", raw_text: "between" }),
      node("result-1", "tool_result", { raw_text: "ok" }, { tool_call_id: "tool-1" }),
      node("answer-1", "assistant", { raw_text: "done" }),
    ]);

    expect(descriptors).toHaveLength(1);
    expect(descriptors[0]).toMatchObject({
      key: "turn-with-tools:turn-1",
      turnId: "turn-1",
      rendererShapeVersion: "production-v1",
      memberNodeIds: ["turn-1", "thought-1", "call-1", "message-1", "result-1", "answer-1"],
    });
  });

  it("uses stable fingerprints and ignores unknown payload fields", () => {
    const base = node("answer", "assistant", {
      raw_text: "visible",
      thinking_text: "thought",
      phase: "done",
      ignored_internal_value: 1,
    }, {
      parent_id: "turn-1",
      status: "done",
      elapsed: 1.25,
    });
    const ignoredChange = node("answer", "assistant", {
      raw_text: "visible",
      thinking_text: "thought",
      phase: "done",
      ignored_internal_value: 999,
    }, {
      parent_id: "turn-1",
      status: "done",
      elapsed: 1.25,
    });
    const visibleChange = node("answer", "assistant", {
      raw_text: "changed",
      thinking_text: "thought",
      phase: "done",
    }, {
      parent_id: "turn-1",
      status: "done",
      elapsed: 1.25,
    });

    const first = buildTranscriptDescriptors([node("turn-1", "turn"), base])[0];
    const second = buildTranscriptDescriptors([node("turn-1", "turn"), ignoredChange])[0];
    const changed = buildTranscriptDescriptors([node("turn-1", "turn"), visibleChange])[0];

    expect(second.fingerprint).toBe(first.fingerprint);
    expect(changed.fingerprint).not.toBe(first.fingerprint);
  });

  it("canonicalizes nested object keys while preserving array order", () => {
    const first = node("call", "tool_call", {
      tool_name: "bash",
      args: { z: 1, nested: { b: 2, a: 1 }, list: ["a", "b"] },
    }, { tool_call_id: "tool" });
    const reordered = node("call", "tool_call", {
      args: { list: ["a", "b"], nested: { a: 1, b: 2 }, z: 1 },
      tool_name: "bash",
    }, { tool_call_id: "tool" });
    const arrayChanged = node("call", "tool_call", {
      tool_name: "bash",
      args: { z: 1, nested: { b: 2, a: 1 }, list: ["b", "a"] },
    }, { tool_call_id: "tool" });

    const fingerprint = (value: TranscriptNode) => buildTranscriptDescriptors([
      node("turn", "turn"),
      value,
    ])[0].fingerprint;

    expect(fingerprint(reordered)).toBe(fingerprint(first));
    expect(fingerprint(arrayChanged)).not.toBe(fingerprint(first));
  });
});

describe("classifyProductionMessage", () => {
  it("applies stats before file-like text and file summary before runtime noise", () => {
    expect(classifyProductionMessage(node("stats", "message", {
      style: "text",
      raw_text: "✻ 1.2s · 3 calls · 4k in 2k out\nCreated file.ts +1 -0",
    }))).toMatchObject({ shape: "message-stats", suppressed: false });

    expect(classifyProductionMessage(node("file", "message", {
      style: "text",
      raw_text: "Created MCP connecting: file.ts +1 -0",
    }))).toMatchObject({ shape: "message-file-summary", suppressed: false });
  });

  it("suppresses snapshot notices without producing a renderable root", () => {
    expect(classifyProductionMessage(node("warning", "message", {
      style: "warning",
      raw_text: "old warning",
    }))).toEqual({
      shape: "suppressed-snapshot-notice",
      suppressed: true,
      text: "old warning",
    });
  });
});

function existingRoot(
  key: string,
  fingerprint: string,
  options: {
    shape?: string;
    turnId?: string;
    memberNodeIds?: string[];
    rootCount?: number;
  } = {},
): HTMLElement[] {
  const roots = Array.from({ length: options.rootCount ?? 1 }, (_, index) => {
    const element = document.createElement("div");
    element.textContent = `${key}:${index}`;
    return element;
  });
  const primary = roots[0];
  primary.dataset.reconcileKey = key;
  primary.dataset.reconcileFingerprint = fingerprint;
  primary.dataset.reconcileShape = options.shape ?? "production-v1";
  if (options.turnId) primary.dataset.reconcileTurnId = options.turnId;
  primary.dataset.reconcileRootCount = String(roots.length);
  primary.dataset.reconcileMemberNodeIds = JSON.stringify(options.memberNodeIds ?? []);
  return roots;
}

function descriptor(id: string, rawText: string) {
  return buildTranscriptDescriptors([
    node(id, "message", { style: "text", raw_text: rawText }),
  ])[0];
}

describe("collectExistingTranscriptBlocks", () => {
  it("indexes one primary with a contiguous compound root range", () => {
    const root = document.createElement("div");
    const compound = existingRoot("turn-with-tools:t1", "fp", {
      turnId: "t1",
      memberNodeIds: ["t1", "call", "result"],
      rootCount: 3,
    });
    root.append(...compound);

    const index = collectExistingTranscriptBlocks(root);

    expect(index.blocks.map((block) => ({
      key: block.key,
      roots: block.roots,
      primary: block.primary,
      turnId: block.turnId,
      memberNodeIds: [...block.memberNodeIds],
    }))).toEqual([{
      key: "turn-with-tools:t1",
      roots: compound,
      primary: compound[0],
      turnId: "t1",
      memberNodeIds: ["t1", "call", "result"],
    }]);
    expect(index.entries).toEqual([{ kind: "block", key: "turn-with-tools:t1" }]);
  });

  it("rejects duplicate keys, overlapping ranges, and orphan reconciliation metadata", () => {
    const duplicate = document.createElement("div");
    duplicate.append(...existingRoot("node:a", "one"), ...existingRoot("node:a", "two"));
    expect(() => collectExistingTranscriptBlocks(duplicate)).toThrow(/duplicate.*node:a/i);

    const overlapping = document.createElement("div");
    const first = existingRoot("node:a", "one", { rootCount: 2 });
    first[1].dataset.reconcileKey = "node:b";
    first[1].dataset.reconcileFingerprint = "two";
    first[1].dataset.reconcileShape = "production-v1";
    first[1].dataset.reconcileRootCount = "1";
    overlapping.append(...first);
    expect(() => collectExistingTranscriptBlocks(overlapping)).toThrow(/overlap|primary.*range/i);

    const orphan = document.createElement("div");
    const orphanChild = document.createElement("div");
    orphanChild.dataset.reconcileFingerprint = "missing-key";
    orphan.append(orphanChild);
    expect(() => collectExistingTranscriptBlocks(orphan)).toThrow(/metadata.*key/i);
  });

  it("classifies unowned roots as immutable boundaries", () => {
    const root = document.createElement("div");
    const before = existingRoot("node:a", "a")[0];
    const pending = document.createElement("div");
    pending.dataset.pendingItemId = "local";
    const after = existingRoot("node:b", "b")[0];
    root.append(before, pending, after);

    const index = collectExistingTranscriptBlocks(root);

    expect(index.entries).toEqual([
      { kind: "block", key: "node:a" },
      { kind: "retained", root: pending },
      { kind: "block", key: "node:b" },
    ]);
  });
});

describe("planTranscriptReconciliation", () => {
  it("plans keep, replace, insert, remove, and canonical full-snapshot order", () => {
    const root = document.createElement("div");
    const keep = descriptor("keep", "same");
    const changed = descriptor("changed", "new");
    const obsolete = descriptor("obsolete", "old");
    root.append(
      ...existingRoot(obsolete.key, obsolete.fingerprint),
      ...existingRoot(changed.key, "stale"),
      ...existingRoot(keep.key, keep.fingerprint),
    );
    const inserted = descriptor("inserted", "new");

    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [keep, changed, inserted],
      { windowed: false },
    );

    expect(plan.keep.map((block) => block.key)).toEqual([keep.key]);
    expect(plan.replace.map(({ current, descriptor: next }) => [current.key, next.key])).toEqual([
      [changed.key, changed.key],
    ]);
    expect(plan.insert.map((next) => next.key)).toEqual([inserted.key]);
    expect(plan.remove.map((block) => block.key)).toEqual([obsolete.key]);
    expect(plan.order).toEqual([keep.key, changed.key, inserted.key]);
    expect([...root.children].map((element) => element.textContent)).toEqual([
      `${obsolete.key}:0`, `${changed.key}:0`, `${keep.key}:0`,
    ]);
  });

  it("preserves absent canonical blocks for windowed snapshots", () => {
    const root = document.createElement("div");
    const page = descriptor("page", "same");
    const outside = descriptor("outside", "old");
    root.append(
      ...existingRoot(outside.key, outside.fingerprint),
      ...existingRoot(page.key, page.fingerprint),
    );

    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [page],
      { windowed: true },
    );

    expect(plan.remove).toEqual([]);
    expect(plan.order).toEqual([outside.key, page.key]);
  });


  it("preserves previous window canonical blocks when a later window has no shared keys", () => {
    const root = document.createElement("div");
    const outside = descriptor("outside", "outside");
    const previous = descriptor("previous-window", "old");
    const next = descriptor("next-window", "new");
    root.append(
      ...existingRoot(outside.key, outside.fingerprint),
      ...existingRoot(previous.key, previous.fingerprint),
    );

    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [next],
      { windowed: true },
    );

    expect(plan.requiresFullRecovery).toBe(false);
    expect(plan.insert.map((descriptor) => descriptor.key)).toEqual([next.key]);
    expect(plan.remove).toEqual([]);
    expect(plan.keep.map((block) => block.key)).toEqual([outside.key, previous.key]);
    expect(plan.order).toEqual([outside.key, previous.key, next.key]);
  });
  it("allows reordering within one windowed segment without crossing a boundary", () => {
    const root = document.createElement("div");
    const a = descriptor("a", "a");
    const b = descriptor("b", "b");
    const boundary = document.createElement("div");
    boundary.dataset.pendingItemId = "pending";
    root.append(
      ...existingRoot(a.key, a.fingerprint),
      ...existingRoot(b.key, b.fingerprint),
      boundary,
    );

    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [b, a],
      { windowed: true },
    );

    expect(plan.requiresFullRecovery).toBe(false);
    expect(plan.order).toEqual([b.key, a.key, { retained: boundary }]);
  });

  it("requests full recovery when descriptor order crosses windowed segments", () => {
    const root = document.createElement("div");
    const a = descriptor("a", "a");
    const b = descriptor("b", "b");
    const outside = existingRoot("node:outside", "outside")[0];
    root.append(
      ...existingRoot(a.key, a.fingerprint),
      outside,
      ...existingRoot(b.key, b.fingerprint),
    );

    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [b, a],
      { windowed: true },
    );

    expect(plan.requiresFullRecovery).toBe(true);
    expect(plan.reason).toMatch(/cross.*segment/i);
    expect(plan.keep).toEqual([]);
    expect(plan.replace).toEqual([]);
    expect(plan.insert).toEqual([]);
    expect(plan.remove).toEqual([]);
  });
});


describe("applyTranscriptReconciliation", () => {
  async function buildApplyFixture() {
    const { renderTranscriptBlocksDetached } = await import("../../src/utils/render");
    const root = document.createElement("div");
    const keep = descriptor("keep", "same");
    const changed = descriptor("changed", "new");
    const inserted = descriptor("inserted", "new");
    const obsolete = descriptor("obsolete", "old");
    const obsoleteRoot = existingRoot(obsolete.key, obsolete.fingerprint)[0];
    const changedRoot = existingRoot(changed.key, "stale")[0];
    const keepRoot = existingRoot(keep.key, keep.fingerprint)[0];
    root.append(obsoleteRoot, changedRoot, keepRoot);
    const plan = planTranscriptReconciliation(
      collectExistingTranscriptBlocks(root),
      [keep, changed, inserted],
      { windowed: false },
    );
    const detached = renderTranscriptBlocksDetached([changed, inserted]);
    return {
      root,
      plan,
      detached,
      keepRoot,
      changedRoot,
      obsoleteRoot,
      before: [obsoleteRoot, changedRoot, keepRoot],
    };
  }

  it("keeps unchanged identity while replacing, inserting, removing, and reordering synchronously", async () => {
    const fixture = await buildApplyFixture();
    const nextChanged = fixture.detached.blocks.find((block) => block.key === "node:changed")!;
    const nextInserted = fixture.detached.blocks.find((block) => block.key === "node:inserted")!;

    const result = applyTranscriptReconciliation(
      fixture.root,
      fixture.plan,
      fixture.detached.blocks,
    );

    expect(result).toEqual({ status: "applied" });
    expect(Array.from(fixture.root.children)).toEqual([
      fixture.keepRoot,
      ...nextChanged.roots,
      ...nextInserted.roots,
    ]);
    expect(fixture.keepRoot.isConnected).toBe(false);
    expect(fixture.keepRoot.parentNode).toBe(fixture.root);
    expect(fixture.changedRoot.parentNode).toBeNull();
    expect(fixture.obsoleteRoot.parentNode).toBeNull();
  });

  it("rejects stale plans before mutation", async () => {
    const fixture = await buildApplyFixture();
    fixture.root.append(document.createElement("div"));
    const before = Array.from(fixture.root.childNodes);

    const result = applyTranscriptReconciliation(
      fixture.root,
      fixture.plan,
      fixture.detached.blocks,
    );

    expect(result.status).toBe("stale");
    expect(Array.from(fixture.root.childNodes)).toEqual(before);
  });

  it.each(["after-remove", "after-replace", "after-insert", "after-order"])(
    "restores exact DOM identity and order when %s fails",
    async (failurePoint) => {
      const fixture = await buildApplyFixture();
      const before = Array.from(fixture.root.childNodes);

      expect(() => applyTranscriptReconciliation(
        fixture.root,
        fixture.plan,
        fixture.detached.blocks,
        {
          failAt(point) {
            if (point === failurePoint) throw new Error(`injected:${point}`);
          },
        },
      )).toThrow(`injected:${failurePoint}`);

      expect(Array.from(fixture.root.childNodes)).toEqual(before);
      expect(fixture.before.every((element) => element.parentNode === fixture.root)).toBe(true);
      expect(fixture.detached.blocks.flatMap((block) => block.roots)
        .every((element) => element.parentNode !== fixture.root)).toBe(true);
    },
  );
});
