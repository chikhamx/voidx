# Turn 内 Compaction 缺失修复 — 技术设计文档

> **Status: Done**

## Context

voidx 在长对话中触发了压缩阈值却没执行压缩，而是等 LLM 三次报错 "context too long" 后退出。根因是 compaction 检查只在 turn 入口做一次，turn 内部的 LLM 循环不再检查。工具执行产生大量输出后 context 膨胀，LLM 调用必然失败，而 retry 逻辑不区分错误类型，盲目重试不可能成功。

此外，当前 compaction agent 的 `build_prompt` 对 head_messages 做了激进截断（每条消息 2000 字符、tool 输出 2000 字符、总 60k 字符上限），导致信息丢失严重。应该直接把完整的 head_messages 作为消息列表传给 LLM，让 LLM 自己总结，同时还能命中 prompt cache。

## Goals and Non-Goals

### Goals

- 每次 `_call_llm` 前检查 context 是否超阈值，超了直接触发自动压缩
- LLM 因 context overflow 报错时，自动触发 compaction 后重试
- compaction agent 优先接收完整 head_messages，不再通过 `build_prompt` 拼成单条 HumanMessage
- compaction agent 自身超窗时按完整 turn/tool 边界保留最近 head_messages，再降级到 fallback summary
- compaction 失败时优雅降级，不丢失对话

### Non-Goals

- 不改变 LangGraph 的拓扑结构（节点和边）
- 不引入新的 graph 节点
- 不改变 `select_details` 的 head/tail 分割逻辑

## 根因分析

### 问题一：compaction 只在 turn 入口检查

```
用户发消息 → _handle_turn()
               │
               ├── _maybe_compact()     ← ① 只在这里检查一次
               │   └── is_overflow()?   ← 用 estimate_context_tokens 估算
               │       └── 未超阈值 → 跳过
               │
               └── graph.ainvoke()      ← ② 进入 LangGraph 循环
                   │
                   ├── prepare → call_llm → execute_tools → call_llm → ...
                   │              │                          │
                   │              │  工具执行产生大量消息      │
                   │              │  context 不断膨胀         │
                   │              │                          │
                   │              └── LLM 报错: context too long
                   │                  ├── retry 1 (等2s)
                   │                  ├── retry 2 (等4s)
                   │                  └── 放弃, should_continue=False
                   │
                   └── 退出, 用户看到 "LLM call failed"
```

### 问题二：compaction agent 的截断导致信息丢失

当前 `build_prompt` 的截断策略：

```
head_messages (可能 120k+ tokens)
        │
        ▼ build_prompt() 三层截断
        │
        │  ① 每条 User 消息截断到 2000 字符
        │  ② 每条 AI 消息截断到 2000 字符
        │  ③ 每条 Tool 结果截断到 2000 字符
        │
        ▼
context_parts (截断后的文本片段列表)
        │
        ▼ _join_with_char_budget(parts, 60_000)
        │
        │  ④ 总字符数限制 60k，超出部分直接丢弃
        │     从前往后保留，最近的对话历史可能被丢弃
        │
        ▼
最终 prompt ≈ 60k 字符 ≈ 15k-20k tokens
```

问题：
- 截断丢掉了大量细节（文件内容、错误信息、工具输出）
- `_join_with_char_budget` 从前往后保留，最近的上下文反而可能被丢弃
- 把所有消息拼成一个大字符串塞进单个 HumanMessage，无法命中 prompt cache
- compaction agent 用的是 `SystemMessage(COMPACTION_PROMPT) + HumanMessage(prompt)` 的结构，和原始消息序列完全不同，provider 无法复用消息级缓存

### 问题三：LangGraph state 的 in-place 修改

`AgentState.messages` 用 `Annotated[list[BaseMessage], add_messages]` 管理。LangGraph 通过 `add_messages` reducer 追踪变化。`_maybe_compact` 中的 `messages.clear() + messages.extend(tail_msgs)` 是 in-place 修改，在 turn 入口（graph 外）没问题，但在 `_call_llm` 内部（graph 节点内）不会被 LangGraph 正确追踪。

## Architecture

### 改后流程

```
graph 循环:
  prepare → call_llm → execute_tools → call_llm → ...
                │                          │
                │  ① pre-flight 检查        │
                │  estimate_context_tokens  │
                │  超阈值? → compaction     │
                │  (返回新 messages 列表)    │
                │                          │
                │  ② LLM 调用              │
                │  成功 → 继续              │
                │  失败 → ③ 检查错误类型    │
                │    context overflow?      │
                │      → compaction + 重试  │
                │    其他错误?              │
                │      → 原有 retry 逻辑    │
```

### compaction agent 改后流程

```
head_messages (完整消息列表)
        │
        ▼ _run_compaction_agent 改造
        │
        │  不再调用 build_prompt 拼字符串
        │  直接用 head_messages 作为消息列表
        │  前面加 SystemMessage(COMPACTION_PROMPT)
        │  末尾加 HumanMessage("请总结以上对话")
        │
        ▼
messages = [
    SystemMessage(COMPACTION_PROMPT),
    ...head_messages,              ← 完整保留，不截断
    HumanMessage(SUMMARY_REQUEST), ← 触发总结
]
        │
        ▼ LLM 调用
        │
        │  优势：
        │  1. 完整信息，无截断丢失
        │  2. head_messages 和主 LLM 调用共享前缀 → prompt cache 命中
        │  3. 消息结构一致 → provider 的 cache 机制自然生效
        │
        ▼
summary (结构化摘要)
```

## Data Model

新增一个内部结果对象，专门描述“已完成一次压缩后应该如何更新 live context”。turn 入口现有 `_maybe_compact` 继续保留 mutation 契约；turn 内压缩使用新的非原地 API，避免 LangGraph state reducer 看不到 mutation。

**文件**: `src/voidx/agent/graph/compaction_coordinator.py`

```python
@dataclass(frozen=True)
class CompactionResult:
    summary: str
    removed_messages: list[BaseMessage]
    live_messages: list[BaseMessage]
    tail_id: str | None
    fallback: bool = False
```

语义：
- `removed_messages`: 被摘要覆盖的旧消息
- `live_messages`: 摘要之后仍保留在 graph state 中的 tail messages；turn 内模式额外保留当前 runtime context 前缀，并插入临时摘要 system message
- `summary`: 写入 system prompt 的长摘要，也同步更新 `_pending_summary` / `_compaction_summary`
- `tail_id`: 持久化删除边界，沿用现有 `select_details` 的 tail anchor
- `fallback`: compaction agent 失败后是否使用了 `fallback_summary`

## API Contract

### 改动 1：`_call_llm` 入口加 pre-flight compaction 检查

**文件**: `src/voidx/agent/graph/core.py`
**位置**: `_call_llm` 方法，LLM 调用前

**Before:**

```python
async def _call_llm(self, state: AgentState) -> dict:
    # ... 构建 llm_messages ...
    context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
    self._usage_stats.update_context(context_tokens)
    # ... 直接调用 LLM ...
```

**After:**

```python
async def _call_llm(self, state: AgentState) -> dict:
    # ... 构建 llm_messages ...
    context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
    self._usage_stats.update_context(context_tokens)

    # Pre-flight: 如果 context 超阈值，先压缩
    if self._compaction.is_overflow({"total": context_tokens}):
        new_messages = await self._in_turn_compact(state["messages"])
        if new_messages is not None:
            state["messages"] = new_messages
            # 重建 llm_messages
            guidance_messages = self._drain_pending_guidance()
            base_messages = [*new_messages, *guidance_messages]
            convergence_messages, convergence_forced = build_convergence_messages(...)
            llm_messages = [*base_messages, *convergence_messages]
            context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
            self._usage_stats.update_context(context_tokens)

    # ... 调用 LLM ...
```

### 改动 2：LLM 报错时识别 context overflow 并触发 compaction

**文件**: `src/voidx/agent/graph/core.py`
**位置**: `_call_llm` 的 retry 循环

**Before:**

```python
except Exception as e:
    if attempt < max_retries:
        delay = (attempt + 1) * 2
        await asyncio.sleep(delay)
    else:
        ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
        return {
            "messages": [AIMessage(content=f"LLM call failed: {e}")],
            "step_count": step,
            "should_continue": False,
        }
```

**After:**

```python
except Exception as e:
    if _is_context_overflow_error(e):
        new_messages = await self._in_turn_compact(state["messages"])
        if new_messages is not None:
            state["messages"] = new_messages
            # 重建 llm_messages 并重试
            # ... 重建逻辑 ...
            continue
    if attempt < max_retries:
        delay = (attempt + 1) * 2
        await asyncio.sleep(delay)
    else:
        ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
        return {
            "messages": [AIMessage(content=f"LLM call failed: {e}")],
            "step_count": step,
            "should_continue": False,
        }
```

### 改动 3：新增 `_is_context_overflow_error` 辅助函数

**文件**: `src/voidx/agent/graph/core.py`

```python
def _is_context_overflow_error(exc: Exception) -> bool:
    """Check if an LLM error is caused by context length overflow."""
    msg = str(exc).lower()
    return any(
        pattern in msg
        for pattern in (
            "context_length_exceeded",
            "context length",
            "too many tokens",
            "maximum context",
            "token limit",
            "input is too long",
            "request too large",
            "context window",
        )
    )
```

### 改动 4：新增非原地 compaction API 和 `_in_turn_compact` 方法

**文件**: `src/voidx/agent/graph/compaction_coordinator.py`

新增 coordinator 方法：

```python
async def compact_for_live_state(
    self,
    messages: list[BaseMessage],
    *,
    force: bool = False,
    include_summary_message: bool = False,
) -> CompactionResult | None:
    """Compact without mutating the caller's message list."""
```

`maybe_compact()` 改为复用这个方法：拿到 `CompactionResult` 后再执行现有的 `messages.clear(); messages.extend(result.live_messages)`，从而保持 turn 入口行为兼容。

**文件**: `src/voidx/agent/graph/compaction.py`

这是 turn 内 compaction 的入口，和 turn 入口的 `_maybe_compact` 不同，它需要：
- 返回新的 messages 列表（而非 in-place 修改）
- 不弹窗询问（`ask=False`）
- 跟踪 turn 内 compaction 次数，防止无限循环

```python
async def _in_turn_compact(
    self: GraphCompactionHost,
    messages: list,
) -> CompactionResult | None:
    """Compact within a turn without mutating LangGraph state."""
    self._in_turn_compaction_count = getattr(self, "_in_turn_compaction_count", 0) + 1
    if self._in_turn_compaction_count > 2:
        logger.warning("In-turn compaction limit reached (2), skipping")
        return None

    try:
        return await self._compaction_component().compact_for_live_state(
            messages,
            force=True,
            include_summary_message=True,
        )
    except Exception as e:
        logger.warning("In-turn compaction failed: %s", e)
        return None
```

### 改动 5：`_run_compaction_agent` 改为直接传完整消息

**文件**: `src/voidx/agent/graph/compaction_coordinator.py`

**Before:**

```python
async def _run_compaction_agent(self, head_messages, previous_summary):
    prompt = self._compaction.build_prompt(head_messages, previous_summary)
    messages = [SystemMessage(content=COMPACTION_PROMPT)]
    messages.append(HumanMessage(content=prompt))
    assistant_msg = await stream_llm(self.model, messages, renderer, ...)
```

**After:**

```python
async def _run_compaction_agent(self, head_messages, previous_summary):
    messages = _build_compaction_messages(
        head_messages,
        previous_summary,
        COMPACTION_PROMPT,
    )

    assistant_msg = await stream_llm(self.model, messages, renderer, ...)
```

其中 `SUMMARY_REQUEST` 是一个新的常量：

```python
SUMMARY_REQUEST = (
    "Summarize the conversation above into the structured format specified in your instructions. "
    "Focus on durable facts, decisions, constraints, open work, and final tool outcomes. "
    "Do not narrate step-by-step; extract what matters for continuing the task."
)
```

### 改动 6：`_call_llm` 用 `REMOVE_ALL_MESSAGES` 重建 messages state

**文件**: `src/voidx/agent/graph/core.py`

当 `_call_llm` 内部触发了 compaction，需要通过返回值更新 LangGraph 的 state。因为 `AgentState.messages` 用 `add_messages` reducer，普通返回新的 messages 列表只会追加/按 id 替换，不会替换整个列表。

**最终方案**: 使用当前 LangGraph 支持的 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 清空 messages state，然后返回重建后的 live messages 与本次 assistant 回复。这样不依赖旧消息是否有稳定 id，也不会被未知 id 删除异常卡住。

```python
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

# compaction 后，返回：
return {
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *compaction_result.live_messages,
        *guidance_messages,
        assistant_msg,
    ],
    ...
}
```

注意：当前 LangGraph 拓扑中 `prepare` 只在 turn 开始执行一次；如果 turn 内压缩后只设置 `_pending_summary`，本次 retry 看不到摘要。因此 `CompactionResult.live_messages` 会包含一个临时 `SystemMessage("## Long Summary\n...")`，让当前 turn 立即获得摘要上下文。下一 turn 开始时，现有 `_prepare_with_stream()` 会把 `_compaction_summary` 编译进正式 runtime system prompt，并剔除历史 system messages。

### 改动 7：`build_prompt` 和 `_join_with_char_budget` 保留但不再用于 compaction agent

`build_prompt` 和 `_join_with_char_budget` 保留用于 `fallback_summary`（compaction agent 失败时的降级路径），不再用于 `_run_compaction_agent`。

### 改动 8：turn 结束时重置 compaction 计数

**文件**: `src/voidx/agent/graph/turn_runner.py`

```python
# _handle_turn 结束时
self._in_turn_compaction_count = 0
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| Pre-flight compaction 失败 | 记录日志，继续调用 LLM（可能成功，也可能失败后走 overflow 重试） |
| Overflow compaction 失败 | 走正常 retry 逻辑，最终 3 次失败后退出 |
| Compaction 成功但 LLM 仍然 overflow | compaction 次数 +1，下次 `_call_llm` 再尝试（最多 2 次） |
| Compaction 成功但 LLM 因其他原因失败 | 走正常 retry 逻辑 |
| `_is_context_overflow_error` 误判 | 最坏情况是多做一次不必要的 compaction，不影响正确性 |
| head_messages 太大导致 compaction agent 也 overflow | 先按完整 turn/tool 边界截断 head_messages；仍失败时走 `fallback_summary` 降级 |

### compaction agent 自身 overflow 的保护

当 head_messages 很大时，直接传给 LLM 可能也会 overflow。需要在 `_run_compaction_agent` 中加保护：

1. 估算 `messages`（SystemMessage + head_messages + SUMMARY_REQUEST）的 token 数
2. 如果超过 `context_limit`，对 head_messages 从头部截断（保留最近的，丢弃最早的）
3. 截断时保留完整的 turn 边界（不拆分单个 turn）

```python
async def _run_compaction_agent(self, head_messages, previous_summary):
    messages = _build_compaction_messages(
        head_messages,
        previous_summary,
        COMPACTION_PROMPT,
    )
    context_tokens = estimate_context_tokens(messages, self.config.model.model)

    # 如果 compaction agent 自身会 overflow，从头部截断 head_messages
    if context_tokens > self._compaction.context_limit:
        budget = (
            self._compaction.context_limit
            - self._compaction.output_token_max
            - COMPACTION_PROMPT_HEADROOM
        )
        head_messages = self._compaction.truncate_head_to_budget(
            head_messages,
            budget=budget,
            model=self.config.model.model,
        )
        messages = _build_compaction_messages(
            head_messages,
            previous_summary,
            COMPACTION_PROMPT,
        )

    assistant_msg = await stream_llm(self.model, messages, renderer, ...)
```

新增 `CompactionService.truncate_head_to_budget`：

```python
def truncate_head_to_budget(self, messages: list, *, budget: int, model: str) -> list:
    """Truncate head messages from the front to fit within token budget.
    Preserves turn boundaries."""
    turns = self._turns(messages)
    # 从最新的 turn 开始保留，直到超预算
    kept: list = []
    total = 0
    for turn in reversed(turns):
        turn_msgs = messages[turn.start:turn.end]
        size = estimate_context_tokens(turn_msgs, model)
        if total + size > budget:
            break
        kept = turn_msgs + kept
        total += size
    return kept
```

注意：这里从**最新的 turn 开始保留**（reversed），和 `_join_with_char_budget` 从前往后保留不同。compaction 时最近的上下文更重要。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 直接传完整 head_messages 给 LLM | 保留 build_prompt 截断逻辑 | 完整信息让 LLM 总结更准确；消息结构一致可命中 prompt cache；截断逻辑仍保留给 fallback |
| 从最新 turn 开始保留（overflow 保护） | 从最早 turn 开始保留 | compaction 时最近的上下文更重要，早期对话可以丢弃 |
| 用 `REMOVE_ALL_MESSAGES` + 重建 live messages 更新 state | 逐条 `RemoveMessage(id=...)` 或 in-place 修改 | 不依赖旧消息 id；LangGraph 的 `add_messages` reducer 不感知 in-place 修改 |
| compaction 重试上限 2 次 | 不设上限 | 防止 compaction 和 LLM 调用之间的无限循环 |
| 用实例变量跟踪 compaction 次数 | 用 state 字段 | 实例变量更简单，turn 结束时重置 |
| `SUMMARY_REQUEST` 作为独立常量 | 嵌入 COMPACTION_PROMPT | 分离指令和触发请求，更清晰；COMPACTION_PROMPT 是 system 级指令，SUMMARY_REQUEST 是用户级请求 |

## Open Questions

- [x] `RemoveMessage` 是否被所有 LangGraph 版本支持？当前依赖已支持 `RemoveMessage` 与 `REMOVE_ALL_MESSAGES`。
- [ ] compaction agent 用完整 head_messages 时，prompt cache 命中率取决于 provider 实现。Anthropic 的 cache 需要 `cache_control` 标记，是否需要在 compaction messages 中加？
- [x] `_in_turn_compact` 中 `messages` 的 in-place 修改和 `RemoveMessage` 返回值是否冲突？最终方案不在 turn 内做 in-place 修改，因此无冲突。
