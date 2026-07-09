# 粘贴文本 MarkupError 修复

> **Status: Done**

## Context

用户在 TUI 中粘贴包含 `[/]` 或 `[/something]` 的文本并按回车提交时，rich 库抛出 `MarkupError`，导致渲染崩溃。根因是 tree 渲染器在计算行可见长度时，对包含 `ANSI_LINE_PREFIX` 标记的粘贴段行裸调 `Text.from_markup(line).plain`，rich 将粘贴内容中的 `[/]` 误解析为 markup 关闭标签。

## Goals and Non-Goals

### Goals

- 消除粘贴含方括号文本时的 `MarkupError` 崩溃
- 确保 style wrapper 的闭合标签 `[/]` 不泄漏为可见文本
- 保持非粘贴段（已转义）的渲染行为不变

### Non-Goals

- 重构整个 markup/ANSI 混合渲染管线
- 修改 `formatting.py` 中的 `_text_from_line`（已有 ANSI 感知逻辑，无需改动）

## Architecture

### 数据流

```
用户粘贴文本
  → start_turn(text)                          [dock/app.py]
  → split_pasted_segments(text)               [dock/formatting.py]
  → _render_turn_segments(segments)           [dock/app.py]
      ├─ 非粘贴段: escape(line)               → 安全，Text.from_markup 不会误解析
      └─ 粘贴段: _markdown_lines → _ansi_line → ANSI_LINE_PREFIX + markdown行
  → tree.new_node(header=..., body_lines=...)
  → tree.render(width)                        [output/tree.py]
      → _walk_render → _full_width_row(line)  ← 崩溃点（修复前）
      → _full_width_row 调用 Text.from_markup(line).plain 计算可见长度
         → 粘贴段的 line 含 ANSI_LINE_PREFIX + markdown行
         → markdown行中的 [/] 被 rich 误解析为关闭标签
         → MarkupError!
```

### 修复方案

在 `tree.py` 中新增两个函数：

1. **`_visible_len(line)`** — ANSI 感知的可见长度计算
   - 无 `_ANSI_LINE_PREFIX` 标记：回退到 `cell_len(Text.from_markup(line).plain)`（原有行为）
   - 有标记：标记前用 `Text.from_markup`（理解 rich 标签），标记后用 `Text.from_ansi`（不解析 markup 标签，`[/]` 作为字面文本）

2. **`_wrap_full_width(line, padding, style)`** — 安全的行包装
   - 无标记：`f"[{style}]{line}{padding}[/]"`（原有行为）
   - 有标记：`f"[{style}]{line[:marker]}{padding}[/]{line[marker:]}"` — 闭合标签和 padding 放在标记**之前**，使 `_text_from_line` 的 `Text.from_markup(line[:marker])` 能正确消费闭合标签

### 替换的调用点

| 函数 | 位置 | 修复前 | 修复后 |
|------|------|--------|--------|
| `_full_width_row` | tree.py | `cell_len(Text.from_markup(line).plain)` | `_visible_len(line)` + `_wrap_full_width` |
| `_permission_row` | tree.py | `cell_len(Text.from_markup(line).plain)` | `_visible_len(line)` + `_wrap_full_width` |
| `_pad_diff_background_row` | tree.py | `cell_len(Text.from_markup(line).plain)` | `_visible_len(line)` |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 粘贴内容含 `[/]` | `Text.from_ansi` 不解析 markup，作为字面文本显示 |
| 粘贴内容含 `[/notopened]` | 同上，不崩溃 |
| 粘贴内容含 `[bold]...[/bold]` | 同上，方括号作为字面文本，不应用 rich 样式 |
| 非粘贴段含方括号 | `escape()` 已转义，`Text.from_markup` 安全 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 在 `tree.py` 中重复定义 `_ANSI_LINE_PREFIX` | 从 `formatting.py` 导入 | `tree.py` 是底层模块（零 `voidx.*` 导入），从 `formatting.py` 导入会创建循环依赖（`dock/__init__` → `dock/app` → `tree`） |
| 闭合标签放在 `_ANSI_LINE_PREFIX` 之前 | 放在行末（原有位置） | 放在行末时 `_text_from_line` 用 `Text.from_ansi` 处理 ANSI 段后的内容，不解析 `[/]`，导致闭合标签泄漏为字面文本 |
| 不修改 `_text_from_line` | 在 `_text_from_line` 中添加 try/except | `_text_from_line` 已有正确的 ANSI 感知逻辑，崩溃发生在上游的 `_full_width_row`，应从源头修复 |

## Open Questions

- [x] 无

## 补充：clarify 输入框粘贴 token 未展开

### 问题

主输入框提交时调用 `_expand_registered_tokens` 把 `[Pasted text #N ...]` token 展开为实际内容。clarify 输入框（`_submit_text_prompt`）直接取 `self._get_input_text()`，不调用展开函数，导致用户在 clarify 中粘贴的文本以字面 token `[Pasted text #1 15 chars]` 传给 agent，而非实际内容。

### 修复

`src/voidx/ui/tui/panels.py` `_submit_text_prompt`：

```python
# 修复前
value = self._get_input_text()
# 修复后
value = self._expand_registered_tokens(self._get_input_text())
```

### 测试

`tests/test_ui/tui/test_tui_input_handling.py` `test_text_prompt_expands_paste_tokens`：在 text prompt 模式下注册粘贴 token，提交后验证队列中收到的是实际内容而非字面 token。
