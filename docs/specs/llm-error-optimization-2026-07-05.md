# LLM 报错驱动的工具调用与提示词优化

## Status

Spec awaiting implementation.

## Summary

从 `~/.voidx/logs/agent_events.jsonl` 中 66 条真实 LLM 错误、75 条真实 replace 失败、2133 条 workflow 失败的调研中,识别出 3 个可落地的优化点。这些优化不涉及 LLM 错误格式的统一抽象(暂不做),而是针对报错暴露的具体问题做精准修复:

1. **LLM 重试策略过于粗暴** — 4xx/schema/auth 错误无脑重试 6 次,浪费时间且永远失败
2. **workflow advance 死循环 guidance 误导** — advance 成功后的 repeat guidance 语义错误,导致 LLM 持续重试到 12 次
3. **checkpoint/clarify 失败无 summary** — 失败路径不设 summary,日志记录为 "unknown error",污染诊断信号

## Problem

### 1. LLM 重试无差别(影响最大)

`src/voidx/agent/graph/core/llm.py:399` 的重试逻辑:

```python
if failed_attempts < max_retries:  # max_retries = 5,即 6 次尝试
    failed_attempts += 1
    delay = failed_attempts * 2
    ...
    await asyncio.sleep(delay)
```

**所有异常都被无脑重试**,唯一例外是 context_overflow(第 380 行先走 compaction)。日志显示 10 条错误本不该重试:

| 错误类型 | 数量 | 重试结果 |
|---------|------|---------|
| 404 model_not_found | 6 | 模型名错误,重试永远失败 |
| 400 context_overflow | 1 | 应走 compaction 而非重试(已有此分支,但 `_is_context_overflow_error` 靠字符串匹配可能漏判) |
| 503 invalid schema | 1 | 工具 schema 问题,重试无意义 |
| auth/余额不足 | 2 | 认证问题,重试浪费 |

每条错误重试 6 次,每次间隔 2/4/6/8/10 秒,总计浪费约 30 秒 + 6 次 API 调用。

### 2. workflow advance 死循环 guidance 误导

`src/voidx/tools/workflow.py:334` 的 `_repeat_guidance`:

```python
def _repeat_guidance(count: int, action: str, node: str) -> str:
    if count == 2:
        return (
            f"Node {node!r} is already active. You just called {action} {node} again. "
            "Do not repeat this call — proceed with the node's workflow steps instead."
        )
    ...
```

**问题**:这段 guidance 对 `enter` 动作正确,但对 `advance` 动作语义错误。advance 的语义是**转移出**节点,不是"再次进入"。当 LLM 在 feedback 节点成功调用 `advance feedback_valid` 后重复调用,guidance 说 "Node 'feedback' is already active" —— 但实际上 feedback 已经被满足并转移了,LLM 收到这个误导信息后困惑,持续重试。

日志证据(session `232c5d8fd6b2`,2026-07-05):
```
10:34:39 | feedback -> feedback_valid (repeated 3x)
10:36:20 | feedback -> feedback_valid (repeated 4x)
...
11:41:37 | feedback -> feedback_valid (repeated 12x)
```

guard 计数到 3(`_REPEAT_MAX`)后返回 error,但 LLM 仍继续重试到 12 次。

### 3. checkpoint/clarify 失败无 summary

`src/voidx/tools/plan_checkpoint.py:75` 和 `src/voidx/tools/clarify.py:54` 的 `interaction_unavailable` 路径:

```python
return ToolResult(
    title="plan: approval unavailable",
    output="...",
    metadata={"plan_decision": "interaction_unavailable", "blocked": True},
    # 没有 summary
)
```

`notify_tool_failure`(`src/voidx/agent/graph/tool_executor/ui.py:113`)在 `result.summary` 为空时 fallback 到 "unknown error":

```python
message=result.summary or "unknown error",
```

日志里有 3421 条 "unknown error"(其中真实会话 53 条),无法区分是 checkpoint 不可用、clarify 被跳过、还是真正的未知错误。

## Goals and Non-Goals

### Goals

- 4xx(非 429)/schema/auth 错误 fail-fast,不重试
- advance 成功后的 repeat guidance 语义正确,明确告知"转移已完成,停止重复调用"
- checkpoint/clarify 所有失败路径都有 summary

### Non-Goals

- LLM 错误格式的统一抽象(HTTP status_code / error_code / error_type / error_message 结构化)—— 暂不做,当前 3 个优化不需要它
- 重试策略的 provider 感知(不同 provider 不同退避策略)
- workflow 死循环的根治(需要更深的 turn-level 限制机制)

## Architecture

### 优化点 1:LLM 错误分类与 fail-fast

在 `src/voidx/agent/graph/core/helpers.py` 新增错误分类函数,在 `llm.py` 的 except 块里调用:

```
helpers.py
├── _is_context_overflow_error(exc)  # 已有,保留
└── _classify_llm_error(exc) -> LLMErrorKind  # 新增
    ├── NETWORK          # APIConnectionError, Connection error → 重试
    ├── TIMEOUT          # asyncio.TimeoutError, timed out → 重试
    ├── RATE_LIMIT       # 429, 503 system busy → 重试
    ├── SERVER_ERROR     # 500, 502, 503 (非 429) → 重试
    ├── CONTEXT_OVERFLOW # 已有逻辑 → compaction
    ├── NON_RETRYABLE    # 400(非 overflow), 401, 403, 404, schema → fail-fast
    └── UNKNOWN          # 其他 → 重试(保守策略)
```

`llm.py` 的 except 块改为:

```python
except Exception as e:
    from .helpers import _classify_llm_error, LLMErrorKind

    kind = _classify_llm_error(e)

    if kind == LLMErrorKind.CONTEXT_OVERFLOW and overflow_compaction_attempts < 1:
        # 已有 compaction 分支,不变
        ...

    if kind == LLMErrorKind.NON_RETRYABLE:
        # fail-fast,不重试
        failure_text = f"LLM call failed (non-retryable: {kind}): {e}"
        ...
        return {"messages": [], ...}

    if failed_attempts < max_retries:
        # 已有重试分支,不变
        ...
```

**分类逻辑**基于 SDK 异常属性(不靠字符串匹配):

```python
def _classify_llm_error(exc: Exception) -> LLMErrorKind:
    # 1. 优先检查 status_code(OpenAI/Anthropic SDK 的 APIStatusError 都有)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code == 429:
            return LLMErrorKind.RATE_LIMIT
        if status_code in (400,):
            # 400 可能是 context overflow,交给 _is_context_overflow_error 判断
            if _is_context_overflow_error(exc):
                return LLMErrorKind.CONTEXT_OVERFLOW
            return LLMErrorKind.NON_RETRYABLE
        if status_code in (401, 403):
            return LLMErrorKind.NON_RETRYABLE
        if status_code == 404:
            return LLMErrorKind.NON_RETRYABLE
        if status_code in (500, 502, 503):
            # 503 可能是 schema 错误(provider 把 schema 校验失败包成 503)
            if _is_schema_error(exc):
                return LLMErrorKind.NON_RETRYABLE
            return LLMErrorKind.SERVER_ERROR
        return LLMErrorKind.UNKNOWN

    # 2. 无 status_code — 检查异常类型
    if isinstance(exc, asyncio.TimeoutError):
        return LLMErrorKind.TIMEOUT
    # APIConnectionError 及其子类
    exc_module = type(exc).__module__
    if "connection" in type(exc).__name__.lower() or "ConnectionError" in type(exc).__name__:
        return LLMErrorKind.NETWORK

    # 3. 字符串 fallback(保守)
    if _is_context_overflow_error(exc):
        return LLMErrorKind.CONTEXT_OVERFLOW
    return LLMErrorKind.UNKNOWN
```

`_is_schema_error` 检查 body/message 里是否包含 schema 相关关键词:

```python
def _is_schema_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("invalid schema", "schema for function", "required is required"))
```

### 优化点 2:advance repeat guidance 语义修正

`src/voidx/tools/workflow.py` 的 `_repeat_guidance` 拆分为 enter 和 advance 两个版本:

```python
def _repeat_guidance(count: int, action: str, node: str) -> str:
    if action == "advance":
        return _advance_repeat_guidance(count, node)
    return _enter_repeat_guidance(count, node)


def _advance_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"You already advanced {node!r} with this condition. "
            "The transition succeeded — do not call advance again. "
            "Proceed with the next node's workflow steps."
        )
    return (
        f"You have called advance {node!r} {count} times with the same condition. "
        "The transition already succeeded. Stop retrying — "
        "either proceed with the next node's workflow, or summarize the blocker and ask the user."
    )


def _enter_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"Node {node!r} is already active. You just called enter {node} again. "
            "Do not repeat this call — proceed with the node's workflow steps instead."
        )
    return (
        f"Node {node!r} is already active and you have called enter {node} {count} times. "
        "Stop retrying. Either advance the current node with a valid exit condition, "
        "or summarize the blocker and ask the user for input."
    )
```

### 优化点 3:checkpoint/clarify 失败路径补 summary

| 文件 | 行 | 路径 | 新增 summary |
|------|-----|------|-------------|
| `plan_checkpoint.py:75` | interaction_unavailable | `summary="plan: approval unavailable"` |
| `clarify.py:54` | interaction_unavailable | `summary="clarify: unavailable"` |
| `clarify.py:72` | user skipped | `summary="clarify: skipped"` |
| `clarify.py:52` | Invalid arguments | `summary="clarify: invalid arguments"` |
| `todo.py:74` | Invalid arguments | `summary="todo: invalid arguments"` |
| `plan_checkpoint.py:73` | Invalid arguments | `summary="plan: invalid arguments"` |

## Data Model

### LLMErrorKind 枚举

```python
class LLMErrorKind(str, Enum):
    NETWORK = "network"              # APIConnectionError → 重试
    TIMEOUT = "timeout"              # asyncio.TimeoutError → 重试
    RATE_LIMIT = "rate_limit"        # 429 → 重试
    SERVER_ERROR = "server_error"    # 500/502/503 → 重试
    CONTEXT_OVERFLOW = "context_overflow"  # → compaction
    NON_RETRYABLE = "non_retryable"  # 400(非 overflow)/401/403/404/schema → fail-fast
    UNKNOWN = "unknown"              # 其他 → 重试(保守)
```

## Error Handling

| 失败场景 | 当前行为 | 优化后行为 |
|---------|---------|-----------|
| 404 model_not_found | 重试 6 次,~30s 后失败 | fail-fast,立即失败 |
| 401/403 auth 错误 | 重试 6 次 | fail-fast |
| 400 bad_request(非 overflow) | 重试 6 次 | fail-fast |
| 503 schema 错误 | 重试 6 次 | fail-fast |
| 400 context_overflow | 走 compaction(已有) | 不变 |
| 429 rate_limit | 重试 6 次 | 不变(重试合理) |
| 500/502/503 server_error | 重试 6 次 | 不变(重试合理) |
| Connection error | 重试 6 次 | 不变(重试合理) |
| Timeout | 重试 6 次 | 不变(重试合理) |
| workflow advance 重复 | guidance 误导,LLM 重试到 12 次 | guidance 语义正确,明确告知"转移已完成" |
| checkpoint/clarify 失败 | 日志 "unknown error" | 日志有明确 summary |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 `getattr(exc, "status_code")` 提取 HTTP 状态码 | 解析 `str(e)` 里的 "Error code: NNN" | status_code 是 SDK 实例属性,可靠;字符串解析脆弱 |
| 503 schema 错误归为 NON_RETRYABLE | 归为 SERVER_ERROR 重试 | schema 错误是确定性失败,重试不会改变结果 |
| UNKNOWN 归为重试 | UNKNOWN 归为 fail-fast | 保守策略,避免误杀可恢复错误 |
| advance guidance 拆分函数 | 在现有函数里加 if/else | 语义清晰,enter 和 advance 是不同动作 |
| 不做错误格式统一抽象 | 做结构化 LLMError(status_code/error_code/error_type/message) | 当前 3 个优化不需要它,避免过度设计 |

## File Structure

| 文件 | 职责 |
|------|------|
| `src/voidx/agent/graph/core/helpers.py` | 新增 `LLMErrorKind` 枚举和 `_classify_llm_error` 函数 |
| `src/voidx/agent/graph/core/llm.py` | except 块调用 `_classify_llm_error`,NON_RETRYABLE 走 fail-fast |
| `src/voidx/tools/workflow.py` | `_repeat_guidance` 拆分为 enter/advance 两个版本 |
| `src/voidx/tools/plan_checkpoint.py` | 失败路径补 summary |
| `src/voidx/tools/clarify.py` | 失败路径补 summary |
| `src/voidx/tools/todo.py` | Invalid arguments 路径补 summary |
| `tests/test_agent/graph/test_call_llm_compaction.py` | 新增 fail-fast 测试 |
| `tests/test_tools/test_workflow_tool.py` | 新增 advance repeat guidance 语义测试 |
| `tests/test_tools/test_plan_checkpoint.py` | 新增 summary 测试 |
| `tests/test_tools/test_clarify_tool.py` | 新增 summary 测试 |
| `tests/test_tools/test_todo_tool.py` | 新增 todo summary 测试 |

## Tasks

### Task 1: LLM 错误分类与 fail-fast

- [ ] 1.1 在 `helpers.py` 新增 `LLMErrorKind` 枚举和 `_classify_llm_error` 函数,覆盖 7 种错误类型
- [ ] 1.2 在 `helpers.py` 新增 `_is_schema_error` 辅助函数
- [ ] 1.3 在 `llm.py` 的 except 块调用 `_classify_llm_error`,NON_RETRYABLE 走 fail-fast 分支
- [ ] 1.4 测试:404 model_not_found 不重试,直接失败
- [ ] 1.5 测试:Connection error 仍然重试
- [ ] 1.6 测试:400 context_overflow 仍走 compaction 分支
- [ ] 1.7 测试:503 schema 错误 fail-fast

**测试命令**:
```bash
./python.sh -m pytest tests/test_agent/graph/test_call_llm_compaction.py -v -k "fail_fast or classify or non_retryable"
```

### Task 2: workflow advance repeat guidance 语义修正

- [ ] 2.1 在 `workflow.py` 将 `_repeat_guidance` 拆分为 `_enter_repeat_guidance` 和 `_advance_repeat_guidance`
- [ ] 2.2 测试:advance 重复调用的 guidance 包含 "transition succeeded" 或 "already advanced"
- [ ] 2.3 测试:enter 重复调用的 guidance 仍包含 "already active"

**测试命令**:
```bash
./python.sh -m pytest tests/test_tools/test_workflow_tool.py -v -k "repeated_advance or repeated_enter"
```

### Task 3: checkpoint/clarify/todo 失败路径补 summary

- [ ] 3.1 `plan_checkpoint.py` interaction_unavailable 路径加 `summary="plan: approval unavailable"`
- [ ] 3.2 `plan_checkpoint.py` Invalid arguments 路径加 `summary="plan: invalid arguments"`
- [ ] 3.3 `clarify.py` interaction_unavailable 路径加 `summary="clarify: unavailable"`
- [ ] 3.4 `clarify.py` user skipped 路径加 `summary="clarify: skipped"`
- [ ] 3.5 `clarify.py` Invalid arguments 路径加 `summary="clarify: invalid arguments"`
- [ ] 3.6 `todo.py` Invalid arguments 路径加 `summary="todo: invalid arguments"`
- [ ] 3.7 测试:各失败路径返回的 ToolResult 有非空 summary

**测试命令**:
```bash
./python.sh -m pytest tests/test_tools/test_plan_checkpoint.py tests/test_tools/test_clarify_tool.py tests/test_tools/test_todo_tool.py -v -k "summary or unavailable or invalid"
```

## Risks

- **误判 NON_RETRYABLE**:如果某个 provider 把可恢复错误包成 400/404,会被 fail-fast。缓解:UNKNOWN 默认重试,只有明确的 4xx 才 fail-fast。
- **SDK 异常属性差异**:不同 provider SDK(OpenAI/Anthropic/DeepSeek/Gemini)的异常结构可能不同。缓解:`getattr(exc, "status_code", None)` 是统一接口,无 status_code 时 fallback 到字符串匹配。
- **advance guidance 拆分影响现有测试**:现有 `test_repeated_advance_returns_warning_then_error` 可能断言 guidance 文本。缓解:更新断言匹配新文本。
- **summary 变更影响 UI 显示**:summary 用于 hidden tool 的日志和状态显示。新增 summary 不会破坏现有 UI,只是让之前显示 "unknown error" 的地方显示更有意义的内容。

## Open Questions

- [ ] `max_retries = 5`(6 次尝试)是否过多?当前优化只改 fail-fast 逻辑,不改 max_retries 值。后续可考虑按 error_kind 设置不同重试次数。
