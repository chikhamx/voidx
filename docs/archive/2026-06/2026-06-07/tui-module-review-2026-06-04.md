# TUI 模块代码审查（已归档）

**模块**: `src/voidx/ui/tui/`
**规模**: 8 文件, 2528 行
**日期**: 2026-06-04（原始审查），2026-06-07（归档）

> **Status: Done** — 原未完成项已关闭，处理记录见 `docs/archive/tui-open-issues.md`。

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

### C1. `_read_input_raw` 的 stdin reader 生命周期管理脆弱 ✅ 已修复

→ 见 `docs/archive/tui-open-issues.md`

### C2. `_flush_committed` 在 `invalidate` 中同步调用，可能阻塞事件循环 ✅ 已修复

→ 见 `docs/archive/tui-open-issues.md`

---

## High

### H1. `PureTui.__init__` 中 40+ 个实例变量，状态管理脆弱 ✅ 已修复

→ 见 `docs/archive/tui-open-issues.md`

### H2. `_insert_text` 在 choice 模式下的快速选择逻辑有 bug ✅ 已修复

只匹配单字符 ASCII value，多字节字符直接 return；Alt+key 在 choice 模式下返回 noop。

### H3. `_render_frame` 中 `dock._needs_clear_screen` 访问了私有属性 ✅ 已修复

改为 `dock.consume_clear_screen_request()` 公共方法。

### H4. `_status_summary` 每帧重复计算所有状态字段 ✅ 已修复

→ 见 `docs/archive/tui-open-issues.md`

### H5. `_render_bottom_elements` 中 command output 渲染使用 heuristic 判断行类型 ✅ 已关闭

→ 见 `docs/archive/tui-open-issues.md`

---

## Medium

### M1. `_input_cursor_position` 用 `len()` 而非 `cell_len()` 计算光标位置 ✅ 已关闭

→ 见 `docs/archive/tui-open-issues.md`

### M2. `_handle_escape` 不恢复 attachment panel 的 suppressed text ✅ 已修复

`_clear_attachment_suppression_on_edit()` 在所有编辑操作中清除 suppressed text。

### M3. `_render_frame` 中 `shutil.get_terminal_size()` 被调用两次 ✅ 已关闭

→ 见 `docs/archive/tui-open-issues.md`

### M4. `ask_choice` 和 `ask_text` 的 timeout 不清理队列 ✅ 已修复

`ask_choice` 和 `ask_text` 都在 `finally` 中调 `_drain_queue`，且进入时也先 drain。

### M5. `_dispatch_escape` 中 Alt+key 不检查 choice 模式 ✅ 已修复

parser.py 在 choice 模式下 Alt+key 返回 noop。

### M6. `_consume` 中异常后 `_last_error` 持续显示 ✅ 已修复

submit 时立即清除 `_last_error`（app.py:505）。

### M7. `_capture_renderable` 的 buffer 复用缺少注释 ✅ 已修复

→ 见 `docs/archive/tui-open-issues.md`

---

## Low

### L1. `_escape_markup` 应使用 `rich.markup.escape` ✅ 已修复

已改为 `return rich_escape(text)`。

### L2. `_clip` 和 `_clip_cells` 功能重叠 ✅ 已修复

`_clip` 已删除，只保留 `_clip_cells`。

### L3. `_filtered_commands` 的匹配逻辑过于宽松 ✅ 已修复

只保留 `n.lower().startswith(p)`，移除了反向匹配。

### L4. `_render_command_output` 的截断用 `len()` 而非 `cell_len()` ✅ 已修复

原始截断代码已重构移除。

### L5. `_input_display_rows` 中 secret 模式不考虑宽字符 ✅ 已修复

改为 `"*" * cell_len(line)`（renderer.py:267）。

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
