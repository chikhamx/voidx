# Per-File Read-Write Lock for Tool Execution — 技术设计文档

## Context

LLM 在同一轮对话中可能发出多个操作同一文件的 tool call（如连续多次 `replace` 或 `write`），当前 `_execute_approved_batch` 通过 `asyncio.gather` 全并发执行所有非 barrier tool call，导致对同一文件的 read-modify-write 操作产生竞态——后续调用读到旧内容、写入互相覆盖。

## Goals and Non-Goals

### Goals

- 同一文件上的 `write` / `replace` / `file` 互斥执行
- 同一文件上的 `read` 之间允许并发
- 同一文件上的 `read` 与 `write`/`replace`/`file` 互斥
- 不同文件的 tool call 保持完全并发（不退化）
- 不影响 `bash`、`agent` 等不直接声明文件路径的工具

### Non-Goals

- 不追踪 `bash` 隐式文件操作
- 不跨 batch 持有锁（每 batch 独立）
- 不替代 barrier 机制（`clarify`/`checkpoint`/`workflow`/`compact` 仍串行分割）

## Architecture

```
_execute_approved_batch (helpers.py)
├── 创建 batch 级 _FileRWLock 管理器
├── 为每个 tool call 提取文件路径（_extract_file_paths）
├── 按是否有文件路径分流 → file_calls / other_calls
├── 阶段一：所有 file_calls 通过 execute_one_file_locked 并发执行（同文件受 rwlock 约束）
└── 阶段二：所有 other_calls（bash/agent 等）通过 execute_one_no_file_lock 并发执行
```

两个阶段串行：阶段一全部完成之后，阶段二才开始。这样确保 bash（如编译/测试）不会在文件写入完成之前开始执行。

执行时序示例（同一文件 `foo.py` + 一个 `bash`，4 个 tool call）：

```
时间 →
  阶段一（file ops）
    replace(foo.py)  ──[获取写锁]══════[执行]══════[释放写锁]──
    read(foo.py)     ──────────────────[获取读锁][执行][释放]──
    write(foo.py)    ──────────────────────────────[获取写锁]...
  阶段二（non-file ops，等待阶段一全部完成）
    bash(pytest)     ───────────────────────────────────[执行]──
```

## Data Model

`_FileRWLock` — 单文件读写锁，无持久化状态：

```
_FileRWLock
├── _condition: asyncio.Condition  (同步原语)
├── _readers: int                  (当前持有读锁的协程数)
└── _writer_active: bool           (是否有写者持有锁)
```

锁管理器为 `dict[str, _FileRWLock]`，key 为 `os.path.normpath` 归一化后的文件路径。每次 `_execute_approved_batch` 调用创建新实例，batch 结束后自动回收。

## API Contract

### `_FileRWLock`

```python
class _FileRWLock:
    async def acquire_read(self) -> None: ...
    async def release_read(self) -> None: ...
    async def acquire_write(self) -> None: ...
    async def release_write(self) -> None: ...
```

### `_extract_file_paths`

```python
def _extract_file_paths(tool_call: dict) -> list[str]:
    """从 tool call args 中提取需要加锁的文件路径列表。
    
    read/write/replace → [file_path]
    file(create/delete) → [file_path]
    file(move)          → [file_path, dest_path]  (排序以避免死锁)
    其他                → []
    """
```

### `_execute_approved_batch` 修改

原 `execute_one_limited` 替换为两个 executor 函数，按是否有文件路径分流到两个串行阶段：

```python
# 分流
file_calls = [tc for tc in calls if _extract_file_paths(tc)]
other_calls = [tc for tc in calls if not _extract_file_paths(tc)]

# 阶段一：文件操作，同文件受 rwlock 约束
async def execute_one_file_locked(tc):
    paths = sorted(set(_extract_file_paths(tc)))
    is_write = tc.get("name") in ("write", "replace", "file")
    rw_locks = [file_lock_manager[p] for p in paths]
    if is_write:
        for lk in rw_locks: await lk.acquire_write()
    else:
        for lk in rw_locks: await lk.acquire_read()
    try:
        return await execute_one_fn(tc)
    finally:
        for lk in rw_locks:
            if is_write: await lk.release_write()
            else: await lk.release_read()

# 阶段二：非文件操作（bash 等），无额外锁
async def execute_one_no_file_lock(tc):
    return await execute_one_fn(tc)

await asyncio.gather(*[execute_one_file_locked(tc) for tc in file_calls])
await asyncio.gather(*[execute_one_no_file_lock(tc) for tc in other_calls])
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| tool call 不包含文件参数（bash/agent） | `_extract_file_paths` 返回 `[]`，不加锁，直接执行 |
| file move 同时锁定源和目标 | 按 `sorted()` 排序后依次获取，避免 AB-BA 死锁 |
| 锁内 `execute_one_fn` 抛出异常 | finally 块确保锁释放，不影响其他 tool call |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 读写锁而非完全串行 | 所有同文件操作全串行 | 同文件多个 read 常见且无害，串行浪费并发度 |
| per-batch 锁管理器 | 全局锁管理器 | batch 间本身串行，全局锁无必要且增加复杂度 |
| `asyncio.Condition` 实现 | 手动 Event+Lock 组合 | Condition 语义更清晰，避免 notify/set 竞态 |
| `file` move 对两端加锁 | 只锁 file_path | move 会影响 dest_path 上的后续操作，需同时保护 |
| file/non-file 分阶段执行 | 全部混在一起通过 rwlock 控制 | bash 需要等待文件写入完成后才能编译/测试，分阶段语义更清晰，也避免 bash 被不必要的 rwlock 检查影响 |
| bash 不加锁

## Open Questions

- [ ] 是否需要记录锁等待时间到 trace 中用于性能分析？
