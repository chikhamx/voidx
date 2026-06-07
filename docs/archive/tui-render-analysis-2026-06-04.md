# TUI 输入框每次输入顶部重复渲染 — 问题分析

## 现象

在 TUI 模式下，每次在输入框中键入一个字符，终端顶部的 transcript 区域都会被完整地重新渲染一遍，导致视觉上的闪烁和重复绘制。

## 根本原因

**每次按键输入都会触发全量重绘（full re-render），包括整个 transcript 树。**

调用链如下：

```
用户按键 → _process_input() → 返回 needs_render=True
         → _render_frame()
         → _render_impl()
         → dock.tree.render(width)   ← 渲染整个 transcript 树
         → 把所有 tree_lines 逐行转为 Rich Text
         → 捕获为 ANSI 字符串
         → 用绝对定位写入终端（\x1b[start_row;1H + \x1b[J + ansi）
```

## 具体问题点

### 1. 每次按键都全量重绘整个帧

**文件**: `src/voidx/ui/tui.py:198-205`

```python
while self._running:
    data = await self._read_input_raw()
    if self._process_input(data):
        self._render_frame()   # ← 每次按键都调用
```

`_process_input` 对任何输入（包括普通字符插入）都返回 `needs_render=True`，然后 `_render_frame` 被调用。

### 2. `_render_impl` 每次都渲染完整的 transcript

**文件**: `src/voidx/ui/tui.py:1256-1267`

```python
tree_lines = dock.tree.render(width)
visible_tree = tree_lines   # ← 没有任何裁剪！全部行都渲染

for line in visible_tree:
    elements.append(_text_from_line(line))  # ← 逐行转为 Rich Text
```

注释写着 "render all lines; terminal handles scrollback"，但实际上 **没有任何行数限制**。如果 transcript 有 500 行，每次按键都会把 500 行全部渲染。

### 3. `_render_frame` 用绝对定位 + 清屏方式写入

**文件**: `src/voidx/ui/tui.py:389-402`

```python
start_row = max(term_height - frame_rows + 1, 1)
sys.stdout.write(f"\x1b[{start_row};1H")  # 光标跳到帧起始行
sys.stdout.write("\x1b[J")                 # 清除从光标到屏幕底部
sys.stdout.write(ansi)                     # 写入整个帧
```

这意味着每次按键：
- 光标跳到帧顶部
- 清除帧区域
- **重新写入整个 transcript + 输入框**

这就是用户看到的"顶部重复渲染"——整个 transcript 区域在每次按键时都被重新绘制。

### 4. 没有脏区域检测

`_render_impl` 不区分"只有输入框变了"还是"transcript 也变了"。即使只是输入了一个字符，也会把 transcript 部分重新渲染一遍。

## 对比：BottomInputDock 的做法

**文件**: `src/voidx/ui/dock.py:268-291`

```python
body_limit = max((self._console.height or 24) - input_height - 1, 1)
lines = self._tree.render(self._width())
body = Group(*[_text_from_line(line) for line in lines[-body_limit:]])  # ← 只取最后 N 行
```

`BottomInputDock._render` 有行数限制，只渲染终端可见区域内的行。但 `PureTui._render_impl` **没有这个限制**，它渲染了所有行。

## 修复方向

### 方案 1：对 transcript 做行数裁剪（推荐，最小改动）

在 `_render_impl` 中对 `visible_tree` 做行数裁剪，只渲染终端可见区域内的行，类似 `BottomInputDock._render` 的做法：

```python
# 计算输入区域占用的行数
input_area_lines = len(self._input_lines) + 1  # input lines + bottom border
panel_count = len(panel_lines) + (1 if panel_lines else 0)
status_count = len(status_lines)
cmd_output_count = len(cmd_output_lines) + (1 if cmd_output_lines else 0)
separator_count = 1  # top separator

fixed_lines = input_area_lines + panel_count + status_count + cmd_output_count + separator_count
term_height = shutil.get_terminal_size().lines
visible_limit = max(term_height - fixed_lines, 1)
visible_tree = tree_lines[-visible_limit:]  # 只取最后 N 行
```

**优点**: 改动小，效果明显，直接减少渲染行数。
**缺点**: 仍然是全量重绘，只是减少了渲染的行数。

### 方案 2：引入脏区域检测

只有 transcript 内容变化时才重绘 transcript 区域，输入变化只重绘输入区域。

**优点**: 从根本上避免不必要的重绘。
**缺点**: 需要较大的架构改动，需要将帧分为 transcript 和 input 两个独立渲染区域，并维护各自的脏标记。

### 方案 3：增量 ANSI 更新

对比前后两帧的 ANSI 输出，只写入差异部分。

**优点**: 最小化终端写入量。
**缺点**: 实现复杂度高，需要 ANSI 级别的 diff 算法，且容易引入渲染不一致的 bug。

## 问题汇总

| 问题 | 位置 | 影响 |
|------|------|------|
| 每次按键全量重绘 | `tui.py:205` | 所有输入都触发完整帧渲染 |
| transcript 无行数裁剪 | `tui.py:1256-1257` | 500 行 transcript 每次都全部渲染 |
| 无脏区域检测 | `_render_impl` 整体 | 输入变化也重绘 transcript |
| 绝对定位+清屏写入 | `tui.py:396-398` | 视觉上看到整个区域闪烁重绘 |

## 建议

优先实施 **方案 1**（行数裁剪），这是最小改动且效果最明显的修复。后续可考虑方案 2 进一步优化。
