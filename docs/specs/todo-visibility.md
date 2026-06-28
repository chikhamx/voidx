# Todo 可见性增强 — 技术设计文档

## Context

LLM 在多步任务中经常忘记更新 todo 列表，事后才刷新。根因有二：

1. **ToolMessage 通道有缺口**：`_latest_runtime_tool_exchange_ids` 只保留消息列表末尾连续段的 todo ToolMessage。如果 LLM 调完 todo 后又调了别的工具（常见——更新 todo 后继续干活），todo 的 ToolMessage 不在末尾连续段，被清掉，LLM 后续轮次看不到完整 content。
2. **Current Task State 渲染冗余**：显示 `Active/Pending: ids`（无语义的 id 串）+ `Call todo with op=read for details.`，对 LLM 决策无帮助，且 id 串不携带任务描述。

## Goals and Non-Goals

### Goals

- ToolMessage 通道：始终保留最近一次 todo 工具调用的完整结果（含 content），不管它在消息流的什么位置。
- Current Task State：简化为只显示 `Todo: {summary}`（如 `1/2 done · 1 active · 0 pending`），去掉 id 列表和 read 提示。

### Non-Goals

- 不改变 todo 工具本身的 schema 或行为。
- 不在 Current Task State 里逐行显示 content——完整 content 由 ToolMessage 通道提供。
- 不保留多轮历史 todo ToolMessage（仍只保留最近一次）。

## Architecture

两条信息通道分工：

```
LLM 看到的 todo 信息
├── ToolMessage（最近一次 todo 调用的完整结果）
│   └── 含每个 item 的 id + content + status
│   └── 由 _latest_runtime_tool_exchange_ids 决定保留哪个
│
└── Current Task State（每轮重新渲染的 overlay）
    └── 只显示 summary 概览
    └── 如 "Todo: 1/2 done · 1 active · 0 pending"
```

**分工逻辑**：Current Task State 给概览（summary），ToolMessage 给详情（content）。LLM 从 summary 知道整体进度，从 ToolMessage 知道每个任务具体要做什么。

## Data Model

无 schema 变更。`TodoRunState` 和 `TodoRunItem` 已有所需字段：

```
TodoRunState
├── summary: str          # "1/2 done · 1 active · 0 pending"
├── items: list[TodoRunItem]
│   ├── id: str
│   ├── content: str      # 任务描述
│   └── status: Literal["pending", "active", "done"]
└── ...
```

## API Contract

### `_latest_runtime_tool_exchange_ids`（修改）

- **Path**: `src/voidx/agent/todo_state.py:243-262`
- **Before**: 只保留消息列表末尾连续 ToolMessage 段对应的 todo tool_call_id。
- **After**: 从末尾往前扫所有消息，找到最近一条 AIMessage，保留其中所有属于 `_REPLAY_SANITIZED_TOOL_NAMES` 的 tool_call id。不要求配对的 ToolMessage 在末尾连续段。如果一个 AIMessage 同时有 todo 和非 todo 的 tool_call，只保留 todo 的，非 todo 的照常处理。
- **Returns**: `set[str]` — 被保留的 tool_call_id 集合。
- **边界**：扫到第一条含 todo tool_call 的 AIMessage 即停，不再往前找更早的。

### `_current_task_state`（修改）

- **Path**: `src/voidx/agent/runtime_context.py:276-283`
- **Before**:
  ```
  - Todo: {summary}
    Active/Pending: {ids}
    Call todo with op=read for details.
  ```
- **After**:
  ```
  - Todo: {summary}
  ```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 消息流中没有任何 todo tool_call | 返回空 set，所有 todo ToolMessage 被清掉（现状行为） |
| todo_state 为 None 或 items 为空 | 不渲染 Todo 行（现状行为） |
| visible items 为空（全 done） | 不渲染 Todo 行（现状行为） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| Current Task State 只显示 summary | 逐行显示 `[status] id — content` | content 由 ToolMessage 通道提供，避免重复；summary 足够给概览 |
| ToolMessage 保留最近一次（不限位置） | 保留最近 N 轮 | 历史状态对决策价值低，且会膨胀 token；最近一次已含完整列表 |
| 不在 Current Task State 显示 id 列表 | 保留 id 列表 | id 无语义，对 LLM 决策无帮助 |

## Open Questions

- [ ] 无
