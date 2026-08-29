> **Status: Done** — P0.6 implemented and verified on 2026-08-29.

---
name: tui-terminal-writer-backpressure
display_name: TUI TerminalWriter Backpressure
summary: 将 TUI terminal 输出迁移到有序 worker，避免慢 PTY 阻塞 asyncio 事件循环
doc_type: implementation-spec
audience: human+llm
status: approved
implementation_status: complete
---

# TUI TerminalWriter 慢 PTY / Backpressure 实施规格

## 1. 目标

实现 `docs/design/cross-ui-performance-addendum.md` 的 P0.6：当 stdout 是慢 SSH、PTY 或发生 terminal backpressure 时，TUI 的 stdin、cancel、resize 和 asyncio heartbeat 不被同步 `write()` / `flush()` 阻塞，同时保证最终帧、committed scrollback 和 terminal restore 的可见顺序正确。

本规格已获用户批准，供维护者和后续实现 agent 直接执行。

## 2. 当前行为与根因

### 2.1 当前文件

- `tui/voidx_cli/terminal_writer.py`
  - 当前 `TerminalWriter` 是同步有界字符串缓冲。
  - `write()` 达到 `byte_budget` 后直接调用 `drain()`。
  - `flush()` 在调用线程循环 drain，并直接调用目标 stream 的 `flush()`。
  - partial write、UTF-8、零进度和 byte budget 已有单元测试。
- `tui/voidx_cli/render_frame.py`
  - `_render_frame()`、`_render_full()`、`_render_diff()`、局部 input/choice/busy repaint 和 cursor positioning 均直接调用同步 writer。
  - 主线程的 `_prev_frame_lines` 是当前 diff 基线；若中间 frame 被合并，不能继续把“已入队但未实际写入”的 frame 当作基线。
- `tui/voidx_cli/app.py`
  - `run()` 同步写 startup sequence。
  - `_flush_committed()` 同步清除活动 frame、写 scrollback、flush。
  - shutdown 同步 flush commit，恢复 termios，再写 exit sequence。
- `tui/voidx_cli/state.py`
  - 保存主线程 frame geometry/cache；尚无 writer generation 或 writer failure 状态。
- `tui/tests/test_terminal_writer.py`
  - 当前只有 5 个同步 writer 测试，没有 worker、generation、慢 PTY、commit/barrier 或 shutdown 集成测试。

### 2.2 根因

当前 byte budget 限制 pending buffer 大小，但没有改变 I/O 所在线程。慢 stream 会在 asyncio 事件循环线程内阻塞 `write()` 或 `flush()`，因此输入、取消和 resize 任务仍会饿死。

## 3. 范围

### 3.1 本次必须实现

1. 单独 worker thread 是 TTY stdout `write()` / `flush()` 的唯一所有者。
2. TTY 主循环只构造并 enqueue 自包含 batch，不等待 terminal 实际 drain。
3. 连续可丢 frame 只保留最新 generation。
4. worker 只使用“最后实际成功写入 terminal 的 frame”作为 diff 基线。
5. committed scrollback、clear/scroll/resize/startup/restore barrier 不丢失、不越序。
6. committed payload 超过 4 MiB 内存 soft limit 时使用 `SpooledTemporaryFile`。
7. partial write、zero progress、EPIPE 和 worker exception 有明确结果。
8. shutdown 按 commit drain → termios restore → exit sequence → worker stop 执行。
9. 同步兼容模式继续支持直接单测、非 TTY 输出和未启动 worker 的调用方。

### 3.2 非目标

- 不修改 OutputTree、stream protocol、Markdown projection 或 transcript persistence。
- 不修改 frontend、desktop 或生成的 protocol 文件。
- 不在 P0.6 中实现输入批处理、paste spool 或 candidate cache。
- 不以提高 render debounce 代替 backpressure 处理。
- 不丢弃 committed 输出来限制队列。
- 不改变 P0.5 `RenderPlan` 的单帧单次采集契约。

## 4. 不变量

1. `TerminalWriter` 是 TTY stdout frame/scrollback 的单一所有者。
2. `FrameBatch` 可合并；`CommitBatch` 和 `BarrierBatch` 不可合并或丢弃。
3. input state 更新和 render scheduling 不等待 stream drain。
4. frame generation 单调递增；worker 不应用小于等于已应用 generation 的 stale frame。
5. worker 的 diff 基线只在完整 batch 成功写完并 flush 后更新。
6. commit 或 invalidating barrier 后，worker frame baseline 失效；下一 frame 必须 full render。
7. queue 中跳过任意中间 frame 后，最终 terminal frame 必须等价于直接写最新 target frame。
8. shutdown 返回时 worker 已退出，所有 spool 文件已关闭。
9. worker 失败后不再接受新的 TTY batch；主循环收到错误并恢复 termios，不能悄悄丢失错误。
10. 既有未启动 worker 的 `write()`、`drain()`、`flush()` 行为保持兼容。

## 5. V1 架构决策

### 5.1 双模式 TerminalWriter

`TerminalWriter` 保留两种模式：

- **同步兼容模式**：构造后、`start()` 前，沿用当前 `write()` / `drain()` / `flush()` 行为。现有 5 个测试和非 TTY 路径继续使用该模式。
- **TTY worker 模式**：`start()` 后，只允许 batch API。TTY 生产路径不得再调用同步 `write()` / `flush()`；若误用，应抛 `RuntimeError`，使隐藏的事件循环阻塞在测试中立即暴露。

`start()`：

```python
writer.start(
    loop=asyncio.get_running_loop(),
    on_frame_result=handle_frame_result,
    on_error=handle_writer_error,
)
```

- 只允许调用一次；重复 start 抛 `RuntimeError`。
- 在调用线程解析并固定目标 stream；worker 生命周期内不跟随后续 `sys.stdout` 替换。
- 创建 daemon worker thread，但正常 shutdown 必须显式 join。
- 保存 asyncio loop、frame-result callback 和 error callback；worker 不直接操作 TUI mutable state。
- `on_frame_result(FrameResult)` 与 `on_error(Exception)` 都只能通过 `loop.call_soon_threadsafe()` 投递，并且各自在 asyncio loop 线程执行。

### 5.2 Batch 数据模型

在 `tui/voidx_cli/terminal_writer.py` 定义内部不可变 batch：

```python
@dataclass(frozen=True)
class FrameBatch:
    generation: int
    start_row: int
    target_lines: tuple[str, ...]
    cursor_ansi: str
    render_ms: float
    force_full: bool = False

@dataclass(frozen=True)
class CommitBatch:
    clear_start_row: int
    payload: _CommitPayload

@dataclass(frozen=True)
class BarrierBatch:
    kind: Literal[
        "startup",
        "clear",
        "scroll",
        "resize",
        "drain",
        "restore",
        "shutdown",
    ]
    ansi: str
    invalidate_frame: bool

@dataclass(frozen=True)
class FrameResult:
    generation: int
    total_lines: int
    changed_lines: int
    render_ms: float
    strategy: str
    applied: bool
```

`_CommitPayload` 可持有内存字符串或 seek 到 0 的 `SpooledTemporaryFile`。batch 不能引用会被主线程后续修改的 list、buffer 或 Rich renderable。

调用方只分配 frame `generation`。Commit/barrier 的全局 queue order 由 writer 在同一个 `Condition` 临界区内分配，不能由调用方传入。内部 queue entry 至少包含：

```text
order                  # writer 分配、对所有 batch 全局单调递增
batch                  # FrameBatch | CommitBatch | BarrierBatch
completion             # commit/barrier 可选的 opaque BatchToken
```

这两个序列语义不同：generation 表示可合并视觉状态的新旧；order 表示不可跨越的真实 queue 顺序。

### 5.3 对外 worker API

V1 使用以下明确 API；实现时名称可做小幅调整，但语义不得改变：

```python
writer.start(loop=loop, on_frame_result=on_frame_result, on_error=on_error)
writer.submit_frame(frame_batch)
commit_token = writer.submit_commit(clear_start_row=start_row, ansi=commit_ansi)
barrier_token = writer.submit_barrier(
    kind="clear",
    ansi=clear_ansi,
    invalidate_frame=True,
)
await writer.wait(barrier_token)
await writer.drain_async()
await writer.shutdown_async()
```

要求：

- `submit_frame()` / `submit_barrier()` 只做不可变参数校验和短临界区 enqueue；`submit_commit()` 可在临界区外执行 UTF-8 计数及本地 spool 构造。所有 submit 的 `Condition` 临界区都必须是 O(1)，且任何 submit 都不得触发 terminal stream I/O。
- `submit_commit()` 在 writer 内构造 `_CommitPayload`，成功 enqueue 后 writer 获得并负责释放 payload；submit 失败时由同一方法立即关闭临时资源。
- `submit_commit()` 和 `submit_barrier()` 返回 opaque `BatchToken`；调用方不能伪造 order。
- `wait()` / `drain_async()` 通过 asyncio Future 等待 worker completion；不得在事件循环上调用 `thread.join()` 或 `Event.wait()`。
- worker 使用 `loop.call_soon_threadsafe()` 完成 token、上报 `FrameResult` 和错误。
- completion 必须对取消安全：调用方取消等待不能导致 worker 在已取消 Future 上直接 `set_result()`。
- `shutdown_async()` 幂等；成功后任何 submit 都抛 `RuntimeError`。

### 5.4 Queue 与 frame 合并

queue 由一个 `Condition` 保护，包含不可丢 batch 和至多一个连续 pending frame target。所有 batch 在成功入队时由 writer 分配一个全局递增 `order`。

enqueue 规则：

1. 新 `FrameBatch` 到达时，如果 queue 尾部是 `FrameBatch`，用新 generation 和新 order 替换尾部 frame。
2. invalidating `CommitBatch` 或 `BarrierBatch` 到达时，可删除它之前仍 pending 的 `FrameBatch`，但不能删除或跨越任何 commit/barrier。
3. `drain` barrier 不删除此前 pending frame；它在所有更小 order 的 batch 完成后才 complete。更大 order 的后续提交不影响该 token。
4. worker 已经取出的 frame 不可中途取消；后续 frame 仍可替换 queue 中尚未取出的 frame。
5. 同一 writer 的 commit/barrier order 只可能由 writer 内部分配，因此不存在调用方乱序输入；测试应验证观察到的执行 order 严格递增。

frame 不计入 4 MiB committed spool soft limit，因为 queue 只保留最新 target。

### 5.5 Worker frame baseline 与 RenderStats

worker 私有状态：

```text
applied_generation
applied_start_row
applied_lines
baseline_valid
```

处理 `FrameBatch`：

1. 若 generation 小于等于 `applied_generation`，不写 terminal，并投递 `FrameResult(applied=False, strategy="stale")`。
2. 只有 `baseline_valid`、start row 相同、`force_full=False` 时才可 diff。
3. diff 算法保持当前语义：逐行比较；变化率 `> 0.8` 时 full render；缩短尾部用 `\x1b[J`。
4. full render 写 `CSI start_row;1H`、`CSI J`、完整 `target_lines`。
5. frame body 与 `cursor_ansi` 作为同一 batch 顺序写入，并在 batch 末尾 flush。
6. 所有 write 和 flush 成功后才更新 baseline。
7. 成功后投递 `FrameResult(applied=True)`；在 queue 中被 coalesce、从未被 worker 取出的 frame 不投递结果。

`RenderStats` 定义固定如下：

- `render_ms` 是 asyncio 主线程从 `_render_frame()` 开始到 target lines、cursor ANSI 和其他 immutable frame metadata 构造完成的耗时；主线程先计算该值，再构造 `FrameBatch(render_ms=...)` 并立即 enqueue。它不包含 enqueue 临界区、worker queue wait、stdout write 或 flush 时间。
- `total_lines`、`changed_lines` 和 `strategy` 来自 worker 实际应用结果，而不是主线程预测。
- TUI 只在 `result.applied` 且 `result.generation == terminal_frame_generation` 时更新 `_render_stats`；较旧结果忽略。
- P0.6 不给 `RenderStats` 增加 terminal drain latency 字段；如后续需要，单独增加 writer metric，不能悄悄改变 `render_ms` 语义。

主线程的 `_prev_frame_lines` 可暂时保留供 geometry/tests 使用，但在 worker 模式下不得作为 terminal 实际 diff 基线。

### 5.6 V1 局部 repaint 决策

TTY worker 模式下，input、choice 和 busy tick 不提交 raw ANSI patch。它们改为触发完整、可合并的 `FrameBatch`。

理由：

- raw patch 依赖某个已实际显示的 frame generation；慢 writer 下该 generation 可能仍在 queue 中或已被合并。
- 将局部 repaint 变成完整 target frame，可直接复用 frame generation/coalescing，并保证跳帧后的最终画面正确。
- P0.5 已限制单个 full frame 的区域采集次数，viewport-first 已限制可见转换量。

同步兼容模式继续保留现有局部 repaint 实现和测试行为。

### 5.7 Cursor 原子性

将 `_position_input_cursor()` 的纯计算与写入拆开：

```python
def _input_cursor_sequence(..., plan: _RenderPlan | None = None) -> str:
    ...
```

- 完整 frame 在主线程计算 `cursor_ansi`，与 `target_lines` 一起放入 `FrameBatch`。
- worker 写 frame body 后立即写 cursor sequence，再 flush。
- worker 模式不得从 `_position_input_cursor()` 单独 enqueue/flush cursor。
- 同步模式继续允许 `_position_input_cursor()` 调用同步 writer。

### 5.8 Commit 语义

TTY `_flush_committed()` 不再分多次同步 write/flush，而是构造一个自包含 `CommitBatch`：

- `clear_start_row` 使用当前 frame start row；worker 若有有效实际 baseline，优先使用实际 baseline 的 start row。
- worker 清除当前活动 frame，再完整写 committed ANSI 和结尾换行。
- commit 成功后 flush 并使 frame baseline 失效。
- queue 接受 commit 后，主线程才推进 `_committed_line_count` / restored committed watermark；submit 失败不得推进。
- commit 之后入队的 frame 自然排在 commit 后，并因 baseline 失效执行 full render。

非 TTY `_flush_committed()` 继续走同步 plain-text writer。

### 5.9 Committed payload spool

常量：

```python
COMMIT_MEMORY_SOFT_LIMIT = 4 * 1024 * 1024
```

`pending_commit_bytes` 的定义是：writer 当前拥有、尚未关闭的所有 **内存型** commit payload 的 UTF-8 字节数，包括 queue 中和 worker 正在写的 payload。它不包含已经强制 rollover 到磁盘的 payload。

`submit_commit()` 规则：

1. 计算 `payload_bytes = len(ansi.encode("utf-8"))`。
2. 若 `pending_commit_bytes + payload_bytes <= soft_limit`，使用 immutable 内存字符串，并在成功 enqueue 时增加 `pending_commit_bytes`。
3. 否则创建：

   ```python
   tempfile.SpooledTemporaryFile(
       max_size=soft_limit,
       mode="w+t",
       encoding="utf-8",
       newline="",
   )
   ```

   写入完整 ANSI 后必须显式调用 `rollover()`，再 `seek(0)`。不能依赖单个 payload 超过 `max_size` 才自动 rollover，因为多个小 payload 的 aggregate 也可能触发 soft limit。
4. spool 构造和写入不得持有 writer 的 `Condition`；成功构造后再进入短临界区分配 order 并 enqueue。若此间内存预算变宽，仍可保守地保留已 rollover payload。
5. submit 前失败、enqueue 失败、worker 成功、worker error 和 shutdown 都必须由恰好一个 owner 关闭 payload。成功 enqueue 后 ownership 从 `submit_commit()` 转移给 writer；此前由提交方法负责。
6. worker 对内存 payload 和 text-mode spool 都按字符边界分块，并复用同一个 partial-write helper。
7. 内存 payload 在成功或失败关闭时减少 `pending_commit_bytes`；spool 不增加该计数。
8. commit 不因 soft limit 被拒绝或丢弃。

构造函数允许内部测试覆盖 `commit_memory_soft_limit`，但 CLI/用户配置不得暴露它。测试可 monkeypatch 本模块的 `SpooledTemporaryFile` factory 跟踪 `rollover()` 和 `close()`。

只读指标：

- `pending_commit_bytes`：当前内存型 queued + in-flight commit bytes；
- `spooled_commits`：累计强制 rollover 的 commit 数；
- `spooled_bytes`：累计强制 rollover 的 UTF-8 字节数。

V1 的 soft limit 是内存安全阈值，不是 terminal write chunk size。保留现有 `byte_budget` 作为 worker 每次底层写入的最大目标 chunk。P0.6 只保证 terminal I/O 不在事件循环执行；本地 spool 写入的进一步异步化不在本阶段范围，但 spool 写入必须位于 `Condition` 临界区之外。

### 5.10 Barrier 语义

- `startup`：写 clear screen + enter terminal sequence；等待完成后才开始首帧。
- `clear`：写 clear sequence，删除此前 pending frame，使 baseline 失效。
- `scroll`：写当前 `_make_room_for_frame()` 所需 scroll ANSI，使 baseline 失效；随后 frame 必须 full。
- `resize`：可无 ANSI，只用于丢弃 pending frame并使 baseline 失效。
- `drain`：不写 ANSI、不失效；此前 batch 完成并 flush 后 completion。
- `restore`：写 move-to-frame-end + exit terminal sequence；不可越过此前 commit。
- `shutdown`：只在此前 drain/restore 完成后停止 worker。

barrier 的 ANSI 即使为空也必须保留其 ordering 和 completion 语义。

### 5.11 Error 与 fallback

worker 捕获 `BrokenPipeError`、`OSError`、partial-write 协议错误和未知 exception：

1. 保存第一个 error，停止处理新的 TTY batch。
2. 关闭所有 queued spool payload。
3. 通过 `loop.call_soon_threadsafe(on_error, exc)` 回报主循环。
4. 所有 pending completion 以同一 error 失败。
5. TUI error callback 标记 writer failed、停止继续 enqueue ANSI，并请求退出 TTY loop。
6. shutdown `finally` 仍尝试恢复 termios；恢复后可向可用的 stderr 写一条 plain error，不再回退到同一个失败 stdout 的同步 writer。
7. EPIPE 视为不可恢复的 stdout 关闭，不打印 traceback 到 stdout。

这满足“可恢复 plain-output fallback”的最低安全边界：terminal mode 必须恢复，错误可通过 stderr 观察，不能继续向失败 stdout 写 ANSI。

### 5.12 Startup 与 shutdown 生命周期

当前 `run()` 的 terminal setup/startup 位于现有 `try/finally` 之前；实现时必须把最外层 cleanup 边界前移，覆盖 callback 安装、terminal setup、writer start 和 startup barrier。建议使用显式局部状态：

```text
terminal_setup_attempted = False
writer_started = False
startup_completed = False
consumer = None
restore_external_logging = None
writer_failed_event = asyncio.Event()
writer_error = None
```

顺序：

1. 进入最外层 `try` 后安装 dock callback/provider，并检测 TTY。
2. TTY 下先设置 `terminal_setup_attempted = True`，再调用 `_setup_terminal()`；这样即使 setup 在保存旧 termios 后失败，`finally` 仍会尝试 `_restore_terminal()`。
3. 取得 running loop，调用 `writer.start(...)`；成功返回后设置 `writer_started = True`。
4. submit 并 await `startup` barrier；成功后设置 `startup_completed = True`。
5. 只有 startup 完成后才创建 consumer、安装 external logging bridge、force commit 首屏并进入 input loop。
6. `finally` 先停止新的 render/input producer，再执行 terminal cleanup。
7. writer 健康且已启动时：force commit → await commit/drain；失败时记录错误但继续 cleanup。
8. 只要 terminal setup 曾尝试，就调用 `_restore_terminal()`；该操作不依赖 stdout 是否仍可写。
9. writer 健康且 startup 已写入时，再 submit/await `restore` barrier（move-to-frame-end + exit ANSI）。worker 已失败时跳过该 ANSI，避免再次写失败 stdout。
10. 只要 `writer_started=True`，最终都 await `shutdown_async()`；它必须在 success/error 两种状态下 reap thread、关闭 spool 并幂等返回。
11. 最后清理 callback/provider、consumer、timer、external logging，并在线程中导出 transcript。

cleanup 中每一步独立捕获和记录异常，后一步不能因前一步失败而跳过。startup 任一步失败时，原始异常仍应向调用方传播；cleanup 异常只记录，除非没有原始异常。测试必须分别注入 `_setup_terminal()`、`writer.start()` 和 startup barrier 失败，验证 termios restore 次数、writer shutdown 条件及 callback 清理。

### 5.13 Writer failure 唤醒 stdin

当前 POSIX `_read_input_raw()` 的 readiness Future 是局部变量，Windows `_read_input_raw_win32()` 则在 `asyncio.to_thread(msvcrt.getwch)` 中长期阻塞；仅设置 `_running = False` 无法唤醒正在等待输入的 `run()`。TTY input loop 必须显式竞速 input read 与 run-local writer-failure event：

```python
input_task = asyncio.create_task(self._read_input_raw())
failure_task = asyncio.create_task(writer_failed_event.wait())
done, _ = await asyncio.wait(
    {input_task, failure_task},
    return_when=asyncio.FIRST_COMPLETED,
)

if writer_failed_event.is_set():
    input_task.cancel()
    await asyncio.gather(input_task, return_exceptions=True)
    failure_task.cancel()
    await asyncio.gather(failure_task, return_exceptions=True)
    raise writer_error

failure_task.cancel()
await asyncio.gather(failure_task, return_exceptions=True)
data = input_task.result()
```

契约：

1. writer error callback 在 asyncio loop 线程保存第一个 `writer_error`、设置 `_terminal_writer_failed = True`、`_running = False` 和 `writer_failed_event.set()`；重复 error 不覆盖第一个异常。
2. input 与 failure 同时完成时 failure 优先；不得再 dispatch 该批 input。
3. 每轮结束都取消并 await 未获胜 task，不能泄漏 task 或吞掉 `CancelledError` 之外的 input exception。
4. POSIX input task 被取消时，现有 `_read_input_raw()` 的 `finally` 必须执行 `loop.remove_reader(fd)`；测试用无数据 pipe 证明取消后 reader 已移除且 task 完成。
5. Windows 不得保留无法取消的 `to_thread(getwch)`。`_read_input_raw_win32()` 改为每 10 ms await 后检查 `msvcrt.kbhit()`；只有可读时才同步调用 `getwch()` 和现有 bounded paste drain。因此取消等待不会遗留 executor thread。
6. writer failure exception 退出 input loop 后进入 5.12 的统一 cleanup；termios restore 和 writer shutdown 仍必须执行。
7. 非 TTY `_read_input_line()` 不参与 writer-failure race，因为非 TTY 不启动 worker。

## 6. 生产文件变更

### 6.1 `tui/voidx_cli/terminal_writer.py`

- 保留现有同步缓冲和 5 个测试行为。
- 新增 batch dataclass、commit payload、worker lifecycle、queue/coalescing、async completion、worker-owned diff、spool、错误回报和指标。
- 抽取单一 `_write_all()` / partial-write helper，sync drain 与 worker 共用；不要复制 partial-write 逻辑。

### 6.2 `tui/voidx_cli/render_frame.py`

- 为 worker 模式构造完整 `FrameBatch`，不直接写 frame ANSI。
- frame generation 每次完整 target 增加一次。
- 拆分 cursor sequence 纯计算。
- `_make_room_for_frame()` 在 worker 模式提交 `scroll` barrier。
- clear/resize 提交 invalidating barrier。
- worker 模式的 input/choice/busy repaint 回退完整 frame；同步模式保持当前局部路径。
- worker 回调只按最新 generation 更新 render stats。
- 保持 `_RenderPlan` 与 P0.5 单次采集测试。

### 6.3 `tui/voidx_cli/app.py`

- TTY `run()` 中取得 running loop 后启动 writer，并等待 startup barrier。
- `_flush_committed()` 在 TTY worker 模式构造单个 `CommitBatch`；非 TTY 保持同步 plain 输出。
- 新增 frame-result callback、writer error callback，以及 run-local writer-failure event。
- input loop 通过 race 等待 input read 或 writer-failure event，确保 stdout worker 失败能立即进入 cleanup。
- shutdown 改为异步顺序：force commit → `drain_async()` → `_restore_terminal()` → restore barrier → `shutdown_async()` → transcript export。
- 主流程异常优先；无主异常时传播首个 terminal-critical cleanup 错误。
- 外部取消在 consumer、timer、writer reap 或 transcript export 期间被延迟，完整 cleanup 后重新传播。

### 6.4 `tui/voidx_cli/async_utils.py`

- 提供 cancellation-safe await helper，以 shielded task 完成不可中断的 cleanup/reap 单元。
- 区分 child 自身取消与调用方取消；调用方取消不会被吞掉，也不会让 join/export 在生命周期方法返回后继续运行。

### 6.5 `tui/voidx_cli/parser.py`

- POSIX `_read_input_raw()` 保持现有 `add_reader` + `finally remove_reader` 语义，并新增取消测试。
- Windows `_read_input_raw_win32()` 不再用 `asyncio.to_thread()` 长期阻塞 `msvcrt.getwch()`；改为每 10 ms `await asyncio.sleep()` 轮询 `msvcrt.kbhit()`，仅在字符可用时于 loop 线程立即调用现有 decode/drain 逻辑。
- 保持 arrow/function key、普通字符和 bracketed-paste 现有输出语义；单次 `_try_drain_win32_paste()` 的既有 20 ms bounded drain 不属于长期阻塞 reader。

### 6.6 `tui/voidx_cli/state.py`

在 `RenderState` 与 `STATE_FIELD_MAP` 增加最少状态：

```text
terminal_frame_generation: int = 0
terminal_writer_failed: bool = False
```

不要把 worker queue、thread、Future、run-local failure event 或 spool 放入 `RenderState`；这些分别由 `TerminalWriter` 或 `run()` 局部变量拥有。

### 6.7 测试文件

- `tui/tests/test_terminal_writer.py`：保留同步兼容测试，并覆盖 worker、慢 PTY、spool、partial write、EPIPE、shutdown 错误传播和 join 期间取消。
- `tui/tests/test_input_advanced.py`：覆盖 startup failure、shutdown order、cleanup 首错传播、POSIX pending input cancellation、writer failure 唤醒，以及 consumer/transcript cleanup 期间的外部取消。
- `tui/tests/test_win32_paste_drain_integration.py`：覆盖 `kbhit()` polling 可取消，同时保持普通 key、function key 和 paste decode 结果。

除上述职责外，不新建第二个 writer 测试文件。

## 7. TDD 实施顺序

每一步必须先写失败测试并确认 RED，再写最小生产代码确认 GREEN。

### Task 1：worker lifecycle 与同步兼容

测试：

- 未 start 时现有 5 个同步测试不变。
- start 后 worker thread 执行 stream write，调用线程不执行 stream write。
- start 后同步 `write()` / `flush()` 抛 `RuntimeError`。
- drain/shutdown completion 可 await 且 shutdown 幂等。

命令：

```bash
./test.py --backend -- tui/tests/test_terminal_writer.py -k "sync or worker or shutdown" -v
```

### Task 2：frame generation 与实际 baseline

测试：

- worker 被 gate 阻塞时 enqueue generation 1/2/3，仅最新 pending frame 被应用。
- generation 1 已写、2 被跳过、3 写入时，3 的 diff 以 1 为 baseline，不以 2 为 baseline。
- start row 变化、force full、invalid baseline 均走 full。
- 最终 terminal 模型等价于 generation 3 target lines。

命令：

```bash
./test.py --backend -- tui/tests/test_terminal_writer.py -k "frame or generation or baseline" -v
```

### Task 3：commit、barrier 与 spool

测试：

- frame → commit → frame 的原始输出顺序正确，commit 不被 frame 合并删除。
- clear/scroll/resize 使下一帧 full。
- 两个 commit 严格保序。
- 超过测试 soft limit 的 commit 使用 spool，内容逐字符相等且 spool 最终关闭。
- drain barrier 等待此前 commit，不等待此后提交。

命令：

```bash
./test.py --backend -- tui/tests/test_terminal_writer.py -k "commit or barrier or spool or drain" -v
```

### Task 4：partial write、zero progress 与 EPIPE

测试：

- worker 模式 partial write 不丢数据、不乱序。
- 连续零进度达到上限后 worker error callback 只调用一次。
- `BrokenPipeError` 失败所有 pending completion，拒绝后续 submit，shutdown 不挂起。

命令：

```bash
./test.py --backend -- tui/tests/test_terminal_writer.py -k "partial or progress or epipe or error" -v
```

### Task 5：慢 PTY heartbeat 集成

此验收测试必须在 Task 1 开始时先写入并对当前同步实现确认 RED；Task 1–4 可逐步补齐其依赖，Task 5 才要求最终 GREEN。不能等 worker 全部完成后才新增一个天然为绿的集成测试。

使用真实 `pty.openpty()` 和独立 reader thread，按以下确定性 gate 执行：

1. `tty.setraw(slave_fd)`，使用一个薄 `TextIO` wrapper 将 ASCII `str` 通过 blocking `os.write(slave_fd, ...)` 写入；ASCII 保证 bytes written 等于 characters written。
2. reader thread 先等待 `start_stall` event；event 触发后固定 `time.sleep(0.250)`，在开始读取前记录 `len(heartbeat_ticks)`，随后用 `select.select()` + 小块 `os.read(master_fd, 4096)` 持续 drain。
3. asyncio heartbeat 每 5 ms 追加 `loop.time()`；开始 stall 前至少预热两个 tick，并记录 `ticks_before_stall`。
4. 提交至少 1 MiB ASCII commit payload，确保远大于常见 PTY output queue；禁止 reader 在 250 ms gate 前读取。
5. 在 worker 模式 await commit/drain token。事件循环必须能在 worker 被 PTY backpressure 阻塞时继续执行 heartbeat。
6. 主要断言：reader 释放时记录的 tick 数与 `ticks_before_stall` 之差至少为 10。这个断言不依赖精确 scheduler gap，并直接证明 stall 窗口内事件循环取得进展。
7. 辅助性能断言：stall 窗口内相邻 heartbeat 的最大 gap `< 0.150 s`。当前同步 writer 会让事件循环至少停顿约 0.250 s，因此稳定 RED；阈值为 gate 的 60%，为 CI 抖动保留余量。
8. reader 释放后 enqueue 连续 frame generation 1/2/3、commit marker 和 restore barrier；等待 shutdown 后断言 commit marker 完整且只出现一次、终态 frame 等价于 generation 3、raw output 最后是 restore/exit sequence。
9. 每个 `await`、thread join 和 drain 都有显式超时。`finally` 设置 stop event、关闭 slave/master fd，并 join reader；关闭 fd 引起的 `EIO`/`EBADF` 仅在 cleanup 阶段允许。
10. macOS/Linux 运行真实 PTY 测试；Windows 平台 skip，并由 gated fake stream 的 deterministic worker tests 覆盖相同 queue/order/error 语义。

为证明 RED 原因，首次运行应记录：同步 baseline 在 250 ms gate 内新增 tick `< 10` 或最大 gap `>= 0.150 s`。实现后不得放宽 gate 或阈值来取得 GREEN。

命令：

```bash
./test.py --backend -- tui/tests/test_terminal_writer.py -k "slow_pty or heartbeat" -v
```

### Task 6：TUI 调用迁移、input cancellation 与 shutdown

测试：

- TTY `run()` 启动 worker；非 TTY 不启动。
- `_render_frame()` worker 模式提交 self-contained frame，不调用 stream。
- input/choice/busy worker 模式提交 full frame；同步模式既有局部测试继续通过。
- `_flush_committed()` worker 模式提交 commit 后失效 frame cache。
- `_setup_terminal()`、`writer.start()`、startup barrier 分别失败时，cleanup 条件、原始异常和 callback 清理正确。
- POSIX pending `_read_input_raw()` 被取消后移除 loop reader，无 pending task。
- Windows `kbhit()` polling 在无输入时可取消；普通 key、function key 和 paste decode 保持不变。
- writer error 与 input 同时完成时 error 优先，`run()` 不挂起并恢复 terminal。
- shutdown 顺序为 commit completion → termios restore → exit sequence completion → worker stopped。

命令：

```bash
./test.py --backend -- \
  tui/tests/test_terminal_writer.py \
  tui/tests/test_input_advanced.py \
  tui/tests/test_win32_paste_drain_integration.py \
  tui/tests/test_frame_advanced.py \
  tui/tests/test_frame_rendering.py \
  tui/tests/test_status_activity.py \
  tui/tests/test_terminal_input.py \
  tui/tests/test_terminal_panels.py -v
```

## 8. 验证命令

聚焦验证：

```bash
./test.py --backend -- \
  tui/tests/test_terminal_writer.py \
  tui/tests/test_input_advanced.py \
  tui/tests/test_win32_paste_drain_integration.py \
  tui/tests/test_frame_advanced.py \
  tui/tests/test_frame_rendering.py \
  tui/tests/test_status_activity.py \
  tui/tests/test_terminal_input.py \
  tui/tests/test_terminal_panels.py -v
```

完整 TUI：

```bash
./test.py --backend -- tui/tests
```

完整 backend：

```bash
./test.py --backend
```

静态与 diff 检查：

```bash
./python.py -m py_compile \
  tui/voidx_cli/async_utils.py \
  tui/voidx_cli/terminal_writer.py \
  tui/voidx_cli/render_frame.py \
  tui/voidx_cli/app.py \
  tui/voidx_cli/parser.py \
  tui/voidx_cli/state.py \
  tui/tests/test_terminal_writer.py \
  tui/tests/test_input_advanced.py \
  tui/tests/test_win32_paste_drain_integration.py
git diff --check -- \
  tui/voidx_cli/async_utils.py \
  tui/voidx_cli/terminal_writer.py \
  tui/voidx_cli/render_frame.py \
  tui/voidx_cli/app.py \
  tui/voidx_cli/parser.py \
  tui/voidx_cli/state.py \
  tui/tests/test_terminal_writer.py \
  tui/tests/test_input_advanced.py \
  tui/tests/test_win32_paste_drain_integration.py \
  docs/specs/tui-terminal-writer-backpressure-2026-08-28.md
```

### 8.1 最终验证（2026-08-29）

- 慢 PTY heartbeat：`1 passed`。
- 规格聚焦集合：`238 passed`。
- 完整 TUI：`397 passed`。
- 完整 backend：`5235 passed, 30 skipped`。
- `py_compile`、目标 `git diff --check` 与相关 Python LSP diagnostics 均通过。
- cleanup 错误传播与取消竞态均先取得目标 RED，再修复为 GREEN；直接终审与 cancellation probes 结论为 PASS。

## 9. 验收标准

以下条件全部满足才可把 P0.6 标记完成：

1. 当前同步 writer 测试全部保留且通过。
2. 慢 PTY 下 heartbeat 不被 terminal write/flush 阻塞。
3. 至少两个中间 frame 被跳过时，最终 target frame 正确。
4. worker diff baseline 来自实际已写 frame。
5. committed payload 在 frame 合并、partial write 和 shutdown 中逐字符不丢失。
6. clear/scroll/resize/commit 后下一帧 full。
7. restore/exit sequence 是最后一个 terminal batch。
8. EPIPE/worker error 不死锁；pending input read 可取消，termios restore 仍执行。
9. setup/startup 任一步失败都不会泄漏 worker、loop reader、callback 或 raw terminal mode。
10. spool 测试证明 aggregate 超过 soft limit 后显式 rollover、内存 commit 指标受限、内容完整且文件关闭。
11. 聚焦测试、完整 `tui/tests`、完整 backend、`py_compile` 和 `git diff --check` 均通过。

## 10. 禁止变更

- 不手动编辑 `frontend/src/rpc/protocol.d.ts`。
- 不修改 Gateway/UI event 协议。
- 不把 committed scrollback 归类为可丢 frame。
- 不让主线程维护“假定已写入”的 worker diff baseline。
- 不在 asyncio loop 上调用 blocking `join()`、`Event.wait()`、stream `write()` 或 stream `flush()`。
- 不用无界 `queue.Queue` 保存完整 frame 历史。
- 不以捕获并忽略 worker exception 的方式让测试变绿。
- 不回滚当前工作树中的 P0.5 或其他不相关改动。

## 11. 回滚边界

实现期间若 worker 集成尚未通过全部验收，可保留同步兼容 `TerminalWriter`，但不得启用半完成的 TTY worker 路径。回滚应只移除 worker mode 的启动和 batch 提交，不删除本轮前已存在的同步 writer、P0.5 RenderPlan 或 scrollback 安全逻辑。

文档在实现和最终验证全部通过后，才能更新 `implementation_status` 并按项目归档规则移动到 `docs/archive/`。
