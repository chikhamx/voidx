---
name: turn-stop-without-pending
display_name: Turn Stop Without Pending Fix
description: 修复主 agent 调用子 agent 后直接调用 turn stop（无文本）导致循环断开的 bug
doc_type: tech-design
audience: human+llm
---

# Turn Stop Without Pending — 技术设计文档

## TL;DR

主 agent 调用子 agent（如 review）后，子 agent 返回结果。主 agent 在 `turn_state="running"` 时直接调用 `turn stop` 而没有先输出纯文本，导致 `pending_provisional` 为 `None`，`validate_turn_call` 失败。原有的 `INVALID_TURN_PROMPT` 说"不要输出文本，调用 turn stop"——与"没有 pending 可以 commit"矛盾，形成死循环，最终返回 `failure_msg` 且 `should_continue=False`，用户看不到任何有意义的文本。

修复方案：新增 `NO_USER_RESPONSE_PROMPT`，在 `turn_terminal` 无文本时引导 LLM 先输出文本；同时放宽第 468 行的强制 `INVALID_TURN` 逻辑，允许 `VALID_TURN`（文本+turn stop）在 `pending_provisional` 为 `None` 时通过；`VALID_TURN` 分支优先用 `pending_provisional`，无则用 `assistant_msg`。

## Context

### 触发场景

1. 主 agent 有活跃 workflow（如 review）
2. 主 agent 调用 `agent` 工具委托 review 子 agent
3. 子 agent 返回 review 结果（`ToolMessage`）
4. 主 agent 在下一次 `_call_llm` 中，`turn_state="running"`
5. LLM 认为任务完成，直接调用 `turn stop`（无文本内容）

### 问题路径

```
_call_llm (turn_state="running")
  → LLM 调用 turn stop（无文本）
  → classify_turn_call → VALID_TURN
  → has_text = False
  → turn_terminal = pending_provisional = None
  → validate_turn_call(assistant_msg, None) → False
  → invalid_turn_repaired = False → True
  → 注入 INVALID_TURN_PROMPT（"不要输出文本，调用 turn stop"）
  → continue

  → LLM 再次调用
  → 场景 A: 纯文本 → PLAIN_TEXT → 设置 pending_provisional → 注入 TURN_STOP_PROMPT → LLM 调用 turn stop → 验证通过 ✓
  → 场景 B: 文本 + turn stop（同一消息）→ 第 468 行强制改为 INVALID_TURN → invalid_turn_repaired=True → failure_msg ✗
  → 场景 C: turn stop（无文本）→ VALID_TURN → validate 失败 → invalid_turn_repaired=True → failure_msg ✗
```

**根因 1：** `INVALID_TURN_PROMPT` 假设 `pending_provisional` 存在，当其为 `None` 时引导方向错误。

**根因 2：** 第 468 行的强制 `INVALID_TURN` 逻辑过于宽泛，把 `VALID_TURN`（文本+turn stop）也强制为 `INVALID_TURN`，阻止了 LLM 在一条消息里同时输出文本和 commit。

## Goals / Non-Goals

### Goals

- 当 `pending_provisional` 为 `None` 且 LLM 调用 `turn stop` 时，引导 LLM 先输出文本
- 允许 LLM 在 `NO_USER_RESPONSE_PROMPT` 后用一条消息（文本+turn stop）完成 commit
- 不破坏现有的 turn control 修复机制（`invalid_turn_repaired` 一次修复机会）
- 不破坏 `pending_provisional` 存在时的行为（文本+turn stop 用 `pending_provisional`，不用新文本）

### Non-Goals

- 不修改 `invalid_turn_repaired` 的"一次修复机会"设计
- 不修改 `classify_turn_call` 的分类逻辑
- 不修改 `validate_turn_call` 的验证逻辑

## Proposed Design

### 核心思路

检查 `turn_terminal`（即 `pending_provisional` 或 `assistant_msg`）是否有文本。如果没有，说明用户还没收到任何文本回复，拒绝 `turn stop` 并提示"你还没回答用户"。

### Request / Data Flow

```
_call_llm (turn_state="running")
  → LLM 调用 turn stop（无文本）
  → VALID_TURN, has_text=False, pending_provisional=None
  → turn_terminal = None
  → validate_turn_call → False
  → invalid_turn_repaired = False → True
  → turn_terminal 无文本 → 注入 NO_USER_RESPONSE_PROMPT（"先输出文本，再调用 turn stop"）
  → continue

  → LLM 再次调用
  → 场景 A: 纯文本 → PLAIN_TEXT → pending_provisional = assistant_msg → 注入 TURN_STOP_PROMPT → turn stop → 验证通过 ✓
  → 场景 B: 文本 + turn stop → VALID_TURN（不再被强制为 INVALID_TURN）→ pending_provisional=None, has_text=True → turn_terminal = assistant_msg → 验证通过 ✓
  → 场景 C: turn stop（无文本）→ VALID_TURN → turn_terminal = None → validate 失败 → invalid_turn_repaired=True → failure_msg ✗（预期行为）
```

### 三处修改

| 位置 | 修改 | 说明 |
|------|------|------|
| `turn_control.py` | 新增 `NO_USER_RESPONSE_PROMPT` | 替代 `MISSING_PENDING_PROMPT`，语义更清晰 |
| `llm.py` 第 468 行 | 放宽强制 `INVALID_TURN` 逻辑 | 当 `classification == VALID_TURN and pending_provisional is None` 时不强制 |
| `llm.py` `VALID_TURN` 分支 | `turn_terminal` 选择 + `repair_prompt` 选择 | 优先用 `pending_provisional`，无则用 `assistant_msg`；`turn_terminal` 无文本时用 `NO_USER_RESPONSE_PROMPT` |

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| 新增 `NO_USER_RESPONSE_PROMPT` 而非修改 `INVALID_TURN_PROMPT` | 修改 `INVALID_TURN_PROMPT` 使其条件化 | `INVALID_TURN_PROMPT` 在有 pending 时语义正确，不应改动；新场景需要不同引导方向 |
| 放宽第 468 行强制逻辑 | 在 `INVALID_TURN` 分支也区分 `pending_provisional` | 从源头修复更干净：`VALID_TURN` 本就是合法分类，不应被强制覆盖 |
| `VALID_TURN` 分支优先用 `pending_provisional` | 统一用 `assistant_msg` | 当 `pending_provisional` 存在时，应使用之前的文本，忽略 turn stop 时附加的额外文本 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM 在 `NO_USER_RESPONSE_PROMPT` 后仍不输出文本 | 两次失败后返回 `failure_msg` | 预期行为，与原有设计一致 |
| 放宽第 468 行可能让其他 `VALID_TURN` 场景绕过强制 | 仅当 `pending_provisional is None` 时放宽，有 pending 时行为不变 | 条件精确，不影响有 pending 的场景 |
| `NO_USER_RESPONSE_PROMPT` 引导 LLM 输出文本+turn stop，但 LLM 可能只输出纯文本 | 纯文本走 `PLAIN_TEXT` 分支，设置 `pending_provisional` 后正常流程 | 两条路径都已覆盖 |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/agent/graph/turn_control.py` | 新增 `NO_USER_RESPONSE_PROMPT` 常量 | 放在 `INVALID_TURN_PROMPT` 之后 |
| `src/voidx/agent/graph/core/llm.py` | 3 处修改：import、第 468 行强制逻辑、`VALID_TURN` 分支 turn_terminal 和 repair_prompt | 见下方详细说明 |
| `src/tests/test_agent/graph/test_turn_stop_without_pending.py` | 新增测试文件 | 两个场景：纯文本路径和文本+turn stop 路径 |

### Existing Behavior

- `turn_prompt_active=True` 时，任何有文本的非 `PLAIN_TEXT` 响应被强制为 `INVALID_TURN`（第 468 行）
- `VALID_TURN` 分支中 `turn_terminal` 在 `turn_prompt_active=True` 时取 `pending_provisional`
- `VALID_TURN` 分支修复时硬编码注入 `INVALID_TURN_PROMPT`

### Target Behavior

- `turn_prompt_active=True` 且 `classification=VALID_TURN` 且 `pending_provisional=None` 时，**不**强制为 `INVALID_TURN`
- `VALID_TURN` 分支中，`pending_provisional` 不为 `None` 时用 `pending_provisional`，否则用 `assistant_msg`（如果有文本）
- `VALID_TURN` 分支修复时，`turn_terminal` 无文本时注入 `NO_USER_RESPONSE_PROMPT`，否则注入 `INVALID_TURN_PROMPT`

### Invariants

- `invalid_turn_repaired` 保持"一次修复机会"设计：第一次失败注入 prompt，第二次失败返回 `failure_msg`
- `classify_turn_call` 和 `validate_turn_call` 的逻辑不变
- 有 `pending_provisional` 时的行为完全不变（文本+turn stop 用 `pending_provisional`，不被强制为 `INVALID_TURN`）

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| `turn stop`（无文本）→ `NO_USER_RESPONSE_PROMPT` → 纯文本 → `TURN_STOP_PROMPT` → `turn stop` | 文本输出，验证通过 | `test_turn_stop_in_running_state_without_pending_emits_text` |
| `turn stop`（无文本）→ `NO_USER_RESPONSE_PROMPT` → 文本+turn stop（同一消息） | 文本输出，验证通过 | `test_turn_stop_with_text_after_missing_pending` |
| `turn stop`（无文本）→ `NO_USER_RESPONSE_PROMPT` → `turn stop`（仍无文本） | `failure_msg`，`should_continue=False` | 预期行为，未单独测试（与原有设计一致） |
| `pending_provisional` 存在时文本+turn stop | 用 `pending_provisional`，忽略新文本 | `test_decision_only_turn_with_text_is_rejected_before_stop` |

### Forbidden Changes

- 不修改 `classify_turn_call` 的分类规则
- 不修改 `validate_turn_call` 的验证逻辑
- 不修改 `invalid_turn_repaired` 的"一次修复机会"设计
- 不修改 `INVALID_TURN_PROMPT` 的内容
- 不修改 `PLAIN_TEXT` 分支的逻辑

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| 纯文本路径 | `./test.py --backend -- src/tests/test_agent/graph/test_turn_stop_without_pending.py::test_turn_stop_in_running_state_without_pending_emits_text -x` | PASS |
| 文本+turn stop 路径 | `./test.py --backend -- src/tests/test_agent/graph/test_turn_stop_without_pending.py::test_turn_stop_with_text_after_missing_pending -x` | PASS |
| Turn control 回归 | `./test.py --backend -- src/tests/test_agent/graph/test_turn_control_e2e.py src/tests/test_agent/test_turn_control.py -v` | 33 PASS |
| Agent 全量回归 | `./test.py --backend -- src/tests/test_agent/` | 987 PASS |

## Open Questions

- [ ] 无
