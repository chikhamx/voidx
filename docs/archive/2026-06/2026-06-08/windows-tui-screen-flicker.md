# Windows TUI 刷屏与选择框闪烁修复

> **Status: Done**

## 问题描述

用户在 Windows 下运行 voidx，LLM 调用 `on_intent` 工具后，终端有几率出现刷屏现象——大量 ANSI escape 序列以可见文本形式输出到终端。

另一个可稳定观察到的现象是：当 classifier / choice prompt 弹出后，每按一次方向键上下移动选择，底部选择框都会闪烁一次。

## 根因

### Root Cause 1: Windows VT processing 未启用

Windows 控制台默认不一定处理 ANSI escape 序列，需要通过 Win32 API `SetConsoleMode` 启用 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`（`0x0004`）标志。voidx 未做此启用。

### 调用链

```
LLM 调用 on_intent
  → dock.start_tool()
    → dock.refresh()
      → PureTui.invalidate()
        → loop.call_soon(_run_scheduled_render)
          → _render_frame()
            → sys.stdout.write("\x1b[{row};1H")  # 绝对定位
            → sys.stdout.write("\x1b[J")          # 清除到末尾
            → sys.stdout.write(ansi)              # 渲染内容
```

`_render_frame` 和 `_flush_committed` 中大量使用 `sys.stdout.write` 直接写 ANSI escape：

| 文件 | 行号 | escape 序列 | 用途 |
|------|------|-------------|------|
| `renderer.py` | 53 | `\x1b[2J\x1b[H` | 清屏+归位 |
| `renderer.py` | 59 | `\x1b[{row};1H` | 绝对定位光标 |
| `renderer.py` | 60 | `\x1b[J` | 清除到屏幕末尾 |
| `renderer.py` | 87 | `\x1b[{term_height};1H` | 定位到屏幕底部 |
| `renderer.py` | 115-117 | 同上 | `_render_input_region` |
| `renderer.py` | 192 | `\x1b[{N}A\x1b[{col}G` | 光标相对移动 |
| `app.py` | 132 | `\x1b[2J\x1b[H` | 启动清屏 |
| `app.py` | 279-280 | `\x1b[{row};1H\x1b[J` | flush 前清除 frame |

在 VT100 未启用的 Windows 上，这些序列被当作普通文本输出，导致刷屏。

### 为什么是"有几率"

- **Windows 10 1511+ / Windows 11 + Windows Terminal**：可能默认启用了 VT100 处理，不刷屏
- **旧版 Windows / 传统 cmd.exe**：VT100 默认关闭，必刷屏
- **ConEmu / Cmder 等**：取决于配置

### `_setup_terminal` 的 Windows 分支

```python
# terminal_mixin.py:36-38
else:
    # Windows: no termios, msvcrt handles raw reads directly
    self._old_termios = None
```

**什么都没做**——没有调用 `SetConsoleMode` 启用 VT100 处理。

### `_flush_committed` 的混合输出问题

```python
# app.py:278-293
sys.stdout.write(f"\x1b[{self._last_frame_start_row};1H")  # 直接写 ANSI
sys.stdout.write("\x1b[J")                                   # 直接写 ANSI
...
self._console.print(rendered)  # 通过 Rich Console 输出
```

在 Windows legacy 模式下：
- `sys.stdout.write` 的 ANSI escape → 被当作垃圾文本输出
- `self._console.print` → Rich 检测到 VT100 未启用，使用 `LegacyWindowsTerm` 通过 Win32 API 定位光标

两种输出方式**不兼容**，进一步加剧刷屏。

### Rich 与裸 ANSI 混用的边界

无论 Rich 在 Windows 上如何处理 legacy console，voidx renderer 都直接通过 `sys.stdout.write()` 输出 ANSI 光标控制序列。因此 voidx 必须自己保证 stdout console mode 支持 VT processing，不能只依赖 Rich 的检测或 fallback。

### Root Cause 2: choice overlay 每次移动都会清屏式重绘

classifier / choice prompt 的方向键路径如下：

```
方向键
  → parser.py::_dispatch_csi()
    → panels.py::_move_choice()
      → invalidate()
        → _render_after_input()
          → _render_input_region()
            → sys.stdout.write(f"\x1b[{start_row};1H")
            → sys.stdout.write("\x1b[J")
            → sys.stdout.write(ansi)
```

`_render_input_region()` 每次都从底部输入区域起始行清除到屏幕末尾，再重画输入框、choice overlay 和状态栏。上下移动选择时，choice overlay 的行数通常不变，只有选中行样式改变；清除整个底部区域会造成可见闪烁，Windows 传统终端更明显。

## 修复方案

### P0：在 `_setup_terminal` 中启用 VT100

在 `terminal_mixin.py` 的 Windows 分支中，调用 `SetConsoleMode` 启用 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`，并在 `_restore_terminal` 中恢复原始模式。

```python
# terminal_mixin.py — Windows 分支
if _sys.platform == "win32":
    import ctypes
    _kernel32 = ctypes.windll.kernel32
    _STD_OUTPUT_HANDLE = -11
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    def _enable_vt_processing() -> int | None:
        """Enable VT100 escape processing on Windows. Returns original console mode."""
        handle = _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        original = mode.value
        if not (original & _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            _kernel32.SetConsoleMode(
                handle,
                original | _ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            )
        return original

    def _restore_console_mode(original: int) -> None:
        handle = _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        _kernel32.SetConsoleMode(handle, original)
```

在 `_setup_terminal` 中调用 `_enable_vt_processing()`，在 `_restore_terminal` 中调用 `_restore_console_mode()`。

实现约束：

- 只在 `sys.platform == "win32"` 时尝试启用。
- 只操作 stdout console handle，当前刷屏来自 stdout renderer。
- `GetConsoleMode()` 失败时视为非 console/不可用，静默跳过，不影响非交互或重定向输出。
- `SetConsoleMode()` 失败时记录 VT 不可用状态，但不抛异常；TUI 后续仍可运行，至少不会因为启动检测失败崩溃。
- `_restore_terminal()` 只在保存过原始 mode 时恢复。

**优点**：改动最小，所有裸 ANSI 光标控制序列自动生效。
**风险**：极旧 Windows 版本不支持 VT processing，仍可能闪烁或显示 ANSI；后续可提示用户切换 Windows Terminal 或降级到行模式。

### P1：choice/classifier 选择移动使用局部重绘

`_move_choice()` 改为标记“choice overlay selection-only render”。当 choice overlay 已经显示、底部区域行数不变时，`_render_after_input()` 调用一个更窄的渲染路径：

1. 重新捕获 `_render_bottom_impl()`。
2. 如果 bottom 行数与上次一致，定位到 `_last_bottom_start_row`。
3. 不使用 `\x1b[J`，逐行写新内容，并用 `\x1b[K` 清理每行尾部。
4. 恢复输入光标位置。
5. 如果行数变化、尚未渲染过 frame、或不在 TTY，则回退到现有 `_render_input_region()`。

这样方向键上下移动只更新固定高度的底部区域，避免每次清除到屏幕末尾。

### 不做：重写 renderer 为 Rich LegacyWindowsTerm

不在本期把所有 `sys.stdout.write(ANSI)` 改成 Rich/Win32 API。这个改动会拆分 Unix/Windows renderer，风险高，而且 P0 能解决 escape 裸输出，P1 能降低最明显的 choice 闪烁。

## 涉及文件

| 文件 | 修改内容 |
|------|----------|
| `src/voidx/ui/tui/terminal_mixin.py` | 添加 VT100 启用/恢复逻辑 |
| `src/voidx/ui/tui/state.py` | 记录 Windows console mode 与 choice 局部重绘状态 |
| `src/voidx/ui/tui/panels.py` | choice 上下移动时标记 selection-only render |
| `src/voidx/ui/tui/renderer.py` | 增加固定行数底部局部重绘路径 |
| `src/voidx/ui/tui/app.py` | `_render_after_input()` 分派 choice 局部重绘 |

## 测试要点

| 测试 | 覆盖点 |
|------|--------|
| `test_windows_enable_virtual_terminal_processing_sets_mode` | 成功读取 stdout console mode 并启用 VT flag |
| `test_windows_enable_virtual_terminal_processing_keeps_existing_mode` | 已启用 VT 时不重复 SetConsoleMode |
| `test_windows_restore_console_mode_restores_original_mode` | restore 使用保存的原始 mode |
| `test_windows_console_mode_helpers_ignore_non_console` | GetConsoleMode / SetConsoleMode 失败时不抛异常 |
| `test_non_windows_terminal_setup_still_uses_termios` | 非 Windows raw mode 行为不变 |
| `test_choice_move_marks_selection_only_render` | choice 上下移动标记局部重绘 |
| `test_choice_move_single_option_does_not_request_render` | 单选项移动不产生无意义重绘 |
| `test_choice_selection_only_render_does_not_clear_to_screen_end` | 固定行数时不用 `\x1b[J`，改用逐行 `\x1b[K` |
| `test_choice_selection_only_render_falls_back_when_row_count_changes` | 行数变化时回退原 `_render_input_region()` |
