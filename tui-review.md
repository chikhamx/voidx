# PureTui (`src/voidx/ui/tui.py`) 代码审查报告

> 审查日期：2026-06-04（基于 2026-06-03 初版，经代码验证后修订）
> 审查范围：`tui.py` 全文（~1730 行）

---

## 概览

`PureTui` 是一个自包含的终端 UI 实现：手动 ANSI 渲染 + raw stdin 输入 + Rich 文本捕获。整体设计扎实，输入解析逻辑尤其精细（UTF-8 截断保护、CSI 序列解析、kitty keyboard protocol 支持）。以下是按严重程度分类的发现，每项均经代码验证。

---

## 🔴 CRITICAL

### C1. `_read_input_raw` 未处理 stdin 关闭，导致无限循环

**行**: 533-548

```python
async def _read_input_raw(self) -> bytes:
    ...
    return os.read(self._stdin_fd, 4096)
```

当 stdin 关闭（如管道断开、父进程退出），`os.read` 返回 `b""`。`_process_input` 对空 bytes 返回 `False`，主循环（202-208行）不退出，无限调用 `_read_input_raw`。

对比非 tty 路径的 `_read_input_line`（550-555行）已正确处理此情况——空行时返回 `b"\x04"` 触发 exit。

**修复**: 在 `_read_input_raw` 返回后、调用 `_process_input` 前，检查空 bytes 并触发退出：

```python
data = await self._read_input_raw()
if self._tty and not data:
    self._request_exit()
    break
if self._process_input(data):
    self._render_after_input()
```

---

### C2. `_read_input_raw` 的 `add_reader` 存在竞态窗口

**行**: 537-543

```python
loop.add_reader(self._stdin_fd, lambda: fut.set_result(None) if not fut.done() else None)
try:
    await fut
finally:
    loop.remove_reader(self._stdin_fd)
return os.read(self._stdin_fd, 4096)
```

`fut.set_result(None)` 到 `os.read()` 之间，如果事件循环再次触发 reader callback（stdin 还有数据），`fut.done()` 已为 `True`，callback 被忽略。`remove_reader` 在 `finally` 中移除 reader，后续数据不会被读取，直到下一次 `_read_input_raw` 重新注册。

**实际影响评估**: 窗口极短（同一事件循环 tick 内），且 `os.read(fd, 4096)` 会一次性读走内核缓冲区中的所有可用字节。快速输入场景下不太可能丢按键。但在极端情况下（内核缓冲区分段到达）理论上可能丢失。

**建议**: 在 reader callback 中直接读取数据到 buffer，而非只设置 future。这样即使多次触发也不会丢数据。

---

## 🟠 HIGH

### H1. `_capture_renderable` 每次创建新 `Console` 实例

**行**: 454-468

```python
def _capture_renderable(self, renderable: object, width: int) -> str:
    buf = io.StringIO()
    cap = Console(file=buf, force_terminal=True, color_system="truecolor",
                   width=width + 2, height=self._console.height)
    cap.print(renderable)
    ansi = buf.getvalue()
    return ansi.rstrip("\n")
```

每次渲染帧调用 2 次（主渲染 + bottom 渲染）。streaming 场景下帧率可达 20fps，每秒创建 40 个短命 `Console` + `StringIO` 对象，GC 压力显著。

**建议**: 缓存专用 capture Console 实例，每帧只重置 buf（`buf.seek(0); buf.truncate()`）。需注意 width 变化时重建。

### H2. `_attachment_matches` 无缓存，一次按键触发多次文件系统扫描

**行**: 1103-1107

```python
def _attachment_matches(self) -> list[FileCandidate]:
    token = self._attachment_token()
    if token is None:
        return []
    return list_file_candidates(self.status.workspace, token.query, limit=8)
```

调用链：`_clamp_attachment_selection` → `_attachment_selectable_count` → `_attachment_matches`；`_accept_attachment_panel_selection` → `_attachment_matches`；`_render_attachment_panel` → `_attachment_matches`。一次按键可能触发 2-3 次文件系统扫描。

**建议**: 缓存结果，仅在 query 文本变化时重新扫描。

### H3. `_position_input_cursor` 不考虑长行换行和命令输出

**行**: 489-496

```python
lines_up = (
    len(self._input_lines)
    - self._cursor_row
    + 1
    + len(panel_lines)
    + (1 if panel_lines else 0)
    + len(status_lines)
)
```

两个问题：

1. `len(self._input_lines)` 是逻辑行数，不考虑长行超过终端宽度时的显示换行。长输入行时光标定位偏移，IME 候选窗口位置也会错。
2. `cmd_output_lines` 没有计入 `lines_up`，但 `_render_impl` 的 `fixed_lines` 计算中是计入的，渲染和光标定位不一致。

**建议**: 计算输入行的实际显示行数（考虑换行），并将 `cmd_output_lines` 纳入 `lines_up` 计算。

### H4. `ask_choice` / `ask_text` 无超时

**行**: 336, 359

```python
return await self._choice_queue.get()  # 无超时
return await self._text_queue.get()    # 无超时
```

如果代码 bug 导致输入不再入队，这些方法会永久挂起，阻塞整个 agent 循环。注意"终端失去焦点"不会导致此问题——输入处理循环是独立运行的。

**建议**: 添加可选的超时参数，默认值如 300 秒。

---

## 🟡 MEDIUM

### M1. `_status_summary` 用 `len()` 而非显示宽度

**行**: 1511

```python
if len(summary) <= width:
    return summary
```

`len(summary)` 计算字符数，不是终端显示宽度。包含 CJK 字符或 emoji 时，显示宽度大于 `len()` 返回值，导致状态栏溢出。

**建议**: 使用 `cell_len()` 代替 `len()`。

### M2. `PureTui.__init__` 中 `asyncio.Queue()` 在无事件循环时创建

**行**: 127, 135

```python
self._choice_queue: asyncio.Queue[str | None] = asyncio.Queue()
self._text_queue: asyncio.Queue[str | None] = asyncio.Queue()
```

Python 3.10+ 允许在无事件循环时创建 `Queue`，但内部 `_loop` 属性为 `None`，首次使用时才获取当前事件循环。如果实例化和使用在不同的事件循环中（测试场景），可能出问题。

**建议**: 延迟到 `run()` 中创建 Queue。

### M3. `run()` 的 finally 块中 `_dump_transcript_log` 前置代码可能抛异常

**行**: 212

```python
if self._tty:
    _dump_transcript_log(Path(self.status.workspace), dock.tree)
```

`_dump_transcript_log` 内部有 try/except（1670行），但 `Path(self.status.workspace)` 在其调用之前。如果 `self.status` 为 None 或 `workspace` 属性不存在，会抛异常。在 finally 块中抛异常会掩盖原始异常。

**建议**: 包裹在 try/except 中。

### M4. `_do_submit` 中 `/paste` 特殊处理硬编码

**行**: 1003

```python
if stripped == "/paste":
    self._record_history(text)
    self._clear_input()
    self.paste_clipboard_image()
    return True
```

`/paste` 在提交阶段被拦截，不走正常的 slash command 路径。如果未来有更多需要 UI 交互的命令，这种模式不可扩展。

**建议**: 将 UI 交互命令的拦截逻辑统一到一个注册机制中。

### M5. `SubmitHandler` 返回值语义不明

**行**: 1280, 1301-1303

```python
keep_running = await self._current_submit_task
...
if not keep_running:
    self._exit_requested = True
    self._exit_app()
    return
```

`on_submit` 返回 `False` 意味着应用退出，但 `SubmitHandler = Callable[[str], Awaitable[bool]]` 的类型标注没有文档说明这个语义。

**建议**: 在 `SubmitHandler` 的文档字符串中明确说明返回值的语义。

### M6. `_consume` 中 `CancelledError` 处理的 `_submit_cancel_requested` 重置时机

**行**: 1281-1284, 1298

```python
except asyncio.CancelledError:
    if not self._submit_cancel_requested:
        raise
    keep_running = True
...
finally:
    self._submit_cancel_requested = False
```

`_submit_cancel_requested` 在 `finally` 中重置。当前代码在单线程 asyncio 中是安全的（`_handle_interrupt` 同步设置标志并 cancel task，`CancelledError` 在 await 点抛出），但如果未来引入嵌套 cancel 场景，`finally` 中的重置可能读到过期值。

**建议**: 在 `except CancelledError` 分支中立即重置 `_submit_cancel_requested`，而非延迟到 `finally`，确保一一对应。

---

## 🔵 LOW

### L1. `PureTui` 类过大（~1600 行），职责过多

类内包含：输入解析、行编辑、历史管理、提交逻辑、渲染、选择面板、命令面板、附件面板、鼠标处理、状态栏。建议拆分为：

- `InputEditor` — 行编辑 + 光标 + 历史
- `PanelManager` — 命令面板 + 附件面板 + 选择面板
- `TerminalRenderer` — ANSI 渲染 + 光标定位
- `PureTui` — 编排以上三者

### L2. `_input_history` 无上限

**行**: 966

```python
self._input_history.append(stripped)
```

长时间运行的 session 会积累无限历史。

**建议**: 限制为最近 1000 条。

### L3. `_command_output_lines` 限制 500 行但无总大小限制

**行**: 272-273

```python
if len(self._command_output_lines) > 500:
    self._command_output_lines = self._command_output_lines[-500:]
```

500 行 × 每行可能很长 = 潜在的大量内存占用。

**建议**: 添加总字节数限制。

### L4. `_filtered_commands` 每次调用遍历全量命令列表

**行**: 1119-1124

命令列表 72 项，每次按键都线性扫描。性能影响可忽略，但可以用前缀树优化。

### L5. `show_transient_output` 中创建临时 `Console` 实例

**行**: 247-254

与 H1 同类问题，每次调用创建新 Console。

### L6. `_escape_markup` 转义不完整

**行**: 1674-1675

```python
def _escape_markup(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
```

只转义了 `\`、`[`、`]`。Rich 的 `Console.print` 默认不解释花括号，当前使用场景不会触发问题，但作为通用转义函数不够完整。

**建议**: 使用 Rich 的 `escape()` 函数，或补充 `{`/`}` 的转义。

---

## ℹ️ INFO

### I1. 输入解析设计精良

UTF-8 截断保护（`_pending_bytes`）、CSI 序列不完整时保存、kitty keyboard protocol 支持、Alt+Enter 换行——这些细节处理得很到位。

### I2. 渲染策略合理

使用 Rich capture → ANSI string → 直接写入，避免了 prompt_toolkit 的复杂性，同时保证了 IME 光标定位正确。

### I3. Ctrl+C 双击退出设计

3 秒超时、清空输入后提示、busy 状态下恢复输入——用户体验考虑周到。

### I4. CSI 解析器正确处理鼠标序列

`_dispatch_csi` 通过 `0x40-0x7E` 范围查找 final byte 来确定序列长度（689-690行），鼠标序列被整体消费（769-770行），不存在截断问题。

---

## 总结

| 严重程度 | 数量 | 关键发现 |
|---------|------|---------|
| CRITICAL | 2 | stdin 关闭无限循环（真实 bug）、add_reader 竞态窗口（理论风险） |
| HIGH | 4 | 每帧创建 Console、文件扫描无缓存、光标定位不考虑换行、ask 无超时 |
| MEDIUM | 6 | 状态栏宽度计算、Queue 初始化时机、transcript log 异常、/paste 硬编码、SubmitHandler 语义、CancelledError 重置时机 |
| LOW | 6 | 类过大、历史无上限、输出无大小限制、命令过滤线性扫描、临时 Console、markup 转义 |

**最需优先修复**：

1. **C1** — stdin 关闭导致无限循环，一行代码即可修复
2. **H1** — 每帧创建 Console，streaming 性能影响显著
3. **H2** — 文件扫描无缓存，按键时重复 IO
4. **H3** — 光标定位不考虑换行，长输入时用户可感知

**初版中已排除的发现**：

- ~~M4（`_render_impl` 中 start 可能为负）~~ — 实际代码使用 `tree_lines[-body_limit:]` 切片，不存在 `start` 变量或 `scroll_offset`，描述的代码与实际不符
- ~~L4（鼠标事件硬编码 6 字节）~~ — CSI 解析器通过 final byte 范围确定序列长度，鼠标序列被整体消费，不存在截断问题
