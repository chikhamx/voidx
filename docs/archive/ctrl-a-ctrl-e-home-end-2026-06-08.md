# Ctrl+A/E 与 Home/End 行首行尾

Date: 2026-06-08

> **Status: Done**

## Goal

增加 `Ctrl+A` 跳转当前行行首、`Ctrl+E` 跳转当前行行尾快捷键，与 Emacs/readline 惯例一致。

同时补齐常见终端发送的 Home/End escape sequence，避免只有部分终端的物理 Home/End 键可用。

## Current State

关键文件：

- `src/voidx/ui/tui/parser.py` — `_dispatch_key()` 按字节码分发 Ctrl 快捷键，已处理 `0x03`(C-c)、`0x04`(C-d)、`0x16`(C-v)。
- `src/voidx/ui/tui/input.py` — `_cursor_home()` / `_cursor_end()` 已实现，Home/End 物理键通过 CSI 路径调用。

现状：

- `0x01`(C-a) 和 `0x05`(C-e) 未被处理，落入 `_dispatch_key()` 末尾 `return (1, None)` 被静默忽略。
- Home/End 物理键只处理了 CSI `ESC[H` / `ESC[F`，缺少 `ESC[1~` / `ESC[4~`、`ESC[7~` / `ESC[8~`、SS3 `ESC O H` / `ESC O F` 等常见变体。
- `_cursor_home()` / `_cursor_end()` 的语义是当前编辑行的行首/行尾，不跨多行输入跳到整段文本开头/结尾。

## Design

### Ctrl+A / Ctrl+E

在 `_dispatch_key()` 中 C-d(`0x04`) 判断之后、Enter(`0x0A`) 之前，增加两个字节码判断：

```python
# Ctrl+A: home
if first == 0x01:
    self._cursor_home()
    return (1, None)

# Ctrl+E: end
if first == 0x05:
    self._cursor_end()
    return (1, None)
```

### Home / End 序列兼容

在 `_dispatch_csi()` 中补齐 tilde 结尾的常见 Home/End 序列：

| 序列 | 含义 |
|------|------|
| `ESC[1~` | Home |
| `ESC[4~` | End |
| `ESC[7~` | Home |
| `ESC[8~` | End |

在 `_dispatch_escape()` 中补齐 SS3 Home/End：

| 序列 | 含义 |
|------|------|
| `ESC O H` | Home |
| `ESC O F` | End |

已有 `ESC[H` / `ESC[F` 继续走现有 CSI final byte 路径。

### 行为语义

- 快捷键和物理键都调用已有 `_cursor_home()` / `_cursor_end()`。
- 多行输入时只移动当前编辑行的 `cursor_col`，不改变 `cursor_row`。
- choice prompt 激活时，已有 `_cursor_home()` / `_cursor_end()` 保护会让这些按键不改变输入光标。

### 无冲突分析

| 快捷键 | 字节码 | 现有用途 | 冲突？ |
|--------|--------|----------|--------|
| C-a | `0x01` | 未使用 | ✗ |
| C-e | `0x05` | 未使用 | ✗ |

- 两个字节码均 < `0x20`，不会命中可打印 ASCII 分支（`0x20 <= first <= 0x7E`）。
- `_cursor_home()` / `_cursor_end()` 已有 choice 面板保护（`if self._active_choice is not None: return`），不会在选项面板中误跳。
- Home/End tilde 序列不与 PageUp/PageDown(`ESC[5~`/`ESC[6~`) 或 Delete(`ESC[3~`) 冲突。

### 不涉及的改动

- 无需修改 `_cursor_home()` / `_cursor_end()` 实现。
- 无需修改 Windows 输入映射（Windows 终端会将 C-a/C-e 作为原始字节 `0x01`/`0x05` 传递，走同一 `_dispatch_key` 路径）。

## Affected Files

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/tui/parser.py` | `_dispatch_key()` 增加 `0x01`/`0x05` 两个分支；`_dispatch_csi()` / `_dispatch_escape()` 补齐 Home/End 序列 |
| `tests/test_pure_tui.py` | 增加 Ctrl+A/E、Home/End 序列和 choice prompt 保护测试 |

## Test Plan

| 测试 | 覆盖点 |
|------|--------|
| `test_ctrl_a_moves_to_current_line_start` | `0x01` 移动到当前行行首 |
| `test_ctrl_e_moves_to_current_line_end` | `0x05` 移动到当前行行尾 |
| `test_ctrl_a_e_ignore_active_choice` | choice prompt 激活时 Ctrl+A/E 不改变输入光标 |
| `test_home_end_escape_sequences_move_cursor` | `ESC[H`、`ESC[F`、`ESC[1~`、`ESC[4~`、`ESC[7~`、`ESC[8~`、`ESC O H`、`ESC O F` 都调用同一行首/行尾语义 |
| `test_home_end_escape_sequences_keep_multiline_row` | 多行输入中 Home/End 只改变当前行列，不改变 `cursor_row` |

## Risks

- **终端差异** — 部分终端（如 screen/tmux 默认配置）可能将 C-a 作为前缀键拦截。这是终端层面的问题，不影响代码实现。用户可通过 `prefix C-a` 或修改终端配置解决。
