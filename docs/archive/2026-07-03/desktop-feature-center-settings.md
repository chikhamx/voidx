> **Status: Done**

# 桌面端功能中心与设置入口 — 技术设计文档

## Context

桌面端当前已经具备完整的 agent 核心能力：会话、模型切换、MCP、LSP、Skills、权限、安全沙箱、语言语气、诊断维护等。这些能力主要通过 `/` slash 命令暴露，前端只把少量高频功能做成了可视控件：Provider/Model 下拉、历史会话、底部 Todo/Terminal/Diff/Status 面板。

问题是桌面端界面上已经出现了若干入口（例如“插件”“已安排”“设置”“账户”“+ 附件”），但部分入口还只是占位，用户无法通过图形界面完成配置。随着功能增长，继续依赖用户记忆 slash 命令会降低桌面端可用性。

本设计把已有 slash 能力整理为桌面端的信息架构，并定义第一版 MVP：把高频配置和集成功能做成可点击、可配置、可测试的 UI，同时保留 slash 命令作为高级入口。

## Goals and Non-Goals

### Goals

- 梳理现有 slash 命令能力，并映射到桌面端功能分类。
- 将左侧“插件”、底部“设置/账户”、输入框 `/`、Composer `+` 等占位入口定义为真实功能入口。
- 新增桌面端可用的设置中心，支持用户手动配置模型、权限、语言语气、IDE、更新检查等。
- 新增插件/集成管理页，支持 MCP、Tavily、Skills 的列表、启用禁用、测试与基础配置。
- 新增命令面板，完整展示 `/` 命令，并支持点击填入或执行。
- 优先复用已有后端能力和配置模型，避免重写 agent runtime。

### Non-Goals

- 不引入 React/Vue，继续使用现有原生 JS + Vite 架构。
- 不重写 slash command handler；第一版只新增结构化 RPC 包装常用配置能力。
- 不实现完整账号体系；“账户”第一版作为 API Keys / Provider 配置入口。
- 不实现复杂文件管理器；Composer `+` 第一版只承载图片粘贴和上下文入口。
- 不改变 TUI 端命令体验。

## Existing Feature Map

### 会话与上下文

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| 新会话 | `/clear`, `/session new` | 左侧“新对话”已接 `session.create` | 保留 |
| 历史会话 | `/list`, `/session list`, `/resume`, `/session resume` | 左侧历史会话列表已实现 | 增加空状态/加载态 |
| 删除会话 | `/session del`, `/session del --dry-run` | 会话菜单 Delete 已接 RPC | 增加批量清理入口 |
| 重命名 | `/title`, `/title auto` | 会话菜单 Rename 已实现 | 保留 |
| 回滚 | `/rollback` | 仅 slash | 放到命令面板/危险操作区 |
| 压缩上下文 | `/compact` | 仅 slash | 设置或命令面板 |

### 模型与 Provider

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| 切换模型 | `/model switch`, `/model <provider/model>` | Composer Provider/Model 下拉已走 `/model switch` | 保留并增强状态反馈 |
| 配置模型 | `/model new` | 仅 slash | 设置中心 > 模型配置 |
| 列出模型 | `/model list` | 仅 slash | 设置中心 > 模型列表 |
| 测试连接 | `/model test` | 仅 slash | 模型配置页按钮 |
| 删除 profile | `/model del` | 仅 slash | 模型列表行操作 |
| Reasoning | `/model reasoning` | 仅 slash | 模型配置页下拉 |
| Context window | `/model ctx` | 仅 slash | 模型配置页数字输入 |

### 权限与安全

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| 权限预设 | `/permission-mode` | Composer 只展示权限文案 | 设置中心 > 权限模式 |
| Sandbox | `/sandbox` | 仅 slash | 设置中心 > 沙箱模式 |
| Approval | `/approval` | 仅 slash | 设置中心 > 审批策略 |
| 允许/拒绝工具 | `/allow`, `/deny`, `/permissions` | 仅 slash | 高级权限规则页 |
| Plan mode | `/plan`, `/unplan`, `/mode plan` | 仅 slash | 模式切换控件/命令面板 |

### 插件与工具集成

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| MCP 管理 | `/mcp list/new/test/tools/enable/disable/restart/del` | 左侧“插件”占位 | 插件页核心功能 |
| Tavily | `/tavily set/show/delete` | 仅 slash | 插件页 > Web Search |
| Skills | `/skills list/show/enable/disable/auto/manual/paths` | 仅 slash | 插件页 > Skills |
| Web tool route | MCP new 自动写 web route | 无 UI | 插件页显示 search/fetch 路由 |

### 代码能力

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| LSP 状态 | `/lsp status` | notice/toast 可见 LSP 状态，但无页 | 设置/插件页 > Language Servers |
| LSP doctor | `/lsp doctor` | 仅 slash | LSP 页按钮 |
| LSP restart | `/lsp restart` | 仅 slash | LSP 页行操作 |
| IDE 选择 | `/code-ide`, `/code-ide status` | 仅 slash | 设置中心 > IDE |
| Diff review | `/diff` + bottom Diff | 底部 Diff 已实现 generate/review/apply | 保留 |
| Terminal | bottom Terminal | 已实现启动和输入 | 保留 |

### 用户偏好与维护

| 能力 | Slash 命令 | 当前桌面状态 | 建议 UI |
|---|---|---|---|
| 语言 | `/lang` | 仅 slash | 设置中心 > 偏好 |
| 语气 | `/tone` | 仅 slash | 设置中心 > 偏好 |
| 并行子代理 | `/parallel on/off/status` | 仅 slash | 设置中心 > Agent |
| 更新检查 | `/upgrade on/off/status/check/now` | 仅 slash | 设置中心 > 更新 |
| 调试日志 | `/debug`, `/log` | 仅 slash | 设置中心 > 高级 |
| 用量 | `/usage` | 仅 slash | Status 面板或命令面板 |

## Information Architecture

```text
左侧 Sidebar
├── 新对话              → session.create
├── 搜索                → session list local filter
├── 任务                → Todo / running sessions / scheduled placeholder
├── 插件                → IntegrationsPanel
├── 项目                → 当前 workspace + future project list
├── 历史会话            → sessions
├── 设置                → SettingsModal
└── 账户                → Provider/API Keys settings

主区 Main
├── Transcript
├── CommandPalette       → `/` 触发，也可按钮打开
└── Composer
    ├── + 上下文          → paste image / attach context
    ├── 权限 pill         → opens permission settings
    ├── Provider select   → model switch
    ├── Model select      → model switch
    └── Send/Cancel

底部 Dock
├── Todo
├── Terminal
├── Diff
└── Status
```

## Architecture

### Frontend Modules

```text
frontend/src/main.js
├── 保留 websocket lifecycle、handleNotification、composer submit
├── 接入新面板打开/关闭事件
└── 调用 settings / commands / integrations RPC

frontend/src/slash.js
├── 从静态小列表扩展为完整命令 catalog
├── 支持分类、搜索、点击填入/执行
└── 保持键盘上下选择

frontend/src/settings.js        新增
├── renderSettingsModal(snapshot)
├── bind model / permission / profile / IDE / update controls
└── submit settings.update RPC

frontend/src/integrations.js    新增
├── renderMcpServers
├── renderTavilyStatus
├── renderSkills
└── bind enable/disable/test/delete/add actions

frontend/src/context-menu.js    新增或并入 main
├── renderComposerContextMenu
└── /paste 与后续文件上下文入口
```

### Backend RPC Layer

现有桌面通信已经走 JSON-RPC over WebSocket。MVP 新增一层结构化 RPC，不替换 slash：

```text
Frontend settings UI
  ├── settings.get
  ├── settings.update
  ├── commands.list
  ├── commands.run
  ├── mcp.list / mcp.setDisabled / mcp.delete / mcp.test
  ├── skills.list / skills.setEnabled / skills.setAuto
  └── lsp.status / lsp.doctor / lsp.restart
        │
        ▼
GatewaySession MethodDispatch
        │
        ▼
Settings / SkillService / MCP manager / LSP manager / SlashHandler
```

原则：

- **读状态走专用 RPC**：UI 不解析 slash 输出。
- **简单执行可走 slash**：例如 `/compact`、`/usage`、`/rollback` 可通过 `commands.run` 提交。
- **配置写入走 Settings API**：模型、权限、语言语气、IDE、更新检查、Skills enable/auto 等必须写入结构化配置。
- **危险操作确认**：删除 MCP、删除 session、rollback、full-access 权限切换必须弹确认。

## Data Model

### CommandCatalogItem

```text
CommandCatalogItem
├── command: string
├── description: string
├── category: "session" | "model" | "permission" | "integrations" | "code" | "preference" | "maintenance"
├── execution: "fill" | "run" | "open-ui"
├── dangerous: boolean
├── requiresArgs: boolean
└── uiTarget?: string
```

元数据来源与生成规则：

- 后端新增 `src/voidx/ui/command_catalog.py`，以 `src/voidx/ui/commands.py::COMMANDS` 的 command/description 为基础源，并叠加显式 metadata map。
- `COMMANDS` 继续作为 slash help 的轻量列表；`command_catalog.py` 负责为桌面端补齐 `category`、`execution`、`dangerous`、`requiresArgs`、`uiTarget`。
- 分类规则按命令前缀确定默认值：`/session`、`/clear`、`/list`、`/resume`、`/title` 归 `session`；`/model` 归 `model`；`/permission-mode`、`/sandbox`、`/approval`、`/allow`、`/deny`、`/permissions`、`/mode`、`/plan`、`/unplan` 归 `permission`；`/mcp`、`/tavily`、`/skills` 归 `integrations`；`/lsp`、`/code-ide`、`/diff`、`/paste` 归 `code`；`/lang`、`/tone`、`/parallel` 归 `preference`；`/compact`、`/debug`、`/log`、`/usage`、`/upgrade`、`/rollback` 归 `maintenance`。
- `execution=fill` 用于需要参数或需要用户确认上下文的命令，例如 `/model switch`、`/mcp new`、`/allow`。
- `execution=run` 仅用于无参数、可直接执行的命令，例如 `/usage`、`/lsp status`、`/parallel status`。
- `execution=open-ui` 用于已有图形入口的配置类命令，例如 `/model new` → `settings:model`、`/permission-mode` → `settings:permissions`、`/mcp list` → `integrations:mcp`、`/skills list` → `integrations:skills`。
- `dangerous=true` 至少覆盖 `/rollback`、`/clear`、`/session del`、`/mcp del`、`/model del`、`/permission-mode full-access`、`/approval never`；前端必须二次确认。
- `commands.list` 返回 catalog 前必须校验每个 `COMMANDS` 条目都有 metadata；缺 metadata 时测试失败，避免前后端静态列表漂移。

### DesktopSettingsSnapshot

```text
DesktopSettingsSnapshot
├── model
│   ├── provider: string
│   ├── model: string
│   ├── base_url?: string
│   ├── protocol?: string
│   ├── reasoning_effort?: string
│   └── context_window?: number
├── profiles: ProfileSummary[]
├── permissions
│   ├── permission_mode: string
│   ├── sandbox_mode: string
│   ├── approval_policy: string
│   ├── approval_reviewer: string
│   └── sandbox_workspace_write: string[]
├── user_profile
│   ├── language: string
│   └── tone: string
├── code_ide: string
├── update_check
│   ├── enabled: boolean
│   ├── last_checked_at?: number
│   └── latest_version?: string
├── parallel_subagents
│   ├── enabled: boolean
│   └── max_concurrent: number
└── paths
    ├── workspace_settings: string
    ├── global_settings: string
    └── skills_state: string
```

### IntegrationSnapshot

```text
IntegrationSnapshot
├── mcp_servers: McpServerSummary[]
├── web_routes
│   ├── search: WebToolRoute
│   └── fetch: WebToolRoute
├── tavily
│   ├── configured: boolean
│   └── source: "env" | "settings" | "none"
├── skills: SkillSummary[]
└── lsp: LspStatus[]
```

## API Contract

### commands.list

- **Method**: JSON-RPC request
- **Signature**: `commands.list(params: {})`
- **Response**:

```json
{
  "commands": [
    {
      "command": "/model switch",
      "description": "Switch to a configured provider",
      "category": "model",
      "execution": "fill",
      "dangerous": false
    }
  ]
}
```

### commands.run

- **Method**: JSON-RPC request
- **Signature**: `commands.run(params: { text: string, mode?: "validate" | "submit" })`
- **Default mode**: `submit`.
- **Behavior**:
  - `validate` 只解析 command catalog，不触发 agent turn 或配置写入。
  - `submit` 先用 command catalog 做同步校验；通过后再转交 SlashHandler dispatch 或现有 `session.submit` 路径。
  - `execution=fill` 或 `requiresArgs=true` 的命令如果缺少参数，必须返回 `command requires arguments`，不得提交。
  - `execution=open-ui` 不走 SlashHandler，返回 `{ "ok": true, "action": "open-ui", "uiTarget": "..." }`。
  - `dangerous=true` 且未传前端确认标记时返回 `confirmation required`。
- **Response**:
  - 可直接执行并已提交：`{ "ok": true, "status": "submitted" }`
  - 打开 UI：`{ "ok": true, "action": "open-ui", "uiTarget": "settings:model" }`
  - 仅校验：`{ "ok": true, "status": "valid", "item": CommandCatalogItem }`
- **Errors**:
  - `invalid command`：不在 command catalog 中。
  - `command requires arguments`：命令需要参数或只能 fill。
  - `confirmation required`：危险命令缺少确认。
  - `runtime busy`：当前 turn 正在运行且命令不能并发。
  - `command failed`：SlashHandler 同步执行失败。

实现约束：`session.submit` 当前只能表示“文本已提交”，不能表达 slash 执行结果；因此 `commands.run` 不能简单等价转发 `session.submit`。MVP 要么调用 SlashHandler 的结构化 dispatch，要么在提交前完成 catalog 级同步校验，并把后续运行失败作为 transcript/event 呈现。

### settings.get

- **Method**: JSON-RPC request
- **Signature**: `settings.get(params: {})`
- **Response**: `DesktopSettingsSnapshot`

### settings.update

- **Method**: JSON-RPC request
- **Signature**: `settings.update(params: { patch: object, scope?: "global" | "workspace" })`
- **Default scope**: `global` for provider/API keys and user preferences; `workspace` only when the user explicitly selects workspace-level override.
- **Response**: `{ "ok": true, "settings": DesktopSettingsSnapshot }`
- **Errors**: invalid enum, invalid context window, invalid scope, secret write failure, write failure.

Supported MVP patch keys:

```json
{
  "model": {
    "provider": "openai",
    "model": "gpt-5.5",
    "base_url": "https://api.openai.com/v1",
    "protocol": "openai",
    "reasoning_effort": "high",
    "context_window": 200000
  },
  "provider_secrets": {
    "provider": "openai",
    "api_key": "sk-...",
    "action": "set"
  },
  "permissions": {
    "permission_mode": "default",
    "sandbox_mode": "workspace-write",
    "approval_policy": "untrusted"
  },
  "user_profile": {
    "language": "zh-CN",
    "tone": "concise"
  },
  "code_ide": "cursor",
  "update_check": { "enabled": true },
  "parallel_subagents": { "enabled": true, "max_concurrent": 4 }
}
```

API Key 安全合约：

- `settings.get` 和 `integrations.get` 永远不返回明文 secret，只返回 `configured: boolean`、`source`、`masked_value`（例如 `sk-...abcd`）和 `scope`。
- `provider_secrets.action=set` 写入用户选择的 scope；默认写 global settings，避免把个人 key 意外提交到 workspace。
- `provider_secrets.action=delete` 只删除指定 provider/scope 的本地配置，不删除环境变量。
- 读取优先级：显式 workspace 设置 > global 设置 > 环境变量；响应中必须标注最终生效的 `source`。
- 如果环境变量已配置，UI 可以显示 `source=env`，但不能覆盖、读取或删除该环境变量；用户保存本地 key 时会成为更高优先级配置。
- 后端日志、toast、错误 detail、JSON-RPC 响应都必须 redacted secret；测试覆盖 secret 不出现在响应和日志中。

### integrations.get

- **Method**: JSON-RPC request
- **Signature**: `integrations.get(params: {})`
- **Response**: `IntegrationSnapshot`
- **Errors**: MCP manager unavailable, skills registry unavailable, LSP manager unavailable. Partial failures should return available sections plus `warnings: IntegrationWarning[]` instead of failing the whole panel.

### mcp.list / mcp.test / mcp.tools / mcp.restart

- **Method**: JSON-RPC request
- **Signature**:
  - `mcp.list(params: {})`
  - `mcp.test(params: { name: string })`
  - `mcp.tools(params: { name: string })`
  - `mcp.restart(params: { name: string })`
- **Response**:
  - `mcp.list`: `{ "servers": McpServerSummary[] }`
  - `mcp.test`: `{ "ok": true, "server": McpServerSummary, "message": string }`
  - `mcp.tools`: `{ "tools": McpToolSummary[] }`
  - `mcp.restart`: `{ "ok": true, "server": McpServerSummary }`
- **Errors**: server not found, disabled server, test failed, restart failed, tool listing failed.

### mcp.setDisabled

- **Method**: JSON-RPC request
- **Signature**: `mcp.setDisabled(params: { name: string, disabled: boolean })`
- **Response**: `{ "ok": true, "server": McpServerSummary }`
- **Errors**: server not found, write failure, restart required.

### mcp.delete

- **Method**: JSON-RPC request
- **Signature**: `mcp.delete(params: { name: string, confirmed: boolean })`
- **Response**: `{ "ok": true }`
- **Errors**: server not found, confirmation required, write failure.

### skills.list / skills.show / skills.setEnabled / skills.setAuto

- **Method**: JSON-RPC request
- **Signature**:
  - `skills.list(params: {})`
  - `skills.show(params: { name: string })`
  - `skills.setEnabled(params: { name: string, enabled: boolean })`
  - `skills.setAuto(params: { name: string, auto: boolean })`
- **Response**:
  - `skills.list`: `{ "skills": SkillSummary[] }`
  - `skills.show`: `{ "skill": SkillDetail }`
  - `skills.setEnabled` / `skills.setAuto`: `{ "ok": true, "skills": SkillSummary[] }`
- **Errors**: skill not found, invalid state transition, write failure.

### lsp.status / lsp.doctor / lsp.restart

- **Method**: JSON-RPC request
- **Signature**:
  - `lsp.status(params: {})`
  - `lsp.doctor(params: { server?: string })`
  - `lsp.restart(params: { server?: string })`
- **Response**:
  - `lsp.status`: `{ "servers": LspStatus[] }`
  - `lsp.doctor`: `{ "ok": boolean, "checks": LspDoctorCheck[] }`
  - `lsp.restart`: `{ "ok": true, "servers": LspStatus[] }`
- **Errors**: server not found, restart failed, doctor unavailable.

### tavily.set / tavily.delete

- **Method**: JSON-RPC request
- **Signature**:
  - `tavily.set(params: { api_key: string, scope?: "global" | "workspace" })`
  - `tavily.delete(params: { scope?: "global" | "workspace" })`
- **Response**: `{ "ok": true, "tavily": { "configured": boolean, "source": "env" | "settings" | "none", "scope"?: string, "masked_value"?: string } }`
- **Errors**: invalid scope, secret write failure, secret delete failure.

RPC 命名约束：`integrations.get` 用于首屏聚合读取；行级按钮调用具体 RPC，避免 UI 从 slash 文本输出中解析状态。所有 delete/restart/full-access 类操作都必须支持前端确认和可恢复错误提示。
## UI Design

### Command Palette

触发方式：

- 输入框键入 `/`。
- 可选：顶部或 composer 添加 “⌘K” 命令按钮。

行为：

- 支持分类展示：会话、模型、权限、插件、代码、偏好、维护。
- 支持模糊搜索 command 和 description。
- Enter 行为：
  - `execution=fill`：填入输入框，让用户补参数。
  - `execution=run`：直接执行，需要危险确认时先弹窗。
  - `execution=open-ui`：打开对应设置页，例如 `/model new` 打开模型配置。

### Settings Modal

Tab：

1. **模型**
   - 当前 provider/model。
   - 已保存 profiles 列表。
   - API Key 输入（默认 masked）。
   - Base URL、Protocol、Reasoning、Context window。
   - Test connection 按钮。

2. **权限**
   - Permission mode preset。
   - Sandbox mode。
   - Approval policy。
   - Extra writable paths。
   - Current rules 只读预览。

3. **偏好**
   - Language。
   - Tone。
   - Parallel subagents enabled/max concurrent。

4. **IDE / 代码**
   - Code IDE preference。
   - LSP status/doctor/restart。

5. **更新与高级**
   - Update check enabled。
   - Debug/log toggles。
   - Compact context。
   - Usage summary。

### Integrations Panel

由左侧“插件”打开。

Tab：

1. **MCP Servers**
   - 列表：name、transport、disabled、tool count。
   - 操作：enable/disable、test、tools、restart、delete。
   - Add server：第一版支持 Tavily、voidx-web、URL、自定义命令四种。

2. **Web Search**
   - Tavily key status。
   - Search/fetch route 指向哪个 MCP server/tool。
   - Set/Delete API key。

3. **Skills**
   - 列表：name、scope、enabled、auto、description。
   - 操作：enable/disable、auto/manual、show。
   - Paths 展示 global/project skill locations。

4. **Language Servers**
   - LSP status。
   - Doctor。
   - Restart server。

### Account / Providers

“账户”第一版不做登录系统，改为 Provider/API Key 管理入口：

- 已配置 provider profiles。
- API key masked 输入。
- 添加/删除 profile。
- Test connection。
- 说明 API key 存储位置：global `.voidx/settings.json` 或 workspace `.voidx/settings.json`。

如后续加入云账号，再把 Account 拆为登录态和本地 Provider 配置两部分。

### Composer `+`

第一版下拉菜单：

- Paste image from clipboard → 执行 `/paste`。
- Add file/folder context → 暂时 disabled，显示 “coming soon”。
- Add web context → 可跳转插件页配置 Tavily/MCP web search。

## Error Handling

| 失败场景 | 处理策略 |
|---|---|
| settings.update 写入失败 | Modal 顶部显示错误 toast，不关闭窗口 |
| enum 值非法 | 前端限制选项；后端返回 validation error |
| API Key 测试失败 | 保留输入，显示失败原因，不保存或询问是否仍保存 |
| MCP server 测试失败 | 不自动启用，显示 stderr/错误摘要 |
| 删除 MCP/Session/Rollback | 必须二次确认 |
| WebSocket 未连接 | 禁用设置保存按钮，显示 disconnected |
| Slash 命令需要参数 | Command Palette 填入输入框，不直接执行 |
| 运行中切换模型 | 禁用模型切换，显示 switching/running 状态 |

## Implementation Plan

### Phase 1 — Command Catalog and Palette

- 扩展 `frontend/src/slash.js`，覆盖 `COMMANDS` 全量分类。
- 添加 `CommandPalette` 行为：搜索、分类、点击填入/执行。
- 保持现有 `/` 键盘交互不回退。
- 测试：`frontend/test/slash.test.js`、`frontend/test/main.test.js`。

### Phase 2 — Settings RPC and Modal

- 后端 `GatewaySession` 注册 `settings.get` / `settings.update`。
- 前端新增 `settings.js`，实现 Settings Modal。
- 将 Composer 权限 pill、底部“设置”、Provider/API key 入口接入 modal。
- 测试：gateway method tests + frontend settings tests。

### Phase 3 — Integrations Panel

- 后端注册 `integrations.get`、`mcp.*`、`skills.*`、`lsp.*` 结构化 RPC。
- 前端新增 `integrations.js`，接入左侧“插件”。
- Tavily、Skills、MCP enable/disable/test/delete 第一版可用。
- 测试：gateway integration tests + frontend integrations tests。

### Phase 4 — Account/API Keys and Composer Context

- “账户”接入 Provider/API Key 管理。
- Composer `+` 接 `/paste`，预留 file/folder context。
- 增加 tooltip 和 empty-state guidance。
- 测试：frontend workbench tests。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|---|---|---|
| 保留 slash，新增结构化 RPC | 全部通过 slash 输出解析 | UI 需要可靠状态，不应解析终端文本 |
| 账户先做 API Keys / Providers | 设计云账号体系 | 当前没有账号后端，先解决本地配置需求 |
| 插件页合并 MCP/Tavily/Skills/LSP | 分散到多个入口 | 用户心智上都属于扩展能力 |
| 命令面板支持 open-ui | 只填入文本 | 高频配置应直接打开图形界面 |
| 使用原生 JS | 引入 React/TS | 当前项目架构轻量，MVP 不扩大技术栈 |

## Open Questions

- [x] 模型/API Key 默认写 global settings，并允许用户显式选择 workspace override。
- [ ] MCP “Custom command” 表单是否需要支持 env/header 的高级编辑器？
- [ ] Skills 是否需要支持从 UI 创建 SKILL.md，还是第一版只管理已有 skills？
- [ ] “已安排”是否保留为未来 scheduled tasks，还是改名为“任务”？
- [x] LSP 第一版支持 status/doctor/restart，不支持从 UI 启用/禁用具体语言服务器。

## Reader Test

一个第一次阅读本文档的人应该能理解：桌面端已有能力主要藏在 slash 命令里；第一版要把命令分成会话、模型、权限、插件、代码、偏好、维护几类；左侧“插件”、底部“设置/账户”、输入框 `/`、Composer `+` 分别要接入哪些真实功能；后端需要新增哪些结构化 RPC；以及 MVP 应该按命令面板、设置弹窗、插件页、账户/上下文入口四个阶段实现。
