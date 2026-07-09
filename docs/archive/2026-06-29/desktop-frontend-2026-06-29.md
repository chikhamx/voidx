# 桌面端前端改造与能力对齐 — 技术设计文档

Date: 2026-06-29

> **Status: Done** — 合并自 `frontend-ui-redesign` 与 `desktop-codex-parity-2026-06-28` 两份文档。
> 后端能力（多会话、终端、diff review、协议 v2）已实现；前端侧已全部实现并接入。
>
> **重要**：本文档 API Contract 已于 2026-06-29 对照后端源码（`src/voidx/ui/gateway/session.py`、
> `adapter.py`、`diff_review.py`、`terminal.py`）逐项核对修正。后端部分 notification 尚未实现，
> 见 [Backend Gaps](#backend-gaps) 章节。
> 2026-07-01 更新：`diff.generate` method 已新增，前端 diff 触发链已接入，
> sidebar fork/delete/rename UI 已补齐，diff-review 字段名 bug 已修复。

## Context

voidx 桌面端（Tauri 2 壳 + 原生 JS 前端）的前端已实现三栏 shell、设计 token 体系、
多会话管理 UI、集成终端面板和 hunk 级 diff review。后端的四项核心能力已实现：

| 后端能力 | 实现位置 | 测试 |
|---------|---------|------|
| 协议 v2（JSON-RPC 2.0 + Thread/Turn/Item） | `src/voidx/ui/protocol/v2/`、`src/voidx/ui/gateway/server.py`、`session.py`、`adapter.py` | 166 测试¹ |
| 多会话（create/switch/fork/delete） | `src/voidx/memory/session.py`（含 `fork_session`）、`src/voidx/ui/gateway/session.py` | 含在 v2 测试中¹ |
| 集成终端（PTY 管理） | `src/voidx/ui/gateway/terminal.py` | 含在 v2 测试中¹ |
| Diff review（hunk 级 accept/reject） | `src/voidx/ui/gateway/diff_review.py` | 含在 v2 测试中¹ |
| Diff 生成（`diff.generate`） | `src/voidx/ui/gateway/session.py:_method_diff_generate` | 3 测试 |

> ¹ 后端 gateway 测试 166 个已于 2026-07-01 重新运行确认通过。
> 前端测试（10 文件 / 176 测试）已于 2026-07-01 重新运行确认通过。

前端 `main.js` 已接入 `session.switch/create/cancel/fork/delete/rename`、`terminal.start/input`、
`diff.generate/review/decide/apply` 的 RPC 调用。全部前端模块
（`rpc.js`、`sidebar.js`、`dock.js`、`terminal.js`、`diff-review.js`）均已实现。

### 当前前端状态

```
index.html (三栏: titlebar → sidebar + main + dock → statusbar)
tokens.css (--vx-* 设计 token 体系)
styles.css (@import tokens.css，全部变量已迁移)
src/
├── main.js      (WebSocket + item 生命周期路由，已接入 session.*/terminal.*/diff.* + workspace.snapshot)
├── rpc.js       (JSON-RPC client 封装)
├── sidebar.js   (会话列表 + 搜索 + fork/delete/rename 菜单)
├── dock.js      (右侧 dock 面板，tab 切换)
├── terminal.js  (集成终端，<pre> 纯文本)
├── diff-review.js (hunk 级 diff review UI + generate 入口)
├── render.js    (快照渲染)
├── stream.js    (流式缓冲 + 流式光标)
├── markdown.js  (Markdown + 代码高亮)
└── slash.js     (斜杠命令补全)
test/            (10 文件 / 176 测试，vitest + jsdom)
```

## Goals and Non-Goals

### Goals

- **三栏布局**：左侧会话列表 + 中间对话区 + 右侧 dock 面板
- **设计 token 体系**：统一 `--vx-*` CSS 变量，消除硬编码值
- **多会话管理 UI**：前端可创建/切换/fork/删除会话，每个会话独立 agent 运行
- **集成终端面板**：前端嵌入终端，实时显示 agent 的 bash 命令输出
- **Diff review UI**：hunk 级 accept/reject，accept 后写回文件
- **消息项增强**：可折叠工具卡片、耗时显示、流式光标

### Non-Goals

- 不切换到 React/TypeScript——保持原生 JS + Vite
- 不引入 Tailwind——手写 CSS + token 变量足够
- 不引入 Zustand/TanStack Query——模块级状态 + DOM 直够
- 不引入虚拟列表——当前消息量不大，原生 `overflow-y: auto` 足够
- 不做多窗口（Tauri 多窗口复杂度高，先用 tab 切换）
- 不做 Worktree 隔离（多会话共享同一工作区，后续再考虑）
- 不做亮色主题（token 体系已预留，实现留到后续）
- 不改 Python 后端的 RPC method 签名（后端已实现，只做前端接入）
- 后端 notification 缺口（`thread.*`、`turn.completed/cancelled`、`terminal.exited`、`diff.applied`）需补充实现，见 [Backend Gaps](#backend-gaps)
- 不做 Plan 卡片（当前无 plan item kind，仅预留）

## Architecture

### 目标布局

```
┌───────────────────────────────────────────────────────────────────┐
│ titlebar (brand · workspace · search · model · connection dot)     │
├───────────┬───────────────────────────────────────┬───────────────┤
│           │                                       │               │
│ LEFT      │ MAIN AREA                             │ RIGHT PANEL   │
│ SIDEBAR   │                                       │ (dockable)    │
│           │ ┌───────────────────────────────────┐ │               │
│ sessions  │ │ transcript                        │ │ tabs:         │
│ list      │ │                                   │ │ • Todo        │
│           │ │                                   │ │ • Terminal    │
│ (search)  │ │                                   │ │ • Diff Review │
│           │ ├───────────────────────────────────┤ │               │
│ new chat  │ │ composer (model · plan · send)    │ │               │
│           │ └───────────────────────────────────┘ │               │
├───────────┴───────────────────────────────────────┴───────────────┤
│ status-bar (session · branch · model · usage)                      │
└───────────────────────────────────────────────────────────────────┘
```

### 目标文件结构

```
frontend/
├── index.html              # 三栏 shell 骨架（重写）
├── styles.css              # 全局样式（引用 tokens.css，变量迁移）
├── tokens.css              # [新增] 设计 token 定义
├── src/
│   ├── main.js             # 入口：bootstrap + WebSocket + 事件路由（改写）
│   ├── rpc.js              # [新增] JSON-RPC client 封装（request/notification 分发）
│   ├── sidebar.js          # [新增] 左侧会话列表渲染 + 搜索
│   ├── dock.js             # [新增] 右侧 dock 面板管理（tab 切换）
│   ├── terminal.js         # [新增] 集成终端（初期 <pre> 纯文本，后续可引入 xterm.js）
│   ├── diff-review.js      # [新增] hunk 级 diff review UI
│   ├── render.js           # 快照渲染（改写：按 Item lifecycle 渲染）
│   ├── stream.js           # 流式缓冲（改写：按 item.delta 聚合 + 流式光标）
│   ├── markdown.js         # Markdown + 代码高亮（保留）
│   ├── slash.js            # 斜杠命令（保留）
│   └── protocol.schema.json
├── test/
│   ├── setup.js            # DOM 骨架（更新为三栏）
│   ├── rpc.test.js         # [新增]
│   ├── sidebar.test.js     # [新增]
│   ├── dock.test.js        # [新增]
│   ├── terminal.test.js    # [新增]
│   ├── diff-review.test.js # [新增]
│   ├── render.test.js
│   ├── stream.test.js
│   ├── markdown.test.js
│   ├── slash.test.js
│   └── main.test.js
```

### 数据流

```
Python backend (gateway WebSocket, JSON-RPC 2.0)
    │
    ├─ workspace.snapshot (on connect + refresh) ──→ sidebar.js 渲染会话列表
    │                                                 + render.js 渲染活跃线程快照
    │
    ├─ item.started/delta/completed ─────→ main.js handleItem 路由
    │                                      ├─ message → appendMessageItem
    │                                      ├─ tool → handleToolItem
    │                                      ├─ assistant_stream → stream.js
    │                                      ├─ todo → dock.js (Todo tab)
    │                                      └─ prompt → request dialog
    │
    ├─ terminal.output notification ─────→ dock.js (Terminal tab)
    │
    └─ diff.apply 的 result ────────────→ dock.js (Diff Review tab) 更新状态
```

> **注意**：后端当前不发送 `terminal.exited` 和 `diff.applied` notification。
> - 终端退出：前端通过 `terminal.output` 数据流中断检测，或调用 `terminal.stop` 的 result 确认。
> - Diff 应用：前端通过 `diff.apply` 的 RPC result（`{files_changed}`）直接更新 UI，无需 notification。
> 见 [Backend Gaps](#backend-gaps)。

### 多会话交互流

```
前端 sidebar.js                后端 GatewaySession
─────────────                  ──────────────────
点击 "New Session"
  ├─ RPC: session.create ─────► session.py: _method_session_create()
  │                            └─ result: {thread_id, title, status}
  │                            └─ 后端调用 register_thread()，下次 snapshot 包含新会话
  │◄───────────────────────────
  │ 前端用 result 立即更新列表，或等待下次 workspace.snapshot 同步
  │
切换到会话
  ├─ RPC: session.switch ─────► session.py: _method_session_switch()
  │                            └─ result: {active_thread_id}
  │                            （若目标会话正在运行，返回 error ERR_TURN_IN_PROGRESS）
  │◄───────────────────────────
  │ 前端用 result 更新活跃会话标记；transcript 内容由 workspace.snapshot 提供
  │
在会话中提交消息
  ├─ RPC: session.submit ─────► session.py: _method_session_submit()
  │                            └─ result: {ok: true}
  │                            ├─ notification: turn.started {thread_id, turn_id, text}
  │                            ├─ notification: item.started (assistant_stream)
  │                            ├─ notification: item.delta (assistant_stream)
  │                            ├─ notification: item.started (tool)
  │                            └─ notification: item.completed (tool)
  │◄───────────────────────────
  │
取消当前 turn
  ├─ RPC: session.cancel ─────► session.py: _method_session_cancel()
  │                            └─ result: {ok: true}
  │◄───────────────────────────
```

> **注意**：后端当前不发送 `thread.created`/`thread.activated` 等 notification。
> 会话列表变更通过 `workspace.snapshot` 全量同步（连接时 + refresh.requested 时广播）。
> 前端也可用 RPC result 立即更新 UI，不等 snapshot。见 [Backend Gaps](#backend-gaps)。

## Data Model

### 设计 Token 体系

新建 `frontend/tokens.css`，所有样式值从这里引用。命名空间 `--vx-*`。

```css
:root {
  /* ── 色彩 ── */
  --vx-bg-base:        #0b0f14;
  --vx-bg-surface:     #0f1620;
  --vx-bg-elevated:    #111a26;
  --vx-bg-hover:       #1a2433;

  --vx-border:         #263241;
  --vx-border-strong:  #304155;

  --vx-text-primary:   #e7edf5;
  --vx-text-secondary: #b7c4d6;
  --vx-text-muted:     #8fa3bb;
  --vx-text-dim:       #9aa9bb;

  --vx-accent:         #7aa2f7;
  --vx-success:        #9ece6a;
  --vx-warning:        #e0af68;
  --vx-error:          #f7768e;

  --vx-diff-add:       #9ece6a;
  --vx-diff-del:       #f7768e;
  --vx-diff-hunk:      #7aa2f7;

  /* ── 间距（4px 基准） ── */
  --vx-space-1:  4px;
  --vx-space-2:  8px;
  --vx-space-3:  12px;
  --vx-space-4:  16px;
  --vx-space-5:  20px;
  --vx-space-6:  24px;
  --vx-space-8:  32px;

  /* ── 圆角 ── */
  --vx-radius-sm: 6px;
  --vx-radius-md: 10px;
  --vx-radius-lg: 16px;

  /* ── 字体 ── */
  --vx-font-sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --vx-font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Cascadia Code", monospace;

  /* ── 字号 ── */
  --vx-text-xs:   11px;
  --vx-text-sm:   13px;
  --vx-text-base: 14px;
  --vx-text-lg:   16px;
  --vx-text-xl:   18px;

  /* ── 侧栏宽度 ── */
  --vx-sidebar-width: 240px;
  --vx-sidebar-collapsed: 48px;
  --vx-dock-width: 360px;
  --vx-dock-collapsed: 0px;

  /* ── 动效 ── */
  --vx-transition: 0.15s ease;
  --vx-shadow-sm: 0 1px 2px rgb(0 0 0 / 0.2);
  --vx-shadow-md: 0 4px 12px rgb(0 0 0 / 0.3);
}
```

### Token 迁移映射

`styles.css` 开头 `@import url("./tokens.css");`，然后将所有旧变量替换：

| 旧变量 | 新变量 |
|--------|--------|
| `--bg-primary` | `--vx-bg-base` |
| `--bg-secondary` | `--vx-bg-surface` |
| `--bg-tertiary` | `--vx-bg-elevated` |
| `--border` | `--vx-border` |
| `--border-light` | `--vx-border-strong` |
| `--text-primary` | `--vx-text-primary` |
| `--text-secondary` | `--vx-text-secondary` |
| `--text-muted` | `--vx-text-muted` |
| `--text-dim` | `--vx-text-dim` |
| `--accent` | `--vx-accent` |
| `--success` | `--vx-success` |
| `--warning` | `--vx-warning` |
| `--error` | `--vx-error` |
| `--space-xs` | `--vx-space-1` |
| `--space-sm` | `--vx-space-2` |
| `--space-md` | `--vx-space-3` |
| `--space-lg` | `--vx-space-4` |
| `--space-xl` | `--vx-space-5` |
| `--radius-sm` | `--vx-radius-sm` |
| `--radius-md` | `--vx-radius-md` |
| `--radius-lg` | `--vx-radius-lg` |
| `--font-sans` | `--vx-font-sans` |
| `--font-mono` | `--vx-font-mono` |

## API Contract

> 以下 method 签名对照后端源码 `src/voidx/ui/gateway/session.py:254-428` 逐项核对。
> 所有 RPC method 已在后端注册并实现。完整协议设计见 `docs/archive/2026-06-28/protocol-v2-2026-06-28.md`。

### 前端→后端 Methods

#### session 管理

| Method | Params | Result | 说明 |
|--------|--------|--------|------|
| `session.create` | `{title?}` | `{thread_id, title, status}` | 创建新会话（`status` 恒为 `"idle"`） |
| `session.list` | `{}` | `{threads: [{thread_id, title, status}]}` | 列出所有已注册会话 |
| `session.switch` | `{thread_id}` | `{active_thread_id}` | 切换活跃会话（目标运行中则返回 `ERR_TURN_IN_PROGRESS`） |
| `session.fork` | `{thread_id, title?}` | `{thread_id, title, status}` | 从现有会话 fork |
| `session.delete` | `{thread_id}` | `{ok: true}` | 删除会话 |
| `session.rename` | `{thread_id, title}` | `{ok: true}` | 重命名 |

#### 消息提交

| Method | Params | Result | 说明 |
|--------|--------|--------|------|
| `session.submit` | `{text}` | `{ok: true}` | 提交消息，启动 turn（作用于当前活跃会话） |
| `session.cancel` | `{}` | `{ok: true}` | 取消当前 turn |

> **注意**：method 名是 `session.submit`/`session.cancel`，不是 `agent.submit`/`agent.cancel`。
> `session.submit` 的 params 只有 `text`，不含 `thread_id`（后端使用当前活跃会话）。
> `main.js:328,343` 当前已使用正确的 method 名。

#### terminal

> 集成终端信任用户操作，不限制可执行命令。

| Method | Params | Result | 说明 |
|--------|--------|--------|------|
| `terminal.start` | `{command, cols?, rows?, cwd?}` | `{terminal_id, pid}` | 启动终端（`command` 为数组，必填） |
| `terminal.input` | `{terminal_id, data}` | `{written: true}` | 写入终端 |
| `terminal.resize` | `{terminal_id, cols, rows}` | `{cols, rows}` | 调整大小 |
| `terminal.stop` | `{terminal_id}` | `{closed: true}` | 关闭终端 |

> **注意**：`terminal.start` 的 `command` 是必填参数（如 `["bash"]`），不是 `{thread_id, cwd?}`。
> `terminal.input` 的 `data` 是原始字节写入 PTY。前端 `main.js` 在单行 input 框场景下会拼接 `"\n"`
> 以触发 shell 命令执行；多行输入或原始 PTY 交互场景不应拼接。

#### diff review

> Hunk 重建仅处理文本文件，二进制文件跳过 hunk 级 review。
> `diff.review` 需要前端传入完整的 unified diff 文本。
> `diff.generate` 从工作区 git diff 生成 unified diff 文本，供 `diff.review` 使用。

| Method | Params | Result | 说明 |
|--------|--------|--------|------|
| `diff.generate` | `{}` | `{diff}` | 从工作区 git diff 生成 unified diff 文本（非 git 仓库返回空字符串） |
| `diff.review` | `{diff}` | `{review_id, snapshot}` | 解析 unified diff，生成 review（`diff` 为字符串，必填） |
| `diff.decide` | `{review_id, file_path, hunk_index, decision}` | `{summary}` | 设置单个 hunk 决策（`decision`: `"approved"`/`"rejected"`/`"pending"`） |
| `diff.apply` | `{review_id}` | `{files_changed: [str]}` | 应用已 approved 的 hunk |

> **`snapshot` 结构**（`diff.review` result）：
> ```json
> {"files": [{"path": "...", "old_path": "", "new_path": "", "operation": "Update",
>   "added": 3, "removed": 1, "hunks": [{"index": 0, "old_start": 1, "old_count": 2,
>   "new_start": 1, "new_count": 3, "section": "", "lines": [...], "decision": "pending"}]}]}
> ```
>
> **`summary` 结构**（`diff.decide` result）：
> ```json
> {"total_hunks": 5, "approved": 2, "rejected": 1, "pending": 2}
> ```

### 后端→前端 Notifications

> 以下为后端**实际发送**的 notification。源码：`adapter.py:417-461`、`session.py:220,326`。

| Method | Params | 说明 |
|--------|--------|------|
| `workspace.snapshot` | `WorkspaceSnapshot` | 全量快照（连接时 + `refresh.requested` 时广播） |
| `turn.started` | `{thread_id, turn_id, text}` | Turn 开始 |
| `item.started` | `{item_id, turn_id, thread_id, kind, lifecycle, data}` | Item 创建 |
| `item.delta` | `{item_id, turn_id, thread_id, kind, lifecycle, data}` | Item 增量更新 |
| `item.completed` | `{item_id, turn_id, thread_id, kind, lifecycle, data}` | Item 完成 |
| `terminal.output` | `{terminal_id, data}` | 终端输出 |
| `ui.request` | `{...}` | 后端→前端请求（权限弹窗等） |
| `capture.started` / `capture.stopped` | `{}` | 截屏状态 |
| `refresh.requested` / `reset.requested` | `{}` | 刷新/重置请求 |
| `startup.shown` | `{model, provider, workspace, session_title, is_new}` | 启动信息 |
| `input.set` | `{text, hints, cursor_pos}` | 输入预设 |
| `notice.set` | `{text}` | 通知文本 |

> **`WorkspaceSnapshot` 结构**（`session.py:225-237`）：
> ```json
> {"threads": [{"thread_id": "...", "title": "", "status": "idle"}],
>  "active_thread_id": "...",
>  "active_snapshot": {"thread_id": "...", "revision": 1, "nodes": [...]}}
> ```

### 前端模块接口

#### rpc.js

```js
// 发送 JSON-RPC request，返回 Promise<result>
export function rpcCall(method: string, params: object): Promise<any>

// 发送 notification（单向，无回复）
export function rpcNotify(method: string, params: object): void

// 注册 notification handler
export function onNotification(method: string, handler: (params: object) => void): void

// 注册 request handler（后端→前端请求，如权限弹窗）
export function onRequest(method: string, handler: (params: object) => Promise<any>): void
```

#### sidebar.js

```js
// 渲染会话列表（threads 来自 workspace.snapshot.threads 或 session.list result）
export function renderSidebar(threads: Array<{thread_id, title, status}>, activeThreadId: string): void

// 更新单个会话项状态（不重渲染整个列表）
export function updateThreadStatus(threadId: string, status: "idle" | "running"): void

// 搜索过滤
export function filterSessions(query: string): void

// 注册回调：用户点击会话项
export function onThreadSelect(callback: (threadId: string) => void): void

// 注册回调：用户点击 "new chat"
export function onNewThread(callback: () => void): void
```

#### dock.js

```js
// 初始化 dock 面板（创建 tab 按钮 + 内容容器）
export function initDock(container: HTMLElement): void

// 切换到指定 tab
export function switchTab(tab: "todo" | "terminal" | "diff"): void

// 渲染 todo 内容到 Todo tab
export function renderTodoInDock(items: TodoItem[], summary: string): void

// 折叠/展开 dock
export function toggleDock(): void
```

#### terminal.js

```js
// 初始化终端面板（挂载到 dock 的 Terminal tab 容器）
export function initTerminal(container: HTMLElement): void

// 追加终端输出（来自 terminal.output notification）
export function appendTerminalOutput(terminalId: string, data: string): void

// 终端关闭时显示状态（前端调用 terminal.stop 后，用 result 更新 UI）
export function showTerminalClosed(terminalId: string): void

// 注册回调：用户输入
export function onTerminalInput(callback: (terminalId: string, data: string) => void): void

// 注册回调：用户启动终端
export function onTerminalStart(callback: (command: string[], cwd?: string) => void): void
```

> **注意**：后端不发送 `terminal.exited` notification。终端退出检测方案见 [Backend Gaps](#backend-gaps)。

#### diff-review.js

```js
// 渲染 diff review 内容（snapshot 来自 diff.review result）
export function renderDiffReview(reviewId: string, snapshot: {files: Array}): void

// 更新单个 hunk 的决策状态（summary 来自 diff.decide result）
export function setHunkDecision(file_path: string, hunk_index: int, decision: "approved" | "rejected" | "pending", summary: object): void

// 注册回调：用户切换 hunk 决策
export function onHunkDecision(callback: (reviewId: string, file_path: string, hunk_index: int, decision: string) => void): void

// 注册回调：用户点击 apply
export function onApplyDiff(callback: (reviewId: string) => void): void
```

> **注意**：`diff.decide` 的 params 是单个 hunk 的 `{review_id, file_path, hunk_index, decision}`，
> 不是批量 `{review_id, decisions}`。`decision` 值为 `"approved"`/`"rejected"`/`"pending"`，
> 不是 `"accept"`/`"reject"`。

## HTML 骨架

```html
<main class="vx-shell">
  <!-- Titlebar -->
  <header class="vx-titlebar">
    <div class="vx-titlebar-left">
      <span class="vx-brand">voidx</span>
      <span class="vx-workspace" id="status-workspace"></span>
    </div>
    <div class="vx-titlebar-center">
      <input type="text" class="vx-search" id="session-search" placeholder="Search sessions..." />
    </div>
    <div class="vx-titlebar-right">
      <span class="vx-model" id="status-model"></span>
      <span class="vx-conn-dot" id="status-dot"></span>
    </div>
  </header>

  <!-- Three-column body -->
  <div class="vx-body">
    <!-- Left sidebar -->
    <aside class="vx-sidebar" id="sidebar">
      <div class="vx-sidebar-header">
        <button class="vx-new-chat" id="btn-new-chat">+ New</button>
      </div>
      <div class="vx-session-list" id="session-list"></div>
    </aside>

    <!-- Main area -->
    <section class="vx-main">
      <div class="vx-transcript" id="transcript" aria-live="polite"></div>
      <form class="vx-composer" id="composer">
        <div class="vx-slash-menu" id="slash-menu"></div>
        <textarea id="input" rows="3" placeholder="Ask voidx... (use / for commands)"></textarea>
        <div class="vx-composer-actions">
          <button type="submit" class="vx-btn vx-btn-send" id="btn-send">Send</button>
          <button type="button" class="vx-btn vx-btn-cancel" id="btn-cancel" disabled>Cancel</button>
        </div>
      </form>
    </section>

    <!-- Right dock -->
    <aside class="vx-dock" id="dock">
      <div class="vx-dock-tabs">
        <button class="vx-dock-tab active" data-tab="todo">Todo</button>
        <button class="vx-dock-tab" data-tab="terminal">Terminal</button>
        <button class="vx-dock-tab" data-tab="diff">Diff</button>
        <button class="vx-dock-toggle" id="dock-toggle">▾</button>
      </div>
      <div class="vx-dock-content" id="dock-content"></div>
    </aside>
  </div>

  <!-- Status bar -->
  <footer class="vx-statusbar">
    <span class="vx-status-session" id="status-session"></span>
  </footer>

  <!-- Request dialog (unchanged) -->
  <dialog id="request-dialog" class="vx-request-dialog">
    <form id="request-form" method="dialog">
      <h2 id="request-title"></h2>
      <div id="request-details"></div>
      <div id="request-controls"></div>
    </form>
  </dialog>
</main>
```

## 消息项增强

### Todo 移入 Dock

当前 todo-panel 是 transcript 上方的独立条。改为 dock 的 Todo tab：

- `handleItem` 中 `kind === "todo"` 调用 `dock.renderTodoInDock()` 而非 `renderTodoPanel()`
- dock 折叠时，todo 有更新则显示 badge 数字
- dock 展开时，直接渲染 todo 列表

### 工具卡片增强

- **可折叠**：点击 header 切换展开/折叠（复用现有 `.node-collapsed` 模式）
- **耗时显示**：`item.completed` 时如果有 `elapsed` 字段，显示在 spinner 旁
- **diff 内联**：`item.delta` 的 `diff_text` 用 `renderDiffBlock()` 渲染（当前是纯文本 `<pre>`）

```js
// handleToolItem 改进伪代码
if (method === "item.completed") {
  spinner.textContent = data.ok ? "done" : "failed";
  spinner.className = `tool-spinner ${data.ok ? "ok" : "err"}`;
  if (data.elapsed != null) {
    const elapsed = document.createElement("span");
    elapsed.className = "tool-elapsed";
    elapsed.textContent = formatElapsed(data.elapsed);
    header.append(elapsed);
  }
  if (data.diff_text) {
    el.append(renderDiffBlock(data.diff_text));
  }
}
```

### 流式光标

`stream.js` 的 `renderStreamText` 在文本末尾添加闪烁光标：

```css
.stream-cursor::after {
  content: "▋";
  animation: vx-blink 1s step-end infinite;
  color: var(--vx-accent);
}
@keyframes vx-blink { 50% { opacity: 0; } }
```

```js
// stream.js renderStreamText 中
if (!stream.committed) {
  const cursor = document.createElement("span");
  cursor.className = "stream-cursor";
  stream.textEl.append(cursor);
}
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| WebSocket 断连 | titlebar 连接点变红，transcript 顶部显示重连提示，自动重连（现有逻辑） |
| 会话列表加载失败 | sidebar 显示 "Failed to load sessions" + 重试按钮 |
| 会话切换时 agent 正在运行 | 后端返回 error `ERR_TURN_IN_PROGRESS`，前端提示等待 |
| dock tab 内容加载失败 | dock 内容区显示错误信息，不影响主对话区 |
| 终端会话异常退出 | Terminal tab 显示关闭状态 + 重新打开按钮（后端无 `terminal.exited`，前端靠 `terminal.output` 中断或 `terminal.stop` result 检测） |
| PTY spawn 失败 | 后端返回 error，终端面板显示错误提示 |
| diff 渲染失败 | Diff tab 显示原始 diff 文本（降级） |
| diff apply 时文件已被修改 | 重新生成 diff review，提示用户冲突 |
| 二进制文件 diff | 跳过 hunk 级 review，直接显示 "binary file changed" |

## Backend Gaps

后端已实现所有 RPC method，但以下 notification **尚未实现**，前端需采用替代方案或等待后端补充：

| Notification | 现状 | 前端替代方案 | 是否需后端补充 |
|-------------|------|-------------|--------------|
| `thread.created` | ❌ 不存在 | 用 `session.create` 的 result 立即更新列表；或等下次 `workspace.snapshot` | 建议补充（多客户端同步场景需要） |
| `thread.activated` / `thread.deactivated` | ❌ 不存在 | 用 `session.switch` 的 result 更新活跃标记 | 建议补充（同上） |
| `thread.renamed` / `thread.deleted` | ❌ 不存在 | 用 RPC result 更新；或等 `workspace.snapshot` | 建议补充（同上） |
| `turn.completed` | ❌ 不存在 | 前端通过 `item.completed`（assistant_stream）推断 turn 结束 | 建议补充（前端需要明确的 turn 结束信号） |
| `turn.cancelled` | ❌ 不存在 | 用 `session.cancel` 的 result 推断 | 建议补充（同上） |
| `terminal.exited` | ❌ 不存在 | `terminal.output` 数据流中断检测；或 `terminal.stop` result | 建议补充（可靠的退出码获取需要） |
| `diff.applied` | ❌ 不存在 | 用 `diff.apply` 的 RPC result（`{files_changed}`）直接更新 UI | 不需要（RPC result 足够） |

> **优先级**：`turn.completed`/`turn.cancelled` 和 `terminal.exited` 应优先补充，
> 因为前端难以从其他信号可靠推断。`thread.*` 系列在单客户端场景下可用 RPC result 替代，
> 但多客户端同步（如 web + desktop 同时连接）时需要 notification。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 保持原生 JS，不切 React | 切换到 React + TypeScript | 迁移成本高，现有 6 个 JS 模块 + 101 个测试全部可用，原生 JS 足以支撑三栏布局 |
| 新建 tokens.css 而非内联在 styles.css | 内联在 styles.css | 独立文件便于维护和未来主题切换（亮色主题） |
| Todo 移入 dock 而非保持独立条 | 保持 transcript 上方独立条 | 三栏布局后 transcript 宽度变窄，独立 todo 条会挤压对话空间；dock 可折叠更灵活 |
| 用 tab 切换会话，不做多窗口 | Tauri 多窗口 | 多窗口 IPC 复杂度高，tab 体验接近且实现简单 |
| 终端初期用 `<pre>` 纯文本 | 直接引入 xterm.js | 降低初期复杂度，后续可引入 xterm.js |
| 终端信任用户操作，不限制命令 | 命令黑名单/白名单 | 集成终端是用户工具，用户对操作自行负责 |
| diff review 内存态 | 持久化到 JSONL | review 是临时交互，会话关闭即丢弃；decides apply 后即失效 |
| diff review 二进制文件跳过 hunk | 强制处理二进制 | `parse_unified_diff` 仅处理文本，二进制无法 hunk 重建 |
| 不引入虚拟列表 | 用虚拟滚动库 | 当前消息量不大，原生 `overflow-y: auto` 足够；未来消息量增大时再引入 |
| 侧栏宽度固定 240px | 可拖拽调整 | 初期固定宽度降低复杂度，后续可加 resize 手柄 |
| 不做 Worktree 隔离 | git worktree per session | 本期范围外，多会话共享同一工作区，后续再考虑 |
| 新增 rpc.js 封装 JSON-RPC | 在 main.js 内联 | 多个模块都需要发 RPC，封装后避免重复，且便于测试 mock |
| diff review 用 RPC result 驱动 UI | 等后端补 `diff.applied` notification | RPC result 足够驱动单客户端 UI；notification 留到多客户端同步时再补 |

## Implementation Phases

### Phase 1: 设计 Token 体系（低风险，纯 CSS）

1. 新建 `frontend/tokens.css`，定义全部 `--vx-*` 变量
2. `styles.css` 开头 `@import url("./tokens.css")`
3. 全局替换 `var(--bg-primary)` → `var(--vx-bg-base)` 等（sed 批量替换）
4. 删除 `styles.css` 中 `:root` 块的旧变量定义
5. 运行 `npm test` + `npm run build` 验证

**验证命令**：`cd frontend && npm test && npm run build`

### Phase 2: 三栏 HTML 骨架 + CSS（中风险，布局变更）

1. 重写 `index.html` 为三栏 shell 骨架（见 HTML 骨架章节）
2. 更新 `styles.css` 添加 `.vx-shell`、`.vx-body`、`.vx-sidebar`、`.vx-main`、`.vx-dock` 布局样式
3. 更新 `test/setup.js` 的 DOM 骨架匹配新 HTML
4. 更新 `main.js` 中的 `querySelector` 选择器匹配新 ID/class
5. 运行测试，修复选择器不匹配的用例

**验证命令**：`cd frontend && npm test`

### Phase 3: RPC client 封装（低风险，新模块）

1. 新建 `frontend/src/rpc.js`，实现 `rpcCall`、`rpcNotify`、`onNotification`、`onRequest`
2. `main.js` 中的 WebSocket 消息收发改为通过 `rpc.js` 统一走
3. 新建 `frontend/test/rpc.test.js`
4. 运行测试确认现有功能不受影响

**验证命令**：`cd frontend && npx vitest run test/rpc.test.js test/main.test.js`

### Phase 4: 左侧会话列表（中风险，新模块）

1. 新建 `frontend/src/sidebar.js`，实现 `renderSidebar`、`updateThreadStatus`、`filterSessions`
2. `main.js` 中处理 `workspace.snapshot` 事件，调用 `sidebar.renderSidebar(snapshot.threads, snapshot.active_thread_id)`
3. 用户点击会话项 → `rpcCall("session.switch", {thread_id})`，用 result `{active_thread_id}` 更新 UI
4. 用户点击 "New" → `rpcCall("session.create", {})`，用 result `{thread_id, title, status}` 立即插入列表
5. 新建 `frontend/test/sidebar.test.js`
6. 更新 `test/setup.js` DOM 骨架包含 sidebar 元素

**验证命令**：`cd frontend && npx vitest run test/sidebar.test.js`

### Phase 5: 右侧 Dock 面板 + Todo 迁移（中风险，新模块）

1. 新建 `frontend/src/dock.js`，实现 `initDock`、`switchTab`、`renderTodoInDock`、`toggleDock`
2. `main.js` 中 `kind === "todo"` 改为调用 `dock.renderTodoInDock()`
3. dock 折叠时 todo 更新显示 badge
4. 新建 `frontend/test/dock.test.js`
5. 更新 `test/setup.js` DOM 骨架包含 dock 元素

**验证命令**：`cd frontend && npx vitest run test/dock.test.js`

### Phase 6: 集成终端面板（中风险，新模块）

1. 新建 `frontend/src/terminal.js`，实现 `initTerminal`、`appendTerminalOutput`、`showTerminalClosed`、`onTerminalInput`、`onTerminalStart`
2. `main.js` 中 `terminal.output` notification 路由到 `terminal.appendTerminalOutput()`
3. 用户输入 → `rpcCall("terminal.input", {terminal_id, data})`
4. dock 切换到 Terminal tab 时，若无可终端则提供 "Start Terminal" 按钮 → `rpcCall("terminal.start", {command: ["bash"], cwd?})`
5. 用户关闭终端 → `rpcCall("terminal.stop", {terminal_id})`，用 result `{closed: true}` 调用 `showTerminalClosed()`
6. 新建 `frontend/test/terminal.test.js`

> **注意**：后端不发送 `terminal.exited`。终端退出检测：`terminal.output` 数据流中断时，
> 前端可调用 `terminal.stop` 确认关闭状态。可靠的退出码获取需后端补充 notification。

**验证命令**：`cd frontend && npx vitest run test/terminal.test.js`

### Phase 7: Diff Review 面板（中风险，新模块）

1. 新建 `frontend/src/diff-review.js`，实现 `renderDiffReview`、`setHunkDecision`、`onHunkDecision`、`onApplyDiff`
2. 用户打开 Diff tab → 前端生成 unified diff 文本² → `rpcCall("diff.review", {diff})`，用 result `{review_id, snapshot}` 调用 `renderDiffReview()`
3. 用户切换 hunk 决策 → `rpcCall("diff.decide", {review_id, file_path, hunk_index, decision})`，用 result `{summary}` 更新 UI
4. 用户点击 apply → `rpcCall("diff.apply", {review_id})`，用 result `{files_changed}` 显示结果
5. 新建 `frontend/test/diff-review.test.js`

> ² diff 文本来源待定：前端调用 `git diff`？还是新增后端 method 生成？
> 见 [Open Questions](#open-questions)。
>
> **注意**：`diff.decide` 是单 hunk 决策，不是批量。`decision` 值为 `"approved"`/`"rejected"`/`"pending"`。
> 后端不发送 `diff.applied` notification，前端用 `diff.apply` 的 RPC result 直接更新 UI。

**验证命令**：`cd frontend && npx vitest run test/diff-review.test.js`

### Phase 8: 消息项增强（低风险，增量改进）

1. `handleToolItem`：添加可折叠 header、elapsed 显示、diff 内联渲染
2. `stream.js`：添加流式光标
3. 更新对应测试用例

**验证命令**：`cd frontend && npx vitest run test/main.test.js test/stream.test.js`

### Phase 9: 集成验证

1. `npm test` 全量通过
2. `npm run build` 构建成功
3. Tauri 桌面端启动验证：`cargo build --release` + 运行
4. 手动测试：会话切换、todo dock、终端面板、diff review、工具卡片折叠、流式光标

**验证命令**：`cd frontend && npm test && npm run build && cd ../desktop/tauri && cargo build --release`

## Open Questions

- [ ] 侧栏是否需要可拖拽调整宽度？初期固定，后续可加 resize 手柄
- [ ] dock 默认展开还是折叠？建议默认折叠，有内容时自动展开
- [ ] 会话搜索：HTML 骨架已预留搜索框（`#session-search`），Phase 4 先做列表渲染，搜索逻辑作为后续增量
- [ ] 终端 tab 是否用 xterm.js？初期用 `<pre>` 纯文本，后续可引入 xterm.js
- [ ] 亮色主题是否在本方案范围内？token 体系已预留，但实现留到后续
- [x] ~~`diff.review` 的 diff 文本来源~~ **已解决**：后端新增 `diff.generate` method，从工作区 `git diff` 生成 unified diff 文本，前端 Diff tab 的 "Generate Diff" 按钮触发调用链 `diff.generate` → `diff.review` → `renderDiffReview`。
- [ ] 多会话并发约束：本期是否限制同时只有一个 running session？后端 `session.switch` 有 `ERR_TURN_IN_PROGRESS` 检查，但 `session.create` 不限制并发运行
