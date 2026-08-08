---
name: desktop-tui-parity-plan
display_name: Desktop TUI Parity Plan
description: 桌面端补齐 TUI 能力差距的实现计划——输入历史、粘贴管线、键盘中断、状态信息密度、忙碌详情、斜杠命令同步、通知行、密钥掩码
doc_type: tech-design
audience: human+llm
---

# 桌面端补齐 TUI 能力差距 — 实现计划

## Context

对比 `tui/voidx_cli/` 与 `frontend/` + `desktop/tauri/` 后确认：桌面端在多会话、内嵌终端、diff 审查、设置面板等方面超出 TUI，但在**日常输入体验与状态信息密度**上存在明确缺口。本文档给出补齐计划。

### 差距清单（按优先级）

| # | 能力 | TUI 参照 | 桌面端现状 |
|---|------|---------|-----------|
| 1 | 输入历史（翻阅/草稿恢复/去重/1000 上限） | `tui/voidx_cli/input.py:242-300` | 无，ArrowUp 仅用于菜单（`frontend/src/main.ts:643-707`） |
| 2 | 粘贴管线（大文本折叠占位、Ctrl+V 图片、批合并） | `tui/voidx_cli/parser.py:95-292`、`clipboard_mixin.py` | 原生粘贴；图片仅右键菜单插入 `/paste` 文本（`context-menu.ts:80-102`），Tauri capabilities 未声明剪贴板权限 |
| 3 | 键盘中断语义（Ctrl+C 取消并恢复草稿、双击退出、Ctrl+D） | `tui/voidx_cli/app.py:538-599` | 仅停止按钮；前端仅一处 metaKey 判断（`main.ts:703`） |
| 4 | 状态栏信息密度（上下文用量/上限、缓存命中率、总 token、工作流/目标段） | `tui/voidx_cli/render_status.py` | 仅模型/权限/推理/running（`services/state.ts:100-165`） |
| 5 | 忙碌活动详情（耗时、token、最新动作、压缩/重试详情） | `tui/voidx_cli/render_activity.py:41-190` | 仅 "running" 文字 + 工具 spinner |
| 6 | 斜杠命令清单同步（后端注入、Tab 补全） | `app.py:473-477` + 后端 `src/voidx/presentation/commands.py` | `frontend/src/ui/slash.ts` 硬编码 37 条，已漂移 |
| 7 | 通知行（notice.set / 错误红行） | `tui/voidx_cli/render_frame.py:544-553` | `notice.set`、`refresh.requested`、`reset.requested` 被直接忽略（`main.ts:407-414`） |
| 8 | 密钥输入掩码 | `tui/voidx_cli/text_prompt_mixin.py` + `render_input.py:18-21` | 明文 textarea（`dialog.ts:226-228`） |
| 9 | guidance 忙碌回显 | `app.py:478-534` | 忙碌提交后本地不追加（`main.ts:560-574`），待验证后端是否补发 |

**明确不补**：终端 raw 模式、bracketed paste 协议、ANSI 帧渲染、退出写 transcript.log、方向键菜单导航（均有桌面端等价物或不需要）。

## Goal

桌面端用户在输入效率、中断控制、状态可见性上达到 TUI 同等水平；斜杠命令与后端单一事实来源，杜绝漂移。

## Architecture

分两批实施，全部改动集中在前端（`frontend/src/`）与桌面 shell 配置，**仅 #6 和 #2 需要动后端/协议**：

- **批次 A（纯前端，低成本）**：#1 输入历史、#3 键盘中断、#7 通知行、#8 密钥掩码。新增 `frontend/src/ui/history.ts`，其余改动落在现有模块。
- **批次 B（跨层）**：#2 粘贴管线（capabilities + 图片转附件走既有 attachments 候选 RPC 或新增 `attachments.paste` RPC）、#4 状态栏用量段（扩展 `updateStatusBar`，数据来自后端 status 通知，需确认 RPC 是否已带用量字段）、#5 忙碌详情（扩展 status item 渲染）、#6 命令清单改由后端经 `workspace.snapshot` 或新增 `commands.catalog` RPC 下发。
- #9 先验证后端行为，若后端已补发 message 项则无需改动。

## Tech Stack

TypeScript (strict, ES2020) + vitest/jsdom；Tauri 2 capabilities JSON；如需 RPC 扩展则改 `src/voidx` 网关层 + `scripts/export_ui_protocol_schema.py` 重新导出 schema（`npm run schema`）。

## File Structure

| 文件 | 职责 | 涉及项 |
|------|------|--------|
| `frontend/src/ui/history.ts`（新建） | 输入历史：环形缓冲（上限 1000）、相邻去重、草稿保存恢复 | #1 |
| `frontend/src/main.ts` | keydown 接入历史翻阅与 Ctrl+C/Ctrl+D 语义；notice.set 渲染；忙碌提交本地回显（若需） | #1 #3 #7 #9 |
| `frontend/src/services/state.ts` | 通知行 DOM 缓存与渲染辅助 | #7 |
| `frontend/src/ui/dialog.ts` | secret 文本请求改用 `type=password` input | #8 |
| `frontend/src/ui/slash.ts` | 移除硬编码目录，改为接收注入清单 + Tab 补全 | #6 |
| `frontend/src/ui/context-menu.ts` | 图片粘贴改走新管线 | #2 |
| `frontend/src/css/base.css`（或 `components.css`） | 通知行样式 | #7 |
| `desktop/tauri/capabilities/default.json` | 声明剪贴板读取权限 | #2 |
| `frontend/test/history.test.ts`（新建）等 | 各模块镜像测试 | 全部 |

## Tasks

### 批次 A

- [x] **A1. 输入历史模块**：新建 `ui/history.ts` — `push(text)`（去重、上限）、`prev()`/`next()`（返回草稿穿梭）、`reset()`。测试先行：`frontend/test/history.test.ts`。
  - 验证：`./test.py --frontend -- test/history.test.ts`
- [x] **A2. main.ts 接入历史**：提交成功后 `push`；input 为空或光标在首行时 ArrowUp/ArrowDown 翻阅（不与菜单冲突——菜单可见时让位，现有顺序已保证）。
  - 验证：`./test.py --frontend -- test/main.test.ts`
- [x] **A3. 键盘中断**：忙碌时 Esc 或 Ctrl+C → `session.cancel`；非空输入 Ctrl+C → 清空（保留到历史）；空输入 Ctrl+C 3 秒内双击 → 调 Tauri 关闭或浏览器忽略。Ctrl+D 空输入 → 关闭（仅 Tauri 上下文）。
  - 验证：`./test.py --frontend -- test/main.test.ts -t "interrupt"`
- [x] **A4. 通知行**：`notice.set` 渲染为 transcript 顶部灰行（样式参照 TUI 灰行语义），`turn.failed` 已有红字消息保持一致；`refresh.requested`/`reset.requested` 至少触发 transcript 重取或清空（与后端确认语义后实现）。
  - 验证：`./test.py --frontend -- test/main.test.ts -t "notice"`
- [x] **A5. 密钥掩码**：`dialog.ts` `renderTextRequest` 在 `request.secret` 时用 `<input type="password">` 替换 textarea，placeholder 文案改为通用描述。
  - 验证：`./test.py --frontend -- test/dialog.test.ts`

### 批次 B

- [x] **B1. 命令清单下发**：复用既有 `commands.list` RPC（snapshot 时拉取一次）；`slash.ts` 改为 `setCommandCatalog` 注入 + 硬编码兜底；Tab 唯一匹配补全与公共前缀扩展已实现（尾空格不再按描述匹配）。
  - 验证：`./test.py --frontend -- test/slash.test.ts`；若加 RPC 则 `./test.py --backend -- src/tests/ -k "commands"`
- [x] **B2. 状态栏用量段**：协议原无用量字段，新增 `usage.get` RPC（`usage_stats_provider` 注入 GatewaySession）；前端在 snapshot 与 turn.completed 时拉取，dock Status tab 新增 Usage 行（`formatUsageLabel`）。
  - 验证：`./test.py --frontend -- test/state.test.ts`
- [x] **B3. 忙碌详情**：`handleStatusItem` 增加 `.status-elapsed` 秒表（1s 间隔，完成时冻结，≥60s 显示 `Xm Ys`）。后端推送的 detail 即最新动作，沿用原渲染。
  - 验证：`./test.py --frontend -- test/render.test.ts`
- [x] **B4. 粘贴管线（图片部分）**：Tauri 无 clipboard 插件且 webview `navigator.clipboard` 足够，capabilities 未改；新增 `attachments.saveImage` RPC（base64→复用 TUI 压缩管线落盘 `.voidx/attachments/`）；菜单 paste 动作与输入框原生 paste 事件均上传图片并插入 `[image-<stem>]`。大文本折叠已实现（`ui/paste.ts`：多行粘贴折叠为 `[Pasted text #N +X lines]` 占位，提交时展开为 `<pasted>…</pasted>`；图片占位 `[Pasted image #N <size>]`，对齐 TUI 语义）。
  - 验证：`./test.py --frontend`；桌面端手动验证 `cd desktop && npm run dev`
- [x] **B5. guidance 回显**：后端本就将 `GuidanceSubmitted/Committed` 映射为 `guidance_preview` item；修复前端 `handleItem` 缺失分支，started 时以 `style: "guidance"` 渲染。
  - 验证：手动 `./python.py -m voidx.main --web` + 前端联调

### 收尾

- [x] **C1. 全量回归**：`./test.py`（`--keep-going` 会下传到底层 runner 导致报错，未用）。前端 397/397，desktop 15/15，后端 3623/3624——唯一失败 `test_goal_resolver_logs_fallback_decision` 由工作区未提交的 goal_resolver/llm.structured 重构（用户 WIP）引起，与本计划改动无关。
- [x] **C2. 桌面端冒烟（自动部分）**：`cargo check` 通过、`cargo test` 15/15、`npm run build`（vite）通过；`npm run dev` 交互冒烟需人工执行。：`cd desktop && ./build.sh --no-frontend` 通过 cargo check；`npm run dev` 手动走一遍历史/取消/粘贴。

## Tests

- 每个批次 A 任务先写失败测试（TDD），镜像 `frontend/test/<module>.test.ts` 命名。
- 涉及协议/schema 变更时跑 `cd frontend && npm run schema` 并确认 `protocol.d.ts` 重新生成。
- 桌面层改动跑 `./test.py --desktop`。

## Risks

1. **历史与菜单键位冲突**：ArrowUp/Down 已被 slash/ref 菜单占用——菜单可见时必须让位（现有 keydown 顺序已天然保证，测试中需覆盖）。
2. **Tauri 剪贴板权限**：capabilities 变更需重新打包才生效，dev 模式可能表现不一致；浏览器上下文回落为纯文本粘贴。
3. **命令清单协议变更**：`workspace.snapshot` 增加字段需前后端同时发布；旧前端连新后端需容错（缺字段时用内置最小清单兜底）。
4. **notice 语义不明**：`refresh.requested`/`reset.requested` 的后端意图未核实，A4 实施前需先读 `src/voidx` 网关侧发送点。
5. **用量字段不可得**：B2/B3 依赖后端 status 推送内容，若协议未携带需先扩协议，工作量上升。
