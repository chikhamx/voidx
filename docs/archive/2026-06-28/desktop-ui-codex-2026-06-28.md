# 桌面 UI 复刻 Codex — 技术设计文档

> **Status: Done** — 前端重构已完成。`frontend/src/` 已含 `markdown.js`、
> `render.js`、`slash.js`、`stream.js`，覆盖 Markdown 渲染、代码高亮、
> 工具节点折叠、slash 补全、流式输出等能力。

## Context

voidx 桌面端目前是一个 Tauri 2 壳 + 原生 HTML/JS 前端的最小实现。后端协议层（`src/voidx/ui/`）已完整支持流式输出、工具调用树、权限弹窗、todo、checkpoint、clarify、diff、subagent 等 30+ 种 UI 事件，但前端（`frontend/src/main.js` + `render.js`，共约 280 行）只做了基础渲染：纯文本消息、简单缩进节点、基础 diff 着色、权限/文本请求弹窗。

对标 Codex 桌面应用的界面体验，本设计在不改动后端 Python 代码的前提下，通过前端重构 + Tauri 壳侧增强，复刻 Codex 的核心 UI 能力：Markdown 渲染、代码语法高亮、工具节点折叠/展开、状态栏、slash 命令补全、流式输出、todo 进度面板等。

### 跨平台考量

- **Windows**：Tauri 2 壳已验证可用（`desktop/tauri/src/main.rs`），Python 路径解析已处理 `.venv/Scripts/python.exe` + `py` launcher 回退
- **macOS**：Tauri 2 原生支持，Python 路径 `.venv/bin/python`，需验证 `beforeDevCommand` 的 `npm run dev --prefix` 在 macOS 上工作（bash 兼容）
- **前端**：纯 Web 技术（HTML/CSS/JS），Tauri WebView 在 Windows 用 WebView2、macOS 用 WKWebView，两者均支持 ES modules、CSS Grid/Flexbox、WebSocket、`<dialog>` 元素
- **字体**：Windows 用 Segoe UI / Cascadia Code，macOS 用 -apple-system / SF Mono，CSS font-stack 已覆盖
- **图标**：已有 `icons/icon.png` + `icons/icon.ico`，macOS 打包需补 `icon.icns`（后续打包阶段处理）

## Goals and Non-Goals

### Goals

- **Markdown 渲染**：assistant 消息渲染为富文本 Markdown，代码块带语法高亮
- **代码语法高亮**：工具调用参数、代码块、diff 中的代码片段带语言感知高亮
- **工具节点折叠/展开**：点击标题切换，默认折叠 `tool_result`，展开 `tool_call` 摘要
- **状态栏**：显示模型、workspace 路径、连接状态、当前会话 ID
- **Slash 命令补全**：输入 `/` 时弹出命令列表，支持键盘导航
- **流式输出**：assistant 消息流式渲染（`assistant_stream.updated` 事件），thinking 阶段用不同样式
- **Todo 面板**：侧边或顶部展示 todo 列表及进度
- **Cancel 按钮**：composer 旁加停止按钮，发送 `cancel` command
- **Subagent 展示**：子 agent 调用用嵌套卡片展示，含名称、步骤、耗时
- **Checkpoint/Clarify 弹窗**：复用现有 `<dialog>` 机制，增强样式

### Non-Goals

- 不改动后端 Python 代码（`src/voidx/`）
- 不做多线程/多会话切换（需后端支持多 session，后续迭代）
- 不做历史回放 UI（后端有 revision 机制，但前端回放逻辑复杂，后续迭代）
- 不做内置浏览器（Codex 的 in-app browsing，需额外 Tauri 能力）
- 不做 Computer Use（需额外原生集成）
- 不做 worktree 隔离（需后端 git 集成）
- 不引入前端框架（React/Vue），保持原生 JS + 轻量库

## Architecture

### 整体架构

```
┌──────────────────────────────────────────────────────────┐
│  Tauri 2 壳 (desktop/tauri/)                          │
│  - 拉起 Python 后端 (voidx.main --web --web-headless)     │
│  - 暴露 get_gateway_url / get_backend_status 命令          │
│  - emit backend_ready / backend_failed 事件               │
│  - on_window_event: 关闭时 kill 后端进程                   │
└────────────────────────┬─────────────────────────────────┘
                         │ WebSocket (ws://127.0.0.1:<port>/?token=xxx)
                         ▼
┌──────────────────────────────────────────────────────────┐
│  前端 (frontend/)                                         │
│                                                           │
│  index.html          ← 语义化 HTML 骨架                   │
│  src/main.js         ← WebSocket 连接 + 事件分发           │
│  src/render.js       ← 节点渲染（重构为模块化）            │
│  src/markdown.js     ← 新增：Markdown 解析 + 代码高亮      │
│  src/slash.js        ← 新增：Slash 命令补全               │
│  src/stream.js       ← 新增：流式输出缓冲 + 渲染           │
│  styles.css          ← 重构：CSS 变量 + 组件样式           │
│                                                           │
│  第三方库（CDN 或 npm）：                                  │
│  - marked (Markdown 解析，~12KB gzipped)                  │
│  - highlight.js (代码高亮，按需加载语言包)                 │
└──────────────────────────────────────────────────────────┘
```

### 前端模块划分

```
frontend/src/
├── main.js              # 入口：WebSocket 连接、事件分发、UI 状态管理
├── render.js            # 节点渲染：TranscriptNode → DOM 元素
├── markdown.js          # 新增：Markdown 解析 + 代码高亮封装
├── slash.js             # 新增：Slash 命令补全逻辑
├── stream.js            # 新增：流式输出缓冲与增量渲染
└── protocol.schema.json # 已有：协议 schema（从后端导出）
```

### 数据流

```
后端 Python                        前端 Web
────────────                      ──────────
voidx.main --web --web-headless
  │
  ├─ GatewayServer (WebSocket)
  │   │
  │   ├─ 连接时推 snapshot ────────► main.js: connect()
  │   │                              ├─ envelope.type === "snapshot"
  │   │                              └─ render.js: renderTranscript(tree)
  │   │
  │   ├─ broadcast_event ──────────► main.js: onMessage()
  │   │                              ├─ envelope.type === "event"
  │   │                              ├─ event.kind === "assistant_stream.updated"
  │   │                              │   └─ stream.js: appendStreamText(text, phase)
  │   │                              ├─ event.kind === "tool.started"
  │   │                              │   └─ render.js: renderToolNode(node, running)
  │   │                              ├─ event.kind === "tool.finished"
  │   │                              │   └─ render.js: updateToolNode(id, elapsed, ok)
  │   │                              ├─ event.kind === "todo.updated"
  │   │                              │   └─ render.js: renderTodoPanel(items)
  │   │                              ├─ event.kind === "subagent.started"
  │   │                              │   └─ render.js: renderSubagentCard(node)
  │   │                              └─ event.kind === "permission_prompt.shown"
  │   │                                  └─ main.js: showRequest(payload)
  │   │
  │   ├─ request (permission/choice/text)
  │   │   └─ UiRequestEnvelope ───► main.js: showRequest(payload)
  │   │                                  └─ <dialog> 弹窗
  │   │
  │   └─ handle_command ◄────────── main.js: sendCommand()
  │       ├─ {kind: "submit", text}     ├─ composer submit
  │       └─ {kind: "cancel"}           └─ cancel button
  │
  └─ handle_response ◄───────────── main.js: sendResponse()
      └─ {request_id, value}             └─ dialog 选择/输入
```

## Data Model

### TranscriptNode（已有，前端消费）

```typescript
// 来自 src/voidx/ui/protocol/transcript.py
interface TranscriptNode {
  id: string;
  parent_id: string | null;
  node_type: "root" | "startup" | "turn" | "tool_call" | "tool_result" |
             "todo" | "subagent" | "message" | "assistant" | "thought" |
             "status" | "permission" | "checkpoint" | "error" | "warn" | "diff";
  status: "running" | "done" | "error";
  title: string;
  header: string;
  header_style: string;
  body_lines: string[];
  collapsed: boolean;
  elapsed: number | null;
  agent_name: string | null;
  step_info: string | null;
  meta: string | null;
  tool_call_id: string | null;
  agent_run_id: string | null;
  message_id: number | null;
  payload: Record<string, any>;
  child_ids: string[];
}
```

### UiEvent（已有，前端消费的关键事件）

```typescript
// 来自 src/voidx/ui/output/events/schema.py，前端只消费以下子集
type ConsumedEvent =
  | { kind: "startup.shown"; model: string; provider: string; workspace: string; session_title: string; is_new: boolean; profile_configured: boolean }  // profile_configured 前端可选消费，用于显示配置缺失提示
  | { kind: "assistant_stream.started"; stream_id: string }
  | { kind: "assistant_stream.updated"; text: string; stream_id: string; phase: "thinking" | "text" }
  | { kind: "assistant_stream.committed"; stream_id: string }
  | { kind: "assistant_stream.discarded"; stream_id: string }
  | { kind: "tool.started"; tool_call_id: string; label: string; args: string; tool_name: string; raw_args: Record<string, any>; display_mode: "show" | "summary" | "hidden"; summary_max_lines: number }
  | { kind: "tool.finished"; tool_call_id: string; label: string; elapsed: number; ok: boolean; detail: string }
  | { kind: "tool_result.appended"; tool_call_id: string; text: string; collapsed: boolean; display_mode: "show" | "summary" | "hidden"; summary_max_lines: number }
  | { kind: "todo.updated"; items: Array<{ id: string; content: string; status: "pending" | "active" | "done" }>; summary: string; todo_op: "write" | "update" | "read" }
  | { kind: "todo.committed" }
  | { kind: "todo.cleared" }
  | { kind: "file_change.appended"; tool_call_id: string; diff_text: string }
  | { kind: "subagent.started"; agent_id: number; subagent_id: string; name: string; description: string; parent_agent_id: number; parent_tool_call_id: string }
  | { kind: "subagent_step.started"; agent_id: number; subagent_id: string; name: string }
  | { kind: "subagent.finished"; agent_id: number; subagent_id: string; ok: boolean; elapsed: number | null; finish_reason: string; summary: string }
  | { kind: "permission_prompt.shown"; prompt: string; choices: Array<[string, string, string]>; tools: Array<{ name: string; pattern: string; args: Record<string, any> }> }
  | { kind: "permission_prompt.cleared" }
  | { kind: "checkpoint_prompt.shown"; checkpoint_id: string; plan: { plan_summary: string; steps: string[]; affected_files: string[]; risks: string[] }; choices: Array<{ label: string; value: string; description: string }> }
  | { kind: "clarify_prompt.shown"; clarify_id: string; question: string; options: string[] }
  | { kind: "status.updated"; status_id: string; label: string; detail: string; stage: "analyzing" | "agent_step" | "compacting" | "working"; display: "record_only" | "tree_node"; parent_tool_call_id: string }
  | { kind: "status.finished"; status_id: string; label: string; detail: string; ok: boolean; remove: boolean }
  | { kind: "markdown.appended"; content: string }
  | { kind: "error.appended"; message: string }
  | { kind: "warning.appended"; message: string };
```

### 前端 UI 状态（新增）

```javascript
// main.js 中的全局 UI 状态
const uiState = {
  connection: "disconnected",     // "connecting" | "connected" | "disconnected" | "error"
  backendStatus: "starting",      // "starting" | "ready" | "failed"
  model: "",                      // 从 startup.shown 事件获取
  provider: "",                   // 从 startup.shown 事件获取
  workspace: "",                  // 从 startup.shown 事件获取
  sessionId: "",                  // 从 snapshot 获取
  streamBuffers: new Map(),       // stream_id → { text: string, phase: string, el: HTMLElement }
  collapsedNodes: new Set(),      // node_id → collapsed 状态（用户手动切换的）
  todoItems: [],                  // 从 todo.updated 事件获取
  activeSubagents: new Map(),     // subagent_id → { name, steps, el }
};
```

## API Contract

### WebSocket 协议（已有，不变）

#### 前端 → 后端

```javascript
// 发送用户消息
{ type: "command", payload: { kind: "submit", text: "用户输入" } }

// 取消当前操作
{ type: "command", payload: { kind: "cancel" } }

// 响应权限/选择/文本请求
{ type: "response", payload: { request_id: "xxx", value: "approve" } }
```

#### 后端 → 前端

```javascript
// 完整 transcript 快照（连接时或重大变更时推送）
{ type: "snapshot", payload: TranscriptSnapshot }

// 增量 UI 事件
{ type: "event", payload: UiEvent }

// 请求用户输入（权限/选择/文本）
{ type: "request", payload: UiRequest }
```

### Tauri 命令（已有）

```javascript
// 获取后端 gateway URL
const url = await invoke("get_gateway_url");  // → string | null

// 获取后端状态
const status = await invoke("get_backend_status");
// → { status: "starting" } | { status: "ready", url: "ws://..." } | { status: "failed", error: "..." }
```

> **迁移注意**：现有 `frontend/src/main.js:36` 使用旧命令名 `invoke("gateway_url")`，实现时需同步更新为 `get_gateway_url`，与 `desktop/tauri/src/main.rs:32` 的 Rust 函数名一致。

### Tauri 事件（已有，前端可选监听）

```javascript
// 后端就绪
listen("backend_ready", (event) => { /* event.payload.url */ });

// 后端启动失败
listen("backend_failed", (event) => { /* event.payload.error */ });
```

### 新增前端模块接口

#### `markdown.js`

```javascript
// 将 Markdown 文本渲染为带语法高亮的 HTML
// @param {string} text - Markdown 源文本
// @returns {HTMLElement} - 渲染后的容器元素
export function renderMarkdown(text) {}

// 对纯代码片段做语法高亮（用于工具参数、diff 中的代码）
// @param {string} code - 代码文本
// @param {string} lang - 语言标识（如 "python", "javascript", "diff"）
// @returns {string} - 高亮后的 HTML
export function highlightCode(code, lang) {}
```

#### `slash.js`

```javascript
// Slash 命令定义
const SLASH_COMMANDS = [
  { command: "/mcp", description: "Manage MCP servers" },
  { command: "/model", description: "Switch model or adjust reasoning" },
  { command: "/lsp", description: "Language server operations" },
  { command: "/session", description: "Session management" },
  { command: "/skills", description: "Skill management" },
  { command: "/init", description: "Initialize project config" },
];

// 根据当前输入返回匹配的命令列表
// @param {string} input - 当前输入文本（以 / 开头）
// @returns {Array<{ command: string, description: string }>}
export function matchSlashCommands(input) {}

// 渲染补全弹窗
// @param {Array} commands - 匹配的命令列表
// @param {number} selectedIndex - 当前选中索引
// @returns {HTMLElement}
export function renderSlashMenu(commands, selectedIndex) {}
```

#### `stream.js`

```javascript
// 初始化或获取流式输出缓冲
// @param {string} streamId - 流 ID
// @param {string} phase - "thinking" | "text"
// @returns {{ text: string, el: HTMLElement }}
export function getOrCreateStream(streamId, phase) {}

// 追加流式文本
// @param {string} streamId
// @param {string} text - 增量文本
// @param {string} phase - "thinking" | "text"
export function appendStreamText(streamId, text, phase) {}

// 提交流式输出（转为正式消息节点）
// @param {string} streamId
export function commitStream(streamId) {}

// 丢弃流式输出
// @param {string} streamId
export function discardStream(streamId) {}
```

## UI 布局设计

### 整体布局

```
┌─────────────────────────────────────────────────────────────┐
│  状态栏 (status-bar)                                         │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ ● voidx  │ │ gpt-4o     │ │ ~/proj   │ │ session #42  │ │
│  └──────────┘ └────────────┘ └──────────┘ └──────────────┘ │
├─────────────────────────────────────────────────────────────┤
│  Todo 面板 (todo-panel，有 todo 时显示)                      │
│  ☑ 解析需求  ☐ 编写测试  ▶ 实现代码  ☐ 验证                 │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  对话区 (transcript)                                         │
│                                                              │
│  ┌─ user ─────────────────────────────────────────────────┐ │
│  │ 帮我实现登录接口                                        │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─ assistant ────────────────────────────────────────────┐ │
│  │ 我来分析一下现有的认证模块...                           │ │
│  │                                                        │ │
│  │ ```python                                              │ │
│  │ def login(username: str, password: str) -> Token:      │ │
│  │     ...                                                │ │
│  │ ```                                                    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ▼ read src/auth.py (12ms)            ← 折叠的工具调用      │
│  ▼ edit src/auth.py (45ms)            ← 点击展开看 diff     │
│  ▶ bash: pytest tests/ (running...)   ← 运行中              │
│                                                              │
│  ┌─ subagent: explore ────────────────────────────────────┐ │
│  │ │ 搜索认证相关文件...                                   │ │
│  │ │ 找到 3 个相关文件                                     │ │
│  │ └─ finished in 2.3s                                    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│  输入区 (composer)                                           │
│  ┌──────────────────────────────────────────┐ ┌─────┐ ┌───┐ │
│  │ Ask voidx...                             │ │ Send │ │ ⏹ │ │
│  └──────────────────────────────────────────┘ └─────┘ └───┘ │
│  ┌─ slash 补全（输入 / 时显示）─────────────────────────────┐ │
│  │ /model  Switch model or adjust reasoning                │ │
│  │ /mcp    Manage MCP servers                              │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### CSS 变量设计（跨平台友好）

```css
:root {
  /* 颜色 — 深色主题（与现有 styles.css 一致） */
  --bg-primary: #0b0f14;
  --bg-secondary: #0f1620;
  --bg-tertiary: #111a26;
  --border: #263241;
  --border-light: #304155;
  --text-primary: #e7edf5;
  --text-secondary: #b7c4d6;
  --text-muted: #8fa3bb;
  --text-dim: #9aa9bb;
  --accent: #7aa2f7;
  --accent-bg: #7aa2f7;
  --success: #9ece6a;
  --warning: #e0af68;
  --error: #f7768e;
  --diff-add: #9ece6a;
  --diff-del: #f7768e;
  --diff-hunk: #7aa2f7;

  /* 字体 — 跨平台 stack */
  --font-sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Cascadia Code", monospace;

  /* 间距 */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 12px;
  --space-lg: 16px;
  --space-xl: 20px;

  /* 圆角 */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;
}
```

### 节点渲染规则

| node_type | 渲染方式 | 默认折叠 | 特殊处理 |
|-----------|---------|---------|---------|
| `message` (user) | 右对齐气泡，纯文本 | 否 | 背景色区分 |
| `assistant` | 左对齐，Markdown 渲染 | 否 | 流式时实时追加 |
| `thought` | 左对齐，斜体，dim 色 | 是 | 显示 meta（耗时） |
| `tool_call` | 卡片，标题=工具名+参数摘要 | display_mode=summary 时折叠 | 点击展开看完整参数 |
| `tool_result` | 卡片，标题=工具名+耗时 | 是（除非 error） | error 时不折叠，红色边框 |
| `diff` | diff 块，文件名标题 | 否 | 语法高亮 + 行号 |
| `subagent` | 嵌套卡片，带 agent 名称 | 否 | 子节点缩进，显示步骤 |
| `todo` | 不在 transcript 中渲染 | — | 由 todo-panel 独立渲染 |
| `status` | 行内状态指示 | 是 | running 时 spinner |
| `error` | 红色卡片 | 否 | — |
| `warn` | 黄色卡片 | 否 | — |
| `checkpoint` | 弹窗 | — | 复用 dialog |
| `permission` | 弹窗 | — | 复用 dialog |
| `startup` | 不渲染（信息已由状态栏展示） | — | 从 startup.shown 事件提取 model/workspace |
| `turn` | 容器节点，不直接渲染 | — | 子节点（message/assistant/tool_call 等）渲染在其下 |
| `root` | 不渲染 | — | 根节点，仅作为树结构起点 |

### 流式输出渲染

```
assistant_stream.started
  → 创建临时 <div class="stream-buffer" data-stream-id="xxx">
  → 插入到 transcript 末尾

assistant_stream.updated (phase="thinking")
  → 追加文本到 thinking 区域（斜体、dim 色）
  → 实时滚动到底部

assistant_stream.updated (phase="text")
  → 追加文本到 text 区域
  → 每 100ms 做一次 Markdown 增量渲染（防抖，实现见下方说明）

assistant_stream.committed
  → 将流式缓冲转为正式 assistant 节点
  → 清除临时 div

assistant_stream.discarded
  → 移除临时 div
```

**防抖实现说明**：使用 `setTimeout` + `clearTimeout` 实现。每次 `assistant_stream.updated` 到达时，追加文本到缓冲区，清除上一个定时器，设置新的 100ms 定时器。定时器触发时执行一次 `renderMarkdown(bufferedText)`。`assistant_stream.committed` 到达时，立即清除定时器并执行最终渲染（不等防抖），确保提交时内容完整。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| WebSocket 断开 | 状态栏显示 "Disconnected"，5s 后自动重连，最多 10 次 |
| WebSocket 重连失败 | 状态栏显示 "Connection lost"，禁用 composer |
| 后端启动失败 (`backend_failed` 事件) | 全屏显示错误信息 + 重试按钮 |
| Markdown 解析失败 | 降级为纯文本 `<pre>` 展示 |
| 代码高亮失败 | 降级为无高亮的纯文本 |
| Slash 命令补全无匹配 | 不显示弹窗 |
| 流式输出中途断连 | 保留已接收文本，标记为不完整 |
| Tauri invoke 失败 | 降级为 `?ws=` URL 参数方式（浏览器直接访问） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 marked + highlight.js | 用 react-markdown + rehype-highlight | 不引入 React，保持原生 JS 轻量；marked 是最成熟的 Markdown 解析库 |
| highlight.js 按需加载语言 | 用 Prism.js | highlight.js 自动语言检测更好，CDN 按需加载体积可控 |
| 不引入前端框架 | 用 React/Vue | 现有前端是原生 JS，引入框架增加构建复杂度；当前交互复杂度原生 JS 可控 |
| 流式渲染用防抖 Markdown | 每次更新都重新渲染 | 流式更新频率高（每 token），全量重渲染性能差；100ms 防抖平衡流畅度和性能 |
| Todo 面板独立于 transcript | 在 transcript 中渲染 todo 节点 | Codex 的 todo 是独立面板，用户需要随时看到进度，不应被对话滚动淹没 |
| 工具节点默认折叠 result | 全部展开 | tool_result 通常很长（文件内容、命令输出），折叠减少视觉噪音；error 时强制展开 |
| CSS 变量做主题 | 用 CSS-in-JS 或 Tailwind | CSS 变量零依赖，跨平台一致，后续支持浅色主题只需改变量 |
| Slash 命令硬编码 | 从后端动态获取 | 后端 slash 命令列表固定（/mcp /model /lsp /session /skills /init），硬编码简单可靠 |

### 已解决问题（Review 后确定）

- **macOS `beforeDevCommand` 兼容性**：推荐改用 `cd ../frontend && npm run dev`，在 bash/zsh 上跨平台兼容性更好，避免 `--prefix` 在不同 shell 下的行为差异
- **highlight.js 加载策略**：通过 npm 安装，打包时只引入常用语言包（python/javascript/bash/json/rust/diff），约 50KB。不使用 CDN，因为 Tauri 打包后是本地文件，CDN 会引入网络依赖

## Open Questions

- [ ] macOS 上 `beforeDevCommand: "npm run dev --prefix ../frontend"` 是否工作？需验证（bash 下 `--prefix` 行为可能不同，可能需要改成 `cd ../frontend && npm run dev`）— **已给出推荐方案，见上方"已解决问题"**
- [ ] highlight.js 语言包加载策略：全量打包 vs CDN 按需？全量约 200KB，常用语言（python/js/bash/json/rust）约 50KB — **已给出推荐方案，见上方"已解决问题"**
- [ ] 流式 Markdown 渲染的防抖间隔 100ms 是否合适？需实际测试 token 速率后调整
- [ ] 是否需要支持浅色主题？Codex 支持主题切换，但当前后端无主题协议，后续可加
- [ ] Todo 面板位置：顶部（当前设计）vs 侧边栏？侧边栏更接近 Codex，但窄屏不友好
