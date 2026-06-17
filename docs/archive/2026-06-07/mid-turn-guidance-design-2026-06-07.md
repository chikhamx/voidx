# Mid-Turn Guidance — 用户引导文本注入设计

> **Status: Done**

## 问题

LLM 在多步循环中运行时（`call_llm -> execute_tools -> call_llm -> ...`），用户无法在不中断当前执行的情况下追加方向性指导。当前用户必须等整个 turn 结束才能输入，或按 Ctrl+C 强制中断。

## 目标

- 用户在 agent turn 运行期间可提交引导文本，例如“注意用 TypeScript”“别忘了处理边界情况”。
- 引导文本在下一次 `_call_llm` 前作为标记过的 `HumanMessage` 注入。
- 不中断当前流式输出和工具执行。
- UI 中明确显示 `[guide] ...`，不与普通用户 turn 混淆。
- 引导消息不改变“最新用户请求”、intent、skill context 和 convergence goal 的语义。

## 当前架构约束

### `prepare` 不是每步执行

当前 LangGraph 拓扑是：

```text
prepare -> call_llm -> execute_tools -> call_llm -> ... -> finalize
```

`prepare` 只在入口执行一次，从 `execute_tools` 回来会直接进入 `call_llm`。因此不能在 `_prepare_with_stream()` 中消费 guidance 队列；注入点必须在 `_call_llm()` 中、构造 `llm_messages` 之前。

### 普通 slash command 会排队到 turn 结束

TUI 的 `_consume()` 在当前 `on_submit()` 完成前不会处理下一个 queued input。用户在 busy 状态下输入 `/guide ...`，如果走普通 submit queue，只会在当前 turn 结束后才 dispatch，无法影响下一步 LLM。

因此 `/guide` 必须有 busy-state 输入旁路：

- TUI busy 时识别 `/guide <text>`，不放入主 `_queue`。
- Web/gateway submit 也识别 `/guide <text>`，直接调用 guidance API。
- 非 busy 时 `/guide <text>` 可走普通 slash dispatch，提交到 pending queue；如果当前没有活跃 turn，命令会提示“queued for next agent step”或“only useful while agent is running”。

## 设计

### 核心机制

在 `VoidXGraph` 上维护 pending guidance 队列：

```python
self._pending_guidance: list[str] = []
```

新增 host API：

```python
GUIDANCE_MAX_CHARS = 2000

def submit_guidance(self, text: str) -> bool:
    """Queue mid-turn guidance for injection before the next LLM call."""
```

行为：

- strip 空白。
- 空文本返回 `False`。
- 超过 2000 字符时截断，并通过 UI 提示。
- 成功入队后发出 guidance UI 事件/消息。

### 消息标记

文件：`src/voidx/llm/message_markers.py`

```python
GUIDANCE_MARKER = "_voidx_guidance"

def is_guidance_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(GUIDANCE_MARKER))
```

引导消息使用：

```python
HumanMessage(
    content=text,
    additional_kwargs={GUIDANCE_MARKER: True},
)
```

### LLM 注入点

文件：`src/voidx/agent/graph/core.py`

在 `_call_llm()` 中，`build_convergence_messages()` 之前消费 `_pending_guidance`：

```python
guidance_messages = self._drain_pending_guidance()
base_messages = [*state["messages"], *guidance_messages]
convergence_messages, convergence_forced = build_convergence_messages(
    ...,
    goal=state.get("goal", "") or latest_user_text(base_messages),
)
llm_messages = [*base_messages, *convergence_messages]
```

返回值必须包含 injected guidance，使后续 graph state 持有这些消息：

```python
result_messages = [*guidance_messages, assistant_msg]
return {
    "messages": result_messages,
    "step_count": step + 1,
    "convergence_forced": convergence_forced,
}
```

如果 `self.model is None` 或 `step > max_s`，可以不消费 guidance，避免无 LLM 时丢失用户提示。Turn 结束时 `_run_once` 会清理未消费 guidance，避免跨 turn 泄漏。

### 最新用户文本过滤

所有“找最新用户文本”的函数必须跳过 guidance：

- `src/voidx/agent/graph/topology.py::latest_user_text`
- `src/voidx/agent/graph/convergence.py::_latest_user_text`
- compaction 的 turn boundary 逻辑

这保证 guidance 不会覆盖原始用户请求，不会改变 skill context、convergence goal、title/intent 等语义。

### Compaction 处理

guidance 不是普通 turn boundary，但它是有效上下文：

- `_turns()`：跳过 guidance，不把它当成新的 user turn。
- `fallback_summary()` / `build_prompt()`：保留 guidance，建议渲染为 `[Guidance]: <text>`，让压缩摘要能记录用户中途约束。
- step hint 仍然跳过，不进入摘要。

### UI 输入通道

#### TUI busy 旁路

文件：`src/voidx/ui/tui/app.py`

在 `_do_submit()` 中，普通入队前增加：

```python
if self._busy and stripped.startswith("/guide "):
    self._submit_guidance_bypass(stripped)
    return True
```

旁路逻辑：

- 记录 history。
- 清空输入框。
- 调用 external command handler，而不是 `_queue.put_nowait()` 或 web request handler。
- 显示短 notice 或由 host 发出 `[guide] ...` 消息。

#### Web/gateway 旁路

文件：`src/voidx/agent/graph/run_loop.py`

`_handle_web_command()` 对 submit 类型做同样识别：

```python
if kind == "submit" and command.text.strip().startswith("/guide "):
    self.submit_guidance(command.text.removeprefix("/guide").strip())
else:
    app.submit_external_input(command.text)
```

#### 普通 `/guide` slash command

文件：`src/voidx/agent/slash/guide.py`

提供非 busy/测试路径：

- `/guide <text>`：调用 host `submit_guidance(text)`。
- `/guide`：提示用法。

`SlashHandler` 注册该 mixin 和命令。

### UI 渲染

新增事件：

```python
class GuidanceSubmitted(UiEventBase):
    kind: Literal["guidance.submitted"] = "guidance.submitted"
    text: str
    truncated: bool = False
```

事件必须加入 `UiEvent` union，并在 `DockEventConsumer` 中渲染为：

```text
[guide] 注意用 TypeScript
```

没有 event bus 时，`submit_guidance()` 直接 `dock.append_message(..., markup=True)` 或 `ui.print(...)`。

### 线程安全

当前 TUI、event bus、gateway handler 都运行在同一个 asyncio event loop，普通 list 足够。若未来跨线程提交 guidance，必须改成 `loop.call_soon_threadsafe()` 或 `asyncio.Queue`。

### 边界情况

| Case | Behavior |
|------|----------|
| 空文本 | 忽略并返回 `False` |
| 超过 2000 字符 | 截断，入队，UI 显示 truncated 标记 |
| 当前 LLM 正在 streaming | guidance 排队，下一次 `_call_llm` 前注入 |
| 当前正在执行工具 | guidance 排队，工具完成后下一次 `_call_llm` 前注入 |
| 当前 turn 没有下一次 `_call_llm` | `_run_once` finally 清空 pending guidance，避免泄漏到下一 turn |
| subagent 正在运行 | guidance 只注入 orchestrator，不传给 subagent |

## 实现顺序

1. `message_markers.py` — 新增 `GUIDANCE_MARKER` 和 `is_guidance_message`。
2. `core.py` — 新增 `_pending_guidance`、`submit_guidance()`、drain helper。
3. `core.py` — 在 `_call_llm()` 中注入 guidance 并返回到 graph state。
4. `topology.py` / `convergence.py` — 最新用户文本跳过 guidance。
5. `compaction.py` — guidance 不作为 turn boundary，但进入 prompt/fallback 摘要。
6. `events/schema.py` / `events/__init__.py` — 新增 `GuidanceSubmitted` 渲染。
7. `ui/tui/app.py` — busy `/guide` 旁路提交。
8. `run_loop.py` — web submit `/guide` 旁路。
9. `slash/guide.py` / `handler.py` / `commands.py` — 普通 `/guide` 命令注册。
10. `turn_mixin.py` — turn 结束后清理 `_pending_guidance`。
11. 测试。

## 测试计划

- `test_call_llm_injects_pending_guidance_before_next_model_call`
- `test_guidance_is_returned_to_graph_state`
- `test_latest_user_text_skips_guidance`
- `test_convergence_goal_skips_guidance`
- `test_compaction_keeps_guidance_but_not_as_turn_boundary`
- `test_tui_busy_guide_bypasses_submit_queue`
- `test_web_submit_guide_bypasses_submit_queue`
- `test_slash_guide_submits_guidance`
- `test_run_once_clears_unconsumed_guidance`
