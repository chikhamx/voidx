# Session 存储架构重构：JSONL Append-Only + SQLite 索引

> **Status: In Progress**
> Created: 2026-06-09

## 1. 问题

当前 voidx 所有 session 数据存入单一 SQLite DB（`~/.voidx/voidx.db`），存在以下问题：

### 1.1 DB 膨胀

`context_frames.messages_json` 将全量 LLM 消息序列化后存入 SQLite。单 session 可产生数百帧，每帧 ~0.5 MB，清理前实测 **906 MB** 占了 DB 的 96%。

`transcript_nodes` 每次 `_persist_transcript_snapshot` 执行 **DELETE 全部 + INSERT 全部**，无增量能力，写入放大严重。

### 1.2 无清理策略

Session 无限期累积。清理前 586 个 session 中 115 个空 session、169 个测试 session。只能手动操作 DB。

### 1.3 Subagent transcript 混杂

Subagent 的 UI 节点混在主 session 的 `transcript_nodes` 里，靠 `agent_run_id` 字段区分。无法独立存储、独立重放、独立清理。

### 1.4 命令扁平

`/list`、`/resume`、`/clear` 散落在顶层命令空间，无 `/session` 命名空间，无法扩展 `del`、`new` 等子命令。

## 2. 目标

1. **JSONL append-only** 存储 transcript 和 context frames，SQLite 只存索引和元数据
2. **可配置清理策略**，通过 `/session del` 交互式删除过期 session
3. **Transcript 重放**：从 JSONL 逐行读取重建 OutputTree
4. **Subagent parent-child chain**：独立 JSONL 文件 + subpath 引用
5. **`/session` 命令体系**：`list` / `new` / `resume` / `del`

## 3. 设计

### 3.1 存储分层

```
~/.voidx/
├── voidx.db                              # SQLite：索引 + 元数据 + 状态
├── sessions/
│   └── <session-id>/
│       ├── transcript.jsonl              # 主对话 transcript
│       ├── context/
│       │   └── <frame-id>.jsonl          # 上下文缓存帧
│       └── subagents/
│           └── <agent-run-id>.jsonl      # subagent transcript
```

**原则**：大体积序列化数据（transcript 节点、context frame 消息）走 JSONL 文件；需要查询和关联的元数据走 SQLite。

### 3.2 JSONL Transcript 格式

每行一个 JSON 对象。参考 Claude Code 的 record type 体系，但适配 voidx 的 OutputTree 模型。

#### Record 类型

| type | 用途 | 必选字段 | 替代现有 |
|------|------|----------|----------|
| `turn_start` | turn 开始 | `turn_id`, `timestamp`, `user_text` | `turns` 表 INSERT |
| `turn_end` | turn 结束 | `turn_id`, `timestamp` | `turns` 表 UPDATE |
| `node` | 新增 UI 节点 | `turn_id`, `node_id`, `node_type`, `header` | `transcript_nodes` INSERT |
| `node_update` | 节点增量更新 | `turn_id`, `node_id` + 变更字段 | `transcript_nodes` UPDATE |
| `summary` | compaction 摘要 | `turn_id`, `content` | `compaction_summary` 字段 |
| `context_frame` | 上下文缓存帧 | `frame_id`, `prefix_hash`, `frame_hash`, `message_count`, `token_estimate` | `context_frames` INSERT |

#### 示例

```jsonl
{"type":"turn_start","turn_id":0,"timestamp":"2026-06-09T07:23:50Z","user_text":"修复TODO固定框重复渲染"}
{"type":"node","turn_id":0,"node_id":0,"parent_node_id":null,"sort_order":0,"node_type":"assistant","header":"assistant","status":"running","metadata":{"tree_id":"a1b2"}}
{"type":"node","turn_id":0,"node_id":1,"parent_node_id":0,"sort_order":1,"node_type":"tool_call","header":"Read file","tool_call_id":"tc_1","status":"done"}
{"type":"node_update","turn_id":0,"node_id":0,"status":"done","elapsed":1.2}
{"type":"turn_end","turn_id":0,"timestamp":"2026-06-09T07:26:08Z"}
{"type":"summary","turn_id":0,"content":"修复了 TodoUpdated 双写问题..."}
```

#### 设计决策

- **`node` vs `node_update`**：新增节点用 `node`（全字段），状态变更用 `node_update`（只写变更字段）。重放时先建节点再 patch，避免全量替换。
- **`summary`**：compaction 产生的摘要。重放时遇到 `summary`，跳过该 turn 之前的 node 记录，只保留 summary 内容。这是懒加载的基础。
- **字段映射**：`node` 的字段与现有 `TranscriptNodeRow` 一一对应，迁移成本低。

### 3.3 SQLite 保留的职责

SQLite 只存索引和元数据，不再存大体积序列化数据。

| 表 | 保留 | 变更 |
|---|---|---|
| `sessions` | ✅ | 不变 |
| `messages` | ✅ | 不变（消息量小，查询频繁，需要 JOIN） |
| `turns` | ✅ | 不变 |
| `transcript_nodes` | ❌ 废弃 | 数据迁移到 JSONL，Phase 4 删除表 |
| `context_frames` | ✅ 瘦身 | 移除 `messages_json`，新增 `file_path` 指向 JSONL |
| `session_runtime_state` | ✅ | 不变 |
| `session_task_runs` | ✅ | 不变 |
| `message_runtime_snapshots` | ✅ | 不变 |

#### context_frames 瘦身后的 schema

```sql
CREATE TABLE context_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_message_id INTEGER,
    frame_kind TEXT NOT NULL DEFAULT 'main',
    agent_role TEXT NOT NULL DEFAULT 'orchestrator',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prefix_hash TEXT NOT NULL,
    frame_hash TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL,       -- 新增：指向 context/<frame-id>.jsonl
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

`messages_json` 列移除。加载 context frame 时，先从 SQLite 查索引（prefix_hash、frame_hash），再从 `file_path` 指向的 JSONL 读取消息内容。

### 3.4 Transcript 重放

#### 现有流程

```
load_transcript(session_id)           # SQLite SELECT
  → TranscriptNodeRow[]
  → transcript_rows_to_tree(rows)     # 构建 OutputTree
  → dock.restore_tree(tree)
```

#### 新流程

```
replay_transcript_jsonl(session_id)   # 逐行读取 JSONL
  → 构建 OutputTree
  → dock.restore_tree(tree)
```

#### 重放器实现

```python
# src/voidx/memory/jsonl_replay.py

async def replay_transcript_jsonl(session_id: str) -> OutputTree | None:
    path = _session_dir(session_id) / "transcript.jsonl"
    if not path.exists():
        return None

    tree = OutputTree()
    nodes: dict[tuple[int, int], OutputNode] = {}  # (turn_id, node_id) → node
    current_turn_id = -1

    async for line in _alines(path):
        record = json.loads(line)
        match record["type"]:
            case "turn_start":
                current_turn_id = record["turn_id"]
            case "node":
                node = _node_from_record(record)
                nodes[(record["turn_id"], record["node_id"])] = node
                parent = nodes.get((record["turn_id"], record["parent_node_id"]))
                tree.add_node(parent or tree.root, node)
            case "node_update":
                _patch_node(nodes, record)
            case "summary":
                # 跳过该 turn 之前的 node，只保留 summary
                _summarize_turn(tree, nodes, record)

    tree.mark_dirty()
    return tree
```

#### 与现有代码的衔接

`transcript_mixin.py` 修改为：

```python
async def _restore_transcript_snapshot(self, *, append=False):
    if self._session is None:
        return False
    active_dock = get_dock()
    if active_dock is None:
        return False

    # 优先 JSONL 重放，fallback 到 SQLite
    tree = await replay_transcript_jsonl(self._session.id)
    if tree is None:
        rows = await load_transcript(self._session.id)
        if not rows:
            return False
        tree = transcript_rows_to_tree(rows)

    active_dock.restore_tree(tree, append=append)
    return True
```

### 3.5 Subagent Parent-Child Chain

每个 subagent 独立 JSONL 文件，主 transcript 通过引用关联。

#### 主 transcript.jsonl

```jsonl
{"type":"node","turn_id":2,"node_id":5,"node_type":"subagent","agent_run_id":"agent_0","header":"🔍 explore","status":"running"}
{"type":"node_update","turn_id":2,"node_id":5,"status":"done","elapsed":3.2}
```

#### subagents/agent_0.jsonl

```jsonl
{"type":"subagent_start","parent_session_id":"abc123","parent_turn_id":2,"parent_node_id":5,"agent_name":"explore","timestamp":"..."}
{"type":"node","turn_id":0,"node_id":0,"node_type":"assistant","header":"assistant","status":"running"}
```

#### 恢复流程

1. 重放主 `transcript.jsonl`
2. 遇到 `node_type=subagent` 时，记录 `agent_run_id`
3. 如需完整恢复 subagent 内容，重放 `subagents/<agent_run_id>.jsonl`
4. 将 subagent tree 挂载到对应 parent node 下

#### 与现有代码的衔接

当前 subagent 节点通过 `agent_run_id` 字段区分，已在 `OutputNode` 和 `TranscriptNodeRow` 中存在。JSONL 写入时将 `agent_run_id` 不为空的节点路由到对应 subagent 文件即可。

### 3.6 `/session` 命令体系

| 命令 | 功能 | 替代现有 |
|------|------|----------|
| `/session list` | 列出历史 session | `/list` |
| `/session new` | 新建空 session | `/clear` |
| `/session resume <id>` | 恢复指定 session | `/resume` |
| `/session del` | 弹出选择器删除旧 session | **新增** |
| `/session del 7d` | 删除 7 天前的 session | **新增** |
| `/session del 15d` | 删除 15 天前的 session | **新增** |
| `/session del 30d` | 删除 30 天前的 session | **新增** |
| `/session del all` | 删除所有 session | **新增** |

#### `/session del` 交互流程

无参数时弹出选择器：

```
Delete sessions older than:
  > 7 days
    15 days
    30 days
    All sessions
    Cancel
```

选择后显示预览：

```
Will delete 42 sessions (7 days old):
  - 12 empty sessions
  - 30 sessions with messages
  - Disk space to reclaim: ~23 MB
Confirm? [y/N]
```

确认后执行删除：SQLite 行 + JSONL 文件 + context 文件，一条不留。

#### 向后兼容

- `/clear` → `/session new` 别名，不破坏现有习惯
- `/list` → `/session list` 别名
- `/resume` → `/session resume` 别名

#### 命令注册

```python
# handler.py dispatch 新增
"/session": lambda: self._session_dispatch(args),
```

```python
# session.py
async def _session_dispatch(self, args: str) -> None:
    parts = args.strip().split(maxsplit=1)
    sub = parts[0] if parts else ""
    sub_args = parts[1] if len(parts) > 1 else ""

    match sub:
        case "list" | "ls" => await self._list_sessions()
        case "new" => await self._clear()
        case "resume" => await self._resume(f"/session resume {sub_args}")
        case "del" | "delete" => await self._delete_sessions(sub_args)
        case _ => ui.print("[dim]Usage: /session list|new|resume|del[/dim]")
```

### 3.7 Session 清理实现

```python
# src/voidx/memory/cleanup.py

from datetime import datetime, timezone, timedelta
from pathlib import Path

async def delete_sessions_older_than(days: int) -> int:
    """Delete sessions not updated in the last N days. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # 1. 找到要删除的 session
    rows = await _fetch_all(
        "SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return 0

    # 2. 删除 JSONL 文件和目录
    for sid in ids:
        session_dir = _session_dir(sid)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    # 3. 删除 SQLite 数据（CASCADE 会清理关联表）
    placeholders = ",".join("?" * len(ids))
    await _execute_commit(
        f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
    )

    return len(ids)


def _session_dir(session_id: str) -> Path:
    return Path.home() / ".voidx" / "sessions" / session_id
```

### 3.8 JSONL 写入器

```python
# src/voidx/memory/jsonl_store.py

import json
from pathlib import Path
from typing import Any

_async_lock = asyncio.Lock()

def _session_dir(session_id: str) -> Path:
    return Path.home() / ".voidx" / "sessions" / session_id


async def append_transcript(session_id: str, records: list[dict[str, Any]]) -> None:
    """Append records to session transcript JSONL."""
    path = _session_dir(session_id) / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _async_lock:
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def append_context_frame(
    session_id: str, frame_id: str, messages: list[dict[str, Any]]
) -> Path:
    """Write context frame messages to JSONL, return file path."""
    dir_path = _session_dir(session_id) / "context"
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{frame_id}.jsonl"
    async with _async_lock:
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return path


async def append_subagent_transcript(
    session_id: str, agent_run_id: str, records: list[dict[str, Any]]
) -> None:
    """Append records to subagent transcript JSONL."""
    path = _session_dir(session_id) / "subagents" / f"{agent_run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _async_lock:
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
```

## 4. 迁移计划

### Phase 1：JSONL 写入层（双写，不破坏现有）

- 新增 `src/voidx/memory/jsonl_store.py`
- 修改 `transcript_mixin.py`：`_persist_transcript_snapshot` 同时写 SQLite + JSONL
- 修改 `context_frames.py`：`save_context_frame` 同时写 SQLite 索引 + JSONL 文件
- 新增 `src/voidx/memory/jsonl_replay.py`
- **验证**：JSONL 文件与 SQLite 数据一致

### Phase 2：JSONL 读取层

- 修改 `_restore_transcript_snapshot`：优先从 JSONL 重放，fallback 到 SQLite
- 修改 `load_context_frames`：从 JSONL 文件读取 messages，SQLite 只查索引
- **验证**：重放结果与现有 SQLite 读取一致

### Phase 3：命令体系 + 清理

- 新增 `/session` 命令命名空间
- 实现 `/session del` 交互式清理
- 旧命令 `/list` → `/session list`，`/clear` → `/session new` 别名
- **验证**：命令交互正确，清理逻辑安全

### Phase 4：移除 SQLite 大字段

- `transcript_nodes` 表标记废弃，不再写入
- `context_frames` 移除 `messages_json` 列，改用 `file_path`
- 提供一次性迁移脚本将旧数据转为 JSONL
- **验证**：全量测试通过，DB 体积显著缩小

## 5. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/voidx/memory/jsonl_store.py` | 新增 | JSONL append-only 写入器 |
| `src/voidx/memory/jsonl_replay.py` | 新增 | JSONL 重放器 |
| `src/voidx/memory/cleanup.py` | 新增 | Session 清理策略 |
| `src/voidx/memory/transcript.py` | 修改 | 新增 `append_transcript_jsonl()` |
| `src/voidx/memory/context_frames.py` | 修改 | `messages_json` → `file_path` |
| `src/voidx/memory/store.py` | 修改 | schema 新增 `context_frames.file_path` |
| `src/voidx/agent/graph/transcript_mixin.py` | 修改 | 双写 + JSONL 优先读取 |
| `src/voidx/agent/slash/session.py` | 修改 | `/session` 命令体系 |
| `src/voidx/agent/slash/handler.py` | 修改 | 注册 `/session` 子命令 |
| `src/voidx/ui/commands.py` | 修改 | `/session` 命令注册 |
| `src/voidx/ui/transcript.py` | 修改 | 支持从 JSONL replay 结果构建 tree |

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| JSONL 文件损坏 | 逐行解析，跳过坏行；SQLite 作为 fallback |
| 迁移期间数据不一致 | Phase 1-2 双写，确保至少一份数据完整 |
| 大 session JSONL 重放性能 | 懒加载：只重放最近 N 个 turn，旧 turn 用 summary |
| `/session del` 误删 | 交互确认 + 预览 + 只删过期 session |
| 并发写入 JSONL | asyncio lock，与现有 `_write_lock` 对齐 |
| 旧版本无法读取新格式 | Phase 1-2 双写期间保持 SQLite 完整，旧版本仍可读 |

## 7. 预期收益

| 指标 | 当前 | 重构后 |
|------|------|--------|
| DB 文件大小 | ~1 GB（清理后 113 MB） | < 10 MB（只有索引和元数据） |
| Transcript 持久化 | DELETE + INSERT 全量 | Append-only 增量 |
| Context frame 存储 | SQLite BLOB | JSONL 文件，按需加载 |
| Session 清理 | 手动操作 DB | `/session del` 交互式 |
| Subagent 隔离 | 混在主 transcript | 独立 JSONL + parent-child chain |
| 崩溃安全 | WAL 但全量替换风险 | Append-only，单行损坏可跳过 |
