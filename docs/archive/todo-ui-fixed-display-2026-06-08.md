# TODO UI 全局节点与固定显示设计

> **Status: Done**

Implemented scope: Phase A global TODO node. Phase B true fixed/sticky display remains a future design.

## Problem

当前 TODO 节点挂在当前 agent 节点下，导致同一任务列表在多轮对话中重复出现：

1. `start_turn()` 会重置 `_current_agent = None`。
2. 下一轮创建新的 assistant/agent 节点。
3. `_update_todo_node()` 只在当前 parent children 中查找旧 todo。
4. 新 parent 下找不到旧 todo，于是创建新的 todo 块。

另一个观察到的问题是 todo 跟随 transcript 滚动，不能持续显示当前任务状态。但这和“重复创建”不是同一个层面的修复。

## Root Cause

```
TodoUpdated event
  -> DockEventConsumer._update_todo_node()
  -> _todo_parent(agent_id)
  -> current agent / subagent
  -> todo node stored under that transient agent node
  -> next TurnStarted clears runtime agent references
  -> next TodoUpdated cannot find prior todo under new parent
  -> duplicate todo node
```

PureTui 还有一层 scrollback flush 机制：

- settled tree prefix 会被刷到原生 scrollback。
- active frame 只渲染未 flush 的 tail。
- 如果 root-level todo 继续 `mark_node_settled()`，它可以被 flush 掉，所以它不是严格 sticky display。
- 如果 root-level todo 不 settled，它会阻塞后续 settled 内容 flush。

因此本期不能把“root 首位”描述成真正固定显示。它只能保证树结构中全局唯一、排序稳定。

## Phasing

### Phase A: Global TODO Node

本期实现。

- Todo 节点挂到 `tree.root` 下。
- 全局只保留一个 root-level todo 节点。
- 每次 `TodoUpdated` 只更新这个节点内容。
- 每次 update 都强制 todo 位于 `root.children[0]`。
- `agent_id` 不再影响 todo 挂载位置。
- 继续调用 `mark_node_settled(todo_node)`，避免阻塞 PureTui scrollback flush。

Phase A 解决重复创建和树结构排序问题，但不承诺 todo 在终端视口中 sticky 固定。

### Phase B: True Fixed Display

后续单独设计。

真正固定显示不应依赖 transcript tree 的 root 首位。更稳的方向是：

- 在 dock/TUI 中维护独立 pinned todo state。
- renderer 在 input/status 上方或固定区域单独渲染 todo。
- pinned todo 不参与 transcript scrollback flush。
- Web UI 如需要固定显示，也用同一个 structured todo state 渲染。

## Implementation Plan

### `src/voidx/ui/output/events/__init__.py`

Update `_update_todo_node()`:

1. Use `root = self._dock.tree.root`.
2. Find an existing todo only in `root.children`.
3. If missing, create it under root.
4. Always call a small helper to move it to `root.children[0]`.
5. Keep payload/body/header update logic unchanged.
6. Keep `mark_node_settled(todo_node)`.
7. Remove `_todo_parent()` because todo no longer has agent-specific parent selection.

Helper behavior:

- If todo is already first root child, no structural change.
- If todo exists elsewhere in root children, remove and insert at index 0.
- After reordering, refresh `_is_last_sibling` flags and mark the tree dirty.

### Tests

Update existing tests that currently expect todo under assistant/subagent:

| Test | New expectation |
|------|-----------------|
| `test_todo_updated_creates_and_updates_single_todo_node` | one todo exists under `root.children`, it is `root.children[0]`, repeated updates do not create duplicates |
| `test_todo_updated_with_agent_id_attaches_under_subagent` | rename or update to assert subagent todo updates write to the global root todo |

Add coverage:

| Test | Coverage |
|------|----------|
| `test_todo_updated_keeps_single_root_node_across_turns` | todo is not duplicated after a new `TurnStarted` |
| `test_todo_updated_reorders_existing_root_todo_to_front` | existing root todo is moved back to first position on update |

## Non-goals

- No sticky viewport/pinned panel in this phase.
- No TUI renderer changes.
- No WebSocket payload change.
- No todo folding behavior change.
- No change to todo tool semantics.

## Acceptance Criteria

- Multiple `TodoUpdated` events across turns produce one root-level todo node.
- Todo node is always `tree.root.children[0]` after update.
- Subagent todo updates update the same global todo node.
- Existing todo rendering, summary payload, visible item limit, and empty list behavior remain unchanged.
- Focused UI event tests pass.
