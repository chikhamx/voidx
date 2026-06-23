# LLM 请求/响应日志记录

> **Status: In Progress**

## 目标

每次 LLM 调用，将完整的请求上下文（messages）和响应（AIMessage）写入日志文件，便于开发者直接查看和调试。

## 拦截点

`src/voidx/agent/graph/core.py` 的 `_call_llm()` 方法（L619），所有 LLM 调用的唯一入口。

在 `await _stream_llm(...)` 调用（L745）成功返回后，记录请求和响应。

## 新增文件

### `src/voidx/llm/request_log.py`

独立的日志记录模块，提供 `log_llm_exchange()` 函数。

```python
def log_llm_exchange(
    messages: list[BaseMessage],
    response: AIMessage,
    *,
    model: str,
    provider: str,
    step: int,
    session_id: str | None = None,
) -> None:
```

**职责：**
1. 序列化 messages 和 response 为可读结构
2. 追加写入 JSONL 日志文件
3. 自动创建日志目录

**日志路径：** `~/.voidx/logs/llm_requests.jsonl`

**日志格式（每行一次请求）：**
```json
{
  "ts": "2026-06-12T10:30:00+08:00",
  "session_id": "abc123",
  "model": "gpt-4",
  "provider": "openai",
  "step": 3,
  "request": {
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "human", "content": "..."},
      {"role": "ai", "content": "...", "tool_calls": [...]}
    ]
  },
  "response": {
    "content": "...",
    "tool_calls": [...],
    "usage": {"input_tokens": 1200, "output_tokens": 300}
  }
}
```

**序列化规则：**
- `HumanMessage` → `{"role": "human", "content": ...}`
- `AIMessage` → `{"role": "ai", "content": ..., "tool_calls": [...]}`
- `SystemMessage` → `{"role": "system", "content": ...}`
- `ToolMessage` → `{"role": "tool", "content": ..., "tool_call_id": ...}`
- `usage_metadata` 从 response 中提取，放在 `response.usage`
- content 为列表时（多模态），转为字符串摘要

**错误处理：**
- 序列化失败时 `logger.warning` 并跳过，不中断主流程
- 文件写入失败时 `logger.warning` 并跳过

## 修改文件

### `src/voidx/agent/graph/core.py`

在 `_call_llm()` 中，`assistant_msg` 成功返回后（L746 附近），添加一行调用：

```python
from voidx.llm.request_log import log_llm_exchange

# ... existing code ...
assistant_msg = await _stream_llm(model_with_tools, llm_messages, renderer, resolve_protocol(self.config.model))
log_llm_exchange(
    llm_messages,
    assistant_msg,
    model=self.config.model.model,
    provider=self.config.model.provider,
    step=step,
    session_id=self._session.id if self._session else None,
)
# ... existing code continues ...
```

## 不做的事

- 不添加配置开关（始终记录，简单直接）
- 不记录流式中间 chunks（只记录最终合并的 AIMessage）
- 不记录 tool execution 的结果（只记录 LLM 请求/响应边界）
- 不使用 LangGraph Callback（拿不到完整 messages，且流式场景复杂）

## 测试

- `tests/test_llm/test_request_log.py`
- 测试序列化各种 message 类型
- 测试 JSONL 文件写入和追加
- 测试序列化失败时不抛异常
- 测试 content 为列表时的处理
