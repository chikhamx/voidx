# voidx vs Codex 桌面框架技术选型对比

> 2026-07-01 · 基于代码实际审查

---

## 一、架构总览对比

```
Codex 桌面端                              voidx 桌面端
┌────────────────────────┐               ┌────────────────────────┐
│  Electron Main (Node)  │               │  Tauri Main (Rust)     │
│  窗口/权限/更新/IPC     │               │  窗口/进程管理/IPC     │
├────────────────────────┤               ├────────────────────────┤
│  Renderer (Chromium)   │               │  WebView (wry/WebKit)  │
│  React + TypeScript    │               │  原生 JS (无框架)       │
├────────────────────────┤               ├────────────────────────┤
│  Rust Sidecar          │    ← 同构 →   │  Python Sidecar        │
│  codex 二进制           │               │  voidx.main --web      │
│  JSON-RPC over stdio   │               │  JSON-RPC over WebSocket│
└────────────────────────┘               └────────────────────────┘
```

两者都是 **壳进程 + Agent Sidecar** 架构，差别在壳的实现和 sidecar 的语言。

---

## 二、逐层技术选型对比

### 2.1 桌面壳层

| 维度 | Codex | voidx | 评价 |
|------|-------|-------|------|
| 框架 | Electron 42 | Tauri 2.1 | **voidx 更优** |
| 渲染引擎 | Chromium 149 (捆绑) | 系统 WebView (wry: WebKit/GTK on Linux, WebKit on macOS, WebView2 on Windows) | 各有取舍 |
| 壳语言 | Node.js (Main) | Rust (Main) | **voidx 更优** |
| 包体积 | ~1.0 GB | ~10-20 MB (壳本身) | **voidx 远优** |
| 内存占用 | 300-600 MB idle | 50-150 MB idle | **voidx 远优** |
| 自动更新 | Sparkle (macOS) / Squirrel (Windows) | Tauri Updater (内置) | 持平 |
| 安全 fuses | @electron/fuses | Tauri CSP + capabilities | 持平 |
| 跨平台 | macOS + Windows | macOS + Windows + Linux | **voidx 更优** |

**结论**：Tauri 在体积、内存、跨平台方面全面优于 Electron。Codex 选 Electron 的唯一优势是 Chromium 渲染一致性（不依赖系统 WebView），以及 Node.js 生态的丰富 npm 包。但对于 agent 桌面端这个场景，渲染一致性不是关键瓶颈。

### 2.2 前端层

| 维度 | Codex | voidx | 评价 |
|------|-------|-------|------|
| 框架 | React + TypeScript | 原生 JS (ES modules) | **Codex 更优**（可维护性） |
| 构建 | Webpack/Vite (Forge 集成) | Vite | 持平 |
| 类型安全 | TypeScript | JSON Schema → TypeScript d.ts (生成) | **Codex 更优** |
| Markdown | (推测) react-markdown | marked + DOMPurify + highlight.js | 持平 |
| 终端 | xterm.js | 自实现 DOM terminal (非 xterm) | **Codex 更优** |
| Diff/Preview | Monaco Editor (推测) | 自实现 diff-review.js | **Codex 更优** |
| 状态管理 | React hooks/context | 模块级变量 + DOM 直接操作 | **Codex 更优** |
| 测试 | (未公开) | vitest + jsdom | voidx 有完整测试 |

**结论**：Codex 的前端工程化程度更高（React + TS + xterm + Monaco）。voidx 的原生 JS 方案轻量但可维护性随功能增长会下降。这是 voidx 当前最大的差距点。

### 2.3 Agent 内核 (Sidecar)

| 维度 | Codex | voidx | 评价 |
|------|-------|-------|------|
| 语言 | Rust | Python (LangGraph) | **Codex 更优**（性能/分发） |
| 开源 | 是 (codex-rs) | 是 (voidx) | 持平 |
| 分发 | 单二进制 | 需 Python venv | **Codex 远优** |
| 启动速度 | ~100ms | ~1-3s (Python 启动) | **Codex 更优** |
| 内存 | ~50-100MB | ~200-400MB | **Codex 更优** |
| 生态 | Rust crates | PyPI (极其丰富) | **voidx 更优** |
| 开发速度 | 较慢 (Rust) | 快 (Python) | **voidx 更优** |
| 沙箱 | OS 级 (Seatbelt/Bubblewrap/Windows tokens) | permission engine (应用级) | **Codex 更优** |

**结论**：Rust 内核在性能、分发、启动速度、内存上全面领先。但 Python 的开发速度和生态丰富度是 voidx 的优势。voidx 的 Tauri Rust 壳 + Python sidecar 是一个合理的折中——壳用 Rust 拿到体积优势，内核用 Python 拿到开发速度。

### 2.4 通信协议

| 维度 | Codex | voidx | 评价 |
|------|-------|-------|------|
| 协议 | JSON-RPC 2.0 | JSON-RPC 2.0 (v2) | **相同** |
| 传输 | JSONL over stdio + WebSocket | WebSocket | Codex 多一种 |
| 核心原语 | Thread / Turn / Item | Thread / Turn / Item (v2 adapter) | **相同** |
| Item 生命周期 | started → delta → completed | started → delta → completed | **相同** |
| 多 Thread | 是 | 是 (GatewaySession.register_thread) | **相同** |
| Schema | JSON + TypeScript 生成 | JSON Schema → TypeScript d.ts 生成 | **相同** |
| 版本 | v2 | v1 (envelope) + v2 (JSON-RPC) 并存 | voidx 有迁移债务 |

**结论**：协议层两者几乎一致。voidx 已经实现了 JSON-RPC 2.0 + Thread/Turn/Item 模型，与 Codex 的 App Server 协议设计对齐。voidx 的 v1/v2 并存是技术债，需要清理。

### 2.5 功能对比

| 功能 | Codex | voidx | 差距 |
|------|-------|-------|------|
| 并行 Thread | ✅ 多窗口 + 多 thread | ✅ 多 thread (单窗口 sidebar) | voidx 缺多窗口 |
| Worktree | ✅ 每 thread 独立 worktree | ❌ | **大差距** |
| Git 集成 | ✅ diff/commit/branch/PR | ✅ diff review (diff-review.js) | voidx 基础有 |
| 内嵌终端 | ✅ xterm.js | ✅ 自实现 (terminal.js) | voidx 功能弱 |
| In-App Browser | ✅ | ❌ | **缺失** |
| Artifact Preview | ✅ | ❌ | **缺失** |
| Automations | ✅ hooks + 触发器 | ❌ | **缺失** |
| Plugins | ✅ | ❌ (有 skills) | 部分覆盖 |
| Skills | ✅ 跨端面 | ✅ | **持平** |
| Computer Use | ✅ OS 级 | ❌ | **缺失** |
| 沙箱 | ✅ OS 级 | ⚠️ 应用级 permission | **差距** |
| 审批模式 | suggest/auto-edit/full-auto | permission engine (rules/sandbox/approval) | 概念一致 |
| MCP | ✅ | ✅ | **持平** |
| Subagents | ✅ | ✅ (child agents) | **持平** |
| Workflow | ✅ (skills + automations) | ✅ (workflow DAG) | **voidx 更结构化** |
| Slash Commands | ✅ | ✅ | **持平** |
| LSP | (未确认) | ✅ | **voidx 有** |
| IDE 集成 | ✅ VS Code / JetBrains | ❌ | **缺失** |
| Cloud 执行 | ✅ codex cloud | ❌ | **缺失** |

---

## 三、哪个更好、更通用？

### 综合评分

| 维度 | Codex | voidx | 说明 |
|------|:-----:|:-----:|------|
| 桌面壳技术选型 | 7 | **9** | Tauri 全面优于 Electron |
| 前端工程化 | **8** | 5 | React+TS vs 原生 JS |
| Agent 内核性能 | **9** | 6 | Rust vs Python |
| Agent 内核开发效率 | 6 | **9** | Python 生态优势 |
| 协议设计 | **8** | 8 | 几乎一致 |
| 功能完整度 | **9** | 5 | Codex 功能远多于 voidx |
| 沙箱/安全 | **9** | 6 | OS 级 vs 应用级 |
| 跨平台 | 6 | **9** | Codex 缺 Linux |
| 可扩展性 | **8** | 7 | plugins/automations vs workflow |
| 分发便利性 | **8** | 4 | 单二进制 vs Python venv |

### 结论

**技术选型层面，voidx 的壳层选型（Tauri + Rust）比 Codex（Electron + Node.js）更好、更通用。**

理由：
1. **体积/内存**：Tauri 壳 ~15MB vs Electron ~1GB，idle 内存差 3-5 倍。对桌面 agent 应用这是硬指标。
2. **跨平台**：voidx 天然支持 Linux，Codex 至今没有。
3. **安全基线**：Tauri 的 capabilities + CSP 模型比 Electron fuses 更细粒度。
4. **壳语言一致性**：Tauri Main 是 Rust，与未来可能的 Rust 内核迁移天然衔接。Codex 的 Electron Main 是 Node.js，与 Rust sidecar 是割裂的。

**但 voidx 在以下方面需要补齐：**

1. **前端框架**（最高优先级）— 原生 JS 随功能增长会失控。建议迁移到 React/Vue + TypeScript，保持 Vite 构建。
2. **终端**（高优先级）— 自实现 terminal.js 功能太弱，应换 xterm.js。
3. **Worktree 支持**（高优先级）— 这是并行 agent 工作的核心基础设施。
4. **OS 级沙箱**（中优先级）— 当前 permission engine 是应用级的，应考虑集成 OS 沙箱。
5. **分发**（中优先级）— Python sidecar 需要 venv 是分发痛点。可考虑 PyInstaller/Nuitka 打包，或长期迁移内核到 Rust。
6. **v1/v2 协议清理**（中优先级）— 废弃 v1 envelope，统一到 v2 JSON-RPC。

**Codex 更好的地方**：功能完整度、OS 级沙箱、单二进制分发、前端工程化。这些是产品成熟度的体现，不是架构选型的根本缺陷——voidx 可以逐步补齐。

**一句话**：voidx 选了一条更难但更正确的路（Tauri + Python），壳层选型优于 Codex；差距在前端工程化和功能完整度，这些是工程投入问题，不是架构问题。
