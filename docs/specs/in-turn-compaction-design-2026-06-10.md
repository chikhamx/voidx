# Turn 内 Compaction 缺失修复 — 技术设计文档

## Context

voidx 在长对话中触发了压缩阈值却没执行压缩，而是等 LLM 三次报错 "context too long" 后退出。根因是 compaction 检查只在 turn 入口做一次，turn 内部的 LLM 循环不再检查。工具执行产生大量输出后 context 膨胀，LLM 调用必然失败，而 retry 逻辑不区分错误类型，盲目重试不可能成功。

此外，当前 compaction agent 的 `build_prompt` 对 head_messages 做了激进截断（每条消息 2000 字符、tool 输出 2000 字符、总 60k 字符上限），导致信息丢失严重。应该直接把完整的 head_messages 作为消息列表传给 LLM，让 LLM 自己总结，同时还能命中 prompt cache。

## Goals and Non-Goals

### Goals

- 每次 `_call_llm` 前检查 context 是否超阈值，超了直接触发自动压缩
- LLM 因 context overflow 报错时，自动触发 compaction 后重试
- compaction agent 直接接收完整 head_messages，不做截断，让 LLM 完整总结
- compaction agent 的消息结构与主 LLM 调用一致，复用 prompt cache
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
- compaction agent 用的是 `SystemMessage(COMPACTION_PROMPT) + HumanMessage(prompt)` 的结构，和主 LLM 调用的消息结构完全不同，无法复用缓存

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

无新增数据模型。复用现有 `CompactionService` 和 `_maybe_compact` 方法。

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

### 改动 4：新增 `_in_turn_compact` 方法

**文件**: `src/voidx/agent/graph/compaction.py`

这是 turn 内 compaction 的入口，和 turn 入口的 `_maybe_compact` 不同，它需要：
- 返回新的 messages 列表（而非 in-place 修改）
- 不弹窗询问（`ask=False`）
- 跟踪 turn 内 compaction 次数，防止无限循环

```python
async def _in_turn_compact(
    self: GraphCompactionHost,
    messages: list,
) -> list | None:
    """Compact within a turn. Returns new messages list or None if compaction failed."""
    self._in_turn_compaction_count = getattr(self, "_in_turn_compaction_count", 0) + 1
    if self._in_turn_compaction_count > 2:
        logger.warning("In-turn compaction limit reached (2), skipping")
        return None

    try:
        compacted, _ = await self._maybe_compact(messages, ask=False)
        if compacted is not None:
            return list(messages)  # messages 已被 _maybe_compact in-place 修改
        return None
    except Exception as e:
        logger.warning("In-turn compaction failed: %s", e)
        return None
```

### 改动 5：`_run_compaction_agent` 改为直接传完整消息

**文件**: `src/voidx/agent/graph/compaction.py`

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
    messages = [SystemMessage(content=COMPACTION_PROMPT)]

    # 如果有之前的摘要，作为上下文注入
    if previous_summary:
        messages.append(HumanMessage(content=(
            "Below is the previous anchored summary of earlier conversation. "
            "Preserve still-true details, remove stale details, and merge in new facts.\n\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>"
        )))
        messages.append(AIMessage(content="Understood. I will update the summary with the new conversation history."))

    # 直接传入完整的 head_messages，不截断
    messages.extend(head_messages)

    # 末尾加总结请求
    messages.append(HumanMessage(content=SUMMARY_REQUEST))

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

### 改动 6：`_call_llm` 返回值中包含 compaction 后的 messages

**文件**: `src/voidx/agent/graph/core.py`

当 `_call_llm` 内部触发了 compaction，需要通过返回值更新 LangGraph 的 state。因为 `AgentState.messages` 用 `add_messages` reducer，返回新的 messages 列表会触发 reducer 合并。

但 `add_messages` 的合并逻辑是追加/替换（按 message id），不是替换整个列表。compaction 需要删除旧消息 + 插入摘要，这和 `add_messages` 的语义不兼容。

**方案**: 在 `_call_llm` 返回时，如果发生了 compaction，返回一个特殊标记，让 `_finalize` 节点处理 messages 替换。

```python
# _call_llm 返回值
return {
    "messages": [...guidance_messages, assistant_msg],
    "step_count": step + 1,
    "convergence_forced": convergence_forced,
    "_compacted_messages": new_messages,  # 新增：compaction 后的完整 messages
}
```

```python
# _finalize 处理
async def _finalize(self, state: AgentState) -> dict:
    compacted = state.get("_compacted_messages")
    if compacted is not None:
        # 替换整个 messages 列表
        # 需要用 LangGraph 的消息替换机制
        ...
    # ... 原有逻辑 ...
```

**更简单的方案**: 不通过 LangGraph state 传递，而是用实例变量。`_call_llm` 内部 compaction 后，直接修改 `state["messages"]` 列表内容（in-place），然后构建 `llm_messages` 时用修改后的列表。LangGraph 的 `add_messages` reducer 在节点返回时会用返回的 messages 做合并，但 `state["messages"]` 的 in-place 修改在同一节点内是可见的。

验证：`_call_llm` 内部 `state["messages"]` 的 in-place 修改是否影响后续节点？答案是**不影响**，因为 LangGraph 在节点返回后用 reducer 处理返回值，`state["messages"]` 的 in-place 修改会被 reducer 的结果覆盖。

**最终方案**: `_call_llm` 返回值中包含 compaction 产生的摘要消息，通过 `add_messages` reducer 自然合并。同时用 `RemoveMessage` 删除被压缩的旧消息。

```python
from langchain_core.messages import RemoveMessage

# compaction 后，返回：
return {
    "messages": [
        # 删除被压缩的旧消息
        *[RemoveMessage(id=msg.id) for msg in compacted_msgs if hasattr(msg, 'id')],
        # 插入摘要
        SystemMessage(content=f"[Compacted summary]\n{summary}"),
        # 正常的 assistant 回复
        assistant_msg,
    ],
    ...
}
```

### 改动 7：`build_prompt` 和 `_join_with_char_budget` 保留但不再用于 compaction agent

`build_prompt` 和 `_join_with_char_budget` 保留用于 `fallback_summary`（compaction agent 失败时的降级路径），不再用于 `_run_compaction_agent`。

### 改动 8：turn 结束时重置 compaction 计数

**文件**: `src/voidx/agent/graph/turn_mixin.py`

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
| head_messages 太大导致 compaction agent 也 overflow | `_run_compaction_agent` 的 LLM 调用也会报错，走 `fallback_summary` 降级 |

### compaction agent 自身 overflow 的保护

当 head_messages 很大时，直接传给 LLM 可能也会 overflow。需要在 `_run_compaction_agent` 中加保护：

1. 估算 `messages`（SystemMessage + head_messages + SUMMARY_REQUEST）的 token 数
2. 如果超过 `context_limit`，对 head_messages 从头部截断（保留最近的，丢弃最早的）
3. 截断时保留完整的 turn 边界（不拆分单个 turn）

```python
async def _run_compaction_agent(self, head_messages, previous_summary):
    messages = _build_compaction_messages(head_messages, previous_summary)
    context_tokens = estimate_context_tokens(messages, self.config.model.model)

    # 如果 compaction agent 自身会 overflow，从头部截断 head_messages
    if context_tokens > self._compaction.context_limit:
        head_messages = self._compaction.truncate_head_to_budget(
            head_messages,
            budget=self._compaction.context_limit - 2000,  # 留给 system + request
        )
        messages = _build_compaction_messages(head_messages, previous_summary)

    assistant_msg = await stream_llm(self.model, messages, renderer, ...)
```

新增 `CompactionService.truncate_head_to_budget`：

```python
def truncate_head_to_budget(self, messages: list, budget: int) -> list:
    """Truncate head messages from the front to fit within token budget.
    Preserves turn boundaries."""
    turns = self._turns(messages)
    # 从最新的 turn 开始保留，直到超预算
    kept: list = []
    total = 0
    for turn in reversed(turns):
        turn_msgs = messages[turn.start:turn.end]
        size = estimate_context_tokens(turn_msgs)
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
| 用 `RemoveMessage` + 摘要消息更新 state | in-place 修改 state["messages"] | LangGraph 的 `add_messages` reducer 不感知 in-place 修改；`RemoveMessage` 是官方支持的删除机制 |
| compaction 重试上限 2 次 | 不设上限 | 防止 compaction 和 LLM 调用之间的无限循环 |
| 用实例变量跟踪 compaction 次数 | 用 state 字段 | 实例变量更简单，turn 结束时重置 |
| `SUMMARY_REQUEST` 作为独立常量 | 嵌入 COMPACTION_PROMPT | 分离指令和触发请求，更清晰；COMPACTION_PROMPT 是 system 级指令，SUMMARY_REQUEST 是用户级请求 |

## Open Questions

- [ ] `RemoveMessage` 是否被所有 LangGraph 版本支持？需要验证当前依赖版本。
- [ ] compaction agent 用完整 head_messages 时，prompt cache 命中率取决于 provider 实现。Anthropic 的 cache 需要 `cache_control` 标记，是否需要在 compaction messages 中加？
- [ ] `_in_turn_compact` 中 `messages` 的 in-place 修改和 `RemoveMessage` 返回值是否冲突？需要验证执行顺序。
