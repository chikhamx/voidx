# 子 Agent 流事件静默优化

> **Status: Done**

## 问题

子 agent（explore / plan / implement / review）的 `StreamingRenderer` 会向 dock
发送 `AssistantStreamUpdated` / `AssistantStreamCommitted` 流事件，导致 thinking
和正文在终端渲染为可见的 stream 节点。随后父 agent 的 `agent` 工具结果又通过
`tool_execution.py` 的 `ToolResultAppended` 把子 agent 完整输出展示一次，形成重复展示。

### 数据流现状

```
子 agent (run_subagent)
├── StreamingRenderer ──→ AssistantStreamUpdated(phase="thinking")  ← 第一次展示
│                      ──→ AssistantStreamUpdated(phase="text")      ← 第一次展示
│                      ──→ AssistantStreamCommitted                  ← 第一次展示
├── CaptureConsole    ──→ ToolStarted / ToolFinished                 ← 工具调用（合理）
│                      ──→ ToolResultAppended                        ← 工具结果（合理）
│
└── return text ──→ agent tool 的 ToolResult.output
                         │
父 agent (tool_execution.py:184-197)
└── ToolResultAppended(text=完整子agent输出)  ← 最终结果展示（应保留）
```

### 设计目标

- 子 agent 的 thinking 过程**完全不可见**
- 子 agent 的正文流**完全不可见**
- 子 agent 的工具调用仍通过 `CaptureConsole` 展示（用户需要知道子 agent 在做什么）
- 子 agent 最终结果只由父 `agent` 工具的 `ToolResultAppended` 展示**一次**
- `StreamingRenderer` 本身的文本累积功能保留（用于 renderer 自身契约和非 headless 渲染；`stream_llm` 最终消息来自 LLM chunks）

## 方案

给 `StreamingRenderer` 增加 `headless` 模式：文本照常累积，但所有 UI 输出（dock 事件
和 Rich Live 渲染）全部抑制。

### 改动清单

### 1. `src/voidx/ui/output/console/streaming.py`

新增 `headless: bool = False` 参数：

```python
def __init__(
    self,
    console: Console,
    debug: bool = True,
    stream_to_dock: bool = True,
    agent_id: int = -1,
    headless: bool = False,  # 新增
) -> None:
```

**修改点**（headless 采用方法级早退，避免 Rich Live 或 `_flush_thinking()` fallback 泄漏输出）：

| 方法 | 位置 | 改动 |
|------|------|------|
| `start()` | L65-66 | `if self._headless: return` after setting `_stream_started` |
| `feed_thinking()` | L73 | append thinking, then `if self._headless: return` before any event/dock output |
| `feed_text()` | L93 | append `_accumulated`, then `if self._headless: return` before dock/Live paths |
| `_flush_thinking()` | L178 | if `self._headless`, clear `_thinking` and return before dock/Rich output |
| `done()` dock path | L136 | `elif dock.active and self._stream_to_dock and not self._headless:` |
| `done()` Live path | L131 | `if self._live:` → 自然跳过（headless 时 `_live` 永远为 None） |

`done()` 的 `headless` 路径只清空状态并返回 `accumulated`，不做任何 UI 操作：

```python
def done(self) -> str:
    if self._thinking and self._phase == "thinking":
        self._flush_thinking()

    if self._live:
        self._live.stop()
        self._live = None
    elif dock.active and self._stream_to_dock and not self._headless:
        # ... 现有 commit/discard 逻辑 ...
    # headless 路径：直接跳过 UI，只返回文本

    full = self._accumulated
    if full.startswith("● "):
        full = full[2:]
    self._accumulated = ""
    self._thinking = []
    self._thinking_full = []
    self._first_text = True
    self._stream_started = False
    self._phase = "thinking"
    return full
```

### 2. `src/voidx/agent/graph/subagent.py`

子 agent 创建 `StreamingRenderer` 时传入 `headless=True`：

```python
# L201
renderer = StreamingRenderer(console, debug=debug, agent_id=agent_id, headless=True)
```

### 3. `src/voidx/agent/graph/tool_execution.py`

不改。父 `agent` 工具的 `ToolResultAppended` 是子 agent 最终结果的唯一终端展示，应保留。
`sub_buffers` 只用于消息回灌，不会渲染到终端，不能替代 `ToolResultAppended`。

### 4. 测试

`tests/test_ui_events.py` 新增：

- `test_streaming_renderer_headless_no_dock_events`
  - `headless=True` 时 `feed_thinking` / `feed_text` / `done` 不 emit 任何事件
  - 但 `done()` 仍正确返回累积文本
  - 覆盖 dock inactive / `stream_to_dock=False` 时不创建 Live、不崩溃

- `test_subagent_streaming_is_headless`（或放 `tests/test_agent/`）
  - 验证子 agent 运行时 dock tree 中无 child agent stream 节点
  - 验证父 `agent` 工具下仍保留一次最终 `ToolResultAppended`

### 5. 不需要改动的文件

| 文件 | 原因 |
|------|------|
| `src/voidx/agent/graph/streaming.py` | `stream_llm` 调 renderer 接口不变 |
| `src/voidx/ui/output/events/schema.py` | 事件 schema 不变 |
| `src/voidx/ui/output/dock/app.py` | 不涉及 |
| `src/voidx/ui/output/capture.py` | `CaptureConsole` 继续正常工作 |
| `frontend/` | 前端不变 |

## 效果对比

**改前**：
```
⟳ Implementing (1/5)           ← step header
  ⏳ Thinking                   ← 多余的 thinking 流节点
    我需要检查 auth 模块...
  ● 首先我检查了 auth...        ← 多余的正文流节点
    ...
  ● reading auth.py             ← 工具调用（合理）
     ...tool result...           ← 工具结果（合理）
  ...agent 工具返回文本...       ← ToolResultAppended 重复打印
```

**改后**：
```
⟳ Implementing (1/5)           ← step header
  ● reading auth.py             ← 工具调用（合理）
     ...tool result...           ← 工具结果（合理）
  ...agent 工具返回文本...       ← 最终结果展示一次
```
（thinking 和正文流不再出现，agent 工具最终结果只保留一次）
