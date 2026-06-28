# 桌面端 Codex 能力对齐 — 技术设计文档

Date: 2026-06-28

> **Status: Design** — 基于现有后端基础能力，规划五项 Codex 桌面端缺失能力的实现路径。

## Context

voidx 桌面端（Tauri 2 壳 + 原生 JS 前端）已实现 Codex 的基础 UI 能力（Markdown 渲染、
代码高亮、工具节点折叠、slash 补全、流式输出、todo 面板、subagent 展示）。但对比
Codex 桌面端的核心差异化能力，voidx 仍有四项缺口：

1. **Thread 持久化与多会话** — voidx 当前单会话运行，无法并行管理多个 agent 线程
2. **集成终端** — 无法在 UI 内实时观察 agent 执行的 shell 命令
3. **Diff review 增强** — 当前只有基础着色，缺 hunk 级 accept/reject 和 inline 评论
4. **协议升级** — 当前扁平事件流，缺 Thread/Turn/Item 三层原语，不利于多会话编排

> **Worktree 隔离**已从本期范围移除（后续再考虑）。多会话共享同一工作区。

**现有后端基础能力**（已验证存在，非新建）：

| 能力 | 现有实现 | 位置 |
|------|---------|------|
| Session CRUD | `create_session` / `list_sessions` / `get_session` / `delete_session` | `src/voidx/memory/session.py:52-176` |
| Session resume | `resume_session` 已在 graph 和 slash host 实现 | `src/voidx/agent/graph/core/_voidx_graph.py:318`、`src/voidx/agent/slash/host.py:256` |
| 消息持久化 | JSONL append-only + SQLite 索引，支持 `session_cleared` / `message_deleted` 级联 | `src/voidx/memory/session.py:181-274` |
| Context frames | 每轮快照持久化，支持回放 | `src/voidx/memory/context_frames.py` |
| Git worktree | `git worktree list` 已分类为只读，worktree 子命令已有基础支持 | `src/voidx/tools/git.py:244-245` |
| 结构化 diff | `parse_unified_diff` → `StructuredDiff{files: [FileDiff{hunks: [DiffHunk{lines}]}]}` | `src/voidx/diffing.py:50` |
| WebSocket gateway | `GatewayServer` + `GatewaySession`，支持 snapshot/event/command/request/response 五种 envelope | `src/voidx/ui/gateway/server.py`、`session.py` |
| 协议 envelope | `PROTOCOL_VERSION = 1`，Pydantic discriminated union | `src/voidx/ui/protocol/envelope.py:15` |
| Transcript snapshot | `TranscriptSnapshot{session_id, revision, nodes}` 已含 revision 字段 | `src/voidx/ui/protocol/transcript.py:36-42` |
| Tauri 壳侧 | `BackendStatus` 状态机、Python 子进程生命周期管理 | `desktop/src-tauri/src/main.rs` |

## Goals and Non-Goals

### Goals

- **多会话管理**：前端可创建/切换/fork/删除会话，每个会话独立 agent 运行
- **集成终端**：前端嵌入终端面板，实时显示 agent 的 bash 命令输出
- **Diff review**：hunk 级 accept/reject，支持 inline 评论，accept 后写回文件
- **协议升级**：引入 Thread/Turn/Item 三层原语，不兼容 v1（桌面端未发布，直接 breaking change）

### Non-Goals

- 不做 Computer Use（需额外原生集成，独立设计）
- 不做 Cloud 表面（需后端云服务，独立设计）
- 不做 IDE 扩展（独立项目）
- 不做多窗口（Tauri 多窗口复杂度高，先用 tab 切换）
- 不引入前端框架（保持原生 JS，当前 750 行可控）
- 不做 OS 级沙箱（保持现有路径检查 + 命令黑名单）
- 不做 Worktree 隔离（多会话共享同一工作区，后续再考虑）
- 不做协议 v1 向后兼容（桌面端未发布，v2 直接替换 v1）

## Architecture

### 整体架构（升级后）

```
┌──────────────────────────────────────────────────────────────┐
│  Tauri 2 壳 (desktop/src-tauri/)                              │
│  - 拉起 Python 后端 (voidx.main --web --web-headless)         │
│  - 暴露 get_gateway_url / get_backend_status                  │
│  - emit backend_ready / backend_failed                        │
│  - on_window_event: 关闭时 kill 后端进程                       │
└────────────────────────┬─────────────────────────────────────┘
                         │ WebSocket (ws://127.0.0.1:<port>/?token=...)
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  前端 (frontend/)                                             │
│                                                               │
│  src/main.js         ← WebSocket 连接 + 事件分发              │
│  src/render.js       ← 节点渲染                               │
│  src/markdown.js     ← Markdown 解析 + 代码高亮               │
│  src/slash.js        ← Slash 命令补全                         │
│  src/stream.js       ← 流式输出缓冲                           │
│  src/sessions.js     ← 新增：多会话 tab 管理                  │
│  src/terminal.js     ← 新增：集成终端 (xterm.js)              │
│  src/diff-review.js  ← 新增：hunk 级 diff review              │
│  styles.css          ← 组件样式                               │
│                                                               │
│  第三方库：                                                    │
│  - marked + highlight.js + DOMPurify (已有)                   │
│  - xterm.js (新增，~100KB gzipped，终端渲染)                  │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  Python 后端 (src/voidx/)                                     │
│                                                               │
│  ui/gateway/         ← WebSocket gateway (升级)               │
│    server.py         ← 多会话路由                             │
│    session.py        ← GatewaySession (升级为多会话)          │
│    terminal.py       ← 新增：PTY 会话管理                     │
│                                                               │
│  memory/             ← 会话持久化 (已有，扩展)                │
│    session.py        ← + fork_session                         │
│                                                               │
│  ui/protocol/        ← 协议 v2 (JSON-RPC 2.0，不兼容 v1)      │
│    envelope.py       ← JSON-RPC 消息模型                      │
│    methods.py        ← method 注册 + dispatch                 │
│    threads.py        ← Thread/Turn/Item 模型                  │
│                                                               │
│  diffing.py          ← 结构化 diff (已有，扩展 accept/reject) │
└──────────────────────────────────────────────────────────────┘
```

### 数据流（多会话）

```
前端 sessions.js                后端 GatewayServer
─────────────                  ──────────────────
点击 "New Session"
  │
  ├─ command: session.create ──► GatewaySession.handle_command()
  │                              ├─ memory/session.py: create_session()
  │                              └─ event: session.created {session_id}
  │◄───────────────────────────── broadcast
  │
切换到会话 tab
  ├─ command: session.switch ──► 挂起当前 turn，激活目标 session
  │                              └─ event: snapshot {session_id, revision, nodes}
  │◄─────────────────────────────
  │
在会话中提交消息
  ├─ command: submit ──────────► agent turn loop (该 session)
  │                              ├─ event: assistant_stream.updated
  │                              ├─ event: tool.started / tool.finished
  │                              └─ event: turn.completed
  │◄─────────────────────────────
```

## Data Model

### Session（扩展）

现有 `SessionInfo` 已有 id/title/workspace/model 字段。新增 status：

```
SessionInfo (扩展)
├── id: str                    (已有)
├── title: str                 (已有)
├── workspace: str             (已有)
├── model_provider: str        (已有)
├── model_name: str            (已有)
├── created_at: str            (已有)
├── updated_at: str            (已有)
├── message_count: int         (已有)
└── status: str                (新增: "active" | "idle" | "running")
```

**迁移策略**：SQLite `sessions` 表加一列 `status TEXT DEFAULT 'idle'`，
`ALTER TABLE` 向后兼容，旧数据 status='idle'。

### Terminal Session（新增，内存态）

```
TerminalSession (内存态，不持久化)
├── id: str                    终端会话 ID
├── session_id: str            关联的 agent 会话
├── pty_fd: int                PTY 文件描述符
├── cwd: str                   当前工作目录
├── history: list[str]         命令历史（最近 N 条）
└── alive: bool
```

### Diff Review（新增，内存态）

```
DiffReview (内存态)
├── id: str                    review ID
├── session_id: str
├── file_diffs: list[FileDiff] (复用现有 StructuredDiff)
├── decisions: dict[str, str]  hunk_key → "accept" | "reject" | "pending"
├── comments: dict[str, str]   hunk_key → comment text
└── created_at: str
```

`hunk_key` 格式：`"{file_path}:{old_start},{old_count}:{new_start},{new_count}"`，
从现有 `DiffHunk` 字段直接生成。

> **二进制文件**：`parse_unified_diff` 仅处理文本文件，二进制文件跳过 hunk 级
> review，直接标记为 "binary file changed"。hunk 重建（accept 后写回）也只
> 处理文本文件。

## API Contract

### 协议升级：v1 → v2（breaking change）

> **不兼容 v1**。桌面端尚未发布，无需向后兼容。协议 v2 的完整设计见
> [`docs/specs/protocol-v2-2026-06-28.md`](protocol-v2-2026-06-28.md)，
> 本文档只描述与桌面端能力对齐相关的部分。

v2 采用 **JSON-RPC 2.0** 作为 wire format，引入 **Thread / Turn / Item** 三层
原语。现有 v1 的 `ProtocolEnvelope`（type+payload+seq+ts）、`UiCommand`、
`UiRequest`/`UiResponse` 全部替换为标准 JSON-RPC 消息：

- **Request**: `{jsonrpc, id, method, params}` — 前端→后端调用 method
- **Notification**: `{jsonrpc, method, params}`（无 id）— 单向事件流
- **Response**: `{jsonrpc, id, result}` 或 `{jsonrpc, id, error}`

30+ 种 `UiEvent` 归并为 7 类 Item（message/assistant_stream/tool/todo/subagent/
status/prompt），每个 Item 有 `started → delta → completed` 生命周期。
UiEvent→Item 转换在 gateway 层（`UiEventItemAdapter`），agent 核心不动。

#### Thread/Turn/Item 原语

```python
# protocol/v2/threads.py
class ThreadInfo(BaseModel):
    thread_id: str            # 即 session_id
    title: str = ""
    workspace: str = "."
    status: Literal["idle", "running"] = "idle"

class TurnInfo(BaseModel):
    turn_id: str
    thread_id: str
    status: Literal["running", "completed", "cancelled"] = "running"

class Item(BaseModel):
    item_id: str
    turn_id: str
    thread_id: str
    kind: Literal["message", "assistant_stream", "tool", "todo",
                  "subagent", "status", "prompt"]
    lifecycle: Literal["started", "delta", "completed"] = "started"
    data: dict[str, Any] = Field(default_factory=dict)
```

**设计理由**：agent 核心逻辑（`run_loop.py`、`UiEventBus`）完全不动，
只在 gateway 层做 UiEvent→Item 的适配转换。协议升级影响面控制在
`ui/protocol/` + `ui/gateway/` + `frontend/`。

### 新增命令

v2 中命令统一为 JSON-RPC method call（详见 protocol-v2 文档的 API Contract）。
与桌面端能力对齐相关的 method：

| Method | Params | Result | 说明 |
|--------|--------|--------|------|
| `session.create` | `{title?}` | `ThreadInfo` | 创建新会话 |
| `session.switch` | `{thread_id}` | `ThreadSnapshot` | 切换活跃会话 |
| `session.fork` | `{thread_id, title?}` | `ThreadInfo` | fork 会话 |
| `session.delete` | `{thread_id}` | `{ok}` | 删除会话 |
| `terminal.start` | `{thread_id, cwd?}` | `{terminal_id}` | 启动终端 |
| `terminal.input` | `{terminal_id, data}` | `{ok}` | 写入终端 |
| `terminal.resize` | `{terminal_id, cols, rows}` | `{ok}` | 调整大小 |
| `terminal.stop` | `{terminal_id}` | `{ok}` | 关闭终端 |
| `diff.review` | `{thread_id}` | `{review_id, file_diffs}` | 生成 diff review |
| `diff.decide` | `{review_id, decisions}` | `{ok}` | 提交 hunk 决策 |
| `diff.apply` | `{review_id}` | `{files_changed}` | 应用已 accept 的 hunk |

> 集成终端信任用户操作，不限制可执行命令。

### Terminal 管理

- **Signature**: `async def start_terminal(session_id: str, cwd: str | None = None) -> str`
- **位置**: `src/voidx/ui/gateway/terminal.py`（新建）
- **行为**:
  1. `pty.openpty()` 创建 PTY 对
  2. spawn `bash` (Linux/macOS) 或 `powershell` (Windows) 子进程
  3. 注册到 `TerminalRegistry`，返回 terminal_id
  4. PTY 输出 → `event: terminal.output {terminal_id, data}`
- **平台差异**:
  - Linux/macOS: `pty` 模块
  - Windows: `winpty` 或 `pywinpty` 第三方库

- **Signature**: `async def write_terminal(terminal_id: str, data: str) -> None`
- **Signature**: `async def resize_terminal(terminal_id: str, cols: int, rows: int) -> None`
- **Signature**: `async def stop_terminal(terminal_id: str) -> None`

### Diff Review

- **Signature**: `async def create_diff_review(session_id: str) -> str`
- **位置**: `src/voidx/ui/gateway/diff_review.py`（新建）
- **行为**:
  1. 调用现有 `git_diff(workspace)` 获取 diff 文本
  2. 调用现有 `parse_unified_diff()` 得到 `StructuredDiff`
  3. 二进制文件跳过 hunk 解析，标记为 "binary file changed"
  4. 构建 `DiffReview`，所有 decisions 初始为 "pending"
  5. 返回 review_id

- **Signature**: `async def apply_diff_decisions(review_id: str, decisions: dict[str, str]) -> None`
- **行为**:
  1. 对每个 accept 的 hunk，用 `difflib` 重建文件内容（仅文本文件）
  2. 写回文件（复用 `file_ops` 的写入逻辑，触发 read coverage 更新）
  3. reject 的 hunk 保持原样
  4. 发送 `event: diff.applied {review_id, files_changed}`

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 会话切换时 agent 正在运行 | 等待当前 turn 完成（最多 5s），超时则 cancel turn |
| PTY spawn 失败 | 返回 error event，终端面板显示错误提示 |
| Windows 无 winpty | 降级为 `subprocess.Popen` + pipe，无交互式 shell |
| diff apply 时文件已被修改 | 重新生成 diff review，提示用户冲突 |
| 二进制文件 diff | 跳过 hunk 级 review，直接显示 "binary file changed" |
| 终端进程异常退出 | 发送 `terminal.exited` 事件，前端显示退出码 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 tab 切换会话，不做多窗口 | Tauri 多窗口 | 多窗口 IPC 复杂度高，tab 体验接近且实现简单 |
| 协议 v2 不兼容 v1 | 向后兼容 | 桌面端未发布，无 v1 生产用户，直接 breaking change 更干净 |
| UiEvent→Item 转换在 gateway 层 | agent 核心直接发 Item | agent 核心不感知协议版本，协议升级不影响 agent 逻辑 |
| 终端用 xterm.js | 自建终端渲染 | xterm.js 成熟（VS Code 也用），支持 ANSI、resize、复制粘贴 |
| 终端信任用户操作，不限制命令 | 命令黑名单/白名单 | 集成终端是用户工具，用户对操作自行负责 |
| diff review 内存态 | 持久化到 JSONL | review 是临时交互，会话关闭即丢弃；decides apply 后即失效 |
| diff review 二进制文件跳过 hunk | 强制处理二进制 | `parse_unified_diff` 仅处理文本，二进制无法 hunk 重建 |
| PTY 而非 subprocess pipe | subprocess + stdin/stdout | PTY 支持交互式 shell、ANSI 颜色、resize，体验完整 |
| 不做 Worktree 隔离 | git worktree per session | 本期范围外，多会话共享同一工作区，后续再考虑 |

## Resolved Questions

- [x] **多会话并行时 LLM 调用** — 共享速率限制。不限制同时运行 turn 数量，
      信任 LLM provider 侧的 429/限流机制自然调节。
- [x] **Worktree 隔离** — 先不实现。从本期范围移除，多会话共享同一工作区。
- [x] **集成终端命令限制** — 信任用户操作，不限制可执行命令。
- [x] **diff review 二进制文件** — 跳过 hunk 级 review，直接显示
      "binary file changed"。hunk 重建仅处理文本文件。
- [x] **协议 v2 item envelope 批量发送** — 支持批量（JSON 数组），
      但必须保证数组内顺序。详见 protocol-v2 文档的 Batch Sending 章节。
