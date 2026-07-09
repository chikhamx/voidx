> **Status: Done**
# Goal Resolution 合并到主循环 — 设计文档

## Context

当前 voidx 每轮用户输入后，先发起一次独立的 `resolve_goal_for_turn()` LLM 请求来判断 intent/goal/next_workflow，再进入主循环。这导致：

1. 每轮多一次独立 LLM 请求，与主循环无共享前缀，无法复用缓存
2. resolver 的 SystemMessage 与主循环完全不同，前缀无法命中
3. resolver 请求很短（~500 tokens），缓存收益有限但延迟开销实打实

**目标**：将 goal resolution 职责合并到主循环首次 call_llm，消除独立请求，让 LLM 在主循环上下文中直接判断 intent/goal 并通过 advance_workflow 推进 workflow。

## Goals and Non-Goals

### Goals

- 消除 resolve_goal_for_turn 的独立 LLM 请求
- 主循环首次 call_llm 同时完成 goal resolution + 首次响应
- 保持本地 fallback（resolve_turn_intent）作为无 LLM 时的降级路径
- 不存入历史记录的 goal resolution 指导消息

### Non-Goals

- 不改变 GoalResolution 的 schema 定义
- 不改变 advance_workflow 的工具接口
- 不在本设计中直接改变 compaction / 子 agent 的行为；但 compaction request 可以复用同一种“主循环临时 guide”模式，见下方扩展设计

## 当前流程

```
用户输入
  │
  ├─ [1] resolve_goal_for_turn()          ← 独立 LLM 请求
  │     输入: user_text, interaction_mode, task_state, workspace, session_time
  │     输出: GoalResolution { intent, goal, confidence, reason, next_workflow }
  │
  ├─ [2] task_state.update_after_turn(resolution, user_text)
  │     → 更新 current_intent, current_goal, pending_approval, recent_user_texts
  │
  ├─ [3] reconcile_workflow_runs_for_turn(goal_resolution, after_state)
  │     → 根据 next_workflow 自动推进 workflow 状态
  │
  ├─ [4] graph.ainvoke(initial_state)
  │     ├─ prepare → 构建 context（用已更新好的 task_state）
  │     ├─ call_llm (step 1)
  │     ├─ execute_tools → call_llm (step 2~N)
  │     └─ finalize
```

### resolve_goal_for_turn 的输入输出

**输入**（`_resolver_messages()`，goal_resolver.py:62-108）：

```
[SystemMessage: resolver prompt + GoalResolution schema]
[HumanMessage: {workspace, session_time, interaction_mode, current_intent,
                current_goal, pending_approval, recent_user_texts, latest_user_text}]
```

**输出**：`GoalResolution`

| 字段 | 类型 | 用途 |
|---|---|---|
| `intent` | TaskIntent | 更新 task_state.current_intent |
| `goal` | Goal \| None | 更新 task_state.current_goal |
| `confidence` | float | 仅记录，不影响逻辑 |
| `reason` | str | 仅记录，不影响逻辑 |
| `next_workflow` | str \| None | 驱动 reconcile_workflow_runs_for_turn 推进 workflow |

### resolve_turn_intent 的 fallback 路径

当 model 为 None 或 `with_structured_output` 不可用时，走本地关键词匹配（task_state.py:273-320）：

- 检测 approval-only 短语（"ok"、"好的"、"确认"等）
- 检测 direct-write 命令（"改"、"修复"、"实现"等）
- 根据 interaction_mode 判断（plan/goal 模式强制 coding）
- 调用 `infer_task_intent()` 做关键词分类

**关键差异**：fallback 不产出 `next_workflow`，无法自动推进 workflow。

## 合并后的流程

```
用户输入
  │
  ├─ [1] resolve_turn_intent()             ← 本地 fallback（不走 LLM）
  │     输入: user_text, interaction_mode, task_state
  │     输出: GoalResolution（基于关键词匹配，无 next_workflow）
  │
  ├─ [2] task_state.update_after_turn(resolution, user_text)
  │     → 更新 current_intent, current_goal, pending_approval, recent_user_texts
  │     → next_workflow = None（本地 fallback 不产出）
  │
  ├─ [3] reconcile_workflow_runs_for_turn(goal_resolution, after_state)
  │     → 无 next_workflow，不执行 resolver 指定的 workflow transition
  │
  ├─ [4] graph.ainvoke(initial_state)
  │     ├─ prepare → 构建 context
  │     │   → step_count == 0 时注入 goal resolution 指导（不存历史）
  │     │   → tool_defs 固定包含 advance_workflow / compact_context 等 runtime control tools
  │     ├─ call_llm (step 1)
  │     │   → LLM 同时完成 goal resolution + 首次响应
  │     │   → 如果 LLM 调用 advance_workflow，触发 workflow 推进
  │     ├─ execute_tools → call_llm (step 2~N)
  │     └─ finalize
```

## Runtime 变化

### 1. turn_runner.py — 去掉 resolve_goal_for_turn 的 LLM 调用

```python
# 当前
intent_resolution = await resolve_goal_for_turn(
    model=host.model, user_text=..., task_state=..., ...
)

# 合并后：只用本地 fallback
intent_resolution = resolve_turn_intent(
    payload.title_text, interaction_mode, base_task_state
)
```

`resolve_goal_for_turn()` 和 `goal_resolver.py` 保留，但默认路径不再调用。后续如果需要显式 goal resolution（如 slash command），仍可调用。

### 2. Goal Resolution 指导注入

在 step 1 时，向消息列表注入 goal resolution 指导，让 LLM 知道它需要判断 intent/goal/next_workflow。

**方案：作为独立 HumanMessage 注入，放在历史对话之后、最新用户消息之前**

```
[SystemMessage: stable sections]              ← 不变
[HumanMessage: workflow context]              ← 不变
[HumanMessage: skill context]                 ← 不变
[HumanMessage: 历史用户消息1]                   ← 不变
...历史对话...
[HumanMessage: goal resolution guide]         ← 新增，step 1 时注入，不存历史
[HumanMessage: 最新用户消息 + task context]     ← 不变
```

**不存入历史**：这条消息是临时的，step 2+ 不再注入。通过在 `compile_messages` 中根据 step_count 条件注入实现。它应靠近最新用户消息，因为 goal resolution 判断的是当前 turn 的最新意图；不要放到 Workflow/Skill Context 后面把整段历史整体后移。

**对缓存的影响**：

| 场景 | 断点位置 | 命中情况 |
|---|---|---|
| step 1 → step 2 | goal resolution guide 消失，最新 user message 前移 | SystemMessage + Workflow/Skill + 历史对话仍命中，断点靠近最新 user message |
| step 2 → step 3 | 无 guide，前缀稳定 | 完全命中到 Task Context 断点 |

step 1→2 的缓存断点在 guide 消失处，但 SystemMessage + Workflow/Skill Context + 历史对话仍可命中。step 2+ 完全不受影响。

**guide 内容**：

```
VOIDX_GOAL_RESOLUTION_GUIDE
Scope: turn-initial-goal-resolution

Before responding, determine the user's intent and goal for this turn.
Rules:
- Use intent=general only for non-code, non-workspace conversation.
- Use intent=coding for codebase inspection, design, docs, review, debugging, or edits.
- Do not infer write permission from analysis words like look at, inspect, 看看, 分析, or 建议.
- If the user's intent clearly indicates which workflow should be active next, call advance_workflow to transition.
- Do not call advance_workflow based on vague or ambiguous approval.

Current context:
{
  "workspace": "{workspace}",
  "session_time": "{session_time}",
  "interaction_mode": "{interaction_mode}",
  "current_intent": "{current_intent}",
  "current_goal": "{current_goal | null}",
  "pending_approval": "{pending_approval | null}",
  "recent_user_texts": ["{previous compact user text}", "{latest compact user text}"],
  "latest_user_text": "{latest_user_text}"
}
```

合并后 guide 被刻意精简：`GoalType`、`user_requested_write`、`needs_confirmation`
等结构化 Goal 字段由本地 `resolve_turn_intent()` fallback 预设，后续如需修正通过
runtime tools 的 state patch 表达；guide 不再要求 LLM 直接产出完整 `GoalResolution`
字段。`recent_user_texts` 会随 guide context 一起提供，帮助模型判断“好的”、
“继续”等短确认是在承接哪一个最近请求。

### 3. GoalResolution 的产出方式变化

| 字段 | 当前产出方式 | 合并后产出方式 |
|---|---|---|
| `intent` | resolver LLM 结构化输出 | 本地 fallback `resolve_turn_intent()` 预设 |
| `goal` | resolver LLM 结构化输出 | 本地 fallback 预设，后续通过 `ToolStatePatch` 更新 |
| `next_workflow` | resolver LLM 结构化输出 | LLM 在 step 1 调用 `advance_workflow` 间接表达 |
| `confidence` | resolver LLM 结构化输出 | 不再产出（本地 fallback 固定 confidence） |
| `reason` | resolver LLM 结构化输出 | 不再产出 |

### 4. Workflow 推进时机变化

| 对比 | 当前 | 合并后 |
|---|---|---|
| resolver 指定的 workflow transition | graph.ainvoke 之前（turn_runner 中） | step 1 的 `advance_workflow` 工具调用 |
| prepare 阶段的 workflow selection | 基于 resolver 更新后的 goal + next_workflow | 基于本地 fallback 更新后的 intent/goal 和已有 workflow_runs |
| step 1 的 advertised tool_defs | 基于 prepare 后的 active workflow_runs 过滤 | 固定为 agent identity 的工具集；workflow 限制只在执行授权层生效 |
| step 1 的 persona | 基于 prepare 后的 active workflow_runs | 仍基于 prepare 后的 active workflow_runs，但没有 resolver `next_workflow` 的预推进 |

**影响**：如果用户从 brainstorm → plan，step 1 是否已经拿到 plan 工具取决于 fallback 后的 goal/intent、现有 active workflow_runs，以及 `prepare` 的 workflow selection。没有 resolver 的 `next_workflow` 后，LLM 仍应在 step 1 使用 `advance_workflow` 明确表达 transition；step 2 才能稳定使用 transition 后的工具/persona。

**这更合理**：LLM 先判断应该进入哪个 workflow，再切换，而不是在进入前就切换。避免了 resolver 判断错误导致 workflow 状态被错误推进的问题。

### 5. 缓存命中对比

| 对比项 | 当前 | 合并后 |
|---|---|---|
| resolve_goal 请求 | 独立请求，~500 tokens | 消除 |
| 主循环 step 1 前缀 | SystemMessage + Workflow/Skill + 历史 + user(task ctx) | SystemMessage + Workflow/Skill + 历史 + goal resolution guide + user(task ctx) |
| 主循环 step 2 前缀 | SystemMessage + Workflow/Skill + 历史 + user(task ctx) | SystemMessage + Workflow/Skill + 历史 + user(task ctx)（guide 消失） |
| step 1→2 缓存命中 | Task Context 断点 | guide 消失 + Task Context 断点，SystemMessage + Workflow/Skill + 历史仍命中 |
| step 2→3 缓存命中 | Task Context 断点 | 同当前 |
| 总 LLM 请求数 | N+1 | N |

**净效果**：少一次独立请求，主循环缓存命中基本不变。step 1→2 因 guide 消失多一个断点，但断点靠近最新 user message，历史对话之前的前缀仍可命中。

### 6. goal_resolver.py 的处理

保留 `goal_resolver.py` 和 `resolve_goal_for_turn()`，但不再被默认路径调用。用途：

- slash command 显式触发 goal resolution
- 测试和调试
- 未来如果需要独立的 goal resolution 服务

### 7. Compaction request 可复用同模式

Compaction 与 goal resolution 有相同的缓存问题：当前有一条专用 LLM 请求，prompt 形状和主循环不同，会增加延迟，并且通常只能复用有限前缀。可以参考本设计，把 compaction request 从“独立 agent 请求”改成“主循环中的临时 guide + 明确产出通道”。

但 compaction 与 goal resolution 有一个关键差异：compaction 往往在 context 接近或超过预算时触发，不能无条件并入主循环。因此建议拆成两条路径：

| 路径 | 触发条件 | 行为 |
|---|---|---|
| Inline compaction guide | 编译后的主循环上下文达到 soft compaction budget，且仍能容纳 guide、预留输出和必要 tail | 在主循环消息末尾追加临时 `VOIDX_COMPACTION_GUIDE`，要求模型通过 `compact_context` 提交结构化 summary；运行时保存 summary 并裁剪旧消息 |
| Dedicated compaction fallback | 主循环上下文无法安全容纳 guide，或 inline summary 失败 | 保留当前 `run_compaction_agent()` / fallback summary 路径 |

**放置位置与 goal guide 不同**：goal resolution guide 应贴近最新用户消息，放在历史对话之后、最新用户消息之前；compaction guide 应放在消息末尾，因为它需要总结前面的对话，而且末尾追加最不破坏主循环前缀。

**产出通道不能只靠普通 assistant 文本**。如果同一次主循环既要回复用户又要提交 compact summary，需要一个不会显示给用户、但能被 runtime 可靠解析并持久化的通道。可选方案：

| 方案 | 说明 | 取舍 |
|---|---|---|
| `compact_context` 工具 | LLM 调用工具提交 summary、tail anchor、removed range | 最结构化，复用现有 tool/state patch 流程；会占用一次 tool step |
| 标记块解析 | assistant 输出隐藏标记块，如 `VOIDX_COMPACTION_SUMMARY` | 无需新增工具；解析和可见输出隔离更脆弱 |
| 独立 fallback 保持现状 | 只在 inline 不可用时走当前 compaction agent | 最安全，但仍有额外 LLM 请求 |

推荐先采用 `compact_context` 工具方案：它最接近 `advance_workflow` 在 goal resolution merge 中承担的角色，都是让主循环 LLM 用结构化工具调用表达 runtime 状态变化。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `src/voidx/agent/graph/turn_runner.py` | `resolve_goal_for_turn()` → `resolve_turn_intent()` |
| `src/voidx/agent/runtime_context.py` | 新增 goal resolution guide 的渲染和注入逻辑 |
| `src/voidx/agent/goal_resolver.py` | 保留，不再被默认路径调用 |
| `src/voidx/agent/graph/core.py` | 注入 step 0 goal guide；在预算允许时追加 inline compaction guide；保持 advertised tool schemas 固定 |
| `src/voidx/agent/graph/tool_executor.py` | 处理 `compact_context` 的 inline summary，复用 compaction coordinator 替换 live messages |
| `src/voidx/agent/graph/compaction_coordinator.py` | 保留 dedicated fallback；提供 live state compaction 复用入口 |
| `src/voidx/tools/compact_context.py` | 新增结构化 summary 提交通道 |
| `src/voidx/workflow/context.py` | 固定渲染全量 workflow definitions，active 状态只在 task state 中表达 |

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 本地 fallback 的 goal 判断不够准确 | step 1 的 persona 可能不对 | fallback 已覆盖大部分场景（关键词匹配 + approval 检测），LLM 在 step 1 可通过 advance_workflow 修正 |
| LLM 不主动调用 advance_workflow | workflow 不推进 | guide 中明确要求 LLM 在 step 1 判断并调用 advance_workflow |
| step 1 延迟增加 | LLM 需要同时做 goal resolution + 首次响应 | 省了一次独立请求，总延迟可能反而降低 |
| guide 消失导致 step 1→2 缓存断点 | 最新 user message 附近前缀不匹配 | SystemMessage + Workflow/Skill + 历史对话仍命中，断点仅在 guide 位置 |
| inline compaction guide 超出上下文预算 | 主循环无法发起或 provider 报 overflow | 只有在预算检查通过时启用 inline；否则回退当前 dedicated compaction |
| compaction summary 和用户可见回复混杂 | 用户看到内部 summary 或 runtime 解析失败 | 使用 `compact_context` 工具/结构化 state patch，不依赖普通文本解析 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|---|---|---|
| goal resolution guide 作为独立 HumanMessage | 放在 Task Context 中 | guide 只在 step 1 需要，放在 Task Context 中会导致 step 2+ 的 Task Context 变化，影响缓存；独立消息更干净 |
| guide 放在历史对话之后、最新用户消息之前 | 放在 Workflow/Skill 之后、历史对话之前 | goal resolution 判断的是最新 turn，贴近最新 user message 更符合语义；同时历史对话前缀不会因为 guide 消失而整体前移 |
| 保留 goal_resolver.py | 删除 | 保持向后兼容，slash command 和测试仍可使用 |
| 本地 fallback 不产出 next_workflow | fallback 也产出 next_workflow | fallback 基于关键词匹配，无法准确判断 next_workflow；让 LLM 在主循环中判断更可靠 |
| compaction guide 放在消息末尾 | 像 goal guide 一样放在最新 user 附近 | compaction 需要总结前面的对话；末尾追加也只影响自身，不破坏前面稳定前缀 |
