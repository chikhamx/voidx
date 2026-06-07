# Thinking 渲染优化：复用 Stream 节点

> **Status: Done**

## 问题

TUI 中 thinking 过程只显示黄色圆点 `● Thinking`，思考内容不可见。

根因：thinking 走 `StatusUpdated` 事件创建 status 节点，该节点被标记为 unsettled，
只能在 active frame 中渲染。但 active frame 有高度限制（`tail_rows[-row_limit:]`），
thinking 的 body_lines 被截断。思考结束后节点被折叠（`collapsed=True`），
内容彻底不可见。

## 方案

Thinking 复用 stream 节点走 active frame，显示为 `⏳ Thinking` + 最多5行内容；
commit 到主区域时不保留 thinking。

### 数据流对比

**现在**：
```
start()       → AssistantStreamStarted + StatusUpdated(thinking)
feed_thinking → StatusUpdated(detail=思考文本)     ← status 节点，被截断
feed_text     → StatusFinished(thinking) + StatusUpdated(streaming) + AssistantStreamUpdated(正文)
done()        → AssistantStreamCommitted + StatusFinished(streaming)
```

**改后**：
```
start()       → AssistantStreamStarted                                ← 初始化事件，不保证创建节点
feed_thinking → AssistantStreamUpdated(text=思考文本, phase="thinking")  ← stream 节点
feed_text     → AssistantStreamUpdated(text=正文, phase="text")          ← 同一节点切换
done()        → AssistantStreamCommitted / AssistantStreamDiscarded      ← 有正文才 commit
```

### 视觉效果

**流式中**：
```
⏳ Thinking
  我需要先检查 auth 模块...
  然后看看路由配置...
  最后分析中间件
```

**切换到正文后**（同一节点原位替换）：
```
● 首先我检查了 auth 模块的结构...
  它包含三个主要文件...
```

**commit 后**：thinking 完全不保留。若最终有正文，只保留正文节点；若只有 thinking 没有正文，移除临时 stream 节点。

## 改动清单

### 1. `src/voidx/ui/output/events/schema.py`

`AssistantStreamUpdated` 加 `phase` 字段：

```python
class AssistantStreamUpdated(UiEventBase):
    kind: Literal["assistant_stream.updated"] = "assistant_stream.updated"
    text: str
    stream_id: str = "default"
    phase: Literal["thinking", "text"] = "text"  # 新增
```

### 2. `src/voidx/ui/output/console/streaming.py` — 核心改动

**删除**：
- `_thinking_status_id` / `_streaming_status_id` 字段
- `_status_started` / `_streaming_status_started` 字段（但保留一个 `_stream_started` 用于 `start()` 幂等）
- `_start_streaming_status()` 方法
- `_finish_thinking_status()` 方法
- `_finish_live_status()` 方法
- 所有 `StatusUpdated` / `StatusFinished` 的 emit（thinking/streaming 相关）
- `_flush_thinking()` 中 status 节点逻辑

**修改**：
- `start()` → 只发 `AssistantStreamStarted`，不发 `StatusUpdated`
- `feed_thinking()` → 发 `AssistantStreamUpdated(text=思考文本, phase="thinking")`
- `feed_text()` → 首次文本时直接切换 phase，发 `AssistantStreamUpdated(text=正文, phase="text")`；正文 `_accumulated` 必须只包含正文，不拼接 thinking
- `_flush_thinking()` → 简化为只清空 `_thinking` 列表（不再操作 status 节点）
- `done()` → 有正文时发最终 `AssistantStreamUpdated(..., phase="text")` + `AssistantStreamCommitted`；无正文但有 thinking 临时节点时发 `AssistantStreamDiscarded`；不发 `StatusFinished`

**非 dock 模式**（Rich Live 直出）的 `_flush_thinking` 保留现有逻辑不变，
因为那条路径没有 active frame 截断问题。

### 3. `src/voidx/ui/output/dock/app.py`

**`set_stream`** — 加 `phase` 参数：

```python
def set_stream(self, text: str, *, parent=None, phase="text") -> bool:
    self._stream_text = text
    self._update_stream_node(parent=parent, phase=phase)
    self._mark_unsettled(self._stream_node)
    self.refresh()
    return True
```

`AssistantStreamStarted` 仍然可以调用 `set_stream("")`，但空文本不会创建节点；真正的 stream 节点由第一次非空 thinking/text update 创建。

**`_update_stream_node`** — 根据 phase 切换渲染：

```python
def _update_stream_node(self, *, parent=None, phase="text") -> None:
    clean = _clean(self._stream_text).strip("\n")
    if not clean:
        return
    # ... 节点创建逻辑不变 ...

    if phase == "thinking":
        bullet = _ansi_rgb("⏳", (235, 203, 139))  # 黄色
        lines = clean.splitlines()
        visible = lines[-5:]  # 最多5行
        self._stream_node.header = _ansi_line(f"{bullet} Thinking")
        self._stream_node.body_lines = [
            _ansi_line(f"  {escape(line)}") for line in visible
        ]
    else:
        # 现有正文渲染逻辑不变
        if clean.startswith("● "):
            clean = clean[2:]
        lines = _markdown_lines(clean, self._markdown_width())
        if not lines:
            return
        bullet = _ansi_rgb("●", (163, 190, 140))  # 绿色
        self._stream_node.header = _ansi_line(f"{bullet} {lines[0]}")
        self._stream_node.body_lines = [_ansi_line(f"  {line}") for line in lines[1:]]
```

**`commit_stream` / `discard_stream`** — `discard_stream` 继续移除临时节点；`commit_stream` 只用于已有正文的 stream。thinking-only 场景必须由 renderer 在 `done()` 时走 discard，避免 thinking 被 settle 进 transcript。

### 4. `src/voidx/ui/output/events/__init__.py` — DockEventConsumer

`AssistantStreamUpdated` 处理传递 `phase`：

```python
case AssistantStreamUpdated() as e:
    return self._dock.set_stream(e.text, parent=self._stream_parent(e.agent_id), phase=e.phase)
```

### 5. `frontend/src/protocol.schema.json`

`AssistantStreamUpdated` 加 `phase` 字段。不要手写维护该文件，修改 Python schema 后运行 `scripts/export_ui_protocol_schema.py` 生成：

```json
"phase": {
    "default": "text",
    "enum": ["thinking", "text"],
    "title": "Phase",
    "type": "string"
}
```

### 6. `frontend/src/render.js`

前端基于 snapshot 渲染节点，stream 节点 commit 后 thinking 内容已不保留，
无需特殊处理。如需在流式中区分 thinking/正文样式，可在事件处理中根据 `phase` 切换 CSS class。

### 7. 测试

**`tests/test_ui_events.py`**：

- `test_streaming_renderer_updates_thinking_and_streaming_status` → 重写
  - 断言 thinking 阶段 stream 节点显示 "Thinking" + 内容
  - 断言 text 阶段 stream 节点切换为正文
  - 断言 commit 后无 thinking 残留

- `test_streaming_renderer_collapses_thinking_content_after_text_starts` → 重写
  - 断言 thinking 最多显示5行
  - 断言 commit 后 thinking 内容不保留

- 新增 thinking-only 边界测试
  - `feed_thinking()` 后 `done()`，断言 `Thinking` 和 thinking 内容均未保留

**`tests/test_agent/test_stream_llm.py`**：FakeRenderer 接口不变，无需改动。

## 不需要改动的文件

| 文件 | 原因 |
|------|------|
| `src/voidx/agent/graph/streaming.py` | `stream_llm` 只调 `renderer.feed_thinking()` / `renderer.feed_text()`，接口不变 |
| `src/voidx/ui/output/dock/nodes.py` | `set_status`/`finish_status` 不再被 thinking 使用，但其他 status 仍需要 |
| `src/voidx/ui/output/tree.py` | 树渲染逻辑不变 |
| `src/voidx/ui/tui/` | TUI 渲染基于 tree.render()，stream 节点渲染变化自动反映 |
| `src/voidx/ui/gateway/session.py` | GatewayEventConsumer 只是透传事件到 WebSocket |

## 可删除的代码

- `StreamingRenderer._thinking_status_id` / `_streaming_status_id`
- `StreamingRenderer._status_started` / `_streaming_status_started`
- `StreamingRenderer._start_streaming_status()`
- `StreamingRenderer._finish_thinking_status()`
- `StreamingRenderer._finish_live_status()`
- `StreamingRenderer` 中所有 `StatusUpdated`/`StatusFinished` 的 emit（thinking/streaming 相关）
