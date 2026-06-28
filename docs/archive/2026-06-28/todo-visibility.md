> **Status: Done**

# Todo 可见性增强 — 技术设计文档

## Context

LLM 在多步任务中经常忘记更新 todo 列表，事后才刷新。根因有二：

1. **ToolMessage 通道有缺口**：`_latest_runtime_tool_exchange_ids`（本设计将重命名为 `_latest_todo_tool_call_ids`）只保留消息列表末尾连续段的 todo ToolMessage。如果 LLM 调完 todo 后又调了别的工具（常见——更新 todo 后继续干活），todo 的 ToolMessage 不在末尾连续段，被清掉，LLM 后续轮次看不到完整 content。
2. **Current Task State 渲染冗余**：显示 `Active/Pending: ids`（无语义的 id 串）+ `Call todo with op=read for details.`，对 LLM 决策无帮助，且 id 串不携带任务描述。

## Goals and Non-Goals

### Goals

- ToolMessage 通道：始终保留最近一次 todo 工具调用的完整结果（含 content），不管它在消息流的什么位置。
- Current Task State：简化为 `Todo: {summary} · active: {content}`，去掉 id 列表和 read 提示，并在 summary 里带上 active 任务的 content（截断）以提供下一步决策线索。
- 函数职责分离：todo 与 workflow 的"最近一次保留"语义解耦，避免互相干扰。

### Non-Goals

- 不改变 todo 工具本身的 schema 或行为。
- 不在 Current Task State 里逐行显示所有 content——完整 content 由 ToolMessage 通道提供。
- 不保留多轮历史 todo ToolMessage（仍只保留最近一次）。
- 不改变 workflow 工具的 replay 保留行为（维持现状）。

## Architecture

两条信息通道分工：

```
LLM 看到的 todo 信息
├── ToolMessage（最近一次 todo 调用的完整结果）
│   └── content = result.output 文本，含每个 item 的 id + content + status
│   └── 由 _latest_todo_tool_call_ids 决定保留哪个
│
└── Current Task State（每轮重新渲染的 overlay）
    └── 显示 summary + active 任务 content（截断）
    └── 如 "Todo: 1/2 done · 1 active · 0 pending · active: 实现X的Y分支"
```

**分工逻辑**：Current Task State 给概览 + 下一步线索（active content），ToolMessage 给完整详情（所有 items）。LLM 从 summary 知道整体进度，从 active content 知道下一步该做什么，从 ToolMessage 知道每个任务具体内容。

**为什么 active content 要进 Current Task State**：即使 ToolMessage 通道出问题（如 todo 调用后又被清掉的边界 case），LLM 仍有足够的决策线索知道下一步。代价是几十 token，值得。

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

## Verification: ToolMessage content 结构

已验证 todo 工具各 op 的 ToolMessage content（LLM 实际看到的文本）来源：

| op | metadata.todo_op | metadata.todo_items | ToolMessage content (result.output) |
|----|------------------|---------------------|-------------------------------------|
| write | （无） | 完整列表 | `Written N items.` + 逐行 `ICON id: content` + `Summary: ...` |
| update | （仅 error/empty 分支有） | 完整列表（成功时） | `Updated N items.` + 逐行 `ICON id: content` |
| read | `"read"` | （无） | 逐行 `ICON id: content`（按 filter） |

**关键结论**：
- write/update 的 ToolMessage content **包含完整 items 列表**（每个 item 的 id + content + status），LLM 能看到详情。✅
- ToolMessage content 来自 `result.output`（文本），不是 metadata。`todo_run_state_from_result` 从 metadata 提取 `todo_items` 用于 Current Task State 渲染，两条通道数据源独立。
- read 操作的 `todo_run_state_from_result` 短路返回 None（不更新 Current Task State），但 ToolMessage content 仍含 items 文本。

**Path 参考**：
- `src/voidx/tools/todo.py:217-283`（write）、`137-215`（update）、`100-135`（read）
- `src/voidx/agent/graph/tool_executor/executor.py:221-234`（ToolMessage 构造，content=result.output）

## API Contract

### `_latest_todo_tool_call_ids`（重命名 + 修改）

- **Path**: `src/voidx/agent/todo_state.py:243-262`
- **Rename**: `_latest_runtime_tool_exchange_ids` → `_latest_todo_tool_call_ids`（旧名暗示"末尾连续段"，新语义是"最近一次 todo 调用"，名称需匹配）
- **Before**: 只保留消息列表末尾连续 ToolMessage 段对应的 todo tool_call_id。
- **After**: 从末尾往前扫所有 AIMessage，找到最近一条含 `todo` tool_call 的 AIMessage，保留其中所有 `todo` tool_call 的 id。不要求配对的 ToolMessage 在末尾连续段。
- **Scope**: **只针对 `todo`，不再覆盖 `workflow`**。workflow 的保留行为维持现状（由 `_trailing_ai_runtime_tool_call_ids` 等其他路径处理）。
- **Returns**: `set[str]` — 被保留的 todo tool_call_id 集合。
- **边界**：
  - 扫到第一条含 todo tool_call 的 AIMessage 即停，不再往前找更早的。
  - 如果该 AIMessage 同时有非 todo 的 tool_call，非 todo 的照常处理（不在本函数职责内）。
  - 消息流中没有任何 todo tool_call → 返回空 set。

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
  - Todo: {summary} · active: {active_content_truncated}
  ```
  - `active_content_truncated`：取第一个 active item 的 content，截断到 60 字符（超出加 `…`）。
  - 如果没有 active item（全 pending 或全 done），不追加 `· active: ...` 部分。
  - visible items 为空（全 done）时，整行不渲染（现状行为）。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 消息流中没有任何 todo tool_call | 返回空 set，所有 todo ToolMessage 被清掉（现状行为） |
| todo_state 为 None 或 items 为空 | 不渲染 Todo 行（现状行为） |
| visible items 为空（全 done） | 不渲染 Todo 行（现状行为） |
| 没有 active item（全 pending） | 渲染 `Todo: {summary}`，不追加 active content |
| active item content 超长 | 截断到 60 字符 + `…` |
| workflow tool_call 在末尾连续段 | 不受本改动影响，维持现状行为 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| Current Task State 显示 summary + active content | 只显示 summary / 逐行显示所有 items | active content 提供下一步决策线索，即使 ToolMessage 通道出问题也有兜底；逐行显示所有 items 与 ToolMessage 重复且膨胀 token |
| ToolMessage 保留最近一次（不限位置） | 保留最近 N 轮 | 历史状态对决策价值低，且会膨胀 token；最近一次已含完整列表 |
| 不在 Current Task State 显示 id 列表 | 保留 id 列表 | id 无语义，对 LLM 决策无帮助 |
| 函数只针对 todo，不覆盖 workflow | 继续用 `_REPLAY_SANITIZED_TOOL_NAMES` 集合 | todo 和 workflow 的"最近一次"语义不同，混在一个函数里会互相干扰（如最近一条 AIMessage 只有 workflow 调用时，旧逻辑会跳过它找 todo，改变 workflow 保留语义） |
| 函数重命名为 `_latest_todo_tool_call_ids` | 保留旧名 | 旧名 `_latest_runtime_tool_exchange_ids` 暗示"末尾连续段"，新语义是"最近一次 todo 调用"，名称需匹配避免误导 |
| active content 截断到 60 字符 | 不截断 / 更短 | 60 字符覆盖大多数任务描述，避免 Current Task State 过长 |

## Test Plan

针对 `_latest_todo_tool_call_ids` 的边界 case：

| Case | 输入消息序列 | 期望输出 |
|------|------------|---------|
| 无 todo 调用 | `[AI(bash), Tool(bash)]` | `set()` |
| todo 在末尾连续段 | `[AI(todo), Tool(todo)]` | `{todo_id}` |
| todo 在中间，后面有其他工具 | `[AI(todo), Tool(todo), AI(bash), Tool(bash)]` | `{todo_id}` |
| todo + 非 todo 混合在一条 AIMessage | `[AI(todo, bash), Tool(todo), Tool(bash)]` | `{todo_id}`（只保留 todo） |
| 多条 AIMessage 含 todo | `[AI(todo_A), Tool(todo_A), AI(todo_B), Tool(todo_B)]` | `{todo_B}`（只保留最近一次） |
| 末尾 AIMessage 只有 workflow | `[AI(todo), Tool(todo), AI(workflow), Tool(workflow)]` | `{todo_id}`（workflow 不影响 todo 保留） |

针对 `_current_task_state` 的渲染：

| Case | todo_state | 期望输出 |
|------|-----------|---------|
| None 或空 items | None | 不渲染 Todo 行 |
| 全 done | items 全 done | 不渲染 Todo 行 |
| 有 active | 1 active + 1 done | `Todo: 1/2 done · 1 active · 0 pending · active: {content}` |
| 全 pending | 2 pending | `Todo: 0/2 done · 0 active · 2 pending`（不追加 active） |
| active content 超长 | content > 60 字符 | 截断 + `…` |

## Open Questions

- [ ] 无
