# TUI 流式输出闪烁优化 — 技术设计文档

> **Status: Done**

## Context

voidx TUI 在 LLM 流式输出长文本时屏幕严重闪烁。根本原因是 `_render_frame` 每次都先清屏再重绘，而流式输出时这个操作频率极高（每秒最多 20 次），清屏与重绘之间的空白帧肉眼可见。

## Goals and Non-Goals

### Goals

- 消除流式输出时的屏幕闪烁
- 保持流式输出的响应性（用户感知到文字在实时增长）
- 不引入额外的渲染延迟

### Non-Goals

- 不重写整个 TUI 渲染架构
- 不引入外部依赖（如 curses、textual）
- 不改变 dock / tree 的数据模型

## 根因分析

### 闪烁链路

```
LLM 流式输出 token
  → StreamingRenderer.feed_text()        # 每 50ms 刷新一次 (FLUSH_INTERVAL=0.05)
    → dock.set_stream()
      → dock.refresh()
        → PureTui.invalidate()
          → loop.call_soon(_run_scheduled_render)  # 零延迟调度
            → _render_frame()
              → sys.stdout.write("\x1b[J")   # ① 清除光标到屏幕底部
              → sys.stdout.write(ansi)       # ② 重绘整个帧
              → sys.stdout.flush()           # ③ 刷新到终端
```

在 ① 和 ② 之间，帧区域是空白的。这个间隙在短内容时（<5ms）人眼感知不到，但长内容重绘慢（20-50ms），空白帧持续时间拉长，闪烁明显。

### 三个叠加因素

| 因素 | 位置 | 影响 |
|------|------|------|
| **`\x1b[J` 清屏再重绘** | `src/voidx/ui/tui/render_frame.py` `_render_frame` | 每帧都先擦后画，中间有空白帧 |
| **刷新频率过高** | `src/voidx/ui/output/console/streaming.py` `FLUSH_INTERVAL=0.05` | 每 50ms 触发一次全帧渲染 |
| **invalidate 无节流** | `src/voidx/ui/tui/app.py` `loop.call_soon` | 零延迟调度，不同事件循环 tick 间不合并 |

### 为什么短输出不闪

短内容重绘快（<5ms），清屏到重绘的间隙人眼感知不到。长内容重绘慢（20-50ms），间隙拉长，闪烁明显。

## Architecture

### 当前渲染策略

```
每次 invalidate:
  1. 移动光标到帧起始行
  2. \x1b[J 清除到屏幕底部          ← 闪烁根源
  3. 写入完整帧 ANSI
  4. flush
```

### 改后渲染策略

```
每次 invalidate:
  1. 计算前后帧差异（diff）
  2. 只更新变化的行，跳过未变化的行
  3. 不使用 \x1b[J（差异渲染时）
  4. 仅在帧高度变化时追加/截断行
  5. flush
```

## Data Model

### 新增：帧缓冲区

在 `RenderState` 中新增字段，缓存上一帧的渲染结果，用于差异对比。

```
RenderState
├── (现有字段...)
├── prev_frame_lines: list[str]       # 上一帧按行拆分的 ANSI 内容
└── prev_frame_start_row: int         # 上一帧起始行号
```

### 新增：渲染统计

```
RenderState
├── (现有字段...)
├── render_stats: RenderStats | None  # 最近一次渲染的统计信息
```

```
RenderStats
├── total_lines: int        # 帧总行数
├── changed_lines: int      # 实际写入的行数
├── render_ms: float        # 渲染耗时
└── strategy: str           # "diff" | "full"
```

## API Contract

### `_render_frame` 改造

**Before:**

```python
def _render_frame(self) -> None:
    # ... 计算 ansi ...
    sys.stdout.write(f"\x1b[{start_row};1H")
    sys.stdout.write("\x1b[J")          # 清屏
    sys.stdout.write(ansi)              # 全量写入
    sys.stdout.flush()
```

**After:**

```python
def _render_frame(self) -> None:
    # ... 计算 ansi ...
    new_lines = ansi.splitlines()
    prev_lines = self._prev_frame_lines
    prev_start = self._prev_frame_start_row

    if prev_lines is not None and start_row == prev_start:
        self._render_diff(start_row, prev_lines, new_lines)
    else:
        # 帧位置变化（滚动等），回退到全量渲染
        self._render_full(start_row, new_lines)

    self._prev_frame_lines = new_lines
    self._prev_frame_start_row = start_row
    sys.stdout.flush()
```

### `_render_diff` 新增方法

- **Signature**: `_render_diff(self, start_row: int, prev_lines: list[str], new_lines: list[str]) -> None`
- **Behavior**: 逐行对比 `prev_lines` 和 `new_lines`，只写入变化的行。对每行使用 `\x1b[{row};1H` 定位 + `\x1b[K` 清行 + 写入内容。帧末尾行数变化时，追加新行或用 `\x1b[J` 清除多余行。
- **Fallback**: 当变化行数超过总行数 80% 时，回退到全量渲染（差异渲染反而更慢）。

### `_render_full` 提取方法

- **Signature**: `_render_full(self, start_row: int, lines: list[str]) -> None`
- **Behavior**: 现有的清屏 + 全量写入逻辑，从 `_render_frame` 中提取出来。

### FLUSH_INTERVAL 调整

| 位置 | Before | After |
|------|--------|-------|
| `src/voidx/ui/output/console/streaming.py` | `0.05` (50ms, 20fps) | `0.1` (100ms, 10fps) |

10fps 对流式文字输出足够流畅，同时将渲染负载减半。

### `invalidate` 节流

**Before:**

```python
loop.call_soon(self._run_scheduled_render)
```

**After:**

```python
loop.call_later(0.016, self._run_scheduled_render)  # ~60fps 上限
```

`call_later(0.016)` 将渲染延迟到 16ms 后执行，同一窗口内的多次 `invalidate` 会被 `_render_scheduled` 标志合并。相比 `call_soon` 的零延迟，这避免了同一事件循环 tick 内连续渲染。

### 缓冲区失效条件

以下情况需要清空 `prev_frame_lines`，强制下次全量渲染：

| 条件 | 位置 |
|------|------|
| 终端尺寸变化 | `src/voidx/ui/tui/render_frame.py` `_render_frame` 入口检测 |
| `_flush_committed` 刷新了已提交内容 | `src/voidx/ui/tui/app.py` `_flush_committed` 末尾 |
| `consume_clear_screen_request` | `src/voidx/ui/tui/render_frame.py` `_render_frame` 清屏分支 |
| `_make_room_for_frame` 滚动 | `src/voidx/ui/tui/render_frame.py` `_render_frame` 滚动分支 |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 差异渲染时行数对不上 | 回退到全量渲染 |
| diff 计算异常 | 捕获异常，回退到全量渲染 |
| 终端不支持精确光标定位 | 不影响，所有现代终端都支持 `\x1b[{row};1H` |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 差异渲染（逐行 diff） | 双缓冲（先写隐藏区域再滚动） | 差异渲染实现简单，不依赖终端扩展功能；双缓冲需要 alternate screen buffer，会破坏滚动回溯 |
| FLUSH_INTERVAL 0.05→0.1 | 保持 0.05 | 10fps 对文字流式输出足够，减半渲染负载 |
| `call_later(0.016)` 替代 `call_soon` | 用 `asyncio.sleep` 节流 | `call_later` 是事件循环原生 API，不引入协程开销 |
| 变化行超 80% 回退全量 | 始终差异渲染 | 差异渲染在大量变化时比全量渲染更慢（逐行定位 + 清行开销），80% 是经验阈值 |
| 不引入 alternate screen | 使用 alternate screen + 双缓冲 | alternate screen 会丢失滚动回溯，用户无法用鼠标回看历史输出 |

## Open Questions

- [ ] 差异渲染的行对比是否需要考虑 ANSI 转义序列的等价性（如颜色码顺序不同但效果相同）？当前方案按原始字符串对比，ANSI 码不同就算变化。实际中同一行内容不变时 ANSI 码也不会变，所以应该没问题。
- [ ] 是否需要在 Windows Terminal / ConEmu / Windows Console Host 上分别测试？Windows Console Host 的 ANSI 支持可能有限。
- [ ] `RenderStats` 是否需要暴露给用户（如 `--debug-render` 开关）？当前仅用于开发调试。
