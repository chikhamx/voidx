# 桌面端 UI 现代化与后端功能对齐设计

> **Status: Done** — Archived on 2026-08-11.

日期：2026-08-10 · 状态：已批准方向，待实施
范围：`frontend/`（desktop/ 为 Tauri 壳，完全复用 frontend/dist，无独立 UI）

## 1. 背景与目标

后端 `src/voidx` 已具备四种运行模式（chat/coding/goal/loop）、上下文压缩（compact）、
AI 审批、按用途独立配置模型等能力，但前端未消费其中多项协议字段，用户无法感知或配置。
同时界面需要一次系统性的视觉升级：干净、克制、现代，暗色为一等公民。

目标（P0 本期）：

1. 四种模式在 UI 可见、可切换（goal/loop 深度流程面板属 P1）。
2. 模型配置中心：主对话 / 上下文压缩 / AI 审批三组按用途配置（model + reasoning effort）。
3. Compact 在聊天流中可见，不再"静默丢消息"。
4. 视觉基座重构：tokens 语义层、暗色默认、动效与留白规范化。
5. 状态栏整合：模式 / 模型 / reasoning / 权限 / write-lock / AI 审批计数。

非目标（本期不做）：goal/loop 三段式深度面板、main.ts 拆分、通知类型化注册表（P1/P2）。

## 2. 已核实的功能差距

证据均来自本期代码盘点（文件:行号）：

| 能力 | 后端事实 | 前端现状 |
|------|---------|---------|
| 运行模式 | `ThreadInfo.runtime_profile` 已在协议中（`src/voidx/presentation/protocol/v2/threads.py:28`；生成类型 `frontend/src/rpc/protocol.d.ts:233`） | 零消费：无徽章、无切换器 |
| 模式切换 | slash `/goal`、`/loop`（`slash/commands/mode.py`） | slash 列表仅有 `/compact` 等，无模式引导 |
| Compact 配置 | `CompactionConfig(profile_name, reasoning_effort, timeout_seconds)`（`src/voidx/config/models.py:22`） | **协议快照未暴露 compaction 字段**（protocol.d.ts 无），需确认 `settings.set` 透传或补协议 |
| Compact 呈现 | TUI 侧有 `active_compaction_text`（`presentation/output/dock/status.py:49`）；自动压缩由 context pressure 触发 | 聊天流无任何提示 |
| AI 审批配置 | `AiApprovalConfig(profile_name, timeout_seconds)`（`config/models.py:15`） | 设置-权限 tab 已有 profile/timeout 配置（`settings.ts:305-313`），保留并迁入新模型配置中心 |
| AI 审批计数 | 快照字段 `ai_approval_count`（protocol.d.ts:180） | 未展示 |
| Reasoning | 6 档 none/low/medium/high/xhigh/max（`llm/domain/model.py:13`） | 模型 tab 只读展示，不可切换 |
| 小项 | `waiting_for_write_lock` 状态、`checkpoint` 节点类型（node_types.py）、`UiTextRequest.secret` | 均未渲染/未遮蔽 |

## 3. 视觉设计系统

设计参照：Linear / Raycast 的桌面美学——层级靠字重与灰阶，而非边框与色块。
现有 tokens（`frontend/css/tokens.css`）基础良好（暖中性灰阶、双主题），本期做收敛而非推倒。

### 3.1 Tokens 收敛

- 保留现有灰阶与变量命名，删除旧别名 `--vx-bg-base`（tokens.css:117/174/220），全库替换为 `--vx-bg-canvas`。
- 新增语义令牌：
  - `--vx-mode-chat / --vx-mode-coding / --vx-mode-goal / --vx-mode-loop`：四模式标识色（chat=灰、 coding=靛蓝、goal=绿、loop=琥珀；暗色对应提亮档）。
  - `--vx-text-tertiary` 归并到现有 `--vx-text-dim`，不新增字号。
- 暗色设为桌面端默认：`theme.ts` 初始化默认值改为 `dark`，`system` 选项保留。

### 3.2 字体与层级

- 界面字体栈、等宽字体栈沿用现有（Inter/系统栈 + SF Mono）。
- 层级规则：标题 15px/600、正文 14px/400、辅助 12.5px/400 muted；禁止新增字重档位。
- 聊天正文区最大宽度沿用 `--vx-content-max: 768px`，两侧留白。

### 3.3 空间、圆角、阴影

- 4px 基栅沿用；卡片圆角统一 `--vx-radius-md(12px)`，浮层 `--vx-radius-lg(16px)`，胶囊 `--vx-radius-full`。
- 卡片分层用 1px `--vx-border`；阴影仅浮层（弹窗、菜单、composer）使用 `--vx-shadow-md/lg`。

### 3.4 动效

- 全部过渡收敛到 `--vx-transition-fast(0.12s)` / `--vx-transition(0.18s)`，仅 fade/slide/opacity/transform。
- 删除任何 >0.28s 或弹性曲线动画（如有）。

### 3.5 CSS 工程

- `styles.css` 的运行时 `@import` 瀑布改为构建期单入口串联（Vite import 顺序），消除 FOUC 风险。
- 各 css 文件头部标注分层注释：tokens → base → layout → components → chat → composer。

## 4. 功能设计（P0）

### 4.1 运行模式系统

- **模式徽章**：会话标题栏右侧显示当前模式（Chat/Coding/Goal/Loop），数据源 `ThreadInfo.runtime_profile`；颜色用 §3.1 模式色令牌。会话列表项也显示小圆点模式色。
- **模式切换器**：composer 左下分段控件（四态），切换即发送对应 slash 命令（`/chat`、`/coding`、`/goal`、`/loop`，以后端注册名为准，实施时核实 `slash/commands/mode.py`）。
- **Goal/Loop 轻量提示**：切换到 goal/loop 时聊天流插入一条系统说明卡（intake 阶段提示语），深度三段式面板属 P1。
- Auto/Plan 交互模式与运行模式是正交概念，本期不在 UI 合并，仅在状态栏展示（见 §4.5）。

### 4.2 模型配置中心（设置 → 模型 tab 重构）

`settings.ts` 的 `renderModelTab` 重构为三组，每组结构一致：`跟随主模型` 开关（默认开）+ profile 下拉 + reasoning effort 下拉（6 档）：

1. **主对话**：provider/model/base_url/protocol/reasoning/ctx —— 沿用现有字段并改为可编辑保存。
2. **上下文压缩**：profile_name + reasoning_effort + timeout（1-300s），映射 `CompactionConfig`。
3. **AI 审批**：profile_name + timeout（1-60s），从权限 tab 迁入（权限 tab 保留 preset 选择），映射 `AiApprovalConfig`。

- 协议风险：快照当前不含 compaction 字段。实施第一步先验证 `settings.get/set` 是否透传任意配置键；若不透传，在后端 settings 快照补 `compaction` 字段（小改动，单独提交）。

### 4.3 Compact 呈现

- 压缩完成时聊天流插入系统分隔条：`── 上下文已压缩 · 释放约 xx tokens ──`，样式为 muted 12.5px + hairline，不打断阅读。
- 数据通道：优先复用后端已有 status 节点/Item（实施时核实 gateway 是否向 web 端发 compaction 状态；TUI 有 `active_compaction_text`，web 若无则需后端补一条 status item，单独小改动）。
- 手动 `/compact` 已有 slash 入口（`slash.ts:7`），保留。

### 4.4 审批体验

- 审批弹窗（`dialog.ts`）视觉重设计：工具名 15px/600 + 参数摘要等宽字体 + 风险等级左边条（error 红 / warning 琥珀 / 默认灰）；按钮主次沿用单色按钮体系。
- `UiTextRequest.secret` 为 true 的文本请求输入框加遮蔽（type=password 语义）。
- 状态栏展示 `ai_approval_count`（AI 审批已自动处理次数），仅在 >0 时显示。

### 4.5 状态栏整合

底部状态栏一行静音呈现（12.5px muted，分隔符 `·`）：
`模式 | 模型名 | reasoning档 | 权限档 | Auto/Plan | write-lock(仅锁定时) | AI审批计数(仅>0)`。
`waiting_for_write_lock` 期间 composer 禁用并显示"等待另一个会话完成写入…"。

### 4.6 小项

- `checkpoint` 节点渲染为可折叠系统行（与 status 节点同款样式）。
- `profile_configured` 通知到达时刷新设置快照缓存。

## 5. 验收标准

1. `./test.py --frontend` 全绿；新增组件有对应 vitest 用例（模式切换、配置中心序列化、compact 分隔条渲染）。
2. `./python.py scripts/export_ui_protocol_schema.py` 后 `protocol.d.ts` 无意外 diff（除非本期补了 compaction 字段，则 diff 应仅含该字段）。
3. 手动验证（`./python.py -m voidx.main --web`）：四模式切换徽章即时更新；设置中配置压缩/审批模型并保存后写入 settings 文件；触发一次压缩聊天流出现分隔条；暗色默认生效。
4. 视觉走查：无 >0.28s 动画；无新增字重/字号；亮暗两主题对比度可读。

## 6. 风险与约束

- **协议缺口**：compaction 配置与压缩事件可能需后端小改（settings 快照字段 + compaction status item）。约束：改动最小化，单独提交，附后端测试。
- **main.ts 腐化**：本期新增通知订阅会进一步增大 main.ts；约束：新功能逻辑放独立模块，main.ts 只加一行注册。
- **样式回归**：tokens 收敛影响全站；约束：每步改完跑前端测试 + 手动亮暗主题走查。
- **禁止事项**：不引入 UI 框架/Tailwind；不改 desktop/ Rust 壳；不动 protocol schema 生成器以外的后端协议结构（除 §4.2/4.3 注明的小改）。

## 7. 分期

- **P0（本期）**：§3 视觉基座 + §4.1–4.6 全部。
- **P1**：goal/loop 三段式面板（intake 引导 / spec 审批卡 / 执行进度）、Auto/Plan 显式建模。
- **P2**：main.ts 拆分、通知类型化注册表、CSS 分层彻底化。
