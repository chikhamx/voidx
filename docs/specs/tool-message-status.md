# ToolMessage status 字段 — 技术设计文档

## Context

当前所有 `ToolMessage` 创建时均使用 LangChain 默认的 `status="success"`，即使工具执行失败（error / blocked / denied / timeout），失败信息也只存在于 `ToolResult.metadata` 中，在 `ToolMessage` 层面丢失。

这导致两个问题：

1. **Anthropic 模型收不到 `is_error` 信号** — Anthropic adapter 已有逻辑：`curr.status == "error"` → `is_error=True`，但因为 status 从未被设为 error，该路径永远不触发。模型无法区分工具返回的正常文本和错误信息。
2. **失败的 tool exchange 无法被自动清理** — LLM 在当前 turn 已看到错误并做出反应，下一 turn 这些失败交换变成冗余历史，浪费 context window 且可能误导模型。

## Goals and Non-Goals

### Goals

- 在所有失败场景的 `ToolMessage` 上设置 `status="error"`
- 持久化 `status` 字段到 `MessageRow`，hydration 时恢复
- 扩展现有 sanitization 机制，在下一 turn 自动移除失败的 tool exchange
- Anthropic 模型通过 `is_error=True` 收到明确错误信号

### Non-Goals

- 不修改 `ToolResult` 数据结构（metadata 仍是失败信息的 source of truth）
- 不修改 OpenAI adapter 的行为（它已正确 strip status 字段）
- 不在 UI 层面做额外展示（status 信息已通过现有 display 机制展示）
- 不做跨 session 的 status 迁移（旧 session 的 ToolMessage 保持 status="success"）

## Architecture

### 数据流

```
ToolResult.metadata (source of truth)
        │
        ▼
  _tool_result_ok() ─── 判断 ok/failed
        │
        ▼
  ToolMessage(status="error"|"success")  ← 新增
        │
        ├──▶ Anthropic adapter: status=="error" → is_error=True
        ├──▶ OpenAI adapter: status 被 strip（现有行为）
        │
        ├──▶ Persistence: MessageRow.status  ← 新增字段
        │
        └──▶ Next-turn sanitization: 移除 status=="error" 的 tool exchange
```

### Sanitization 流程

```
messages (下一 turn 输入)
    │
    ▼
sanitize_failed_tool_exchanges()
    │  1. 扫描 ToolMessage(status="error")，收集 tool_call_ids
    │  2. 从 AIMessage 的所有 tool-call 表示中移除匹配项
    │  3. 跳过匹配的 ToolMessage
    │  4. 若 AIMessage 移除所有 tool_calls 后为空，丢弃该 AIMessage
    │  5. preserve_latest=True 时保留当前 turn 的失败交换
    ▼
sanitized messages
```

`AIMessage` 里 tool call 可能出现在三处，sanitization 必须同时处理，避免删掉 `ToolMessage` 后仍留下悬空的 assistant tool use：

- `AIMessage.tool_calls`
- Anthropic-style structured content block：`{"type": "tool_use", "id": ...}`
- OpenAI-style `AIMessage.additional_kwargs["tool_calls"]`

## Data Model

### MessageRow 新增字段

```
MessageRow
├── id: int | None
├── session_id: str
├── role: str
├── content: str
├── content_format: str
├── tool_calls: list[dict] | None
├── tool_call_id: str | None
├── status: str | None = None        ← 新增，"success" | "error"，None 视为 "success"
└── created_at: str
```

- `status` 可选字段，`None` 等价于 `"success"`，保证向后兼容
- 持久化时仅当 `status == "error"` 才写入，减少存储和迁移负担
- `status` 只对 `role == "tool"` 有语义；其他 role 即使 JSONL 中意外出现该字段也忽略

### row_fingerprint 扩展

```python
payload = {
    "role": row.role,
    "content": row.content,
    "content_format": row.content_format,
    "tool_calls": row.tool_calls or [],
    "tool_call_id": row.tool_call_id or "",
    "status": row.status or "success",    # 新增
}
```

## API Contract

### ToolMessage 创建点改动

所有 11 个创建点，按失败语义分类：

| # | 位置 | 场景 | status |
|---|------|------|--------|
| 1 | `executor.py:222` | 正常执行结果 | `ok` 变量决定 |
| 2 | `executor.py:319` | denied | `"error"` |
| 3 | `guards.py:57` | runtime guard blocked | `"error"` |
| 4 | `guards.py:88` | repetitive tools skip/terminate | `"error"` |
| 5 | `helpers.py:77` | deduped read | `"success"`（非失败） |
| 6 | `helpers.py:141` | blocked after barrier | `"error"` |
| 7 | `subagent.py:232` | subagent repetitive guard | `"error"` |
| 8 | `subagent.py:283` | subagent tool result | `result_ok()` 决定 |
| 9 | `subagent.py:295` | subagent denied | `"error"` |
| 10 | `streaming.py:163` | missing tool result repair | `"error"` |
| 11 | `message_rows.py:83` | hydration from persisted rows | 从 `row.status` 恢复 |

所有 `status` 值通过 shared helper 归一化：

```python
def message_status(value: object) -> Literal["success", "error"]:
    return "error" if value == "error" else "success"
```

用途：

- ToolMessage 创建时避免重复写字符串判断
- hydration 时防御坏 JSONL，非法值 fallback 为 `"success"`
- persistence 时只写入归一化后的 `"error"`，不写 `"success"`

### 新增函数

```python
# voidx/agent/tool_exchange_sanitizer.py

def sanitize_failed_tool_exchanges(
    messages: list[BaseMessage],
    *,
    preserve_latest: bool = False,
) -> list[BaseMessage]:
    """Remove failed tool exchanges (status='error') from message history.

    Pattern mirrors sanitize_todo_replay_messages():
    1. Collect tool_call_ids from ToolMessage(status='error')
    2. Strip matching tool calls from AIMessage.tool_calls, content blocks, and additional_kwargs
    3. Skip matching ToolMessages
    4. Drop empty AIMessages
    """
```

实现建议独立放在 `voidx/agent/tool_exchange_sanitizer.py`：

- 语义不属于 todo/runtime-only 工具，避免继续膨胀 `todo_state.py`
- 但可以复用 `sanitize_todo_replay_messages()` 的实现模式和测试形状
- 后续若有其他 semantic replay 清理，也能放在同一模块中

### 调用点

在 `_call_llm` 中，与现有 `sanitize_todo_replay_messages` 并列调用：

```python
messages = sanitize_todo_replay_messages(messages, preserve_latest_tool_exchange=True)
messages = sanitize_failed_tool_exchanges(messages, preserve_latest=True)
```

同时在 `streaming._sanitize_messages_for_replay()` 中作为最终 provider-call 前兜底：

```python
sanitized = sanitize_todo_replay_messages(
    sanitized,
    preserve_latest_tool_exchange=True,
)
sanitized = sanitize_failed_tool_exchanges(sanitized, preserve_latest=True)
return _repair_tool_result_adjacency(sanitized)
```

原因：

- 主 agent 走 `_call_llm`，但 subagent 内部循环直接维护自己的 `messages` 并调用 `stream_llm()`
- `streaming._sanitize_messages_for_replay()` 是所有 provider 请求前的共同 replay 清理点
- `_repair_tool_result_adjacency()` 必须在失败清理之后运行，负责补齐仍然缺失的 tool result
- repair 补出的 `ToolMessage(content="Tool result unavailable...")` 也必须设置 `status="error"`

## Persistence

### save_message 改动

`turn_runner.py` 中保存 ToolMessage 时提取 status，并同步写入 session message cache：

```python
elif isinstance(msg, ToolMessage):
    status = message_status(getattr(msg, "status", None))
    row_id = await save_message(MessageRow(
        session_id=host._session.id,
        role="tool",
        content=str(msg.content),
        tool_call_id=getattr(msg, "tool_call_id", None),
        status=status,
        created_at=memory_now(),
    ))
```

`host._session_msg_cache.append(MessageRow(...))` 也必须包含相同的 `status`，否则同进程增量 hydration 会继续从缓存恢复成默认 `"success"`。

### MessageRow / JSONL 改动

`memory/session.py` 是实际 JSONL 持久化边界，需要改三处：

```python
class MessageRow(BaseModel):
    ...
    status: str | None = None
```

```python
if message_status(msg.status) == "error":
    record["status"] = "error"
```

```python
status = record.get("status") if isinstance(record.get("status"), str) else None
...
MessageRow(..., status=status)
```

`append_session_record()` 本身不需要改；它只负责写入调用方传入的 dict。

### message_from_row 改动

```python
if row.role == "tool":
    return ToolMessage(
        content=row.content,
        tool_call_id=row.tool_call_id or "",
        status=message_status(row.status),
        id=msg_id,
    )
```

### JSONL 兼容性

`append_session_record` 写入时，`status` 仅在非 None 时包含。旧记录无 `status` 字段 → hydration 时 `MessageRow.status=None` → `message_from_row` 视为 `"success"`。无需迁移。

新记录仅当失败时写入 `"status":"error"`；成功记录仍保持旧 shape，减少存储 churn。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 旧 session 无 status 字段 | `None` 视为 `"success"`，向后兼容 |
| status 字段值非法 | `message_status()` fallback 为 `"success"` |
| sanitization 移除所有消息 | 不应发生（system/user 消息不受影响），但即使发生也不 crash |
| 失败 ToolMessage 无 tool_call_id | 不参与 exchange 移除，但保留原消息，避免误删 |
| AIMessage 同时包含失败和成功 tool calls | 只移除失败 call，保留成功 call 及其 ToolMessage |

## Test Plan

- `test_tool_result_error_sets_tool_message_status_error`：`metadata={"error": True}` 的普通工具结果生成 `ToolMessage.status == "error"`
- `test_tool_result_exit_code_nonzero_sets_status_error`：`metadata={"exit_code": 1}` 生成 error status
- `test_denied_and_blocked_tool_messages_set_status_error`：permission denied、runtime guard blocked、barrier blocked 都标记 error
- `test_deduped_read_tool_message_remains_success`：重复 read skip 仍为 success
- `test_missing_tool_result_repair_sets_status_error`：`_repair_tool_result_adjacency()` 补出的 ToolMessage 是 error
- `test_message_row_round_trips_tool_status`：JSONL save/load 后 `MessageRow.status` 和 hydrated `ToolMessage.status` 都恢复
- `test_row_fingerprint_includes_status`：同一 row 只有 status 不同也产生不同 fingerprint，避免增量 cache 误复用
- `test_sanitize_failed_tool_exchanges_removes_failed_exchange`：移除失败 AI tool call + ToolMessage
- `test_sanitize_failed_tool_exchanges_preserves_success_sibling`：同一个 AIMessage 内失败/成功混合时只删失败项
- `test_sanitize_failed_tool_exchanges_cleans_content_and_additional_kwargs`：同时清理 structured content block 和 `additional_kwargs["tool_calls"]`
- `test_sanitize_failed_tool_exchanges_preserve_latest`：尾部当前 turn 的失败 exchange 在 `preserve_latest=True` 时保留
- `test_streaming_sanitize_applies_failed_exchange_cleanup`：subagent/streaming 路径不经过 `_call_llm` 时也会在 provider-call 前清理旧失败 exchange

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| status 字段加在 MessageRow 上 | 单独的 error 表 | 最小改动，与现有 schema 一致 |
| sanitization 复用 todo 模式 | 在 compaction 中处理 | 逻辑更清晰，compaction 是压缩而非语义清理 |
| preserve_latest=True | 总是移除 | LLM 当前 turn 可能还需要参考刚发生的错误 |
| status=None 视为 success | 必填字段 | 向后兼容，旧数据无需迁移 |
| deduped read 标记为 success | 标记为 error | deduped read 不是失败，是优化跳过 |
| sanitizer 独立成 `tool_exchange_sanitizer.py` | 放进 `todo_state.py` | 语义不同，独立模块更容易测试和复用 |
| 在 `_call_llm` 和 `streaming` 双层调用 | 只在 `_call_llm` 调用 | streaming 是 provider-call 前共同兜底，可覆盖 subagent |
| repair 在失败清理之后运行 | repair 之后再清理 | repair 要补齐清理后仍缺失的合法 tool result；补出的缺失结果本身是 error |

## Open Questions

- [ ] 是否需要在 UI 层展示 status 信息（如错误标记）？当前不在 scope 内
- [ ] 是否要把 `message_status()` 放在 `message_rows.py`，还是放到新的 sanitizer/status helper 模块中？倾向放到新模块，避免 hydration 和 tool executor 各自复制
