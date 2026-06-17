# TUI 上下文压缩刷屏问题定位

> **Status: Done**
> **日期**: 2026-06-07
> **严重度**: High — 用户可复现，影响正常使用

## 现象

上下文自动压缩触发后，TUI 终端出现刷屏（内容重复、光标跳跃、画面闪烁）。

## 根因

`_run_compaction_agent` 使用 `StreamingRenderer(console, stream_to_dock=False)`，在 TUI 模式下走 `Live` 路径直接写 stdout，与 TUI 帧渲染冲突。

修复前确认：

- `src/voidx/agent/graph/compaction.py:262` 仍创建非 headless renderer。
- `src/voidx/ui/output/console/streaming.py:115-126` 在 `dock.active=True` 且 `stream_to_dock=False` 时仍会创建 Rich `Live` 并直接写 stdout。
- `src/voidx/ui/tui/app.py:213-224` 的 `invalidate()` 已经改成事件循环内调度并合并渲染，因此不是同步立即 `_render_frame()`；但 Rich `Live` 和 TUI 仍会竞争同一个 stdout，刷屏根因仍成立。

## 调用链

```
turn_mixin._run_turn()
  → _maybe_compact()                              # 检测上下文溢出
    → _run_compaction_agent()                      # 生成摘要
      → StreamingRenderer(console, stream_to_dock=False)  ← 问题入口
        → feed_text()
          → dock.active=True, stream_to_dock=False
            → Live(console).update()               # 直接写 stdout
            → dock.after_output()                  # 触发 TUI invalidate
              → loop.call_soon(_run_scheduled_render)
                → TUI._render_frame()              # TUI 也写 stdout
```

## 冲突机制

| 组件 | 操作 | 冲突 |
|------|------|------|
| `Live` | 移动光标、写入 Markdown 内容 | 直接操作 stdout |
| TUI `_render_frame` | 绝对定位渲染帧、光标控制 | 也操作 stdout |
| `Live.flush` | 调 `dock.after_output()` → `invalidate()` | 触发 TUI 重渲染 |

三者互相抢 stdout，`Live` 写入的内容被 TUI 当作帧内容重新渲染，导致内容重复和光标跳跃。

## 关键代码

**问题入口**：`src/voidx/agent/graph/compaction.py:262`

```python
renderer = StreamingRenderer(console, debug=self._debug, stream_to_dock=False)
```

**冲突路径**：`src/voidx/ui/output/console/streaming.py:115-126`

```python
# dock.active=True 但 stream_to_dock=False → 走 Live 路径
if self._live is None:
    self._live = Live(Markdown(""), console=self._console, ...)
    self._live.start()
self._live.update(Markdown(self._accumulated))
dock.after_output()  # → TUI invalidate → 又触发帧渲染
```

## 修复方向

压缩 agent 的输出不需要实时显示给用户，TUI 模式下应避免 `Live` 直接写 stdout。

| 方案 | 改动 | 效果 |
|------|------|------|
| `headless=True`（推荐） | `compaction.py:262` 改为 `headless=True` | 完全静默，用户只看状态栏提示 |
| `stream_to_dock=True` | `compaction.py:262` 改为 `stream_to_dock=True` | 压缩输出走事件路径进 dock，但摘要不需要显示在对话流中 |

## 选定方案

已使用 `headless=True`，并保留 `stream_to_dock=False` 表达 compaction 输出不进入对话流的意图：

```python
renderer = StreamingRenderer(
    console,
    debug=self._debug,
    stream_to_dock=False,
    headless=True,
)
```

`StreamingRenderer.done()` 在 headless 模式下仍会返回累积文本，`stream_llm()` 也会返回最终 `AIMessage`，因此不会影响摘要提取和 token usage 记录。

## 影响范围

仅影响 TUI 模式。Web UI 模式下 dock 用 Rich Live 独立渲染，不冲突。

## 处理结果

- `src/voidx/agent/graph/compaction.py`：compaction agent 的 `StreamingRenderer` 改为 `headless=True`，避免 Rich `Live` 在 TUI 中直接写 stdout。
- `tests/test_compaction.py`：compaction agent 路径捕获 renderer，断言 `_headless is True` 且 `_stream_to_dock is False`。
- `tests/test_ui_events.py` 既有 headless renderer 测试继续覆盖 renderer 层能力。

## 验证

- `.venv/bin/python -m pytest tests/test_compaction.py::TestCompactionRetry::test_run_compaction_agent_builds_messages_and_extracts_text tests/test_ui_events.py::test_streaming_renderer_headless_suppresses_ui_output -q`
