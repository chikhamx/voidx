# TUI 模块代码审查

**模块**: `src/voidx/ui/tui/`
**规模**: 6 文件, 2052 行
**测试**: 63 个，全部通过
**日期**: 2026-06-04

## 架构概览

`PureTui` 通过 4 个 Mixin 组合：

| Mixin | 文件 | 职责 |
|-------|------|------|
| `_InputParserMixin` | `parser.py` | 原始字节 → 按键事件分发 |
| `_InputEditorMixin` | `input.py` | 行编辑、光标移动、历史 |
| `_PanelManagerMixin` | `panels.py` | 命令面板、附件面板、选择覆盖层 |
| `_TerminalRendererMixin` | `renderer.py` | ANSI 渲染、光标定位、状态栏 |

`app.py` 定义 `PureTui` 主类和公共 API，`helpers.py` 提供纯函数工具。

---

## Critical

### C1. `_read_input_raw` 的 stdin reader 生命周期管理脆弱

**文件**: `parser.py:19-35`

`loop.add_reader` 的回调只设置一次 future。如果 `os.read` 返回 0 字节（终端关闭后内核缓冲区仍有残留），`data or b"\x04"` 会触发退出。更关键的是 `remove_reader` 在 `finally` 中执行，如果事件循环在同一轮迭代中重新注册了同 fd 的 reader，可能误删。

**建议**: 使用 `asyncio.StreamReader` 包装 stdin fd，让 asyncio 负责生命周期管理。

### C2. `_flush_committed` 在 `invalidate` 中同步调用，可能阻塞事件循环

**文件**: `app.py:361-411`

`invalidate()` → `_flush_committed()` → `dock.tree.render(width)` 遍历整棵树渲染所有行。长会话中树很大时，这个同步操作会阻塞事件循环，导致输入响应延迟。`invalidate` 在 dock 的 `refresh` 回调中被频繁调用。

**建议**: 缓存 `tree.render` 结果，只在树标记 dirty 时重新渲染；或限制 `_flush_committed` 的单次刷新量。

---

## High

### H1. `PureTui.__init__` 中 40+ 个实例变量，状态管理脆弱

**文件**: `app.py:67-156`

约 40 个实例变量分散在 8 个逻辑分组中。Mixin 方法通过 `self.xxx` 直接访问，没有封装。变量名拼写错误不会在初始化时被捕获，只会在运行时抛 `AttributeError`。

**建议**: 将相关状态提取为独立 dataclass（如 `InputState`、`ChoiceState`、`RenderState`），Mixin 通过 `self._input.x` 访问。

### H2. `_insert_text` 在 choice 模式下的快速选择逻辑有 bug

**文件**: `input.py:22-44`

当 `active_choice` 存在时，`_insert_text` 尝试匹配单字符 value 或 label 前缀。第二个循环从 `i > self._choice_selected` 开始搜索，第三个循环从头搜索——如果当前选中项匹配，第三个循环会重复选中它（无变化）。更严重的是，多字节 UTF-8 字符（如中文）输入时 `text.lower()` 可能匹配到意外的 choice。

**建议**: 快速选择逻辑应只匹配 `value` 为单字符的选项（第一个循环已做），后两个循环的 label 前缀匹配应避免重复匹配当前选中项，且应考虑多字节字符的安全性。

### H3. `_render_frame` 中 `dock._needs_clear_screen` 访问了私有属性

**文件**: `renderer.py:48-51`

直接访问 `dock._needs_clear_screen`，违反封装。如果 dock 实现改变，这里会静默失败或抛 `AttributeError`。

**建议**: 在 dock 上提供 `needs_clear_screen` 公共属性或方法。

### H4. `_status_summary` 每帧重复计算所有状态字段

**文件**: `renderer.py:351-424`

每帧调用 14 次 `getattr` + `_call_status`/`_call_bool`/`_call_int`，然后构建 5 个 variant 字符串逐一测试宽度。快速输入时 `invalidate` 可能每秒被调用数十次。

**建议**: 缓存 status summary，只在 `self.status` 变化或宽度变化时重新计算。可用 dirty flag 或版本号。

### H5. `_render_bottom_elements` 中 command output 渲染使用 heuristic 判断行类型

**文件**: `renderer.py:309-322`

对每行 command output 先检查 `ANSI_LINE_PREFIX`，再检查 `[bold]...[/bold]`，都失败后才用 `Text(line, style="dim")`。`[bold]...[/bold]` 的检查是 heuristic，如果行内容恰好以 `[bold]` 开头但不是 Rich markup（如 diff 输出），会被错误地传给 `_text_from_line`。

**建议**: 在 `_render_command_output` 中就区分行类型（用标记或类型字段），而不是在渲染时用 heuristic 判断。

---

## Medium

### M1. `_input_cursor_position` 用 `len()` 而非 `cell_len()` 计算光标位置

**文件**: `input.py:202-208`

`cursor += len(self._input_lines[row]) + 1` 使用字符数而非 cell 宽度。CJK 字符占 2 个 cell，导致返回值与实际显示位置不一致。虽然 `find_attachment_token` 用字符串索引，功能上可能没问题，但语义不一致。

**建议**: 明确 `_input_cursor_position` 返回的是字符索引还是 cell 索引，并在文档中标注。

### M2. `_handle_escape` 不恢复 attachment panel 的 suppressed text

**文件**: `panels.py:34-43`

ESC 关闭 attachment panel 时，`_attachment_panel_suppressed_text = self._get_input_text()` 记录当前文本以防止面板重新弹出。但用户后续修改输入后，suppressed text 永远不会被清除（除非用户选择了文件），导致 attachment panel **永远无法再次弹出**。

**建议**: 在输入文本变化时（`_insert_text`、`_delete_backward` 等）清除 `_attachment_panel_suppressed_text`，或用更精确的匹配条件（如只比较 `@token` 部分）。

### M3. `_render_frame` 中 `shutil.get_terminal_size()` 被调用两次

**文件**: `renderer.py:34,47`

第一次在 L34 获取 `term_height`，TTY 模式下在 L47 再次获取。两次调用之间终端可能 resize，导致不一致。

**建议**: 只调用一次，存入局部变量。

### M4. `ask_choice` 和 `ask_text` 的 timeout 不清理队列

**文件**: `app.py:304-359`

如果 `asyncio.wait_for` 超时，`_choice_queue`/`_text_queue` 中可能有残留值。下次调用时 `queue.get()` 可能立即返回残留值。

**建议**: 在 `finally` 块中 drain 队列：`while not queue.empty(): queue.get_nowait()`。

### M5. `_dispatch_escape` 中 Alt+key 不检查 choice 模式

**文件**: `parser.py:165-168`

Alt+key 直接调用 `_insert_text`，在 choice 模式下会触发快速选择。Alt+H 可能意外选中选项而非插入字符。

**建议**: 与 `_insert_text` 的 choice 模式处理保持一致。

### M6. `_consume` 中异常后 `_last_error` 持续显示

**文件**: `app.py:577-585`

`on_submit` 抛异常时 `_last_error` 被设置，直到下次 submit 才在 L566 清除。当前错误会一直显示在状态栏。

**建议**: 在成功 submit 后立即清除 `_last_error`，而非等到下次 submit。

### M7. `_capture_renderable` 的 buffer 复用缺少注释

**文件**: `renderer.py:114-135`

`seek(0) + truncate(0)` 清空 buffer 的逻辑正确但不够直观，容易在后续维护中被误改。

**建议**: 加注释说明 `seek(0) + truncate(0)` 的语义，或改用更明确的 `getvalue()` + 新建 buffer。

---

## Low

### L1. `_escape_markup` 应使用 `rich.markup.escape`

**文件**: `helpers.py:64-65`

手动转义 `\[` 和 `\]`，与 Rich 内置的 `markup.escape` 行为可能存在边界差异。

**建议**: 直接使用 `rich.markup.escape`。

### L2. `_clip` 和 `_clip_cells` 功能重叠

**文件**: `helpers.py:112-138`

`_clip` 用 `len()` 截断，`_clip_cells` 用 `cell_len()` 截断。`_clip` 不处理宽字符。

**建议**: 审计 `_clip` 调用点，需要 CJK 支持的统一用 `_clip_cells`。

### L3. `_filtered_commands` 的匹配逻辑过于宽松

**文件**: `panels.py:125-130`

`n.lower().startswith(p) or p.startswith(n.lower())` 中，`p.startswith(n.lower())` 意味着输入 `/approval on-failure` 也会匹配 `/approval`，导致命令面板显示不精确。

**建议**: 移除 `p.startswith(n.lower())` 分支，只保留前缀匹配。

### L4. `_render_command_output` 的截断用 `len()` 而非 `cell_len()`

**文件**: `renderer.py:434`

`line[: width - 4] + "…"` 用字符数截断，包含 CJK 字符的行会超出终端宽度。

**建议**: 使用 `_clip_cells` 替代手动截断。

### L5. `_input_display_rows` 中 secret 模式不考虑宽字符

**文件**: `renderer.py:250`

`"*" * len(line)` 用字符数生成星号，但原行可能包含 CJK 字符（占 2 cell），导致星号数量与实际 cell 宽度不匹配，光标定位偏移。

**建议**: 用 `cell_len(line)` 个 `*` 匹配实际显示宽度。

---

## 优先修复建议

| 优先级 | 编号 | 理由 |
|--------|------|------|
| P0 | M2 | 用户可复现的功能 bug：ESC 后 attachment panel 永久失效 |
| P0 | H2 | choice 模式下多字节字符可能意外选中选项 |
| P1 | C2 | 长会话中输入响应延迟 |
| P1 | H1 | 40+ 实例变量是技术债核心来源，建议逐步重构 |
| P1 | M4 | timeout 后队列残留可能导致下次交互行为异常 |
| P2 | H3 | 私有属性访问，重构 dock 时会断裂 |
| P2 | H4 | 性能优化，非紧急 |
| P2 | L4/L5 | CJK 支持缺陷，影响中文用户体验 |
