# 前端 JS 单元测试体系 — 技术设计文档

> **Status: Done** — vitest + jsdom 测试体系已落地,5 个测试文件 / 101 个用例全部通过(`npm test`)。源码侧改动(`_resetForTest`、`import.meta.env.TEST` 守卫、私有函数 export)均已实现。

## Context

desktop 前端有 5 个 JS 模块（共 941 行），但从未建立测试体系：`package.json` 无测试运行器依赖、无 `test` 脚本，目录下无任何 `*.test.js` 文件。近期连续修复了两个渲染 bug（消息不显示、工具调用信息不显示），这些 bug 本应被单元测试拦住。本设计为前端引入 vitest + jsdom 测试框架，覆盖纯函数和 DOM 渲染逻辑。

## Goals and Non-Goals

### Goals

- 引入 vitest + jsdom 测试基础设施，与现有 vite 8.x 构建链兼容
- 覆盖 5 个 JS 模块的可测试函数，按可测试性分三层递进
- 重构 main.js 使其可被测试导入而不触发顶层副作用（浏览器行为不变）
- 每个测试文件独立、无状态泄漏，`npm test` 一键运行全部

### Non-Goals

- 不做 E2E 测试（需要真实浏览器 + WebSocket 后端，成本高）
- 不做视觉回归测试 / 快照测试（前端是纯渲染层，DOM 结构断言已足够）
- 不追求 100% 覆盖率，聚焦渲染逻辑和纯函数
- 不改 Rust 侧（desktop/src-tauri）测试

## Architecture

### 测试框架选型

| 选项 | 决策 | 理由 |
|------|------|------|
| vitest | ✅ 采用 | 与 vite 8.x 原生集成，共享 vite.config.js（需显式添加 test 字段），ESM 开箱即用 |
| jest | ❌ 否 | 需额外配置 ESM 转换，与 vite 生态割裂 |
| mocha | ❌ 否 | 需手动拼 chai + jsdom + esm loader，配置繁琐 |

### 模块可测试性分层

```
Layer 1 — 纯函数（无 DOM 依赖）
├── slash.js     → matchSlashCommands, renderSlashMenu
└── render.js    → stripRichMarkup, nodeClassName, formatElapsed, formatToolMeta, diffLineClass

Layer 2 — DOM 渲染函数（依赖 document API，jsdom 可满足）
├── markdown.js  → renderMarkdown, highlightCode
├── stream.js    → getOrCreateStream, appendStreamText, commitStream, discardStream, takeCommittedStreams
└── render.js    → renderNodeElement, renderTranscript, renderTodoPanel

Layer 3 — main.js 渲染逻辑（依赖模块级 DOM 引用 + 顶层副作用）
├── appendMessageItem  → 需 export + transcriptEl 注入
└── handleToolItem     → 需 export + transcriptEl 注入
```

### main.js 重构策略

**问题**：main.js 顶层有副作用代码——`document.querySelector` 12 个 DOM 元素、调用 `setTranscriptElement(transcriptEl)`、调用 `bootstrap()` 发起 WebSocket 连接。import 时立即执行，测试环境无法控制。

**方案**：用环境守卫包裹副作用，不改变函数定义位置。

```javascript
// 重构前（main.js 顶部）
const transcriptEl = document.querySelector("#transcript");
// ... 11 个其他 querySelector
setTranscriptElement(transcriptEl);
bootstrap().catch(...);

// 重构后
const transcriptEl = document.querySelector("#transcript");
// ... 11 个其他 querySelector
setTranscriptElement(transcriptEl);

// 仅在浏览器环境（非测试）自动启动
// import.meta.env 在 Vite/Vitest 中始终有定义；TEST 是 vitest 注入的编译时常量，
// 浏览器构建时被 tree-shake 为 undefined，bootstrap() 正常执行
if (!import.meta.env.TEST) {
if (!import.meta.env?.TEST) {
  bootstrap().catch((error) => {
    setConnectionStatus("error", error instanceof Error ? error.message : String(error));
  });
}
```

**导出策略**：在函数定义前加 `export`，不改变函数体。

```javascript
export function handleItem(method, params) { ... }
export function appendMessageItem(itemId, data) { ... }
export function handleToolItem(method, itemId, data) { ... }
```

**render.js 私有函数导出**：`diffLineClass`、`formatToolMeta`、`formatElapsed` 目前是模块私有（无 `export`），测试需访问。同样加 `export`，不影响浏览器行为：

```javascript
export function diffLineClass(line) { ... }
export function formatToolMeta(payload) { ... }
export function formatElapsed(seconds) { ... }
```

**风险控制**：`export` 关键字不影响浏览器行为（Vite 构建时 tree-shaking 处理）。`import.meta.env.TEST` 是 vitest 注入的环境变量，浏览器构建时为 `undefined`，`bootstrap()` 正常执行。

## Data Model

### 测试 DOM 骨架（test/setup.js）

**关键**：DOM 骨架必须在**模块顶层**执行，不能放在 `beforeEach` 中。原因：vitest 执行顺序是 setupFiles 导入 → 测试文件导入（触发 main.js 模块顶层 `document.querySelector`）→ `beforeEach` 执行。若 DOM 骨架在 `beforeEach` 中，main.js import 时 jsdom DOM 为空，所有 `querySelector` 返回 null。

```javascript
// test/setup.js — jsdom 环境全局 setup
// 模块顶层立即设置 DOM 骨架，确保 main.js import 时 querySelector 能找到元素
// 结构与 index.html 完全对齐，避免 class/结构断言失败
document.body.innerHTML = `
  <main class="shell">
    <header class="status-bar">
      <div class="status-item">
        <span class="status-dot disconnected" id="status-dot"></span>
        <span class="status-brand">voidx</span>
      </div>
      <div class="status-item status-model" id="status-model"></div>
      <div class="status-item status-workspace" id="status-workspace"></div>
      <div class="status-item status-session" id="status-session"></div>
    </header>
    <section class="todo-panel" id="todo-panel" aria-label="Task progress"></section>
    <section class="transcript" id="transcript" aria-live="polite"></section>
    <form class="composer" id="composer">
      <div class="slash-menu" id="slash-menu"></div>
      <textarea id="input" rows="3"></textarea>
      <button type="submit" class="btn-send" id="btn-send">Send</button>
      <button type="button" class="btn-cancel" id="btn-cancel" disabled>Cancel</button>
    </form>
    <dialog id="request-dialog" class="request-dialog">
      <form id="request-form" method="dialog">
        <h2 id="request-title"></h2>
        <div id="request-details"></div>
        <div id="request-controls"></div>
      </form>
    </dialog>
  </main>
`;

import { beforeEach } from "vitest";

// 每个测试前重置 DOM 内容（保留骨架结构，清除动态添加的子元素）
beforeEach(() => {
  document.querySelector("#transcript").innerHTML = "";
  document.querySelector("#todo-panel").innerHTML = "";
  document.querySelector("#slash-menu").innerHTML = "";
  document.querySelector("#input").value = "";
});
```

### vite.config.js 测试配置

vitest 需要显式配置 `test` 字段（并非真正的零配置）：

```javascript
// vite.config.js 追加 test 配置
import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  build: { outDir: "dist" },
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.js"],
    globals: true,
  },
});
```

### stream.js 状态隔离

stream.js 使用模块级 `Map` 和数组（`streams`、`committedEls`），测试间需清理。方案：在 stream.js 中导出一个 `_resetForTest()` 函数，仅在测试环境使用：

```javascript
// stream.js 追加
export function _resetForTest() {
  streams.clear();
  committedEls.length = 0;
  transcriptEl = null;
}
```

## API Contract

### 测试文件 → 被测模块映射

| 测试文件 | 被测模块 | 被测函数 | 测试用例数 |
|---------|---------|---------|-----------|
| `test/slash.test.js` | slash.js | matchSlashCommands, renderSlashMenu | ~8 |
| `test/render.test.js` | render.js | stripRichMarkup, nodeClassName, formatElapsed, formatToolMeta, diffLineClass, renderNodeElement (含 subagent), renderTranscript, renderTodoPanel | ~22 |
| `test/markdown.test.js` | markdown.js | renderMarkdown, highlightCode | ~8 |
| `test/stream.test.js` | stream.js | getOrCreateStream, appendStreamText, commitStream, discardStream, takeCommittedStreams | ~12 |
| `test/main.test.js` | main.js | handleItem (路由分发), appendMessageItem, handleToolItem | ~15 |

### 测试用例签名示例

#### slash.test.js

```javascript
matchSlashCommands("")           → []
matchSlashCommands("hello")      → []
matchSlashCommands("/m")         → [{command:"/mcp",...}, {command:"/model",...}]
matchSlashCommands("/mcp")       → [{command:"/mcp",...}]
matchSlashCommands("/xyz")       → []
renderSlashMenu([], 0)           → div.slash-menu (无子元素)
renderSlashMenu(cmds, 0)         → div.slash-menu > 2×div.slash-item (第一个有 .selected)
```

#### render.test.js（纯函数）

```javascript
stripRichMarkup("[bold]text[/bold]")     → "text"
stripRichMarkup("[red]err[/red] ok")     → "err ok"
stripRichMarkup(null)                    → ""
nodeClassName({node_type:"assistant"})   → "node node-assistant"
nodeClassName({node_type:"tool_call", status:"error"}) → "node node-tool_call node-error"
formatElapsed(0.5)    → "500ms"
formatElapsed(2.34)   → "2.3s"
formatElapsed(null)   → ""
diffLineClass("+++ a") → "diff-meta"
diffLineClass("+add")  → "diff-add"
diffLineClass("-del")  → "diff-del"
diffLineClass(" ctx")  → "diff-context"
```

#### render.test.js（DOM 渲染）

```javascript
// subagent 渲染（F6 补充）
renderNodeElement({node_type:"subagent", id:"n1", title:"child agent", payload:{name:"explore", description:"searching"}})
  → article.node.node-subagent > .subagent-card > .subagent-header > .subagent-name("explore")
  → .subagent-steps (textContent="searching")

renderNodeElement({node_type:"subagent", id:"n2", title:"child", elapsed:3.5, payload:{name:"plan"}})
  → .subagent-elapsed (textContent="3.5s")

// root/turn/todo 返回 null
renderNodeElement({node_type:"root", id:"r1"})     → null
renderNodeElement({node_type:"turn", id:"t1"})     → null
renderNodeElement({node_type:"todo", id:"td1"})    → null
```

#### main.test.js（DOM 渲染）

```javascript
appendMessageItem("msg-1", {style:"text", text:"hello"})
  → transcriptEl 内有 .message-item.message-text > pre (textContent="hello")

appendMessageItem("msg-2", {style:"markdown", text:"**bold**"})
  → transcriptEl 内有 .message-item.message-markdown > .markdown-body

handleToolItem("item.started", "t1", {tool_call_id:"c1", tool_name:"bash", args:{cmd:"ls"}})
  → transcriptEl 内有 .tool-item[data-tool-id="c1"] > .tool-header > .tool-name("bash") + .tool-spinner("running")
  → .tool-args pre (textContent 含 "cmd")

handleToolItem("item.completed", "t1", {tool_call_id:"c1", ok:true, detail:"done"})
  → .tool-spinner.textContent === "done", className 含 "ok"
  → .tool-detail pre (textContent="done")
```

#### main.test.js（handleItem 路由分发，F4 补充）

handleItem 是 item-kind 分发器，路由到各子处理器。测试验证每种 kind 正确路由：

```javascript
// message kind → appendMessageItem
handleItem("item.started", {kind:"message", item_id:"m1", data:{style:"text", text:"hi"}})
  → transcriptEl 内有 .message-item

// tool kind → handleToolItem
handleItem("item.started", {kind:"tool", item_id:"t1", data:{tool_call_id:"c1", tool_name:"bash"}})
  → transcriptEl 内有 .tool-item

// assistant_stream kind → appendStreamText
handleItem("item.started", {kind:"assistant_stream", item_id:"s1", data:{phase:"text"}})
  → transcriptEl 内有 .stream-buffer

// todo kind → renderTodoPanel
handleItem("item.started", {kind:"todo", item_id:"td1", data:{items:[{id:"x",content:"task",status:"pending"}], summary:"plan"}})
  → todo-panel 有 .todo-item

// status kind → 无渲染（静默返回）
handleItem("item.started", {kind:"status", item_id:"st1", data:{}})
  → transcriptEl 无新子元素
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| DOMPurify 在 jsdom 下行为差异 | 测试只断言结构（元素存在、className、textContent），不断言净化后的精确 HTML |
| stream.js 模块级状态泄漏 | `beforeEach` 调用 `_resetForTest()` + 重新 `setTranscriptElement` |
| main.js import 触发 bootstrap | `import.meta.env.TEST` 守卫，测试环境跳过 |
| main.js 函数引用模块级 transcriptEl | 测试 setup.js 注入 `#transcript` DOM 元素，main.js 顶层 querySelector 自动获取 |
| vitest 与 vite 8.x 版本不兼容 | 锁定 vitest ^4.x（peerDeps 支持 vite ^6/^7/^8，已验证 4.1.9 兼容 vite 8.1.0） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| vitest 而非 jest | jest | 与 vite 原生集成，零额外配置，ESM 原生支持 |
| `import.meta.env.TEST` 守卫而非提取 init() | 提取 init() 函数 | 改动最小，不改变模块结构，浏览器行为零风险 |
| 导出 `_resetForTest()` 而非 vi.resetModules() | vi.resetModules() | resetModules 会导致每个测试重新 import 模块，丢失 mock 状态，且 stream.js 的 Map 需要显式清空更可靠 |
| 测试文件放 `frontend/test/` 而非 `frontend/src/__tests__/` | co-located 测试 | 与 Python tests/ 目录风格一致，src/ 保持纯净 |
| 不测 main.js 的 connect/bootstrap | 全覆盖 | 这些函数依赖 WebSocket 和全局 socket 状态，测试成本高收益低，聚焦渲染逻辑 |
| 测试 handleItem 路由分发 | 仅测子处理器 | handleItem 是 item-kind 分发器，最近修复的渲染 bug 正是路由层问题，必须覆盖路由正确性 |
| vitest 需显式 test 配置 | 零配置 | 文档原称"零配置"不准确，需在 vite.config.js 显式设置 test.environment/setupFiles/globals |

## Open Questions

- [x] ~~vitest ^2.x 是否与 vite 8.x 完全兼容？~~ 已解决：vitest 4.1.9 的 peerDeps 为 `vite ^6.0.0 || ^7.0.0 || ^8.0.0`，与 vite 8.1.0 兼容。锁定 vitest ^4.x。
- [ ] DOMPurify 在 jsdom 下是否正常工作？若不行，markdown.js 测试可能需要 mock DOMPurify.sanitize 为透传
