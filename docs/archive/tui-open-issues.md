# TUI 模块代码审查 — 未完成项

> **Status: Done**

**模块**: `src/voidx/ui/tui/`
**日期**: 2026-06-04（原始审查），2026-06-07（关闭）

从 `docs/reviews/tui-module-review.md` 中提取的问题已重新核对并处理。仍需要长期演进的点不再作为 open issue 保留。

## 处理结果

| 编号 | 原问题 | 处理结果 |
|------|--------|----------|
| C1 | `_read_input_raw` 的 stdin reader 生命周期管理脆弱 | 已改为持久 `asyncio.StreamReader` 包装 stdin fd 的 dup，并在 TUI 退出时关闭 transport。 |
| C2 | `_flush_committed` 在 `invalidate` 中同步调用，可能阻塞事件循环 | `invalidate()` 现在只标记状态并通过事件循环 `call_soon` 合并刷新；重复 invalidate 会被 coalesce。`OutputTree.render()` 已有缓存/增量渲染。 |
| H1 | `PureTui.__init__` 中大量实例变量，状态管理脆弱 | 已将运行时状态拆入 dataclass state 对象，并保留旧字段访问兼容层，便于后续逐步把 mixin 改为直接访问分组状态。 |
| H4 | `_status_summary` 每帧重复计算所有状态字段 | 已增加 width + snapshot 缓存；`invalidate()` 标记 dirty，同一宽度同一状态重复调用复用缓存。 |
| H5 | command output 渲染使用 `[bold]` heuristic 判断行类型 | 原 `[bold]...[/bold]` heuristic 已不存在。当前渲染路径使用 ANSI marker 和 escaped Rich markup；此项按原描述关闭。 |
| M1 | `_input_cursor_position` 用 `len()` 而非 `cell_len()` | 当前 docstring 已明确返回 logical character offset，且调用方 `find_attachment_token()` 使用字符索引；不作为 bug 修复。 |
| M3 | `_render_frame` 中 `shutil.get_terminal_size()` 被调用两次 | 已由现有代码关闭：TTY 分支第二处是 fallback，不会在正常路径重复调用。 |
| M7 | `_capture_renderable` 的 buffer 复用缺少注释 | 已由现有代码关闭：buffer 复用和清空逻辑已有注释。 |

## 验证

- `tests/test_pure_tui.py::test_read_input_raw_uses_stream_reader_for_pipe_bytes`
- `tests/test_pure_tui.py::test_read_input_raw_returns_ctrl_d_on_stdin_eof`
- `tests/test_pure_tui.py::test_invalidate_coalesces_render_until_next_loop`
- `tests/test_pure_tui.py::test_status_summary_reuses_cache_until_marked_dirty`
- `tests/test_pure_tui.py::test_pure_tui_groups_runtime_state`
