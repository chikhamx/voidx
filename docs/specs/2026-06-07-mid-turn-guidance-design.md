# Mid-Turn Guidance — 用户引导文本注入设计

> **Status: Draft**

## 问题

LLM 在多步循环中运行时（`call_llm → execute_tools → call_llm → ...`），用户无法在不中断当前执行的情况下追加方向性指导。当前用户必须等整个 turn 结束才能输入，或按 Ctrl+C 强制中断。

## 目标

- 用户在 LLM 运行期间可输入引导文本（如"注意用 TypeScript"、"别忘了处理边界情况"）
- 引导文本在 LLM 的**下一步** `call_llm` 前作为 HumanMessage 注入
- 不中断当前流式输出和工具执行
- 引导文本在 UI 中有明确标识，不与正常用户消息混淆

## 设计

### 核心机制

在 `GraphRunLoopHost` 上维护 `_pending_guidance: list[str]` 队列，`prepare` 节点每步检查并消费该队列，将引导文本注入为标记过的 HumanMessage。

这与现有的 `step_hint` 机制一致（`_voidx_step_hint` marker），复用相同的消息标记模式。

### 消息标记

新增 `GUIDANCE_MARKER = "_voidx_guidance"`，用于：

1. 标记引导消息，使其在 compaction 和用户文本提取中被正确处理
2. 在 UI 渲染时区分引导文本与正常用户消息

### 数据流

```
用户输入引导文本
    ↓
UI 事件 / slash command
    ↓
host._pending_guidance.append(text)
    ↓
prepare 节点（每步执行）
    ↓
检查 host._pending_guidance
    ↓
消费队列 → 构造 HumanMessage(content=text, additional_kwargs={GUIDANCE_MARKER: True})
    ↓
注入 state["messages"]
    ↓
call_llm 在下一步看到引导文本
```

### 改动清单

#### 1. 消息标记 — `src/voidx/llm/message_markers.py`

```python
GUIDANCE_MARKER = "_voidx_guidance"

def is_guidance_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(GUIDANCE_MARKER))
```

#### 2. Host 队列 — `src/voidx/agent/graph/core.py` (VoidXGraph)

新增属性：

```python
self._pending_guidance: list[str] = []
```

新增方法：

```python
def submit_guidance(self, text: str) -> None:
    """Submit mid-turn guidance text for injection at next step."""
    self._pending_guidance.append(text)
```

#### 3. Prepare 节点注入 — `src/voidx/agent/graph/core.py` (`_prepare_with_stream`)

在 `_prepare_with_stream` 末尾、return 之前，消费 `_pending_guidance`：

```python
# ── inject pending guidance ──────────────────────────────────
guidance_messages = []
while self._pending_guidance:
    text = self._pending_guidance.pop(0)
    guidance_messages.append(
        HumanMessage(content=text, additional_kwargs={GUIDANCE_MARKER: True})
    )
if guidance_messages:
    return {**base, "skill_runs": skill_runs, "messages": guidance_messages}
```

由于 `AgentState.messages` 使用 `add_messages` reducer，返回的 messages 会自动追加到现有消息列表。

#### 4. UI 输入通道

**方案：新增 slash command `/guide`**

在 `src/voidx/agent/slash/` 下新增 `guide.py`：

- `/guide <text>` — 提交引导文本
- 在 dock UI 中显示为 `[guide] <text>` 样式

**备选：dock 输入框特殊前缀**

在 dock 的输入处理中，当 LLM 正在运行时，用户输入以 `> ` 前缀开头则视为引导文本。这需要修改 `handle_user_input` 逻辑。

推荐 `/guide` 方案，更明确且不需要修改输入解析逻辑。

#### 5. Compaction 处理 — `src/voidx/llm/compaction.py`

引导消息在 compaction 中应与 step_hint 消息类似处理：

- 在 `_latest_user_text` 等函数中跳过引导消息（`is_guidance_message` 检查）
- 在 compaction 选择中保留引导消息（它们是有效的上下文）

#### 6. UI 事件 — `src/voidx/ui/output/events/schema.py`

新增事件类型：

```python
class GuidanceSubmitted(UiEventBase):
    kind: Literal["guidance.submitted"] = "guidance.submitted"
    text: str
```

#### 7. Dock 渲染 — `src/voidx/ui/output/dock/`

引导消息在 dock 中以特殊样式显示（如 `[dim][guide][/dim] text`），区别于正常用户消息。

### 线程安全

`_pending_guidance` 是一个普通 list，在 asyncio 单线程模型下是安全的：

- `submit_guidance` 由 UI 事件回调调用（同一个事件循环）
- `_prepare_with_stream` 在图执行中调用（同一个事件循环）
- 不需要锁

### 边界情况

1. **空引导文本** — `submit_guidance` 忽略空字符串
2. **引导文本过长** — 限制单条 2000 字符，超出截断并警告
3. **Turn 结束时未消费** — `_run_once` 结束后清空 `_pending_guidance`，避免跨 turn 泄漏
4. **Subagent** — subagent 的 `run_subagent` 独立运行，引导文本不传递给 subagent（可后续扩展）

### 不改动的部分

- `AgentState` schema 不变（利用 `add_messages` reducer 自动合并）
- LangGraph 图拓扑不变（`prepare → call_llm → execute_tools → call_llm → ... → finalize`）
- `stream_llm` 不变
- 现有 slash command 不变

## 实现顺序

1. `message_markers.py` — 新增 `GUIDANCE_MARKER` 和 `is_guidance_message`
2. `core.py` — 新增 `_pending_guidance` 队列和 `submit_guidance` 方法
3. `core.py` — `_prepare_with_stream` 中消费引导队列
4. `compaction.py` — 引导消息的 compaction 处理
5. `slash/guide.py` — `/guide` 命令
6. `events/schema.py` — `GuidanceSubmitted` 事件
7. Dock 渲染 — 引导消息的特殊样式
8. `turn_mixin.py` — turn 结束后清理 `_pending_guidance`
9. 测试
