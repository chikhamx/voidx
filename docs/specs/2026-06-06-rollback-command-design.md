# `/rollback` 命令设计

> **Status: In Progress**

## 问题

`SessionChangeTracker` 已有完整的文件快照和回滚基础设施（`capture_file`、`rollback_current`、`RollbackResult`），但没有用户入口。用户无法在 agent 修改文件后撤销改动，只能手动 `git checkout` 或丢弃工作区变更。

## 设计目标

- 用户可通过 `/rollback` 查看当前轮次被修改的文件列表
- 用户可通过 `/rollback <file>` 回滚指定文件
- 用户可通过 `/rollback all` 回滚所有文件
- 回滚后给出清晰的反馈（恢复/删除/错误）
- 不回滚的文件保持不变

## 现有基础设施

```
SessionChangeTracker
├── _snapshots: dict[str, FileSnapshot]  ← 文件原始内容快照
│   └── FileSnapshot(path, resolved_path, existed, content)
├── _files: dict[str, FileChangeRecord]  ← 变更统计（用于展示）
│   └── FileChangeRecord(path, added, removed)
├── rollback_current() → RollbackResult  ← 全量回滚（已实现）
│   └── RollbackResult(restored, removed, errors)
└── _visible: bool  ← finish_turn 后为 True
```

`rollback_current()` 是全量回滚——恢复所有快照文件、删除所有新建文件。需要扩展为支持单文件回滚。

## 方案

### 1. `SessionChangeTracker` 新增单文件回滚

```python
def rollback_file(self, path: str) -> RollbackResult:
    """Roll back a single file by its display path."""
```

逻辑：
- 遍历 `_snapshots`，找到 `snapshot.path == path` 的条目
- 执行与 `rollback_current` 相同的恢复/删除逻辑，但只针对这一个文件
- 从 `_snapshots` 和 `_files` 中移除该条目
- 返回 `RollbackResult`

`rollback_current()` 改为遍历所有快照调用 `rollback_file`，复用逻辑。

### 2. `/rollback` slash 命令

**无参数** — 列出当前可回滚的文件：

```
/rollback
```

输出示例：
```
Modified files this turn:
  [cyan]src/voidx/ui/session.py[/cyan]  [green]+12[/green] [red]−3[/red]
  [cyan]tests/test_ui_events.py[/cyan]  [green]+80[/green] [red]−0[/red]

Usage: /rollback <file> or /rollback all
```

**指定文件** — 回滚单个文件：

```
/rollback src/voidx/ui/session.py
```

输出：
```
Restored: src/voidx/ui/session.py
```

**`all`** — 回滚所有文件：

```
/rollback all
```

输出：
```
Restored: src/voidx/ui/session.py, tests/test_ui_events.py
```

### 3. 改动清单

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/session.py` | 新增 `rollback_file(path)` 方法；`rollback_current()` 复用之 |
| `src/voidx/agent/slash/handler.py` | 新增 `/rollback` handler → `_rollback(args)` |
| `src/voidx/ui/commands.py` | 注册 `/rollback` 和 `/rollback all` 到 COMMANDS 列表 |
| `tests/test_ui_session_changes.py` | 新增 `test_rollback_single_file`、`test_rollback_all`、`test_rollback_nonexistent` |

### 4. 不需要改动的文件

| 文件 | 原因 |
|------|------|
| `src/voidx/agent/graph/tool_execution.py` | `capture_tool_call` 已正确调用 |
| `src/voidx/agent/graph/run_loop.py` | `begin_turn`/`finish_turn` 已正确调用 |
| `src/voidx/ui/output/dock/` | 回滚不需要 dock 交互 |

### 5. 边界情况

- **无变更时** `/rollback`：打印 `[dim]No changes to roll back.[/dim]`
- **文件路径不匹配**：打印 `[dim]No snapshot found for: {path}[/dim]`，列出可用路径
- **回滚后 `_visible` 状态**：如果 `_snapshots` 为空，设 `_visible = False`
- **快照 key 是 resolved 绝对路径，display path 是相对路径**：`rollback_file` 接受 display path，内部按 `snapshot.path` 匹配
- **同一文件多次编辑**：快照只保留第一次编辑前的内容（`capture_file` 有去重），回滚恢复到第一次编辑前——这是正确行为
