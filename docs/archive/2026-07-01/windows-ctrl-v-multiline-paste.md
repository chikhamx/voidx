> **Status: Done** — 实现位于 `src/voidx/ui/tui/parser.py:55-124`，测试位于 `tests/test_ui/tui/test_win32_paste_drain.py`（14 个用例）。R1/R4 的真机验证仍待执行。

# Windows Ctrl+V 多行粘贴 — 技术设计文档

## Context

Windows 平台下，用户在终端中通过 Ctrl+V 粘贴多行文本时，只有第一行被保留，后续行丢失或被当作独立提交。macOS 上 Cmd+V 多行粘贴工作正常。本设计文档定义修复方案。

### 根因

voidx TUI 有两条粘贴路径：

**路径 A — Bracketed paste（终端自动粘贴）**
- `src/voidx/ui/tui/helpers.py:26` 通过 `\x1b[?2004h` 启用 bracketed paste mode
- 终端在粘贴时发送 `\x1b[200~` ... `\x1b[201~` 包裹的完整内容
- `src/voidx/ui/tui/parser.py:200-220` 的 `_process_paste` 收集完整文本后调用 `_insert_pasted_text`，正确处理换行
- macOS 终端/iTerm2 走此路径，多行粘贴正常

**路径 B — Ctrl+V 手动触发（0x16）**
- `src/voidx/ui/tui/parser.py:291-292` 收到 `0x16` 后返回 `"paste_clipboard"` action
- `src/voidx/ui/tui/parser.py:188-191` 调用 `_paste_clipboard_quiet()`
- `src/voidx/ui/tui/clipboard_mixin.py:45-51` 的 `_paste_clipboard_quiet` 先调 `paste_clipboard_image(quiet_no_image=True)`（`clipboard_mixin.py:46`）尝试图片；若图片失败且 status ∈ `{"no_image","unsupported"}`，再调 `_paste_clipboard_text_quiet()`（`clipboard_mixin.py:51`）
- `_paste_clipboard_text_quiet` → `paste_clipboard_text(quiet_no_text=True)`（`clipboard_mixin.py:42`）→ `_read_clipboard_text()`（`clipboard_mixin.py:33-40`，模块级函数在 `clipboard_mixin.py:78`）
- `_read_clipboard_text` 优先用 app 注入的 `paste_clipboard_text_from_system`，否则回退到 `src/voidx/ui/tools/clipboard_text.py:25` 的 `read_clipboard_text`
- `src/voidx/ui/tools/clipboard_text.py:66-73` 的 `_capture_clipboard_text_windows` 调 `_win32_clipboard_text`（`tools/clipboard_text.py:76-115`），用 Win32 API `GetClipboardData(CF_UNICODETEXT)` 读剪贴板
- 读到的文本经 `paste_clipboard_text`（`clipboard_mixin.py:35-36`）传入 `_insert_pasted_text`，该方法能正确处理多行

**问题出在 Windows 控制台不发送 bracketed paste 序列，也不发送 0x16：**

`src/voidx/ui/tui/parser.py:55-88` 的 `_read_input_raw_win32` 使用 `msvcrt.getwch()` 逐字符读取控制台输入缓冲区。在 Windows Terminal / 现代控制台中，Ctrl+V 是终端级粘贴快捷键，不往输入缓冲区写入 `0x16`，而是将剪贴板内容作为键盘输入逐字符灌入。多行文本中的 `\r`（0x0D）在 `parser.py:287-288` 被当作 submit：

```python
if first == 0x0D:
    return (1, "submit")
```

第一行末尾的 `\r` 直接触发提交，剩余行丢失。

## Goals and Non-Goals

### Goals

- Windows 下 Ctrl+V 粘贴多行文本时，所有行保留在输入区，不提前提交
- 第一行末尾的 `\r` 不再触发提交，其后续行作为粘贴内容保留，超过阈值时折叠为 `[Pasted text #N +M lines]` token。第一行内容在 `\r` 到达前已逐字符插入编辑器（与当前行为一致），不纳入粘贴折叠——这是 Windows 逐字符灌入模型的固有限制，无法在不引入每键延迟的前提下避免
- 不破坏单行 Enter 提交、Ctrl+C 中断等现有行为
- 不依赖特定终端模拟器（Windows Terminal、cmd.exe、PowerShell 均需工作）

### Non-Goals

- 不修改 macOS / Linux 的粘贴路径
- 不修改 Web UI / 桌面端的粘贴逻辑（它们走不同的输入通道）
- 不实现 Windows 下 bracketed paste mode 的终端级支持（这是终端模拟器的职责）

## Architecture

### 方案选择

在 `_read_input_raw_win32` 层面检测"快速连续到达的含换行字符"，组装成 bracketed paste 序列后交给 `_process_input`。

```
用户 Ctrl+V
    │
    ▼
Windows Terminal 将剪贴板内容逐字符灌入控制台输入缓冲区
    │
    ▼
_read_input_raw_win32 (parser.py:55)
    │
    ▼  ← 新增：检测到 \r 或 \n 时，继续读取缓冲区剩余字符
    │     若短时间内有后续字符到达，判定为粘贴
    │     组装成 \x1b[200~ + content + \x1b[201~
    │
    ▼
_process_input (parser.py:145)
    │
    ▼ 检测到 _PASTE_START
    │
    ▼
_process_paste (parser.py:200) → _insert_pasted_text (parser.py:222)
    │
    ▼
多行文本作为整体插入 / 折叠为 token
```

### 为什么不在 `_dispatch_key` 层处理裸 `\r`

`_dispatch_key` 无法区分"用户按 Enter 提交"和"粘贴内容中的 `\r`"。只有输入层能通过时序和连续性判断是否为粘贴。

### 为什么不依赖 0x16

Windows Terminal 拦截 Ctrl+V 后不发送 `0x16`，路径 B 在 Windows Terminal 下根本不会被触发。旧版 cmd.exe 可能发送 `0x16`，但行为不一致，不能作为可靠入口。

## Data Model

无新增持久化数据，无新增实例状态。粘贴检测在 `_try_drain_win32_paste` 内用局部变量完成，函数返回后状态即释放。

## API Contract

### `_read_input_raw_win32` 改造

- **Signature**: `async def _read_input_raw_win32(self) -> bytes`（签名不变）
- **Behavior 变更**:
  1. 读取首字符（与现有逻辑相同）
  2. 若首字符为 `\r` 或 `\n`，进入粘贴检测：在短超时（默认 20ms）内循环非阻塞读取 `msvcrt` 缓冲区剩余字符
  3. 若换行符计数 ≥ 2 或总长度 > 8，判定为粘贴，返回 `\x1b[200~` + 累积内容 + `\x1b[201~`（判定条件详见下方"判定条件设计说明"）
  4. 若超时无后续字符，判定为普通 Enter，返回原始 `\r`
  5. 非换行首字符走原逻辑
- **调用点改造伪代码**:

  现有 `_read_input_raw_win32` 用 `asyncio.to_thread(_read)` 包装阻塞的 `msvcrt.getwch()`。改造后，`_read` 闭包在读到首字符后调用 `_try_drain_win32_paste`，二者都在同一个工作线程内执行：

  ```python
  async def _read_input_raw_win32(self) -> bytes:
      import msvcrt

      def _read() -> bytes:
          ch = msvcrt.getwch()
          # 0x00 / 0xe0：功能键/方向键，走原映射逻辑（不变）
          if ch == "\x00" or ch == "\xe0":
              ch2 = msvcrt.getwch()
              # ... 现有 _WIN_KEY_MAP 映射逻辑不变 ...
              mapped = _WIN_KEY_MAP.get(ch2)
              if mapped:
                  return mapped.encode("utf-8")
              return ("\x00" + ch2).encode("utf-8")
          # 首字符为换行：尝试 drain 粘贴
          if ch in ("\r", "\n"):
              pasted = self._try_drain_win32_paste(ch)
              if pasted is not None:
                  return pasted
              # 不是粘贴，回退为普通换行字节
              return ch.encode("utf-8")
          # 其他字符走原逻辑
          return ch.encode("utf-8")

      return await asyncio.to_thread(_read)
  ```

  关键点：`_try_drain_win32_paste` 返回 `None` 时，`_read` 用 `ch.encode("utf-8")` 回退到原始单字符字节，保证普通 Enter 不受影响。

### 新增辅助函数 `_try_drain_win32_paste`

- **Signature**: `def _try_drain_win32_paste(self, first_char: str, timeout_ms: int = 20) -> bytes | None`
- **Path**: `src/voidx/ui/tui/parser.py`
- **Request**: 首字符（已通过 `getwch` 读到，类型为 `str`）
- **Response**:
  - 返回 `bytes`：组装好的 bracketed paste 序列（判定为粘贴）
  - 返回 `None`：不是粘贴，调用方按原逻辑处理
- **逻辑**:
  1. 若 `first_char` 不是 `\r` / `\n`，直接返回 `None`
  2. 用 `msvcrt.kbhit()` 检测缓冲区是否有待读字符
  3. 在 `timeout_ms` 内循环 `msvcrt.getwch()` 读取所有可用字符，累积到 `buffer`（`str` 类型，因为 `getwch` 返回 `str`）
  4. 判定为粘贴的条件：`buffer` 中换行符数量 ≥ 2，**或** `len(buffer)` 超过单行阈值（默认 8）。注意：`first_char` 本身就是换行符，因此"包含换行符"恒为真，不能作为判定条件——必须用换行符**计数**或**总长度**区分"粘贴多行"与"手动 Enter 后偶发跟随字符"。详见下方"判定条件设计说明"。
  5. 满足条件返回 `b"\x1b[200~" + buffer.encode("utf-8", errors="replace") + b"\x1b[201~"`（**必须显式 `.encode()`**，`getwch` 返回 `str` 不能直接与 `bytes` 拼接，否则触发 `TypeError`）
  6. 否则返回 `None`

#### 判定条件设计说明

朴素条件 `len(buffer) > 1 and ("\r" in buffer or "\n" in buffer)` 会退化为 `len(buffer) > 1`（因为 `first_char` 本身就是换行），导致"Enter 后 20ms 内有任何后续字符"即误判为粘贴。这有两个严重场景：

- **快速连按两次 Enter**：第二个 `\r` 被 drain 进 buffer，第一次 Enter 的提交被吞掉（变成粘贴插入），用户预期的两次提交变成零次提交——这是**意图丢失**，不是单纯的 UX 退化。
- **Enter 后立即按字母/退格**：被误判为粘贴，提交未生效。

因此判定条件改为：**换行符计数 ≥ 2**（多行粘贴的强信号），**或** `len(buffer) > 8`（单行但较长的粘贴，如粘贴一长串无换行文本时也能覆盖）。阈值 8 远大于"手动快速连按 1-2 键"的典型后续长度，同时远小于真实粘贴内容长度。

#### 实现骨架

  ```python
  def _try_drain_win32_paste(self, first_char: str, timeout_ms: int = 20) -> bytes | None:
      import msvcrt
      import time

      if first_char not in ("\r", "\n"):
          return None

      buffer = first_char
      deadline = time.monotonic() + (timeout_ms / 1000.0)
      while time.monotonic() < deadline:
          if not msvcrt.kbhit():
              time.sleep(0.001)  # 1ms 自旋间隔，避免 CPU 空转
              continue
          ch = msvcrt.getwch()
          # 功能键前导字节：不属于粘贴内容，终止 drain。
          # 必须消费第二字节，否则它会残留在缓冲区被下一次
          # _read_input_raw_win32 当作独立首字符读取，产生幽灵按键。
          if ch == "\x00" or ch == "\xe0":
              msvcrt.getwch()  # 丢弃功能键的第二字节
              break
          buffer += ch

      # 判定条件：换行符计数 >= 2（多行粘贴），或总长度 > 8（单行长粘贴）。
      # 不能用 "换行符 in buffer" —— first_char 本身就是换行，该条件恒为真。
      newline_count = buffer.count("\r") + buffer.count("\n")
      if newline_count >= 2 or len(buffer) > 8:
          return b"\x1b[200~" + buffer.encode("utf-8", errors="replace") + b"\x1b[201~"
      return None
  ```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `msvcrt.kbhit()` 不可用（非 Windows） | 函数不被调用，走原逻辑 |
| 超时内无后续字符 | 返回 `None`，按普通 Enter 处理 |
| 粘贴内容含无效 UTF-8 | `_try_drain_win32_paste` 用 `buffer.encode("utf-8", errors="replace")`；`_process_paste` 另有 `decode("utf-8", errors="replace")` 双重兜底 |
| msvcrt 在 `to_thread` 工作线程中调用 | `kbhit()`/`getwch()` 操作的是进程级控制台输入句柄。`_read` 闭包与 `_try_drain_win32_paste` 在**同一个工作线程**内顺序执行，不与其他线程并发访问控制台，线程安全。但需确保同一时刻只有一个 `_read_input_raw_win32` 在途（由 asyncio 单任务调度保证） |
| 用户快速手打 Enter + 字符（误判为粘贴） | 20ms 超时窗口极短，人工输入间隔（典型 >50ms）远大于此，误判概率低。判定条件已加严（换行符计数 ≥ 2 或长度 > 8），"Enter 后跟 1-2 个字符"不会触发粘贴。**残余风险**：快速连按两次 Enter（两个 `\r` 间隔 <20ms）仍可能被判定为粘贴，导致第一次 Enter 的提交被吞掉——这是**意图丢失**而非单纯 UX 退化。缓解措施：真机验证连按 Enter 的实际间隔，必要时缩短 `timeout_ms` 或提高换行符计数阈值。详见 R4 |
| 旧版 cmd.exe 发送 0x16 | 路径 B 仍有效，不受影响 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 在输入层组装 bracketed paste 序列 | 在 `_dispatch_key` 对裸 `\r` 做特殊处理 | 输入层能通过时序区分粘贴与手动 Enter，`_dispatch_key` 无法区分 |
| 用 20ms 超时窗口 | 用更长超时（如 100ms） | 20ms 足以覆盖终端灌入粘贴内容的延迟（通常 <5ms），同时最小化对手动快速输入的误判 |
| 用 `msvcrt.kbhit()` 非阻塞检测 | 用线程 + 阻塞读取 | `kbhit` 是 Windows 标准方式，无需引入线程复杂度 |
| 组装 bracketed paste 序列而非直接调 `_insert_pasted_text` | 直接插入 | 复用现有 `_process_paste` 路径，保证折叠/历史/token 逻辑一致 |
| 判定条件用换行符计数 ≥ 2 或长度 > 8 | 朴素的 `len > 1 and 换行符 in buffer` | 朴素条件因 `first_char` 本身是换行而退化为 `len > 1`，导致"Enter 后跟任意字符"即误判；加严条件能区分"多行粘贴"与"手动 Enter + 偶发跟随字符" |
| 功能键前导字节 break 前消费第二字节 | 直接 break 不消费 | 不消费会导致第二字节残留缓冲区，被下次读取当作幽灵按键 |

## Risks（待真机验证的假设）

以下假设在实现后必须用真机测试验证，验证前不应视为已解决：

- **[R1] 20ms 超时阈值与分批灌入**：假设终端灌入粘贴内容的延迟 <20ms（通常 <5ms）。若实际终端延迟更高（如远程桌面、慢速 SSH），可能漏判粘贴。**特别注意**：Windows Terminal 灌入大段文本时可能**分批次**写入控制台输入缓冲区，批间隔可能超过 20ms。此时 drain 在首批后超时返回，首批被正确包成 bracketed paste，但后续批次的首个 `\r` 会再次触发 `_dispatch_key` 的 submit 逻辑，回到原始 bug。验证方式：在 Windows Terminal / cmd.exe / PowerShell 三种环境下，分别粘贴 10 行、100 行、1000 行文本，观察是否完整保留。若大文本漏判，考虑备选方案：检测到首批粘贴后延长后续 drain 的超时窗口（指数退避），或在 drain 循环中遇到换行后重置 deadline。
- **[R2] 换行符格式**：假设 Windows Terminal 灌入的换行为 `\r\n` 或 `\r`。但 Windows Terminal 的 `pasteWithNewlines` / `input.forceVT` 等设置可能改变灌入字符序列（如改为 `\n`）。验证方式：在开启/关闭相关设置下分别粘贴多行文本，用调试日志记录 `buffer` 的实际字节。`_insert_pasted_text` 已对 `\r\n`/`\r`/`\n` 三种格式做归一化（`parser.py:233`），drain 阶段的判定逻辑 `buffer.count("\r") + buffer.count("\n")` 已覆盖所有格式。
- **[R3] 功能键前导字节混入粘贴流**：若粘贴内容恰好以 `\x00` 或 `\xe0` 开头（罕见但可能），drain 会提前 break 并消费第二字节。当前实现接受此边界——最坏情况退化为普通 Enter，不丢数据。注意：break 前已用 `msvcrt.getwch()` 消费功能键的第二字节，避免它残留缓冲区被下次读取当作幽灵按键。
- **[R4] 快速连按 Enter 被误判为粘贴**：用户快速连按两次 Enter（两个 `\r` 间隔 <20ms），第二个 `\r` 被 drain 进 buffer，满足 `newline_count >= 2`，第一次 Enter 的提交被吞掉（变成粘贴插入）。这是**意图丢失**——用户预期的两次提交变成零次提交。缓解措施：真机测量快速连按 Enter 的实际间隔；若间隔普遍 <20ms，考虑缩短 `timeout_ms` 至 10ms，或将判定阈值改为 `newline_count >= 3`（但会漏判两行粘贴）。此风险与 R1 的超时阈值存在张力——缩短超时降低误判但增加漏判，需真机数据权衡。
- **[R5] 单次返回大段 bracketed paste 序列与 `_pending_bytes` 交互**：`_read_input_raw_win32` 单次返回完整的 `\x1b[200~` + content + `\x1b[201~` 序列。`_process_input` 的 `_pending_bytes` 机制（`parser.py:147-149`）仅处理截断的 UTF-8 多字节序列和未完成的 CSI 序列，不涉及 bracketed paste 标记——`_process_paste`（`parser.py:200-220`）能处理任意长度的 `_paste_buffer` 累积。因此单次返回大段序列无大小上限问题，无需分块。但若粘贴内容极长（如 10000 行），`to_thread` 工作线程会长时间占用——可接受，因为主循环在此期间本就等待输入。
