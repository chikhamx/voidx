---
name: goal-evaluator-loop
display_name: Goal Evaluator & Autonomous Loop
description: 为 voidx goal 模式增加独立评估器和自主循环机制，借鉴 Claude Code /goal
doc_type: tech-design
audience: human+llm
---

# Goal Evaluator & Autonomous Loop — 技术设计文档

## TL;DR

voidx 的 goal 模式目前只做"设定目标 + 路由到 plan workflow"，缺少完成判定和自主循环。本设计借鉴 Claude Code 的 `/goal`，引入：(1) `GoalSpec` 扩展 `done_condition` 和 `max_turns` 字段；(2) 独立评估器 `goal_evaluator.py`，在每轮结束后用轻量 LLM 检查 transcript 是否满足完成条件；(3) 在 `run_loop.py` 的 turn 完成后嵌入评估循环，未达成时自动启动下一轮。目标是让 `/goal <desc> when <condition>` 成为真正的自主循环。

## Context

### 当前行为

voidx goal 模式（`/goal <desc>`）的执行路径：

1. `slash/handler.py:_goal()` → `task_state.set_goal(desc)` + 切换到 `InteractionMode.GOAL`
2. `turn_runner.py:run_once()` 检测 `interaction_mode == "goal"` → 调 `resolve_goal_mode()`（纯函数，不调 LLM）
3. `resolve_goal_mode()` 返回 `PlanResolution(join="plan", leave=None)` → 进入 plan workflow
4. plan workflow 走完后 turn 结束，**无后续循环**

关键问题：
- **无完成判定**：设了 goal 但没有任何机制判断"是否达成"
- **无自主循环**：走完 plan workflow 后就结束了，不会自动继续
- **`resolve_goal_for_turn`（带 LLM 的版本）是死代码**：定义于 `goal_resolver.py:83` 但从未被调用

### 问题来源

调研 Claude Code 和 Codex 后发现，主流 agent 的 goal 模式都依赖两个核心机制：
- **CC**：独立小模型（Haiku）每轮评估 transcript + 自主循环直到条件满足
- **Codex**：文件化里程碑 + stop-and-fix 规则 + 验证命令

voidx 已有 workflow 体系（plan → tdd → verify），天然适合嵌入评估循环，但缺少"循环回到执行"的机制。

### 为什么现在要做

当前 goal 模式对用户承诺了"保持聚焦于目标"，但实际无法保证目标达成——用户仍需手动逐轮驱动。这让 goal 模式沦为"带标签的 plan 模式"，没有体现其价值。

## Goals / Non-Goals

### Goals

- `/goal <desc> when <condition>` 语法：设定目标 + 可验证的完成条件
- 每轮 turn 结束后，独立评估器检查 transcript 是否满足条件
- 未达成时自动启动下一轮，无需用户手动驱动
- 轮次预算（`max_turns`）和中断机制（`/goal clear`、Ctrl+C）
- 评估器与执行模型分离，避免"自己判断自己"

### Non-Goals

- 不做 Codex 风格的文件化里程碑（plans.md / implement.md / documentation.md）
- 不做验证命令执行（评估器只看 transcript，不跑命令）
- 不做 token 预算控制（仅做轮次预算）
- 不改变现有 plan/brainstorm/debug 等 workflow 的内部逻辑
- 不做多目标编排（复合目标应拆分为多个 `/goal`）

## Proposed Design

### 整体架构

```
用户: /goal <desc> when <condition>
  │
  ▼
slash/handler._goal()
  │  解析 desc + condition，存入 GoalSpec
  ▼
run_loop._handle_user_input()
  │  首次：用户输入作为第一轮的 user_text
  ▼
turn_runner.run_once()  ←─────────────┐
  │  正常执行 turn（读文件/改代码/跑测试）  │
  ▼                                   │
turn 完成                              │
  │                                   │
  ▼                                   │
goal_evaluator.evaluate()              │
  │  独立 LLM 读取 transcript + condition │
  │  返回 achieved / not_achieved       │
  ▼                                   │
  ├─ achieved → 输出 "Goal achieved" 摘要，循环结束
  │                                   │
  ├─ not_achieved + turns < max_turns ─┘
  │                                   自动启动下一轮
  └─ not_achieved + turns >= max_turns → 输出 "Goal budget exhausted"，停止
```

### Request / Data Flow

1. 用户输入 `/goal fix auth tests when all tests in tests/test_auth pass`，slash handler 解析出 `desc="fix auth tests"` 和 `condition="all tests in tests/test_auth pass"`
2. `GoalSpec(desc="fix auth tests", done_condition="all tests in tests/test_auth pass", max_turns=20)` 存入 `task_state.current_goal`，切换到 `InteractionMode.GOAL`
3. `run_loop` 检测到 goal 模式且有 `done_condition`，进入 **goal loop** 模式
4. 首轮：用户原始输入作为 `user_text` 传入 `run_once()`
5. `run_once()` 正常执行（resolve_goal_mode → plan workflow → tdd → verify）
6. turn 完成后，`run_loop` 调用 `goal_evaluator.evaluate(transcript, condition)`
7. 评估器用独立 LLM 调用判断 transcript 中是否有证据表明条件已满足
8. 未达成 → `run_loop` 自动构造下一轮的 `user_text`（如 "continue working on: <desc>"）并调 `run_once()`
9. 达成 → 输出摘要，退出 goal loop

### API / Function Contract

| Name | Input | Output | Error Behavior |
|------|-------|--------|----------------|
| `GoalSpec.done_condition` | `str \| None` | — | 空字符串视为 None |
| `GoalSpec.max_turns` | `int` (default 20) | — | ≤0 视为无限制 |
| `GoalSpec.goal_turn_count` | `int` (default 0) | — | 每轮 +1，持久化在 task_state |
| `goal_evaluator.evaluate()` | `transcript: list[BaseMessage]`, `condition: str`, `model: BaseChatModel`, `config: ModelConfig` | `GoalEvalResult(achieved: bool, reason: str)` | LLM 失败 → `achieved=False, reason="evaluator_error"` |
| `slash/handler._goal()` | `arg: str`（支持 `when` 分隔） | — | 无 `when` → 退化为当前行为（无循环） |
| `run_loop._run_goal_loop()` | `initial_text: str`, `task_state: TaskState` | — | max_turns 到达或用户中断时退出 |

## Data Model / Migration

### GoalSpec 扩展

```text
GoalSpec (src/voidx/runtime/task_state.py)
├── desc: str (existing, max 120 chars)
├── done_condition: str | None (new, max 2000 chars, default None)
├── max_turns: int (new, default 20, ge=1, le=200)
└── goal_turn_count: int (new, default 0, ge=0)
```

**迁移策略**：`done_condition`、`max_turns`、`goal_turn_count` 均有默认值，旧 `GoalSpec` 数据（只有 `desc`）可正常反序列化。`model_config = {"extra": "ignore"}` 已存在，向前兼容。

### GoalEvalResult

```text
GoalEvalResult (src/voidx/agent/goal_evaluator.py)
├── achieved: bool
├── reason: str (max 500 chars, 评估理由)
└── next_hint: str (max 200 chars, 给下一轮的引导提示，可为空)
```

### TaskState 扩展

```text
TaskState (src/voidx/runtime/task_state.py)
├── ... (existing fields)
└── goal_loop_active: bool (new, default False, 标记是否处于自主循环中)
```

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| 评估器用独立 LLM 调用 | 让执行模型自己判断 | CC 证明了执行/评估分离的价值：避免"自己判断自己"的偏差 |
| 评估器只看 transcript | 评估器自己跑验证命令 | CC 模型更简单可靠；让执行模型负责跑命令并把输出留在 transcript 中 |
| 轮次预算而非 token 预算 | 同时做 token 预算 | 轮次预算更直观，实现更简单；token 预算可作为后续增强 |
| `when` 关键字分隔 desc 和 condition | 正则解析、JSON 格式 | `when` 符合自然语言习惯，与 CC 的 `/goal <condition>` 语法接近 |
| 无 `when` 时退化为当前行为 | 强制要求 condition | 向后兼容，不破坏现有 `/goal <desc>` 用法 |
| 下一轮 user_text 自动构造 | 让评估器生成下一步指令 | 简单可控；评估器只做判断不做生成，职责单一 |
| `goal_turn_count` 存在 GoalSpec 中 | 存在 TaskState 顶层 | 与 goal 生命周期绑定，`clear_goal` 时自动重置 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| 评估器 LLM 调用增加延迟和成本 | 每轮多一次 LLM 调用 | 用轻量模型（低 max_tokens、无 reasoning）；评估器超时 10s |
| 评估器误判（假阳性）导致提前停止 | 目标未真正达成 | 评估器 prompt 要求引用 transcript 证据；用户可 `/goal clear` 后手动继续 |
| 评估器误判（假阴性）导致无限循环 | 浪费轮次和 token | max_turns 硬上限；连续 N 轮无进展时自动停止 |
| 上下文压缩导致评估器丢失早期证据 | 长任务中 transcript 被压缩 | 评估器看压缩后的 transcript（与执行模型同视角）；condition 本身是锚点 |
| 用户中断后状态不一致 | goal_loop_active 残留 | KeyboardInterrupt 和 `/goal clear` 都清理 goal_loop_active |
| 复合条件无法评估 | "redesign auth + add OAuth + write tests" | 文档建议拆分；评估器 prompt 中说明复合条件应判定为 not_achieved |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/runtime/task_state.py` | 扩展 `GoalSpec`：增加 `done_condition`、`max_turns`、`goal_turn_count`；`TaskState` 增加 `goal_loop_active` | 默认值确保向前兼容 |
| `src/voidx/agent/goal_evaluator.py` | **新建**：`GoalEvalResult`、`evaluate()` 函数 | 独立 LLM 调用，结构化输出 |
| `src/voidx/agent/slash/handler.py` | 扩展 `_goal()`：解析 `when` 语法 | 无 `when` 时退化为当前行为 |
| `src/voidx/agent/graph/run_loop.py` | 新增 `_run_goal_loop()` 方法：turn 完成后调评估器，未达成自动启动下一轮 | 嵌入 `_handle_user_input` 的 goal 分支 |
| `src/voidx/agent/runtime_context.py` | 在 `_current_task_state()` 中注入 goal loop 状态 | 让执行模型知道当前处于自主循环中 |
| `src/voidx/agent/goal_resolver.py` | 删除死代码 `resolve_goal_for_turn` 或标记 deprecated | 清理未使用的 LLM resolver |

### Existing Behavior

- `slash/handler.py:_goal()` 接收 `<desc>` 字符串，调 `task_state.set_goal(desc)` + 切换 `InteractionMode.GOAL`
- `turn_runner.py:run_once()` 检测 `interaction_mode == "goal"` → 调 `resolve_goal_mode()` → `PlanResolution(join="plan")`
- `run_loop.py:_handle_user_input()` 调 `self._run_once(user_input)` 后返回，等待下一个用户输入
- `runtime_context.py:_current_task_state()` 在 goal 模式下注入 `"Constraint: goal mode should keep work scoped to the current user goal and task state."`
- `GoalSpec` 只有 `desc` 字段，`model_config = {"extra": "ignore"}`

### Target Behavior

- `/goal fix auth tests when all tests in tests/test_auth pass` → `GoalSpec(desc="fix auth tests", done_condition="all tests in tests/test_auth pass", max_turns=20)`
- `/goal fix auth tests` → `GoalSpec(desc="fix auth tests")`（无 condition，退化为当前行为，无自主循环）
- goal loop 激活时，每轮 turn 完成后自动调评估器
- 评估器返回 `achieved=True` → 输出摘要，退出循环
- 评估器返回 `achieved=False` 且 `goal_turn_count < max_turns` → 自动启动下一轮
- `goal_turn_count >= max_turns` → 输出 "Goal budget exhausted (N/N turns)"，退出循环
- system prompt 中注入 `Goal loop: turn N/N, condition: <condition>`

### Invariants

- `GoalSpec.done_condition` 为 None 时，**不触发**自主循环（退化为当前行为）
- `goal_turn_count` 在 `/goal clear` 或 `/goal <new>` 时重置为 0
- 评估器**绝不**执行工具或命令，只读 transcript 做判断
- 评估器 LLM 调用失败时，视为 `not_achieved`（不阻断循环，但计入轮次）
- 用户 Ctrl+C 中断时，`goal_loop_active` 必须被清理为 False
- `max_turns` 到达时必须停止，不可被评估器覆盖
- `run_once()` 内部逻辑不改动——goal loop 在 `run_once()` 外层控制

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| 无 `when` 条件的 `/goal` | 退化为当前行为，无自主循环 | `test_goal_no_condition_no_loop` |
| 评估器 LLM 超时 | 视为 not_achieved，继续循环（计入轮次） | `test_evaluator_timeout_continues_loop` |
| 评估器 LLM 返回无效结构 | 视为 not_achieved，继续循环 | `test_evaluator_invalid_output_continues_loop` |
| `max_turns` 到达 | 输出 budget exhausted，停止循环 | `test_goal_loop_budget_exhausted` |
| 用户 Ctrl+C 中断 | 清理 `goal_loop_active`，回到正常模式 | `test_goal_loop_interrupted` |
| `/goal clear` 中断 | 清理 goal + `goal_loop_active`，回到 auto 模式 | `test_goal_clear_during_loop` |
| 评估器连续 3 轮返回相同 reason（无进展） | 自动停止，输出 "no progress detected" | `test_goal_loop_no_progress_stop` |
| condition 为空字符串 | 视为 None，不触发循环 | `test_empty_condition_no_loop` |
| 评估器在第一轮就返回 achieved | 立即停止，输出摘要 | `test_goal_achieved_first_turn` |

### Forbidden Changes

- 不修改 `turn_runner.py:run_once()` 内部的 turn 执行逻辑
- 不修改 `resolve_goal_mode()` 的返回值（仍为 `PlanResolution(join="plan")`）
- 不修改现有 workflow 的 DAG 定义或 transition 规则
- 不在评估器中执行任何工具或命令
- 不改变 `InteractionMode` 枚举值
- 不在 goal loop 中修改 `max_turns` 的值
- 不移除 `GoalSpec.model_config = {"extra": "ignore"}`

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| GoalSpec 扩展 | `./test.py --backend -- src/tests/test_runtime/test_task_state.py -k "test_goal_spec_done_condition"` | done_condition/max_turns 字段正确序列化 |
| 评估器逻辑 | `./test.py --backend -- src/tests/test_agent/test_goal_evaluator.py -k "test_evaluate_achieved"` | achieved=True when transcript 含通过证据 |
| 评估器假阴性 | `./test.py --backend -- src/tests/test_agent/test_goal_evaluator.py -k "test_evaluate_not_achieved"` | achieved=False when transcript 无证据 |
| 评估器超时 | `./test.py --backend -- src/tests/test_agent/test_goal_evaluator.py -k "test_evaluate_timeout"` | 返回 not_achieved + reason="evaluator_error" |
| slash 解析 | `./test.py --backend -- src/tests/test_agent/slash/test_slash_goal.py -k "test_goal_with_when"` | 正确解析 desc + condition |
| slash 无 when | `./test.py --backend -- src/tests/test_agent/slash/test_slash_goal.py -k "test_goal_without_when"` | 退化为当前行为 |
| goal loop 循环 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "test_goal_loop_continues"` | 未达成时自动启动下一轮 |
| goal loop 达成 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "test_goal_loop_achieved"` | 达成时输出摘要并停止 |
| goal loop 预算耗尽 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "test_goal_loop_budget_exhausted"` | max_turns 到达时停止 |
| goal loop 中断 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "test_goal_loop_interrupted"` | Ctrl+C 清理 goal_loop_active |
| 无进展停止 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "test_goal_loop_no_progress"` | 连续 3 轮无进展时停止 |
| 回归 | `./test.py --backend -- src/tests/test_agent/test_goal_resolver.py` | 现有 goal resolver 测试不受影响 |
| 回归 | `./test.py --backend -- src/tests/test_workflow/` | workflow reconcile 测试不受影响 |

## Resolved Questions

- [x] **评估器模型**：复用主模型降配（temperature=0, max_tokens=512），缓存命中友好。参考 `ResolverGoal` 的 structured output 注册方式（`with_structured_output`）。
- [x] **评估器视野**：复用主模型的完整上下文（不修改上下文），降配置调用。评估器已有全局视野（transcript + workflow_runs 状态），无需额外信号注入。
- [x] **下一轮 user_text**：固定模板 `"continue working on: {desc}"`，评估器只做判断不做生成。
- [x] **`/goal status`**：不需要，无实际价值。
