# 前端 UI 改造设计方案

## Context

voidx 桌面端前端当前是单栏布局（status-bar → todo-panel → transcript → composer），原生 JS + 手写 CSS。功能上能跑，但布局简单、缺少会话列表和侧边面板、CSS 值硬编码、消息项类型不够丰富。

调研了 OpenAI Codex 桌面 App（闭源，Electron + React）和开源克隆 `codex-desktop`（Tauri 2 + Rust + React，spec-first 阶段）。codex-desktop 的设计文档描述了三栏布局、设计 token 体系、IPC 命名规范和消息项分类，这些设计思路值得借鉴。

本方案在 voidx 现有技术栈（原生 JS + Vite + Tauri 2）上，分阶段引入这些改进，不切换到 React/TypeScript。

## Goals and Non-Goals

### Goals

- 三栏布局：左侧会话列表 + 中间对话区 + 右侧 dock 面板
- 设计 token 体系：统一 `--vx-*` CSS 变量，消除硬编码值
- IPC 命名规范：系统化 gateway 方法名和事件 topic
- 消息项分类丰富化：增加 plan 卡片、可折叠工具卡片、流式光标

### Non-Goals

- 不切换到 React/TypeScript——保持原生 JS + Vite
- 不引入 Tailwind——手写 CSS + token 变量足够
- 不引入 Zustand/TanStack Query——模块级状态 + DOM 直够
- 不克隆 Codex 的全部功能（worktree、automations、sites 等）
- 不改变 Python 后端和 gateway 协议的核心架构

## Architecture

### 当前布局

```
┌───────────────────────────────────────────────────┐
│ status-bar (dot · model · workspace · session)     │
├───────────────────────────────────────────────────┤
│ todo-panel (conditional)                           │
├───────────────────────────────────────────────────┤
│                                                    │
│ transcript (single column, scrollable)             │
│                                                    │
├───────────────────────────────────────────────────┤
│ composer (textarea · send · cancel)                │
└───────────────────────────────────────────────────┘
```

文件结构：`index.html`（单页）→ `main.js`（WebSocket + 事件路由）→ `render.js`（快照渲染）→ `stream.js`（流式缓冲）→ `markdown.js`（Markdown + 高亮）→ `slash.js`（斜杠命令）。

### 目标布局

```
┌───────────────────────────────────────────────────────────────────┐
│ titlebar (brand · workspace · search · model · connection dot)     │
├───────────┬───────────────────────────────────────┬───────────────┤
│           │                                       │               │
│ LEFT      │ MAIN AREA                             │ RIGHT PANEL   │
│ SIDEBAR   │                                       │ (dockable)    │
│           │ ┌───────────────────────────────────┐ │               │
│ sessions  │ │ transcript (virtualized)          │ │ tabs:         │
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
├── index.html              # 三栏 shell 骨架
├── styles.css              # 全局样式（引用 tokens.css）
├── tokens.css              # [新增] 设计 token 定义
├── src/
│   ├── main.js             # 入口：bootstrap + WebSocket + 事件路由
│   ├── sidebar.js          # [新增] 左侧会话列表渲染 + 搜索
│   ├── dock.js             # [新增] 右侧 dock 面板管理（tab 切换）
│   ├── render.js           # 快照渲染（节点 → DOM）
│   ├── stream.js           # 流式缓冲
│   ├── markdown.js         # Markdown + 代码高亮
│   ├── slash.js            # 斜杠命令
│   └── protocol.schema.json
├── test/
│   ├── setup.js            # DOM 骨架（更新为三栏）
│   ├── sidebar.test.js     # [新增]
│   ├── dock.test.js        # [新增]
│   ├── render.test.js
│   ├── stream.test.js
│   ├── markdown.test.js
│   ├── slash.test.js
│   └── main.test.js
```

### 数据流

```
Python backend (gateway WebSocket)
    │
    ▼ JSON-RPC 2.0
    │
    ├─ WorkspaceSnapshot (on connect) ──→ sidebar.js 渲染会话列表
    │                                      + render.js 渲染活跃线程快照
    │
    ├─ item.started/delta/completed ─────→ main.js handleItem 路由
    │                                      ├─ message → appendMessageItem
    │                                      ├─ tool → handleToolItem
    │                                      ├─ assistant_stream → stream.js
    │                                      ├─ todo → dock.js (Todo tab)
    │                                      └─ prompt → request dialog
    │
    ├─ terminal.* notifications ─────────→ dock.js (Terminal tab)
    │
    └─ diff.review notifications ────────→ dock.js (Diff Review tab)
```

## Data Model

### 设计 Token 体系

新建 `frontend/tokens.css`，所有样式值从这里引用。命名空间 `--vx-*`。

```css
:root {
  /* ── 色彩 ── */
  --vx-bg-base:        #0b0f14;   /* 应用底色 */
  --vx-bg-surface:     #0f1620;   /* 面板/卡片底色 */
  --vx-bg-elevated:    #111a26;   /* 悬浮元素底色 */
  --vx-bg-hover:       #1a2433;   /* hover 态底色 */

  --vx-border:         #263241;   /* 默认边框 */
  --vx-border-strong:  #304155;   /* 强调边框 */

  --vx-text-primary:   #e7edf5;   /* 主文本 */
  --vx-text-secondary: #b7c4d6;   /* 次文本 */
  --vx-text-muted:     #8fa3bb;   /* 弱化文本 */
  --vx-text-dim:       #9aa9bb;   /* 最弱文本 */

  --vx-accent:         #7aa2f7;   /* 品牌强调色 */
  --vx-success:        #9ece6a;   /* 成功 */
  --vx-warning:        #e0af68;   /* 警告 */
  --vx-error:          #f7768e;   /* 错误 */

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

`styles.css` 开头引入：`@import url("./tokens.css");`，然后将所有 `var(--bg-primary)` 改为 `var(--vx-bg-base)` 等。

### Token 迁移映射

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

### IPC 方法命名规范

当前 gateway 已注册的方法名风格不统一。规范如下：

**规则**：`domain.verb`，全小写，点分。

| 域 | 当前命名 | 规范命名 | 说明 |
|----|---------|---------|------|
| session | `session.submit` | `session.submit` | ✅ 已合规 |
| session | `session.cancel` | `session.cancel` | ✅ 已合规 |
| session | `session.create` | `session.create` | ✅ 已合规 |
| session | `session.fork` | `session.fork` | ✅ 已合规 |
| session | `session.delete` | `session.delete` | ✅ 已合规 |
| session | `session.rename` | `session.rename` | ✅ 已合规 |
| session | `session.switch` | `session.switch` | ✅ 已合规 |
| session | `session.list` | `session.list` | ✅ 已合规 |
| terminal | `terminal.start` | `terminal.start` | ✅ 已合规 |
| terminal | `terminal.input` | `terminal.input` | ✅ 已合规 |
| terminal | `terminal.resize` | `terminal.resize` | ✅ 已合规 |
| terminal | `terminal.stop` | `terminal.stop` | ✅ 已合规 |
| diff | `diff.review` | `diff.review` | ✅ 已合规 |
| diff | `diff.decide` | `diff.decide` | ✅ 已合规 |
| diff | `diff.apply` | `diff.apply` | ✅ 已合规 |

**结论**：当前方法命名已符合 `domain.verb` 规范，无需改动。新增方法时遵循此规则。

### 事件 Topic 命名规范

当前事件使用 `item.{lifecycle}` 格式（如 `item.started`、`item.delta`、`item.completed`）。扩展规范：

**规则**：非 item 事件使用 `domain:scope:event` 格式。

| 事件 | 当前格式 | 规范格式 | 说明 |
|------|---------|---------|------|
| item 生命周期 | `item.started` | `item.started` | ✅ 保持不变 |
| turn 生命周期 | `turn.started` | `turn.started` | ✅ 保持不变 |
| terminal 输出 | `terminal.output` | `terminal.output` | ✅ 保持不变 |
| terminal 退出 | `terminal.exited` | `terminal.exited` | ✅ 保持不变 |
| diff 审查 | `diff.review.started` | `diff.review.started` | ✅ 保持不变 |

**结论**：当前事件命名已基本规范。item 事件保持 `item.{lifecycle}` 不变（因为 kind 字段已区分域），非 item 事件使用 `domain.event` 格式。

### 前端模块接口

#### sidebar.js

```js
// 渲染会话列表
export function renderSidebar(threads: ThreadInfo[], activeThreadId: string): HTMLElement

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

// 追加终端输出到 Terminal tab
export function appendTerminalOutput(sessionId: string, data: string): void

// 渲染 diff 审查内容到 Diff tab
export function renderDiffReview(sessionId: string, diffText: string): void

// 折叠/展开 dock
export function toggleDock(): void
```

## 消息项分类

### 当前 Item kinds（7 种）

| kind | 渲染位置 | 当前状态 |
|------|---------|---------|
| `message` | transcript | ✅ text/markdown/guidance 三种 style |
| `assistant_stream` | transcript | ✅ 流式文本 + thinking |
| `tool` | transcript | ✅ started/delta/completed 生命周期 |
| `todo` | transcript 上方 | ⚠️ 当前在 transcript 外部，应移到 dock |
| `subagent` | transcript | ⚠️ 仅占位，不渲染 |
| `status` | — | ✅ 不渲染 |
| `prompt` | dialog | ✅ 权限请求对话框 |

### 改进项

#### 1. Todo 移入 Dock

当前 todo-panel 是 transcript 上方的独立条。改为 dock 的 Todo tab：

- `handleItem` 中 `kind === "todo"` 调用 `dock.renderTodoInDock()` 而非 `renderTodoPanel()`
- dock 折叠时，todo 有更新则显示 badge 数字
- dock 展开时，直接渲染 todo 列表

#### 2. 工具卡片增强

当前 `handleToolItem` 渲染的 tool-item 是平铺的。改进：

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
  // diff_text 用 diff block 渲染而非纯 pre
  if (data.diff_text) {
    el.append(renderDiffBlock(data.diff_text));
  }
}
```

#### 3. 流式光标

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
// 流未 commit 时追加光标元素
if (!stream.committed) {
  const cursor = document.createElement("span");
  cursor.className = "stream-cursor";
  stream.textEl.append(cursor);
}
```

#### 4. Plan 卡片（未来）

当前 voidx 没有 plan item kind。如果未来加入 plan 模式（类似 codex-desktop 的 `plan_start` / `plan_approve`），可以新增 `kind: "plan"` 的 item，渲染为可展开的计划卡片。**本次不实现**，仅预留。

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

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| WebSocket 断连 | titlebar 连接点变红，transcript 顶部显示重连提示，自动重连（现有逻辑） |
| 会话列表加载失败 | sidebar 显示 "Failed to load sessions" + 重试按钮 |
| dock tab 内容加载失败 | dock 内容区显示错误信息，不影响主对话区 |
| 终端会话异常退出 | Terminal tab 显示退出码 + 重新打开按钮 |
| diff 渲染失败 | Diff tab 显示原始 diff 文本（降级） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 保持原生 JS，不切 React | 切换到 React + TypeScript | 迁移成本高，现有 6 个 JS 模块 + 101 个测试全部可用，原生 JS 足以支撑三栏布局 |
| 新建 tokens.css 而非内联在 styles.css | 内联在 styles.css | 独立文件便于维护和未来主题切换（亮色主题） |
| Todo 移入 dock 而非保持独立条 | 保持 transcript 上方独立条 | 三栏布局后 transcript 宽度变窄，独立 todo 条会挤压对话空间；dock 可折叠更灵活 |
| item 事件保持 `item.{lifecycle}` 格式 | 改为 `domain:scope:event` | kind 字段已区分域，改格式会破坏现有前端和测试，无实际收益 |
| 不引入虚拟列表 | 用虚拟滚动库 | 当前消息量不大，原生 `overflow-y: auto` 足够；未来消息量增大时再引入 |
| 侧栏宽度固定 240px | 可拖拽调整 | 初期固定宽度降低复杂度，后续可加 resize 手柄 |

## Implementation Phases

### Phase 1: 设计 Token 体系（低风险，纯 CSS）

1. 新建 `frontend/tokens.css`，定义全部 `--vx-*` 变量
2. `styles.css` 开头 `@import url("./tokens.css")`
3. 全局替换 `var(--bg-primary)` → `var(--vx-bg-base)` 等（ sed 批量替换）
4. 删除 `styles.css` 中 `:root` 块的旧变量定义
5. 运行 `npm test` + `npm run build` 验证

**验证命令**：`cd frontend && npm test && npm run build`

### Phase 2: 三栏 HTML 骨架 + CSS（中风险，布局变更）

1. 重写 `index.html` 为三栏 shell 骨架
2. 更新 `styles.css` 添加 `.vx-shell`、`.vx-body`、`.vx-sidebar`、`.vx-main`、`.vx-dock` 布局样式
3. 更新 `test/setup.js` 的 DOM 骨架匹配新 HTML
4. 更新 `main.js` 中的 `querySelector` 选择器匹配新 ID/class
5. 运行测试，修复选择器不匹配的用例

**验证命令**：`cd frontend && npm test`

### Phase 3: 左侧会话列表（中风险，新模块）

1. 新建 `frontend/src/sidebar.js`，实现 `renderSidebar`、`updateThreadStatus`、`filterSessions`
2. `main.js` 中处理 `WorkspaceSnapshot` 事件，调用 `sidebar.renderSidebar()`
3. 用户点击会话项 → 发送 `session.switch` 方法
4. 用户点击 "New" → 发送 `session.create` 方法
5. 新建 `frontend/test/sidebar.test.js`
6. 更新 `test/setup.js` DOM 骨架包含 sidebar 元素

**验证命令**：`cd frontend && npx vitest run test/sidebar.test.js`

### Phase 4: 右侧 Dock 面板（中风险，新模块）

1. 新建 `frontend/src/dock.js`，实现 `initDock`、`switchTab`、`renderTodoInDock`、`appendTerminalOutput`、`renderDiffReview`
2. `main.js` 中 `kind === "todo"` 改为调用 `dock.renderTodoInDock()`
3. `main.js` 中 terminal/diff 通知路由到 dock
4. 新建 `frontend/test/dock.test.js`
5. 更新 `test/setup.js` DOM 骨架包含 dock 元素

**验证命令**：`cd frontend && npx vitest run test/dock.test.js`

### Phase 5: 消息项增强（低风险，增量改进）

1. `handleToolItem`：添加可折叠 header、elapsed 显示、diff 内联渲染
2. `stream.js`：添加流式光标
3. 更新对应测试用例

**验证命令**：`cd frontend && npx vitest run test/main.test.js test/stream.test.js`

### Phase 6: 集成验证

1. `npm test` 全量通过
2. `npm run build` 构建成功
3. Tauri 桌面端启动验证：`cargo build --release` + 运行 exe
4. 手动测试：会话切换、todo dock、工具卡片折叠、流式光标

**验证命令**：`cd frontend && npm test && npm run build && cd ../desktop/src-tauri && cargo build --release`

## Open Questions

- [ ] 侧栏是否需要可拖拽调整宽度？初期固定，后续可加 resize 手柄
- [ ] dock 默认展开还是折叠？建议默认折叠，有内容时自动展开
- [ ] 是否需要会话搜索？Phase 3 先做列表渲染，搜索作为后续增量
- [ ] 终端 tab 是否用 xterm.js？初期用 `<pre>` 纯文本，后续可引入 xterm.js
- [ ] 亮色主题是否在本方案范围内？token 体系已预留，但实现留到后续
