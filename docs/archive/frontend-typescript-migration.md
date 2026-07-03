# frontend TypeScript 迁移 — 技术设计文档

> **Status: Done** | 2026-07-03

## Context

`frontend/` 目前有 13 个 Vanilla JS 源文件 + 1 个 `.d.ts`（`protocol.d.ts`），通过 Vite 构建。模块间存在交叉依赖，RPC 通信有自动生成的类型定义（`protocol.d.ts`），但实际代码中类型未被强制执行。随着功能增长，DOM 操作的空值问题、回调签名的隐式 any、重构时的遗漏风险正在累积。

## Goals and Non-Goals

### Goals

- 将 `frontend/src/` 下 13 个 `.js` 文件迁移为 `.ts`
- 将 `frontend/test/` 下 13 个 `.js` 测试文件 + `setup.js` 迁移为 `.ts`
- 配置文件 `vite.config.js` → `vite.config.ts`，`test/setup.js` → `test/setup.ts`
- 保持现有构建流程不变（`npm run dev`、`npm run build`、`npm test`）
- 渐进迁移：允许中间态混用 JS/TS

### Non-Goals

- 不迁移 `npm/bin/voidx.js` 和 `npm/bin/postinstall.js`（Node bootstrap 脚本，零构建依赖）
- 不改变 `desktop/`（Tauri，无前端 JS）
- 不引入 React/Vue 等框架
- 不修改 `index.html` 的 DOM 骨架结构
- 不改变现有运行时的行为

## Architecture

```
frontend/
├── tsconfig.json              ← 新建：编译器配置
├── vite.config.ts             ← 从 .js 改名，内容基本不变
├── index.html                 ← 改 <script> 引用路径
├── package.json               ← 不改（依赖无变化）
├── src/
│   ├── main.ts                ← 入口，改名，import 路径去 .js 后缀
│   ├── protocol.d.ts          ← 不变（自动生成）
│   ├── types.ts               ← 新建：前端内部类型定义
│   ├── rpc.ts                 ← 改名 + 标注 WebSocket、回调类型
│   ├── stream.ts              ← 改名
│   ├── markdown.ts            ← 改名
│   ├── render.ts              ← 改名
│   ├── dock.ts                ← 改名
│   ├── slash.ts               ← 改名
│   ├── sidebar.ts             ← 改名
│   ├── terminal.ts            ← 改名
│   ├── diff-review.ts         ← 改名
│   ├── settings.ts            ← 改名
│   ├── integrations.ts        ← 改名
│   └── context-menu.ts       ← 改名
└── test/
    ├── setup.ts               ← 改名
    ├── main.test.ts           ← 改名（共 13 个测试文件）
    ├── render.test.ts
    ├── ...
    └── workbench.test.ts
```

### 模块依赖图（迁移后不变）

```
main.ts ────────┬── render.ts ──────┬── markdown.ts
                │                   └── stream.ts
                ├── slash.ts
                ├── stream.ts ────── markdown.ts
                ├── rpc.ts          (独立)
                ├── sidebar.ts
                ├── dock.ts ──────── render.ts
                ├── terminal.ts
                ├── diff-review.ts
                ├── settings.ts
                ├── integrations.ts ──── rpc.ts
                └── context-menu.ts   (独立)
```

## Data Model

### 新建 `frontend/src/types.ts` — 前端内部类型

```
types.ts
├── TranscriptNode         (from protocol.d.ts — Item)
├── StreamState            { text: string; thinking: string; phase: string; el: HTMLElement; thinkingEl: HTMLDetailsElement; thinkingSummary: HTMLElement; thinkingBody: HTMLElement; textEl: HTMLElement; debounceTimer: number | null; committed?: boolean }
├── RpcPending             { resolve: (value: unknown) => void; reject: (reason: Error) => void }
├── SlashCommand           { command: string; description: string; category: string; execution: string; ... }
├── SessionThread          { id: string; title: string; status: string; ... }
├── DiffFile               { path: string; hunks: DiffHunk[] }
├── DiffHunk               { header: string; lines: DiffLine[] }
├── SettingsState          { dialog: HTMLDialogElement | null; content: HTMLElement | null; ... }
├── TerminalCallbacks      { onInput: Function | null; onStart: Function | null }
├── SidebarCallbacks       { onThreadSelect: Function | null; onNewThread: Function | null; ... }
└── DockTab                "todo" | "terminal" | "diff" | "status"
```

### `protocol.d.ts`（保持不变）

由 `npm run schema` 从 `protocol.schema.json` 自动生成，定义了 `VoidxUiProtocol`、`JsonRpcRequest`、`Item`、`ThreadInfo` 等 RPC 层类型。迁移时直接 import 使用。

### `tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "strict": true,
    "noEmit": true,
    "allowJs": true,
    "checkJs": false,
    "isolatedModules": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true
  },
  "include": ["src/**/*.ts", "test/**/*.ts", "src/**/*.d.ts"],
  "exclude": ["dist"]
}
```

关键决策：
- `allowJs: true` + `checkJs: false` — 允许 JS/TS 混用，不对旧 JS 文件报错（渐进迁移）
- `strict: true` — 新写的 TS 文件全量严格检查
- `moduleResolution: "bundler"` — 匹配 Vite 的模块解析

## API Contract

### 受影响的文件引用

| 位置 | 变更 |
|------|------|
| `index.html` | `<script type="module" src="./src/main.js">` → `./src/main.ts` |
| `vite.config.ts` | `setupFiles: ["./test/setup.js"]` → `./test/setup.ts` |
| 所有 `import ... from "./xxx.js"` | 去掉 `.js` 后缀，改为 `"./xxx"` |
| `AGENTS.md` | 更新测试示例 `.js` → `.ts` |

### 不改的对外接口

- `npm run dev` / `npm run build` / `npm test` / `npm run schema` 命令签名不变
- 构建产物 `frontend/dist/` 结构不变
- WebSocket RPC 协议不变

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `document.querySelector` 返回 null | TS `strict` 下强制判空，添加 early return 或 `!` 断言 |
| 第三方库类型缺失（marked, DOMPurify, hljs） | `npm i -D @types/marked @types/dompurify`（hljs 自带类型） |
| 动态属性访问（`el.dataset.xxx`） | DOM 类型已覆盖，无需额外处理 |
| `protocol.d.ts` 类型与运行时不符 | schema 生成工具保证一致性；手动接口加 `as` 断言并注释 |
| 回调函数签名不匹配 | 定义 `types.ts` 中的回调类型，统一约束 |

## Migration Strategy

### 阶段 1：基础设施（不影响运行业务）

1. 安装 `@types/marked`、`@types/dompurify`
2. 新建 `tsconfig.json`
3. `vite.config.js` → `vite.config.ts`（内容不变）
4. `test/setup.js` → `test/setup.ts`（内容不变）
5. 改 `index.html` 入口引用
6. 验证 `npm run dev` / `npm test` 通过

### 阶段 2：核心模块迁移（自底向上，按依赖关系）

按依赖深度逐层迁移，每层迁移后跑测试：

1. `rpc.js` → `rpc.ts`（无依赖，最底层）
2. `context-menu.js` → `context-menu.ts`（无依赖）
3. `markdown.js` → `markdown.ts`（仅依赖第三方库）
4. `types.ts`（新建，无运行时代码）
5. `stream.js` → `stream.ts`
6. `slash.js` → `slash.ts`
7. `render.js` → `render.ts`
8. `terminal.js` → `terminal.ts`
9. `dock.js` → `dock.ts`
10. `sidebar.js` → `sidebar.ts`
11. `diff-review.js` → `diff-review.ts`
12. `settings.js` → `settings.ts`
13. `integrations.js` → `integrations.ts`
14. `main.js` → `main.ts`（最后，依赖所有模块）

### 阶段 3：测试迁移

源文件全部迁移完成后，批量将测试文件 `.js` → `.ts`。测试文件中 import 的路径同步修改。

### 阶段 4：收尾

1. `allowJs: false` + 删除 `checkJs`（禁止新 JS 文件）
2. 更新 `AGENTS.md` 中关于前端测试 `*.test.js` → `*.test.ts` 的描述
3. 更新 `frontend/package.json` 的 `schema` 脚本路径引用（如涉及）

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 渐进迁移（allowJs + checkJs=false） | 一次性全部迁移 | 降低风险，每步可验证，不阻塞并行开发 |
| 保持 Vanilla JS 架构 | 同时引入 React/Vue | 非本次目标，引入框架是独立的架构决策 |
| npm/bin 不迁移 | 全项目 TS | Bootstrap 脚本无构建步骤，引入 TS 增加启动失败风险且无收益 |
| import 去 `.js` 后缀 | 保留 `.js` 后缀（Vite 支持） | TS 官方推荐不加扩展名，IDE 支持更好，更简洁 |
| `types.ts` 集中管理内部类型 | 各模块内联 interface | 避免循环依赖，类型复用方便 |
| Vite 的 `moduleResolution: "bundler"` | `"node"` | Vite 实际使用 bundler 解析，保持一致 |

## Open Questions

- [ ] 是否需要 `eslint` + `@typescript-eslint`？建议阶段 4 后再评估
- [ ] `protocol.d.ts` 生成的类型名如 `Id`、`Id1`、`Id2` 不够语义化——是否在迁移后提 PR 改进 `json-schema-to-typescript` 的输出？
