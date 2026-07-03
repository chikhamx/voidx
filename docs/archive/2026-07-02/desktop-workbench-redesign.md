> **Status: Done**

# 桌面端工作台改版 — 技术设计文档

## Context

当前桌面端是深色三栏布局：

- 顶部整行 `vx-titlebar` 承载品牌、会话搜索和模型状态。
- 左侧 `vx-sidebar` 只包含 `+ New` 和 session 列表。
- 中间 `vx-main` 包含 transcript 和底部 composer。
- 右侧 `vx-dock` 以竖向 dock 承载 Todo、Terminal、Diff。
- 底部 `vx-statusbar` 只显示 session 状态。

目标参考图是浅色桌面工作台：左侧固定工作区导航，中间白色主画布，composer 是居中的圆角输入面板，终端和关键信息面板在底部横向区域展示。用户还要求 composer 支持供应商和模型切换，底部放关键面板信息。

本设计只定义桌面端 Web/Tauri 前端改版方案，不写实现代码，不改变 TUI。

## Goals

- 把桌面端从深色三栏布局改为浅色工作台布局，视觉接近参考图。
- 左侧侧栏提供一级导航和项目列表：新对话、搜索、已安排、插件、项目分组、当前项目高亮。
- 中央主区提供空状态标题：`我们应该在 voidx 中构建什么？`。
- Composer 改为居中输入面板，包含附件入口、权限模式、供应商选择、模型选择、发送按钮。
- 供应商和模型选择必须能触发已有模型切换能力，不引入未设计的后端协议耦合。
- 右侧 dock 收敛为底部横向关键面板：Todo、Terminal、Diff、状态信息。
- 保留现有 transcript、session、terminal、diff review、todo 数据流能力。
- 保持实现可分阶段落地，并能用前端测试覆盖主要行为。

## Non-Goals

- 不重写 agent runtime、LLM provider、slash command、permission engine。
- 不改 TUI 布局。
- 不引入 React/Vue 等框架，继续使用现有原生 JS 模块。
- 不把本地目录浏览器做成完整文件管理器。
- 不实现真实 IDE 文件树、编辑器标签页或完整项目切换生命周期。
- 不在本 spec 中实现代码或测试。

## Design Principles

- **保留数据流，重排界面层**：优先改 `frontend/index.html`、`frontend/styles.css` 和小型前端模块，避免大后端改动。
- **先兼容，再增强**：供应商/模型切换先复用已有 `/model switch` 命令路径；后续如需要再独立设计专用 RPC。
- **底部承载操作上下文**：Todo、Terminal、Diff、状态面板聚合到底部，减少右侧竖栏对主对话区的压迫。
- **空状态不是落地页**：空会话第一屏直接是可输入的工作台，而不是营销页。
- **测试锁定行为，不锁死像素**：测试验证 DOM 语义、交互和状态同步；视觉细节通过截图/手测验收。

## Information Architecture

### Top Bar

顶部栏应变薄，只保留窗口级操作和状态：

- 左侧：macOS 风格窗口控制点由系统窗口提供时不重复绘制；Web fallback 可保留轻量导航按钮。
- 中间：当前 workspace/project 简短标识。
- 右侧：底部面板显示切换、连接状态、当前供应商/模型摘要。

旧的全宽 session 搜索从顶部移到左侧导航中的“搜索”入口。

### Left Sidebar

左侧侧栏使用浅灰背景，宽度约 260px。结构：

```text
新对话
搜索
已安排
插件

项目
  Frameworks
  opt
  .claude
  Downloads
  ...
  voidx   ← 当前项目高亮

历史会话
  最近会话标题 + 相对时间

设置
账户
```

第一版不要求真实读取全部目录树。项目列表可以由当前 workspace 和已有 session/workspace 信息生成，缺失时显示当前 workspace basename（如 `voidx`）和少量静态入口。后续如果需要完整本地项目列表，应另起 spec 设计数据来源、权限和性能策略。

### Main Canvas

主画布为白色背景，布局分两种状态：

1. **空会话 / 无 transcript 内容**
   - 显示居中大标题：`我们应该在 voidx 中构建什么？`
   - 标题下方放 composer 面板。
   - composer 下方显示一行当前工作上下文：项目、权限模式、供应商/模型；git branch 只有在已有可靠数据源时才显示。

2. **有 transcript 内容**
   - transcript 占据主区上半部分，继续使用现有消息渲染函数。
   - composer 固定在主区底部中央，最大宽度约 960px。
   - transcript 底部留出 composer 高度，避免内容被遮挡。

### Composer

Composer 从“textarea + Send/Cancel 按钮”改为单个圆角输入面板：

```text
┌────────────────────────────────────────────┐
│ 用户输入区域                               │
├────────────────────────────────────────────┤
│ +  完全访问 ▾     Provider ▾  Model ▾   ↑ │
└────────────────────────────────────────────┘
```

控件含义：

- `+`：附件/上下文入口。第一版可以只作为按钮占位，不接入文件上传。
- 权限模式：第一版显示静态默认值 `完全访问`，或显示现有配置值；不提供切换入口，避免暗示已接入真实权限变更。
- Provider select：供应商选择，如 `openai`、`anthropic`、`deepseek`、`gemini`、自定义 provider。
- Model select：模型选择，跟随 provider 更新可选项。
- Send：圆形上箭头按钮，复用现有 submit 行为。
- Cancel：运行中状态下替换 Send 按钮位置，显示为停止按钮，复用现有 cancel 行为。

输入行为：

- Enter 提交。
- Shift+Enter 换行。
- `/` 触发 slash menu。
- 提交成功后清空输入。
- 未连接或空输入不提交。
- **运行中状态**：turn 开始（`turn.started`）后输入框 disabled、Send 按钮替换为 Cancel；turn 结束（assistant stream 完成或 cancel 结果返回）后恢复输入框和 Send 按钮。运行中不接受新提交，避免排队歧义。

### Provider / Model Switching

第一版模型切换不新增后端 RPC。前端选择 provider/model 后，通过现有 session submit 路径发送 slash command：

```text
/model switch <provider>/<model>
```

理由：

- 后端已经有 `/model switch` 能力。
- 避免把 UI spec 扩大到 profile CRUD、API key、provider catalog 的后端协议设计。
- 切换行为会自然进入现有 runtime 输出和错误处理。

前端状态：

- `startup.shown` 事件的 schema 要求同时提供 `provider`、`model`、`workspace`、`session_title`、`is_new`，前端应直接使用 `provider` 和 `model` 同步 select。
- 如果未来兼容旧事件且缺少 `provider`，才从 `model` 的 `provider/model` 字符串中解析；解析失败则 provider 显示 `custom`。
- Provider 改变时只更新 Model select 的可选项，不立即提交切换命令。
- Model 改变时提交一次 `/model switch <provider>/<model>`。如果用户只改变 provider 而未选择 model，不发送命令，避免 provider/model 连续 change 造成双提交。
- 切换 select 时不直接假设成功，先显示 pending 状态；收到后续 startup/status 或 slash 输出后再稳定显示。
- 切换失败时保留后端最终状态为准，并在 Status 面板显示最近一次切换错误或提示用户使用 `/model new`。
- **竞态窗口**：model change 触发 `/model switch` 提交后，在 switch 响应到达之前用户可能继续输入消息。该窗口期内的消息使用切换前模型。前端应在 model change 后将 composer 状态短暂置为 "switching"（如 500ms 内 disable 提交），避免用户误以为已切换。切换确认（收到后续 `startup.shown` 或 slash command 输出）后恢复可提交状态。

模型列表来源：

- 第一版使用前端内置小型 catalog 作为 UI 选项：
  - `openai`: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`
  - `anthropic`: `claude-sonnet-4-6`, `claude-opus-4-1`
  - `deepseek`: `deepseek-chat`, `deepseek-reasoner`
  - `gemini`: `gemini-3-pro`, `gemini-2.5-pro`
  - `custom`: 当前模型值
- 如果当前 provider/model 不在 catalog 中，动态追加到对应 provider 的选项中。
- 后续可新增 `model.list` RPC 作为单独增强。

### Bottom Panels

右侧 `vx-dock` 改为底部横向 `vx-bottom-panel`。面板 tabs：

- `Todo`：当前计划/任务进度。
- `Terminal`：当前 terminal 输出与输入。
- `Diff`：diff review 和应用入口。
- `Status`：连接状态、当前 session、workspace、provider/model、运行状态、权限模式。

Status 面板第一版只展示已存在前端状态，不新增后端查询：

- 连接状态：来自 `setConnectionStatus` / `uiState.connection`。
- 当前 session：来自 `workspace.snapshot.active_thread_id` 或 `session.create/session.switch` 返回值。
- Workspace：来自 `startup.shown.workspace`。
- Provider/model：来自 `startup.shown.provider` 和 `startup.shown.model`。
- 运行状态：来自 `turn.started`、assistant stream completed、cancel 结果和 tool started/completed。
- 权限模式：第一版使用静态/配置展示值；真实切换另起增强。
- Git branch：第一版如果没有现成事件，不显示真实 branch；可显示 `branch unavailable` 或隐藏该字段，避免伪造状态。

默认策略：

- 空会话时底部默认折叠为一行 status strip，避免压缩首屏。有 terminal 输出时自动展开底部面板并切换到 Terminal tab。
- 有 todo 更新时自动高亮 Todo tab，但不强制切换焦点。
- 有 terminal 输出时 Terminal tab 显示 activity dot；底部面板折叠时 activity dot 也显示在 toggle 按钮上。
- 有 diff review 时 Diff tab 显示 changed count。
- **权限模式展示**：`startup.shown` 事件携带 `profile_configured` 字段（当前 gateway adapter 未转发该字段到前端，需在 Phase 3 实现时同步补齐）。第一版展示逻辑：`profile_configured=true` 时显示"已配置"，`false` 时显示"未配置"；这样无需引入真实权限切换即可反映后端配置状态。

折叠策略：

- 右上角/底部 tab 区提供切换按钮。
- 折叠后保留一行 bottom strip，显示项目、权限、branch、provider/model、session。
- 展开高度建议 280px，允许用户后续拖拽调整；第一版可固定高度。
- 如果 branch 没有可靠来源，bottom strip 不显示 branch，或以 disabled text 标记为 unavailable。

### Session List

现有 session list 保留，但视觉上移动到左侧下半部分“历史会话”区域。

行为保持：

- 点击 session 仍调用 `session.switch`。
- 新对话仍调用 `session.create`。
- session rename/fork/delete 菜单保留。
- 搜索入口可以展开为 sidebar 内搜索框，复用 `filterSessions`。

### Responsive Behavior

桌面端目标宽度优先：

- >= 1200px：左侧栏 + 主画布 + 底部面板完整显示。
- 900px - 1199px：左侧栏可收缩为 icon rail，主 composer 最大宽度缩小。
- < 900px：左侧栏默认折叠，底部面板默认折叠，composer 保持可用。

Tauri 桌面窗口最小宽度建议不低于 900px。若当前配置允许更小窗口，应保证不出现文字重叠。

## Component Boundaries

### `frontend/index.html`

负责静态 DOM 骨架：

- `vx-shell` 切换到 `vx-workbench-shell` 风格。
- 新增 sidebar nav sections。
- 新增 empty state title 容器。
- composer 内新增 toolbar、provider/model selects、permission pill。
- 将 dock 从右侧 aside 移到底部区域。

### `frontend/styles.css`

负责视觉系统：

- 新增浅色主题 token override 或直接在 workbench class 下定义浅色变量。
- 左侧 sidebar、主画布、composer、bottom panel 样式。
- 保留既有 transcript/message/tool/diff/todo 样式兼容。
- 避免在改版中删除当前正在变更的 transcript rendering 样式，减少与 `desktop-chat-layout-refactor.md` 的冲突。

### `frontend/src/sidebar.js`

保持 session 渲染职责，新增或调整：

- 支持将 session list 渲染到“历史会话”区域。
- 支持 sidebar 内搜索框。
- 保留现有 public API：`renderSidebar`、`addThread`、`filterSessions`、事件注册函数。

### `frontend/src/dock.js`

从右侧 dock 语义改为 bottom panel 语义：

- `initDock` 继续绑定 tab。
- `switchTab` 继续按 `data-tab`/`data-pane` 切换。
- `toggleDock` 从切换宽度改为切换底部面板展开/折叠 class。
- `renderTodoInDock` 保持可用。
- 为减少测试和调用方 churn，函数名第一版可保持 `Dock` 命名；DOM/CSS 语义迁移到底部 panel。后续清理命名可单独提交。

### `frontend/src/main.js`

新增 UI 状态同步：

- 读取 provider/model select。
- 处理 `startup.shown` 时同步 provider/model、workspace、status strip。
- provider 变化只刷新 model options；model 变化时发送一次 `/model switch <provider>/<model>`。
- submit/cancel 复用现有路径。

### `frontend/src/terminal.js` and `frontend/src/diff-review.js`

不改变数据协议，只适配底部面板容器尺寸。

## Data and Protocol

第一版不新增 protocol schema。

复用事件/RPC：

- `startup.shown`: 同步 provider、model、workspace。
- `workspace.snapshot`: 渲染 session 和 transcript。
- `session.create`
- `session.switch`
- `session.submit`
- `session.cancel`
- `terminal.start`
- `terminal.input`
- `terminal.output`
- `diff.generate`
- `diff.review`
- `diff.decide`
- `diff.apply`

模型切换：

- UI 触发 `session.submit`，text 为 `/model switch <provider>/<model>`。
- 若 socket 未连接，select 不应发送命令，并应回退到原值或显示 disabled。
- 若目标 provider 没有 API key，现有 `/model switch` 会输出错误；UI 不应自行保存这个失败状态为当前模型。

后续增强候选：

- `model.list`: 返回 profiles/providers/models。
- `model.switch`: 直接切换 profile 或 provider/model。
- `permission.mode.get/set`: 支持权限模式真实切换。
- `workspace.list/switch`: 支持项目列表真实切换。

这些增强不属于第一版。

## Accessibility

- 所有 icon-only 按钮必须有 `aria-label`。
- Provider/model selects 必须有可访问 label。
- Bottom panel tabs 使用 button，保留 `active` 状态和 `aria-selected`。
- 发送按钮运行中 disabled，停止按钮有明确 label。
- 颜色不能作为唯一状态表达，activity dot 需要 title/aria-label。
- 输入框聚焦态明显。

## Testing Strategy

### Frontend Unit Tests

新增/调整 Vitest：

- `frontend/test/workbench.test.js`
  - 渲染 shell 时存在 sidebar nav：新对话、搜索、已安排、插件、项目。
  - 当前 workspace basename 显示为项目高亮；没有 workspace 时回退显示 `voidx`。
  - 空 transcript 时显示 `我们应该在 voidx 中构建什么？`。
  - Provider select 和 Model select 存在，并根据 provider 更新 model options。
  - 改变 provider 只刷新 model options，不发送 RPC。
  - 改变 model 时通过 `session.submit` 只发送一次 `/model switch <provider>/<model>`。
  - `startup.shown` 同步 provider、model、workspace 到 composer 和 Status 面板，包含 `uiState.provider` 被正确存储的断言。
  - Bottom panel tabs 包含 Todo、Terminal、Diff、Status。
  - `toggleDock` 折叠底部面板为 status strip；展开后之前选中的 tab 保持 active、内容不丢失。
  - **Model catalog 对齐**：Phase 3 实现时需验证 catalog 模型名称（`gpt-5.5`、`claude-sonnet-4-6` 等）与当前 `/model switch` 实际支持的 profile 一致。这些名称已在后端测试代码中使用，但仍建议跑一次端到端验证。

### Existing Tests

保持以下测试通过：

- `frontend/test/sidebar.test.js`
- `frontend/test/dock.test.js`
- `frontend/test/terminal.test.js`
- `frontend/test/diff-review.test.js`
- `frontend/test/main.test.js`
- `frontend/test/render.test.js`

### Visual QA

手动或浏览器截图检查：

- macOS 桌面窗口下接近参考图：浅色侧栏、白色主画布、居中 composer、底部面板。
- 1280x800：不重叠、不横向溢出。
- 1440x900：主 composer 宽度合理，底部面板不压缩主输入。
- 900px 宽：左侧栏和底部面板折叠策略可用，composer 不被遮挡。
- 长 session list 滚动正常。
- terminal 输出较长时只在 panel 内滚动。

## Rollout Plan

### Phase 1: Static Workbench Layout

- 调整 `index.html` 和 `styles.css`，完成浅色工作台、左侧 nav、主空状态、底部 panel。
- 不改变数据流。
- 测试 DOM 骨架和 tab 切换。

### Phase 2: Composer Toolbar

- 增加 provider/model selects、permission pill、context row、圆形 send/cancel。
- 保持 submit/cancel 行为。
- 测试 Enter、Shift+Enter、slash menu、运行中 disabled 状态。
- **已知中间态**：Phase 2 交付后 provider/model selects 可见但不可操作（dead controls）。建议 Phase 2 至少让 selects 以只读方式展示当前 provider/model 值，避免 UI 上有无效控件。完整切换行为由 Phase 3 接入。

### Phase 3: Model Switch Integration

- `startup.shown` 同步 provider/model 到 `uiState.provider` 和 `uiState.model`。注意当前 `main.js` 的 `startup.shown` handler（L252）仅存 `uiState.model` 而未存 `uiState.provider`，实现时需补齐。
- **补齐 `profile_configured` 转发**：gateway adapter（`adapter.py:438-448`）当前在 `startup.shown` 通知中未包含 `profile_configured` 字段，实现时需加到 forwarded payload 中，使前端 Status 面板可展示配置状态。
- provider change 刷新 model options，不提交。
- model change 发送一次 `/model switch provider/model`。
- 测试 RPC payload、未知模型动态追加、未连接时 disabled/回退。

### Phase 4: Bottom Panel Polish

- Todo/Terminal/Diff/Status panel 完整迁移到底部。
- activity dot、fold strip、状态摘要。
- 测试 `switchTab`、`toggleDock` 和内容保留。

## Risks and Mitigations

### Risk: Model select appears authoritative but only sends slash command

Mitigation:

- UI 显示 pending 状态，不在发送后立即宣称切换成功。
- 若后续收到 startup/status 同步，再更新稳定状态。
- 文案避免“已切换”，使用当前 command 状态。
- 切换失败以现有 slash command 输出为准，select 回到最后确认成功的 provider/model。

### Risk: Existing dirty changes conflict with layout work

Mitigation:

- 本 spec 要求实现时先确认 `git status`。
- 不重写 `render.js`/transcript refactor 正在调整的逻辑。
- 新增 workbench 样式尽量 scoped 到 shell/layout class。

### Risk: Static project list feels fake

Mitigation:

- 第一版只高亮当前 workspace，其他项目入口可来自后续真实数据。
- 文档明确完整项目切换不是第一版目标。

### Risk: Bottom panel reduces vertical space

Mitigation:

- 默认可折叠。
- 空状态可使用 collapsed strip。
- Terminal/Diff 内部滚动，不挤压 composer。

### Risk: Light theme breaks existing markdown contrast

Mitigation:

- 用 workbench-level color tokens 覆盖。
- 视觉 QA 必须包含 markdown、code block、diff、tool output。

### Risk: 浅色 Web 工作台 + 深色 TUI 视觉断裂

Mitigation:

- 第一版接受两套色调并存：Web/Tauri 前台用浅色 workbench，TUI 保持深色。
- Phase 1 完成后评估是否需要 TUI 端浅色主题开关；如果需要，另起 spec 设计终端主题系统。
- 桌面前端统一使用 workbench-level color tokens，后续 theme switch 只变更 token 映射。

## Acceptance Criteria

- 新桌面端第一屏视觉结构与参考图一致：浅色左侧栏、白色主画布、居中标题、居中 composer、底部面板。
- 左侧有新对话、搜索、已安排、插件、项目分组；当前 workspace basename 高亮，缺失时回退为 `voidx`。
- Composer 包含 provider/model 控件；provider change 不提交，model change 通过现有 `/model switch <provider>/<model>` 能力提交一次。
- 底部 panel 承载 Todo、Terminal、Diff、Status，能切换和折叠。
- Status 面板只展示有可靠来源的状态；branch 无可靠来源时不显示，权限模式第一版只显示静态/现有配置值且不提供切换。
- 现有 session、submit、cancel、terminal、diff review、todo 数据流不被破坏。
- 前端 focused tests 通过。
- 没有新增后端协议作为第一版硬依赖。

## Deferred Decisions

- 真实权限模式切换：另起 spec 设计 `/permission-mode` 或专用 RPC 接入。
- 真实项目列表和 workspace 切换：另起 spec 设计数据来源、权限、最近项目排序和切换生命周期。
- 右上角“切换底部面板显示”第一版只控制展开/折叠；是否同时 focus terminal 留到实现验证后决定。
- 深色主题开关不进入第一版；第一版先统一浅色 workbench，后续再设计主题系统。

## Review Log

### Review 1

Findings:

- `startup.shown` 字段描述过于保守，实际 schema 已要求 `provider` 和 `model`。
- Provider/model 两个 select 都触发提交会造成双提交风险。
- Status 面板列出 branch 和权限模式，但第一版没有可靠数据来源或真实切换能力。

Fixes:

- 明确 `startup.shown.provider/model` 是第一优先数据源。
- 改成 provider change 只刷新 options，model change 才提交一次 `/model switch`。
- 明确 Status 只展示可靠状态；branch 缺失不显示，权限模式第一版只展示。

### Review 2

Findings:

- Main Canvas 和 bottom strip 中仍把 git branch 写得像必显字段。
- Acceptance Criteria 固定写死 `voidx` 高亮，不够适配其他 workspace。
- Open Questions 中包含已经明确延后的第一版范围问题。

Fixes:

- 把 branch 改为有可靠来源才显示。
- Acceptance Criteria 改成 current workspace basename，缺失时回退 `voidx`。
- Open Questions 改为 Deferred Decisions，明确哪些内容不进第一版。

### Review 3

Findings:

- No blocking contradictions remain in the first-version scope.
- Remaining matches for `git branch` and `Open Questions` are historical review-log references or explicitly deferred decisions, not active requirements.
- Provider/model switching, bottom panel status data, project fallback behavior, and first-version non-goals now have concrete acceptance criteria.

Fixes:

- No spec changes required beyond recording this convergence review.

### Review 4

Findings:

- `main.js` 的 `startup.shown` handler 仅存储 `uiState.model`，未存储 `uiState.provider`，与 spec 要求 composer/Status 展示 provider 不一致。
- Gateway adapter 转发 `startup.shown` 时刻意丢弃了 `profile_configured` 字段，前端无法利用该字段展示配置状态。
- Catalog 模型名称（`gpt-5.5`、`claude-sonnet-4-6` 等）已在后端测试代码中实际使用，非占位符，与 `/model switch` 兼容。
- Provider/model select 的中间态（Phase 2 dead controls）未定义过渡策略。
- Composer 运行中状态、model switch 竞态窗口、底部面板默认折叠/展开策略存在歧义或未覆盖。
- 浅色 Web 工作台与深色 TUI 存在视觉断裂风险。

Fixes:

- 在 Phase 3 中明确要求 `main.js` 的 `startup.shown` handler 补齐 `uiState.provider` 存储。
- 在 Phase 3 中明确要求 gateway adapter 补齐 `profile_configured` 字段转发。
- 补充 Composer 运行中行为：输入框 disabled、Cancel 替换 Send。
- 补充 model switch 竞态窗口处理：切换后短暂 disable 提交。
- 明确底部面板默认行为：空会话折叠 strip，terminal 输出自动展开。
- 补充 Status 面板 `profile_configured` 展示逻辑。
- 补充 Phase 2 中间态建议：selects 至少只读展示当前值。
- 新增 Risk：浅色 Web + 深色 TUI 视觉断裂。
- 增强测试覆盖：provider 存储断言、toggleDock 展开内容保留。
- 补充 catalog 对齐验证说明。
