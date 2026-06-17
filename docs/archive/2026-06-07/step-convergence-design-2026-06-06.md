> **Status: Done**

# Step Convergence — Runtime 步数收敛保障设计

> 让 LLM 感知执行进度，在步数耗尽前主动收敛；runtime 层硬约束确保即使 LLM 不收敛也不会卡死或输出无效内容。

## 1. 背景与目标

当前主循环和 subagent 都已有硬性步数限制：

```text
prepare -> call_llm -> router -> execute_tools -> call_llm ...
                              \-> finalize -> end
```

现有约束：
- `_prepare()` 先把 `step_count` 加 1，所以 `_call_llm()` 看到的是当前 LLM 调用序号。
- `_call_llm()` 中 `step > max_s` 直接返回 `should_continue=False`。
- `_call_llm()` 中 `has_tool_budget = step < max_s - 1`，因此 `step == max_s - 2` 是最后一个有工具的 LLM 调用，`step >= max_s - 1` 已经无工具。
- `_router()` 在 tool-call 分支里检查 `step_count >= max_steps` 并结束。
- subagent 使用 `range(1, max_steps + 1)`，最后一步无工具。

问题：
- LLM 不知道还剩几步，可能到最后工具步还在探索。
- 无工具的最终调用没有显式收敛提示。
- `_finalize()` 当前是空操作，无法在极端情况下补出有效收尾。
- 如果 step hint 作为真实 `HumanMessage` 写入 state/session，会污染历史、持久化和 compaction。

目标：
1. 在剩余步数不足时引导 LLM 收敛。
2. 最后有工具步和无工具步文案与当前 step 语义一致。
3. step hint 只作为本次 LLM 调用的临时尾部消息，不进入 LangGraph state、session、compaction 或后续 turns。
4. 主循环和 subagent 使用同一套 convergence helper。
5. `_finalize()` 只在明确触发 forced convergence 且输出无效时兜底，避免覆盖正常短回复。

## 2. 核心设计

新增 `src/voidx/agent/graph/convergence.py`，集中放置：

```python
STEP_HINT_MARKER = "_voidx_step_hint"

def build_step_hint(step: int, max_steps: int, *, has_tool_budget: bool) -> str:
    ...

def build_final_convergence_prompt(step: int, max_steps: int, goal: str) -> str:
    ...

def build_convergence_messages(
    *,
    step: int,
    max_steps: int,
    has_tool_budget: bool,
    goal: str,
) -> tuple[list[HumanMessage], bool]:
    ...

def generate_fallback_summary(state: AgentState) -> str:
    ...
```

`build_convergence_messages()` 返回：
- 临时 `HumanMessage` 列表，仅用于本次 LLM call。
- `forced_convergence` bool，只在无工具最终收敛 prompt 注入时为 true。

所有 convergence hint message 都带标记，便于测试和防御性过滤：

```python
HumanMessage(
    content=hint,
    additional_kwargs={STEP_HINT_MARKER: True},
)
```

## 3. Step 语义

当前主循环的工具预算是：

```python
has_tool_budget = step < max_s - 1
```

因此：
- `step <= max_s - 5`：通常不注入 hint。
- `step in [max_s - 4, max_s - 3]`：普通收敛预警，工具仍可用。
- `step == max_s - 2`：最后有工具步，明确要求结束探索、用现有工具产出可收敛结果。
- `step >= max_s - 1`：无工具最终步，注入 final convergence prompt。
- `step > max_s`：不调用 LLM，直接 `should_continue=False`。

示例：

```python
def build_step_hint(step: int, max_steps: int, *, has_tool_budget: bool) -> str:
    remaining_calls = max_steps - step
    if remaining_calls > 4:
        return ""
    if has_tool_budget and step == max_steps - 2:
        return (
            f"[Step {step}/{max_steps}] This is the LAST step with tools. "
            "Use tools only for final verification or essential missing facts, then converge."
        )
    if has_tool_budget:
        return (
            f"[Step {step}/{max_steps}] {remaining_calls} LLM calls remain. "
            "Start converging; avoid broad new exploration."
        )
    return ""
```

最终无工具 prompt：

```python
def build_final_convergence_prompt(step: int, max_steps: int, goal: str) -> str:
    return (
        f"[Step {step}/{max_steps}] FINAL response step. No tools are available.\n\n"
        "Provide the best final answer now:\n"
        "1. Result: what was accomplished or learned\n"
        "2. Pending: what remains uncertain, blocked, or needs follow-up\n\n"
        f"Original goal: {goal or '(unknown)'}\n"
        "Do not describe tool calls or request more tool use."
    )
```

## 4. 主循环实现

在 `_call_llm()` 中不要 mutate `state["messages"]`。

```python
convergence_messages, forced_convergence = build_convergence_messages(
    step=step,
    max_steps=max_s,
    has_tool_budget=has_tool_budget,
    goal=state.get("goal") or _latest_user_text(state.get("messages", [])),
)
llm_messages = [*state["messages"], *convergence_messages]
```

后续本次 LLM 调用全部使用 `llm_messages`：
- `estimate_context_tokens(llm_messages, ...)`
- `save_context_frame_from_messages(..., messages=llm_messages, metadata={...})`
- `_stream_llm(model_with_tools, llm_messages, ...)`
- `usage_stats.record_call(..., messages=llm_messages, ...)`

返回 state 时仍只返回 assistant message：

```python
return {
    "messages": [assistant_msg],
    "step_count": step + 1,
    "convergence_forced": forced_convergence,
}
```

这样 step hint 不会进入：
- LangGraph `state["messages"]`
- session message persistence
- transcript
- compaction input
- 下一次 LLM call 的历史

## 5. `_finalize()` 兜底

需要在 `AgentState` 增加可选字段：

```python
convergence_forced: NotRequired[bool]
```

兜底只在 forced convergence 已触发时检查，避免覆盖正常短回复：

```python
async def _finalize(self, state: AgentState) -> dict:
    if not state.get("convergence_forced"):
        return {}

    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and not last.tool_calls:
        content = extract_text(last).strip()
        if len(content) >= 20:
            return {}

    return {"messages": [AIMessage(content=generate_fallback_summary(state))]}
```

fallback summary 从 state 中提取：
- goal / latest user request
- step/max_steps
- tool result ids 和简短结果
- 明显文件路径
- pending/blocked 状态

不要声称任务完成；只说明“达到步数上限，以下是目前可确认的信息和下一步”。

## 6. Subagent 实现

subagent 同样不能把 hint append 到长期 `messages`。

```python
convergence_messages, forced_convergence = build_convergence_messages(
    step=step,
    max_steps=agent_def.max_steps,
    has_tool_budget=bool(active_tool_defs),
    goal=task_description,
)
llm_messages = [*messages, *convergence_messages]
```

本次调用使用 `llm_messages`：
- token estimate
- context frame
- `stream_llm`
- usage stats

调用完成后只 append assistant/tool messages：

```python
messages.append(assistant_msg)
sub_messages.append(assistant_msg)
```

如果最后无工具步返回空内容，subagent 返回 `generate_fallback_summary(...)` 的简化版本，而不是把 convergence prompt 存入 `messages`。

## 7. Compaction 与持久化

主策略是“不让 hint 进入 state/session”，因此 compaction 正常情况下看不到 step hint。

仍建议增加防御性过滤：
- `CompactionService._turns()` 跳过 `additional_kwargs[STEP_HINT_MARKER]` 的 `HumanMessage`。
- `select_details()` 测试覆盖带 hint 的消息不会改变 turn 边界。

不应依赖 `_semantic_messages`。当前实现没有这个抽象，compaction 直接处理传入的 messages 并按 `HumanMessage` 切 turn。

## 8. 缓存影响

当前 runtime context 已经是：
- system prompt：稳定 sections + `Session Date` + optional `Long Summary`
- 最新 user message：`Runtime State` + `Current DateTime` + task state
- 历史消息：原始语义消息

convergence hint 作为本次 LLM call 的临时尾部 `HumanMessage`：
- 不修改 system prompt。
- 不修改最新真实 user message。
- 不修改历史消息。
- 只让最后一条临时消息 recompute。

context frame 可以记录这条临时 hint，因为它代表真实发送给模型的 payload；但 session messages 不能持久化它。

## 9. 改动清单

| 文件 | 改动 |
|------|------|
| `src/voidx/agent/state.py` | 增加 `convergence_forced: NotRequired[bool]` |
| `src/voidx/agent/graph/convergence.py` | 新增 convergence helper 和 fallback summary |
| `src/voidx/agent/graph/core.py` | `_call_llm` 使用临时 `llm_messages` 注入 hint/final prompt；`_finalize` gated fallback |
| `src/voidx/agent/graph/subagent.py` | 使用临时 `llm_messages`，避免 hint 累积 |
| `src/voidx/llm/compaction.py` | 防御性跳过 `_voidx_step_hint` HumanMessage |

## 10. 测试策略

单元测试：
- `test_build_step_hint_normal_window`
- `test_build_step_hint_last_tool_step`
- `test_build_final_convergence_prompt`
- `test_generate_fallback_summary`
- `test_convergence_messages_are_marked`

主循环集成测试：
- `_call_llm` 在收敛窗口使用临时 `llm_messages`，但返回 state 不包含 hint。
- no-tools final step 注入 final prompt，`tool_defs=[]`。
- `_finalize` 只在 `convergence_forced=True` 且输出无效时兜底。
- 短但有效的普通回复不会被 `_finalize` 覆盖。
- context frame 记录临时 hint，但 session persistence 不保存 hint。

subagent 测试：
- 每一步不会累积旧 hint。
- final step 无工具且有 final prompt。
- returned `sub_messages` 不包含 hint。

compaction 测试：
- 带 `_voidx_step_hint` 的 `HumanMessage` 不参与 turn split。
- hint 不会成为 `tail_id`。

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| LLM 忽略 hint 继续调工具 | final no-tools step 物理剥夺工具 |
| Hint 污染历史 | 只构造临时 `llm_messages`，不 mutate state/session |
| `_finalize` 覆盖正常短回复 | 只在 `convergence_forced=True` 时触发 |
| Step 文案与工具预算不一致 | 文案基于 `has_tool_budget` 和当前 `step < max_s - 1` 规则 |
| compaction 切到 hint turn | hint 不进入 state，另加防御性过滤 |
