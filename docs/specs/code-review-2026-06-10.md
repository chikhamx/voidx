# Code Review: In-Turn Compaction & Modularity Remediation

> **Review Date**: 2026-06-10
> **Commit Range**: `a741260..cf11a10`
> **Reviewer**: voidx automated review
> **Verdict**: NEEDS_CHANGE

---

## Scope

本次审查覆盖 4 个主要改动区域：

| 区域 | 关键文件 | 变更规模 |
|------|---------|---------|
| In-turn compaction | `compaction_coordinator.py`, `compaction.py`, `core.py`, `contracts.py` | ~800 行新增/修改 |
| 模块化重构 | `turn_runner.py`, `tool_executor.py`, `host.py`, `session_runtime.py`, `render_*.py`, `bus.py`, `consumers.py` | ~2000 行拆分 |
| Provider 修复 | `provider.py` | ~350 行修改 |
| 新工具 | `apply_patch.py`, `file_state.py` | ~450 行新增 |

---

## High Severity

### H1. 重试循环缺少总迭代上限 — 可能无限循环

**文件**: `src/voidx/agent/graph/core.py:693-724`

```python
max_retries = 2
failed_attempts = 0
while True:
    try:
        assistant_msg = await _stream_llm(...)
        break
    except Exception as e:
        if _is_context_overflow_error(e):
            result = await self._in_turn_compact(state_messages)
            if result is not None:
                compaction_happened = True
                state_messages = list(result.live_messages)
                llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)
                context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                self._usage_stats.update_context(context_tokens)
                await save_context_frame(...)
                continue  # ← failed_attempts 不递增
        if failed_attempts < max_retries:
            failed_attempts += 1
            delay = failed_attempts * 2
            self._ui.ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
            await asyncio.sleep(delay)
        else:
            self._ui.ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
            failure_msg = AIMessage(content=f"LLM call failed: {e}")
            return {
                "messages": replacement_messages(failure_msg),
                "step_count": step,
                "should_continue": False,
            }
```

**问题**: 当 `_is_context_overflow_error` 触发 compaction 后 `continue`，`failed_attempts` 不递增。`_in_turn_compact` 有 `count > 2` 的上限（`compaction.py:49`），但即使 compaction 返回 `None`（达到上限），`continue` 仍会回到循环顶部重新调用 LLM。如果 LLM 持续返回 overflow 错误且 compaction 已无法再压缩，循环将无限执行。

**复现场景**:

```
iteration 1: LLM → overflow → compaction(count=1) → continue
iteration 2: LLM → overflow → compaction(count=2) → continue
iteration 3: LLM → overflow → compaction(count=3) → returns None → continue
iteration 4: LLM → overflow → compaction(count=3) → returns None → continue
... 无限循环
```

**建议修复**:

```python
max_retries = 2
failed_attempts = 0
total_attempts = 0
while total_attempts < max_retries + 6:  # 安全上限
    total_attempts += 1
    try:
        assistant_msg = await _stream_llm(...)
        break
    except Exception as e:
        if _is_context_overflow_error(e):
            result = await self._in_turn_compact(state_messages)
            if result is not None:
                compaction_happened = True
                state_messages = list(result.live_messages)
                llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(state_messages)
                context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                self._usage_stats.update_context(context_tokens)
                await save_context_frame(...)
                continue
            # compaction 无法再压缩，视为失败
            failed_attempts += 1
            if failed_attempts >= max_retries:
                break
            await asyncio.sleep(failed_attempts * 2)
            continue
        failed_attempts += 1
        if failed_attempts >= max_retries:
            break
        await asyncio.sleep(failed_attempts * 2)
```

---

### H2. Summary SystemMessage 缺少稳定 id

**文件**: `src/voidx/agent/graph/compaction_coordinator.py:461`

```python
def _live_messages(
    runtime_prefix: list[BaseMessage],
    semantic_tail: list[BaseMessage],
    summary: str,
    *,
    include_summary_message: bool,
) -> list[BaseMessage]:
    if not include_summary_message:
        return [*runtime_prefix, *semantic_tail]
    summary_message = SystemMessage(content=f"{IN_TURN_SUMMARY_PREFIX}{summary}")
    return [*runtime_prefix, summary_message, *semantic_tail]
```

**问题**: `SystemMessage` 没有设置 `id`。在 `replacement_messages` 中，当 `compaction_happened=True` 时，会先发 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 清空所有消息，再添加 `state_messages`。由于 `REMOVE_ALL_MESSAGES` 先清空了状态，首次不会出问题。

但如果在重试循环中发生两次 compaction（H1 的场景），第二次 `replacement_messages` 会再次执行 `RemoveMessage(id=REMOVE_ALL_MESSAGES) + *state_messages`。此时 `state_messages` 中的 summary message 仍然没有 id，LangGraph 的 `add_messages` reducer 会将其视为新消息。虽然 `REMOVE_ALL_MESSAGES` 确保了正确性（每次都先清空），但缺少 id 使得消息去重不可靠，且与 LangGraph 的最佳实践不一致。

**建议修复**:

```python
summary_message = SystemMessage(
    content=f"{IN_TURN_SUMMARY_PREFIX}{summary}",
    id="in-turn-compaction-summary",
)
```

---

### H3. Compaction ValueError 浪费重试次数

**文件**: `src/voidx/agent/graph/compaction_coordinator.py:357-374`

```python
if context_tokens > host._compaction.context_limit:
    budget = max(
        0,
        host._compaction.context_limit
        - host._compaction.output_token_max
        - COMPACTION_PROMPT_HEADROOM,
    )
    head_messages = host._compaction.truncate_head_to_budget(
        head_messages,
        budget=budget,
        model=host.config.model.model,
    )
    if not head_messages:
        raise ValueError("compaction input exceeds context budget")
    messages = _build_compaction_messages(head_messages, previous_summary, COMPACTION_PROMPT)
    context_tokens = estimate_context_tokens(messages, host.config.model.model)
    if context_tokens > host._compaction.context_limit:
        raise ValueError("compaction input exceeds context budget")
```

**问题**: `ValueError` 被外层 retry loop（line 174 `except Exception as e`）捕获，会用相同的截断后输入重试。由于截断逻辑是确定性的，相同输入必然产生相同结果，重试纯粹浪费时间。

**建议修复**: 在 retry loop 中单独 catch `ValueError` 并直接 break：

```python
for attempt in range(1, COMPACTION_MAX_RETRIES + 2):
    try:
        summary = await run_agent(head_msgs, previous_summary)
        ...
    except ValueError:
        last_error = e
        break  # 不重试，直接走 fallback
    except Exception as e:
        last_error = e
        ...
```

---

## Medium Severity

### M1. `_is_context_overflow_error` 模式过宽

**文件**: `src/voidx/agent/graph/core.py:102-116`

```python
def _is_context_overflow_error(exc: Exception) -> bool:
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

**问题**: `"request too large"` 可能匹配 HTTP 413 proxy 错误（如上传大图片），`"context window"` 可能出现在无关错误消息中。误判会触发不必要的 compaction。

**建议**: 收窄匹配模式，优先匹配 provider 特有的错误标识：

```python
_OVERFLOW_PATTERNS = (
    "context_length_exceeded",   # OpenAI
    "prompt is too long",        # Anthropic
    "too many tokens",           # 通用
    "maximum context length",    # OpenAI
    "input is too long",         # OpenAI
    "token limit exceeded",      # 通用
)
```

移除 `"request too large"` 和 `"context window"` 这两个过于宽泛的模式。

---

### M2. apply_patch 拒绝合法的空上下文行

**文件**: `src/voidx/tools/apply_patch.py:285-289`

```python
if not current:
    raise ValueError(f"Malformed hunk line in {file_patch.display_path}: empty unprefixed line")
```

**问题**: 标准 unified diff 中，空上下文行有时以真正的空行（无空格前缀）出现。`git apply` 对此容错处理，但此解析器会拒绝。

**建议修复**:

```python
if not current:
    # 容错：将空行视为空上下文行
    hunk_lines.append(_HunkLine(kind=" ", text=""))
    i += 1
    continue
```

---

### M3. apply_patch 多文件写入无原子性保证

**文件**: `src/voidx/tools/apply_patch.py:134-149`

```python
if not inp.dry_run:
    written: list[_PatchPlan] = []
    try:
        for plan in plans:
            if plan.status == "delete":
                plan.path.unlink()
            else:
                plan.path.parent.mkdir(parents=True, exist_ok=True)
                plan.path.write_text(plan.new_content, encoding="utf-8")
            written.append(plan)
    except Exception as exc:
        _restore_written_plans(written)
        return ToolResult(
            output=f"Patch write failed and rollback was attempted: {exc}",
            metadata={"error": True},
        )
```

**问题**: 如果 5 个文件 patch 中第 3 个写入失败，文件 1-2 已写入新内容，文件 4-5 仍为旧内容。rollback 恢复文件 1-2 到原始状态，但 patch 整体处于不一致状态（部分新、部分旧）。

**建议**: 这是文件级 patch 的固有限制。短期建议在返回的错误信息中明确说明部分写入已发生，长期可考虑先写临时文件再 rename。

---

## Low Severity

### L1. Header 空字符串值不等于移除

**文件**: `src/voidx/llm/provider.py:37-39`

```python
def _strip_stainless_headers() -> dict[str, str]:
    return {k: "" for k in _STAINLESS_HEADERS_TO_STRIP} | {"User-Agent": "voidx/1.0"}
```

**问题**: 设置 header 为空字符串 (`""`) 不等于移除 header。某些 HTTP 客户端/服务端会把空值 header 当作 `x-stainless-lang: ` 发出去，而非省略该 header。

**建议**: 确认 httpx/openai SDK 对空字符串 header 的处理行为，或改用 `None` 值（如果 SDK 支持）。

---

### L2. Compaction agent 传入原始消息含非文本内容

**文件**: `src/voidx/agent/graph/compaction_coordinator.py:432`

```python
def _build_compaction_messages(
    head_messages: list[BaseMessage],
    previous_summary: str | None,
    system_prompt: str,
) -> list[BaseMessage]:
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    ...
    messages.extend(head_messages)  # ← 直接传入原始消息
    messages.append(HumanMessage(content=SUMMARY_REQUEST))
    return messages
```

**问题**: AIMessage 中的 `tool_calls` 大参数 dict、图片等非文本内容会全部传给 compaction LLM，浪费 token 或导致不支持的内容类型错误。

**建议**: 考虑在传入前剥离 `tool_calls` 和媒体内容，只保留文本摘要。

---

### L3. `truncate_head_to_budget` 的 O(n²) prepend 模式

**文件**: `src/voidx/llm/compaction.py:282-289`

```python
for turn in reversed(turns):
    turn_msgs = messages[turn.start:turn.end]
    size = estimate_context_tokens(turn_msgs, model)
    if total + size > budget:
        break
    kept = [*turn_msgs, *kept]  # ← 每次 prepend，O(n²)
    total += size
return kept
```

**问题**: 每次 `[*turn_msgs, *kept]` 都创建新列表，对于大量 turns 是 O(n²)。

**建议**: 收集 (start, end) 范围，最后一次性切片：

```python
ranges = []
for turn in reversed(turns):
    turn_msgs = messages[turn.start:turn.end]
    size = estimate_context_tokens(turn_msgs, model)
    if total + size > budget:
        break
    ranges.append((turn.start, turn.end))
    total += size
return [m for s, e in reversed(ranges) for m in messages[s:e]]
```

---

## Passed Areas

| 区域 | 判定 | 说明 |
|------|------|------|
| 模块化重构 | ✅ PASS | `turn_mixin→turn_runner`, `tool_execution→tool_executor`, `renderer→render_*.py`, `events/__init__→bus+consumers` 等拆分均为干净的提取，逻辑无变化 |
| LSP 改动 | ✅ PASS | 仅更新 `__init__.py` 导出 |
| DeepSeek 协议 | ✅ PASS | `DeepSeekChatOpenAI` 的 streaming reasoning_content 保留逻辑正确，provider-specific reasoning kwargs 映射完整 |
| `_in_turn_compaction_count` 重置 | ✅ PASS | 在 `turn_runner.py:387` 的 finally 块中正确重置 |
| `RemoveMessage(REMOVE_ALL_MESSAGES)` 模式 | ✅ PASS | 先清空再添加的模式在 LangGraph `add_messages` reducer 下正确 |
| `_runtime_prefix` 过滤 | ✅ PASS | 正确跳过已有的 `IN_TURN_SUMMARY_PREFIX` SystemMessage，避免重复 |
| `apply_patch` 安全性 | ✅ PASS | `resolve_safe` 正确阻止路径穿越，binary patch / rename 被拒绝，staleness 检查到位 |
| `file_state.py` | ✅ PASS | 简洁的 mtime 跟踪，逻辑正确 |

---

## Summary

| 严重度 | 数量 | 必须修复 |
|--------|------|---------|
| 🔴 High | 3 | ✅ 全部 |
| 🟡 Medium | 3 | M1, M2 建议修复 |
| 🟢 Low | 3 | 可后续处理 |

**关键行动项**:

1. **H1**: 给 `_call_llm` 的重试循环加总迭代上限 + compaction 返回 None 时递增 failed_attempts
2. **H2**: 给 `_live_messages` 的 summary SystemMessage 加稳定 id
3. **H3**: 在 compaction retry loop 中单独 catch ValueError 并 break
4. **M1**: 收窄 `_is_context_overflow_error` 的匹配模式
5. **M2**: apply_patch 容错处理空上下文行
