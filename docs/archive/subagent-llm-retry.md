# 子 Agent LLM 调用重试机制 — 技术设计文档
> **Status: Done**


## Context

主 agent（`src/voidx/agent/graph/core/llm.py`）在调用 `stream_llm` 时有完整的重试机制：`max_retries=5`、线性退避（2/4/6/8/10s）、错误分类（`_classify_llm_error` → `LLMErrorKind`），区分可重试与不可重试错误。

子 agent（`src/voidx/agent/graph/subagent.py` 的 `run_subagent`）直接调用 `stream_llm`，**无应用层重试**。`stream_llm` 抛异常时，被 `run_subagent` 的 `except Exception` 捕获 → `mark_finished("error")` → `raise` → 由 `AgentTool.execute` 的 `except` 捕获返回失败 `ToolResult`。

这意味着一次网络抖动或临时 429 就会导致子 agent 整体失败，而主 agent 在同样场景下能自动恢复。本设计在子 agent 中增加复用主 agent 错误分类与退避策略的 LLM 重试层，同时保留子 agent 现有 `raise` → `AgentTool` 失败 `ToolResult` 的错误传播链路。

## Goals and Non-Goals

### Goals

- 子 agent 的 `stream_llm` 调用具备 `max_retries=5` 的重试能力：初始调用 + 最多 5 次重试，共最多 6 次尝试
- 线性退避：2s, 4s, 6s, 8s, 10s
- 复用 `_classify_llm_error` + `LLMErrorKind`，NON_RETRYABLE 不重试
- 重试耗尽后保持现有行为：raise 异常，由外层 `except` 处理
- 重试时发送 `llm:retry` UI 状态事件（`StatusUpdated` / `StatusFinished`）；失败传播保持子 agent 现有 `raise` 链路
- 确保所有重试状态退出路径都会清理 `llm:retry` 状态，避免 UI 遗留 pending 状态

### Non-Goals

- 上下文压缩（CONTEXT_OVERFLOW 时自动压缩）— 子 agent 无 compaction 基础设施，超出范围；本次不做无效的盲重试
- Malformed tool call 修复 — `stream_llm` 只会标记 malformed tool call 元数据，主 agent 的修复重试发生在 `_prepare_with_stream`；子 agent 暂不补齐该行为，作为独立后续工作处理
- 修改 `stream_llm` 本身 — 重试逻辑在调用方
- 修改主 agent 的重试逻辑

## Architecture

### 当前调用链

```
run_subagent (subagent.py:155-395)
  └─ while step < 50  (主循环)
       └─ assistant_msg = await stream_llm(...)  ← 无重试，异常直接上抛
       └─ 处理 tool_calls / 返回文本
  └─ except Exception → mark_finished("error") → raise
```

### 目标调用链

```
run_subagent (subagent.py)
  └─ while step < 50  (主循环)
       └─ 内层 LLM 重试循环 (新增)
            ├─ try: assistant_msg = await stream_llm(...)
            │   成功 → 清理 retry 状态 → break，继续原有逻辑
            ├─ except: kind = _classify_llm_error(e)
            │   ├─ NON_RETRYABLE → 清理 retry 状态 → raise (不重试)
            │   ├─ CONTEXT_OVERFLOW → 清理 retry 状态 → raise (不压缩、不盲重试)
            │   ├─ 其他可重试 → failed_attempts += 1
            │   │   ├─ <= 5: 退避 failed_attempts*2s → continue
            │   │   └─ > 5: 清理 retry 状态 → raise (重试耗尽)
            │   └─ UI 事件: StatusUpdated("llm:retry") / StatusFinished
       └─ 处理 tool_calls / 返回文本 (不变)
  └─ except Exception → mark_finished("error") → raise (不变)
```

### 关键设计决策

1. **重试逻辑内联在主循环中**，不提取为独立函数。原因：重试循环需要访问 `model_with_tools`、`llm_messages`、`renderer`、`config` 等局部变量，提取为函数需要大量参数传递，反而降低可读性。

2. **重试耗尽后 raise 而非返回文本**。原因：保持现有错误传播链路不变。`run_subagent` 的 `except Exception` 会 `mark_finished("error")` 并 `raise`，`AgentTool.execute` 捕获后返回失败 `ToolResult`。如果改为返回文本，会绕过这条链路，主 agent 收到的是“正常文本”而非错误信号。

3. **CONTEXT_OVERFLOW 不做压缩，也不盲重试**。原因：子 agent 没有 `CompactionService`、`_preflight_compact_if_needed` 等基础设施；同一上下文重复调用大概率继续失败，只会增加最多 30s 延迟。因此子 agent 将 `CONTEXT_OVERFLOW` 视为不可恢复失败：清理 retry 状态后直接 `raise`，由现有错误传播链路生成失败 `ToolResult`。

4. **复用 `_classify_llm_error` 和 `LLMErrorKind`**。这两个定义在 `voidx.agent.graph.core.helpers` 中，是纯函数和枚举，无状态依赖，可直接导入复用。

5. **显式清理 retry 状态**。只要曾经发送过 `StatusUpdated(status_id="llm:retry")`，后续无论成功、NON_RETRYABLE 退出，还是重试耗尽退出，都必须发送 `StatusFinished(status_id="llm:retry")`。

## API Contract

### 新增常量

```python
# subagent.py 模块级
_LLM_MAX_RETRIES = 5
```

语义与主 agent 保持一致：`_LLM_MAX_RETRIES = 5` 表示最多 5 次重试，不包含首次调用；因此总尝试次数最多为 6 次。

### 新增导入

```python
# subagent.py 顶部
from voidx.agent.graph.core.helpers import LLMErrorKind, _classify_llm_error
from voidx.runtime.ui import StatusFinished, StatusUpdated
```

### 内层重试循环（伪代码）

```python
# 在 run_subagent 主循环内，替换原来的:
#   assistant_msg = await stream_llm(model_with_tools, llm_messages, renderer, resolve_protocol(config.model))
# 为:

llm_failed_attempts = 0
retry_status_active = False
while True:
    try:
        assistant_msg = await stream_llm(
            model_with_tools, llm_messages, renderer, resolve_protocol(config.model),
        )
        if retry_status_active and ui_port.via_events():
            await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
        break
    except Exception as e:
        kind = _classify_llm_error(e)
        if kind in {LLMErrorKind.NON_RETRYABLE, LLMErrorKind.CONTEXT_OVERFLOW}:
            if retry_status_active and ui_port.via_events():
                await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
            raise
        if llm_failed_attempts < _LLM_MAX_RETRIES:
            llm_failed_attempts += 1
            delay = llm_failed_attempts * 2
            retry_detail = f"retrying in {delay}s: {e}"
            if ui_port.via_events():
                retry_status_active = True
                await ui_port.events.emit(StatusUpdated(
                    status_id="llm:retry",
                    label="Retrying",
                    detail=retry_detail,
                ))
            else:
                ui_port.ui.print(f"[dim]Retrying ({retry_detail})[/dim]")
            await asyncio.sleep(delay)
            continue
        if retry_status_active and ui_port.via_events():
            await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
        raise
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| NON_RETRYABLE (400/401/403/404) | 不重试；如 retry 状态已激活则先发送 `StatusFinished("llm:retry")`，再 raise |
| CONTEXT_OVERFLOW | 不压缩、不盲重试；清理 retry 状态后直接 raise，由外层 `except` 捕获 → `mark_finished("error")` |
| RATE_LIMIT (429) | 线性退避重试，最多 5 次重试（总尝试最多 6 次） |
| SERVER_ERROR (500/502/503) | 线性退避重试，最多 5 次重试（总尝试最多 6 次） |
| TIMEOUT | 线性退避重试，最多 5 次重试（总尝试最多 6 次） |
| NETWORK (ConnectionError) | 线性退避重试，最多 5 次重试（总尝试最多 6 次） |
| UNKNOWN | 线性退避重试，最多 5 次重试（总尝试最多 6 次） |
| 重试后成功 | 发送 `StatusFinished("llm:retry")` 后继续原有子 agent 逻辑 |
| 重试耗尽 | 如 retry 状态已激活则先发送 `StatusFinished("llm:retry")`，再 raise，由外层 except 捕获 → `mark_finished("error")` |

## Test Plan

- **测试文件**：新增或扩展 `src/tests/test_subagent_llm_retry.py`；mock 点包括 `voidx.agent.graph.subagent.stream_llm`、`voidx.agent.graph.subagent._classify_llm_error`、`asyncio.sleep`、`ui_port.events.emit` / `ui_port.ui.print`。
- **临时错误成功重试**：模拟前 1-2 次 `stream_llm` 抛 RATE_LIMIT / SERVER_ERROR，后续成功；断言最终返回成功结果、调用次数为失败次数 + 1、退避使用 2s/4s，且 `asyncio.sleep` 被 mock 不产生真实等待。
- **NON_RETRYABLE 不重试**：模拟 `_classify_llm_error` 返回 NON_RETRYABLE；断言只调用一次 `stream_llm`，异常继续上抛，并触发外层 `mark_finished("error")` 链路。
- **CONTEXT_OVERFLOW 不盲重试**：模拟 `_classify_llm_error` 返回 CONTEXT_OVERFLOW；断言只调用一次 `stream_llm`，不调用 `asyncio.sleep`，异常继续上抛。
- **重试耗尽**：模拟连续可重试错误；断言总尝试次数为 6 次（初始调用 + 5 次重试），最终异常上抛，并保持现有失败 `ToolResult` 行为。
- **事件模式 cleanup**：在 `ui_port.via_events()` 为 true 时，断言每个激活过的 `StatusUpdated(status_id="llm:retry")` 都有对应 `StatusFinished(status_id="llm:retry")`，覆盖成功、NON_RETRYABLE、CONTEXT_OVERFLOW、重试耗尽四类退出路径。
- **非事件模式 fallback**：在 `ui_port.via_events()` 为 false 时，断言重试提示走 `ui_port.ui.print(...)`，且不发送 retry status events。
- **目标命令**：`./test.py --backend -- src/tests/test_subagent_llm_retry.py`；必要时追加相关 agent graph 测试文件做回归。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 重试逻辑内联 | 提取为独立函数 | 内联避免大量参数传递，重试循环只涉及局部变量 |
| 重试耗尽后 raise | 返回错误文本 | 保持现有错误传播链路不变，不破坏 AgentTool 的失败处理 |
| 不处理 CONTEXT_OVERFLOW 压缩 | 为子 agent 增加 compaction | 超出当前范围，子 agent 无 compaction 基础设施 |
| 不处理 malformed tool call 修复 | 复制主 agent malformed 修复流程 | 该流程不是 `stream_llm` 内部行为，需单独设计和测试，避免本次重试改动扩大范围 |
| 复用 `_classify_llm_error` | 在 subagent.py 中重新实现 | 单一数据源，避免分类逻辑分叉 |
| `_LLM_MAX_RETRIES = 5` | 可配置化 | 与主 agent 一致，暂不需要配置化 |
| 所有退出路径清理 retry 状态 | 仅成功时清理 | 防止 UI status dock 遗留 pending retry 状态 |

## Open Questions

- 无。已确认 `asyncio.sleep` 是非阻塞等待；并行子 agent 同时退避不会阻塞事件循环，但会增加对应子任务的端到端延迟。
