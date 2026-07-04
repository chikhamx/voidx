# 项目-目录-会话分层侧栏与会话切换 — 技术设计文档

> **Status: Done**

Date: 2026-07-04

## Context

当前侧栏（`frontend/src/sidebar.ts`）是扁平的会话列表：所有会话平铺在 `#session-list` 下，仅按 `ThreadInfo` 渲染单层条目。用户希望侧栏改为三层结构：

1. **第一层：项目** — 当前 workspace（例如 `voidx`）。
2. **第二层：目录** — 项目下按目录分组（例如 `Frameworks`、`opt`、`.claude`、`Downloads`）。
3. **第三层：会话** — 每个目录下挂自己的会话列表。

交互要求：

- 每个目录行可创建新会话（目录级"新建会话"按钮）。
- 点击任意目录下的会话可切换；中间内容区（transcript）随切换的会话刷新，包含新会话的空状态。
- 不同层级会话可随用户点击自由切换。

参考交互见 `.voidx/attachments/clipboard-20260704-133819-15bd473e.png`（Claude 风格项目/目录/会话树）。

## Goals and Non-Goals

### Goals

- 侧栏从扁平会话列表升级为 项目 → 目录 → 会话 三层树。
- 每个目录行提供"新建会话"入口，新建会话归属该目录。
- 切换会话时中间 transcript 区域完整刷新（含空会话）。
- 后端 `session.create` 支持指定目录；snapshot 返回可分组的 thread metadata。
- 保持现有 `session.switch / fork / delete / rename` 语义不变，仅扩展分组维度。

### Non-Goals

- 不做多项目切换器（项目层暂为当前 workspace 名，后续再扩展项目下拉）。
- 不实现目录的增删改 UI（目录来自 workspace 实际结构或 thread metadata，不在本版做目录管理）。
- 不改变 transcript 渲染逻辑本身，只改切换时的数据来源。
- 不做会话跨目录移动。

## Current State

关键文件与现状：

- `frontend/src/sidebar.ts`
  - `ThreadInfo { thread_id, title?, status? }` — 前端本地接口，无分组字段。
  - `renderSidebar(threads, activeThreadId)` — 平铺渲染 `#session-list`。
  - `addThread / updateThreadStatus / filterSessions` — 均基于扁平 `.vx-session-item`。
  - `onThreadSelect / onNewThread / onThreadFork / onThreadDelete / onThreadRename` — 回调注册。
- `frontend/src/main.ts`
  - `handleNotification("workspace.snapshot", ...)` — 用 `params.threads` 调 `renderSidebar`，用 `params.active_snapshot` 调 `renderTranscript`。
  - `onThreadSelect` → `rpcCall("session.switch", { thread_id })` → 更新 `uiState.sessionId` + `updateStatusBar`。
  - `onNewThread` → `rpcCall("session.create", {})` → `addThread` + 设 active。
  - `onThreadDelete / onThreadRename / onThreadFork` — 对应 RPC。
- `src/voidx/ui/protocol/v2/threads.py`
  - `ThreadInfo` 已有 `workspace: str = "."` 字段，但前端未使用，且无目录维度。
- `src/voidx/ui/protocol/v2/snapshot.py`
  - `WorkspaceSnapshot { threads, active_thread_id, active_snapshot }` — threads 为 `list[ThreadInfo]`。
- `src/voidx/ui/gateway/session.py`
  - `GatewaySession` 维护 `_threads: dict[str, ThreadInfo]`、`_active_thread_id`。
  - `register_thread / unregister_thread / list_threads / switch_thread`。
  - `_register_default_methods` 注册 `session.create / switch / fork / delete / rename`。
- `frontend/test/sidebar.test.ts` — 现有侧栏测试，需扩展为树形结构测试。

## Architecture

### 分层模型

```
Project (workspace name, 例如 "voidx")
└── Directory (相对 workspace 的目录路径, 例如 "Frameworks" / "opt" / ".claude")
    └── Session (thread_id, title, status)
```

- **项目层**：取 `workspace` basename，与现有 `workspaceBasename()`（`main.ts:755`）一致。V1 单项目，不可切换。`renderSidebar` 新增 `projectName` 参数，由 `main.ts` 调用 `workspaceBasename(uiState.workspace)` 派生后传入（`main.ts:411` 调用点）。
- **目录层**：每个会话归属一个目录。目录来源为 thread 的 `directory` 字段（相对 workspace 的路径）。空字符串或 `.` 归为根目录组，显示名为项目名或 "Root"。
- **会话层**：现有 `ThreadInfo`，增加 `directory` 字段后按目录分组渲染。

### 数据流

```
后端 ThreadInfo.directory
  → WorkspaceSnapshot.threads[]
    → workspace.snapshot 通知
      → frontend renderSidebar(threads, activeThreadId)
        → 按 directory 分组 → 项目/目录/会话树渲染
```

切换会话：

```
用户点击会话
  → onThreadSelect(thread_id)
  → rpcCall("session.switch", { thread_id })
  → 后端 switch_thread → broadcast_snapshot
  → workspace.snapshot → renderTranscript(active_snapshot)
  → 中间区域刷新（含空会话空状态）
```

新建目录会话：

```
用户在目录行点击"新建会话"
  → onNewThread(directory)
  → rpcCall("session.create", { directory })
  → 后端创建 thread，绑定 directory
  → 返回 { thread_id, title, status, directory }
  → addThread(thread, activeThreadId) 插入对应目录组
  → 自动切换为 active
```

## Data Model

### ThreadInfo 扩展（后端 + 前端）

```
ThreadInfo
├── thread_id: str
├── title: str = ""
├── workspace: str = "."          # 已有
├── directory: str = ""           # 新增：相对 workspace 的目录路径，"" 或 "." 表示根
├── model_provider: str = ""
├── model_name: str = ""
├── status: Literal["idle","running"] = "idle"
├── created_at: str = ""
├── updated_at: str = ""
└── message_count: int = 0
```

- 后端 `src/voidx/ui/protocol/v2/threads.py` 的 `ThreadInfo` 增加 `directory` 字段。
- 前端 `frontend/src/sidebar.ts` 的 `ThreadInfo` 接口仅增加 `directory?: string`；后端 `ThreadInfo` 的其余字段（`model_provider`/`model_name`/`created_at`/`updated_at`/`message_count`）前端不消费，无需对齐。

### SessionInfo 扩展（持久化层）

`directory` 需要持久化，否则重启后丢失。涉及 `src/voidx/memory/session.py` 与 `src/voidx/memory/store.py`：

```
SessionInfo (session.py)
├── id: str
├── title: str = "New session"
├── workspace: str = "."
├── directory: str = ""           # 新增：相对 workspace 的目录路径，"" 表示根
├── model_provider: str = "anthropic"
├── model_name: str = "claude-sonnet-4-6"
├── created_at / updated_at / message_count
```

- `sessions` 表（`store.py` `_init_schema`）`CREATE TABLE` 增加 `directory TEXT NOT NULL DEFAULT ''` 列。
- 迁移：参照现有 `message_count` 的 `ALTER TABLE sessions ADD COLUMN directory TEXT NOT NULL DEFAULT ''`（`store.py:147` 模式，`try/except sqlite3.OperationalError: pass`）。
- `create_session`（`session.py:52`）：签名增加 `directory: str = ""` 参数；INSERT 语句与参数补 `directory`；构造 `SessionInfo` 时写入。
- `fork_session`（`session.py:171`）：从 `source.directory` 复制到新会话（与 `workspace` 同理）；INSERT 与 `SessionInfo` 构造补 `directory`。
- `get_session` / `list_sessions`（`session.py:72` / `:86`）：SELECT 已用 `SELECT *`，构造 `SessionInfo` 时补 `directory=row["directory"]`。

### 前端分组结构（运行时派生，非持久化）

```typescript
interface DirectoryGroup {
  directory: string;       // 相对路径，"" 为根
  label: string;           // 显示名，根目录用项目名或 "Root"
  sessions: ThreadInfo[];
}
```

## API Contract

### session.create（扩展）

- **Method**: `session.create`
- **Params**: `{ directory?: string }` — 新会话归属目录，缺省为根。
- **Response**: `{ thread_id, title, status, directory }`
- **变更**（gateway 链路，`src/voidx/ui/gateway/session.py`）:
  - `register_thread`（`:186`）签名扩展为 `(thread_id, *, title="", directory="", workspace=".")`，构造 `ThreadInfo` 时写入 `directory`/`workspace`。
  - `_method_session_create`（`:404`）：从 `params` 取 `directory`，传给 `create_session(directory=...)`；调用 `register_thread` 时传入 `directory`；返回值补 `directory` 字段。
  - `_method_session_fork`（`:415`）：fork 后的 `register_thread` 传入 `info.directory`（`create_session` 已持久化，`SessionInfo` 携带）；返回值补 `directory`。

### session.switch（不变）

- **Method**: `session.switch`
- **Params**: `{ thread_id }`
- **Response**: `{ active_thread_id }`
- **行为**: 切换 active thread 并广播 snapshot，前端据 `active_snapshot` 刷新 transcript。

### workspace.snapshot（扩展）

- **Method**: `workspace.snapshot`（通知）
- **Params**: `WorkspaceSnapshot`，其中 `threads[].directory` 新增字段。
- **前端处理**: `renderSidebar` 按 `directory` 分组渲染三层树。

### 前端 sidebar.ts 新增/修改函数

```typescript
// 按目录分组
function groupByDirectory(threads: ThreadInfo[], projectName: string): DirectoryGroup[]

// 渲染三层树
export function renderSidebar(threads: ThreadInfo[], activeThreadId: string | null, projectName: string): void

// 在指定目录插入新会话
export function addThread(thread: ThreadInfo, activeThreadId: string | null): void

// 新建会话回调带目录参数
export function onNewThread(callback: (directory: string) => void): void
```

## UI Structure

侧栏 DOM 结构（示意）：

```html
<aside id="sidebar">
  <div class="vx-sidebar-header">
    <span class="vx-project-name">voidx</span>
    <!-- 后续可放项目切换器 -->
  </div>
  <div id="session-list">
    <!-- 目录组 -->
    <div class="vx-directory-group" data-directory="Frameworks">
      <div class="vx-directory-row">
        <span class="vx-directory-name">Frameworks</span>
        <button class="vx-directory-new-chat">+</button>
      </div>
      <div class="vx-session-children">
        <div class="vx-session-item" data-thread-id="...">...</div>
      </div>
    </div>
    <!-- 根目录组（directory=""） -->
    <div class="vx-directory-group" data-directory="">
      ...
    </div>
  </div>
</aside>
```

- 目录行：名称 + 新建会话按钮（`+`）。点击时从 `data-directory` 取目录路径传给 `onNewThread(directory)`。
- 根目录组同样有新建按钮，`data-directory=""`，点击调 `onNewThread("")`。
- 会话条目：复用现有 `.vx-session-item` 结构（title / status dot / 菜单按钮）。
- 根目录组放最后或最前，显示项目名或 "Root"。
- `onNewThread` 回调签名从 `() => void` 改为 `(directory: string) => void`（`sidebar.ts:10` 的 `newThreadCb` 类型同步改）。目录行按钮通过闭包或 `dataset.directory` 绑定；`main.ts:244` 的 `onNewThread(() => {...})` 改为 `onNewThread((directory) => rpcCall("session.create", { directory }))`。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `session.create` 指定目录不存在 | 后端按目录路径创建 thread metadata，不要求目录真实存在；前端按 directory 字段分组 |
| `session.switch` 的 thread 不存在 | 后端返回 `MethodParamsError`，前端 console.warn，不切换 UI |
| snapshot 中 thread 无 `directory` 字段 | 前端视为根目录（`""`） |
| 目录分组为空 | 不渲染该目录组 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 目录作为 thread 字段而非独立实体 | 独立 Directory 表 | 最小改动，复用现有 ThreadInfo；避免引入目录管理复杂度 |
| 根目录用空字符串表示 | 用 "." 或 "Root" 常量 | 与 workspace 相对路径语义一致，空串天然表示根 |
| 项目层暂不可切换 | 多项目下拉 | 本版聚焦目录-会话分层；项目切换需额外 workspace 管理，列为后续 |
| 前端运行时派生分组 | 后端返回树结构 | 后端保持扁平 thread 列表，前端负责分组渲染，降低协议复杂度 |

## Open Questions

- [x] 目录排序规则：按字母序 + 根目录置顶（已实现，见 `groupByDirectory` 排序逻辑）。
- [x] 目录折叠状态是否持久化（localStorage）？V1 默认展开，不持久化，后续再加。
- [x] `session.create` 的 directory 是否需要校验真实存在？不校验，纯 metadata。

## Implementation Outline

1. **后端协议**：`ThreadInfo`（`threads.py`）增加 `directory` 字段。
2. **后端持久化**：`SessionInfo`（`session.py:27`）增加 `directory`；`sessions` 表 `CREATE TABLE` + `ALTER TABLE` 迁移加 `directory` 列（`store.py`）；`create_session` / `fork_session` 的 INSERT 与构造补 `directory`；`get_session` / `list_sessions` 构造补 `directory=row["directory"]`。
3. **后端 gateway**：`register_thread`（`session.py:186`）签名扩展；`_method_session_create` / `_method_session_fork` 传 `directory` 并在返回值补 `directory`；snapshot 自然携带。
4. **前端 sidebar.ts**：`ThreadInfo` 加 `directory?`；新增 `groupByDirectory`；`renderSidebar` 改为三层树渲染并接收 `projectName` 参数；`onNewThread` 回调签名改为 `(directory: string) => void`。
5. **前端 main.ts**：`handleNotification("workspace.snapshot")`（`:411`）调 `workspaceBasename(uiState.workspace)` 传 `projectName`；`onNewThread`（`:244`）改为 `(directory) => rpcCall("session.create", { directory })`。
6. **测试**：`frontend/test/sidebar.test.ts` 覆盖目录分组、新建目录会话、跨目录切换、空会话渲染；后端测试覆盖 `session.create` 带 directory、`SessionInfo.directory` 持久化与读取、`fork_session` 复制 directory。
