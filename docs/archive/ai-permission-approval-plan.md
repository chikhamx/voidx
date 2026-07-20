> **Status: Done** — Archived on 2026-07-20.

---
name: ai-permission-approval-plan
display_name: AI 权限审批实现计划
description: 按安全边界、服务生命周期和前后端集成拆解 AI 权限审批的 TDD 实现任务
doc_type: tasks
audience: llm
---

# AI 权限审批 — 实现计划

## Goal

按 `docs/specs/ai-permission-approval.md` 实现 `ai_approval` 模式：对所有可人工批准的 dangerous/extreme + ask 调用进行严格、单次、可追溯来源的 AI 语义审批，任何不确定状态回退人工，并保持 blocked 与现有四种权限模式行为不变。
## Final Verification Status

自动化实现已完成：T1–T11 的代码与对应测试已落地；本轮新增并验证了 `/permission ai_approval` 的 slash handler 回归覆盖。最终自动化验收结果记录如下：

- Backend 全量：`3310 passed, 31 skipped`
- Frontend 全量：`290 passed`
- TUI：`268 passed`
- Graph + slash focused：`61 passed`
- Permission + execution focused：`40 passed`
- 去重专项：`6 passed`
- Frontend production build：通过
- `git diff --check`：通过
- 独立 review：PASS，无 findings

自动化验收完成。后续需求新增了同一 session 成功 dangerous 调用去重：按工具名与规范化完整参数复用审批，来源标记为 `cached`；失败、参数变化、session reset/clear、settings 或 slash 权限模式切换均清理或绕过缓存。`Manual Acceptance` 保留为待真实外部模型环境执行的项目，不将模拟测试结果冒充真实模型验收。


## Architecture

权限引擎继续负责 sandbox、session 与 risk；无状态 `AiApprovalService` 只负责候选投影、按次解析 profile、调用模型和严格校验响应；graph 只应用合法 allow 并将剩余决策交给现有人工流程。设置由 workspace Settings 持久化，经 gateway 校验和热更新；前端只操作同一份 `permissions.ai_approval` 数据。

## Tech Stack

- Python 3、Pydantic、LangChain structured output、asyncio、现有 `retry_async`
- pytest（通过 `./test.py --backend`）
- TypeScript、DOM API、Vitest（通过 `./test.py --frontend`）

## Source of Truth

实现前必须遵守设计文档中的以下章节，不在本计划重复策略细节：

- `Trust Boundary and Prompt Safety`
- `Response Contract and Validation`
- `Configuration`
- `Failure Matrix`
- `Invariants`

禁止在实现中扩大到 normal/blocked、缓存 profile/key、接受 partial response，或写 session/persistent grant。extreme 可进入 AI，但仍只允许单次批准且不进入成功复用缓存。

## File Structure

| Path | Responsibility |
|---|---|
| `src/voidx/config/enums.py` | 新 PermissionMode |
| `src/voidx/config/models.py`, `src/voidx/config/__init__.py` | AiApprovalConfig 定义与导出 |
| `src/voidx/config/settings_permissions.py`, `src/voidx/config/settings.py` | workspace-only 配置读写 |
| `src/voidx/permission/presets.py` | ai_approval 的 safe 等价人工 scopes |
| `src/voidx/permission/ai_approval.py` | 请求投影、prompt、模型调用、响应校验 |
| `src/voidx/agent/graph/permissions.py`, `contracts.py` | graph 接入与 AI allow 应用 |
| `src/voidx/agent/graph/core/voidx_graph.py` | service 生命周期与 settings 热更新 |
| `src/voidx/tools/base.py` | approved_by metadata 模型 |
| `src/voidx/ui/gateway/session/method/settings.py` | 配置 API、async profile 校验 |
| `frontend/src/ui/settings.ts` | 设置页配置与说明 |
| `frontend/src/ui/model.ts`, `frontend/src/services/state.ts` | 快捷模式与 pill |

## TDD Tasks

每个任务严格执行：先写测试并运行确认因目标行为缺失而 RED，再写最小实现，最后运行同一命令确认 GREEN。不要把后续任务的实现提前放入当前任务。

### T1 — PermissionMode 与 preset 语义

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 新增：
  - `test_ai_approval_mode_sandbox_and_policy`
  - `test_ai_approval_dangerous_uses_safe_scopes`
  - `test_ai_approval_extreme_stays_once`
  - `test_existing_permission_modes_are_unchanged`
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`
- [ ] 修改 `src/voidx/config/enums.py`：新增 `PermissionMode.AI_APPROVAL = "ai_approval"`。
- [ ] 修改 `src/voidx/permission/presets.py`：dangerous 使用 `(ONCE, SESSION)`；extreme 继续命中现有 `_ask_once`；normal 保持现有 allow。
- [ ] 运行同一命令确认 GREEN。

Acceptance：`sandbox_mode == "workspace-write"`、`approval_policy == "untrusted"`，且没有 normal + ask 新行为。

### T2 — 强类型配置与 workspace 持久化

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 新增：
  - `test_ai_approval_config_defaults`
  - `test_ai_approval_config_timeout_bounds`
  - `test_ai_approval_settings_round_trip`
  - `test_ai_approval_settings_are_workspace_only`
  - `test_ai_approval_corrupt_settings_fall_back_to_defaults`
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`
- [ ] 修改 `src/voidx/config/models.py`：新增 `AiApprovalConfig(profile_name="", timeout_seconds=Field(12.0, ge=1.0, le=60.0))`；增加拒绝 NaN/Infinity 的校验。
- [ ] 修改 `src/voidx/config/__init__.py`：导出模型。
- [ ] 修改 `src/voidx/config/settings_permissions.py`：同步 get/set；损坏数据 get 返回默认；set 写完整 `model_dump(mode="json")`。
- [ ] 修改 `src/voidx/config/settings.py`：`WORKSPACE_ONLY_KEYS` 增加 `ai_approval`。
- [ ] 运行同一命令确认 GREEN。

Acceptance：无 `enabled`、无 `max_risk`，`permission_mode` 是唯一开关。

### T3 — 安全请求投影

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 新增投影测试：
  - 完整 bash/powershell command；
  - 文件正文只保留长度与 SHA-256；
  - 路径、git 子命令和 agent 最小字段保留；
  - 大小写敏感键统一 redacted；
  - 未知工具、不可序列化参数、关键字段过长不进入候选；
  - 单项 16 KiB、整批 48 KiB 边界；
  - 参数内提示注入文本只能出现在 JSON data，不改变 system policy；
  - `args_sha256` 对规范化 args 稳定。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v -k projection`
- [ ] 新建 `src/voidx/permission/ai_approval.py`，实现私有的规范化、redaction、投影和尺寸检查函数，以及 `AiApprovalRequestItem`。
- [ ] 不记录、不 dock 输出原始参数或投影。
- [ ] 运行同一命令确认 GREEN。

Acceptance：投影规则与设计逐项一致；不使用自由文本“参数摘要”。

### T4 — 响应 schema 与严格批次校验

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 新增：
  - 全 allow、全 deny、混合结果；
  - 响应乱序仍按 ID 匹配；
  - 缺项、未知 ID、重复 ID、空 ID、非法 decision 均整批无效；
  - 请求空 ID/重复 ID 不调用模型；
  - structured output 外层 raw/parsed 兼容；
  - 不接受 partial success。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v -k "response or batch or review"`
- [ ] 在 `src/voidx/permission/ai_approval.py` 实现 `AiApprovalItemResult`、`AiApprovalResponse`、`AiApprovalResult` 和严格 validator/coercion。
- [ ] 运行同一命令确认 GREEN。

Acceptance：任一完整性错误都返回 `reason="invalid_response"` 和空 `allowed_ids`。

### T5 — 无状态模型调用与 profile 生命周期

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 使用 fake Settings/model 新增：
  - 空 profile_name 解析当前 profile；
  - 指定 profile 精确解析且不回退首项；
  - 无 settings/profile/key、structured output 不支持均 unavailable；
  - timeout、连接异常、解析异常均空 allow；
  - 只重试允许的瞬态异常；
  - extreme 进入模型；blocked/risk=None 不进入模型；
  - profile 切换/删除后下一次 review 读取新值；
  - service 实例不持有 model/profile/api_key。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v -k "service or profile or timeout"`
- [ ] 在 `src/voidx/permission/ai_approval.py` 实现 async `AiApprovalService.review`：按次读取配置/profile、创建 chat/resolver model、structured invoke、timeout/retry 和失败回退。
- [ ] 使用设计规定的 system policy；args 作为 JSON data 发送。
- [ ] 运行同一命令确认 GREEN。

Acceptance：service 无缓存；模型失败永不抛过授权边界。

### T6 — approval 来源 metadata

- [ ] 在 `src/tests/test_permission/test_ai_approval.py` 新增：
  - `ApprovedToolRisk` 可解析 `approved_by="ai"` 与 `approved_by="cached"`；
  - 旧 metadata 无字段仍兼容；
  - 非法来源被拒绝或按明确默认处理。
- [ ] 在 `src/tests/test_agent/graph/test_graph_authorization.py` 新增 AI allow 的 metadata 断言。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_permission/test_ai_approval.py src/tests/test_agent/graph/test_graph_authorization.py -v -k approved_by`
- [ ] 修改 `src/voidx/tools/base.py`：`ApprovedToolRisk` 增加受限 `approved_by`。
- [ ] 修改 `src/voidx/agent/graph/permissions.py`：`_tool_call_with_approval_risk(decision, approved_by=...)` 写入 `approved_risk` 内部；人工审批写 `user`，AI 写 `ai`，成功 dangerous 调用的精确复用写 `cached`。
- [ ] 运行同一命令确认 GREEN。

Acceptance：执行侧 `_approved_tool_risks_for_call` 不丢失 AI 来源。

### T7 — Graph 授权链接入

- [ ] 在 `src/tests/test_agent/graph/test_graph_authorization.py` 新增：
  - AI allow 单次进入 approved，不弹人工；
  - AI deny 回退人工；
  - mixed allow/deny 只询问剩余项；
  - extreme 可传 AI；blocked/risk=None 不传 AI；
  - service unavailable/invalid response 全部人工；
  - AI allow 不写 session allow；仅工具成功执行后，同 session 的同工具+完整参数调用可命中内存去重；
  - 人工对剩余项选 always 时 AI allow 项不参与 session 写入；
  - dock 仅提示 `AI 审批: allow <tool>`，不含 args/reason；
  - 空/重复 tool-call ID 全部人工。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_agent/graph/test_graph_authorization.py -v -k ai_approval`
- [ ] 修改 `src/voidx/agent/graph/contracts.py`：声明 `_settings` 与 `_ai_approval`。
- [ ] 修改 `src/voidx/agent/graph/permissions.py`：筛选 dangerous/extreme + ask；调用 service；移出 AI allow；剩余项复用原人工逻辑。
- [ ] 过滤必须在 graph 与 service 两层都执行，形成 defense in depth。
- [ ] 运行同一命令确认 GREEN。

Acceptance：blocked 仍先处理；AI 不影响原有 choice/session 语义。

### T8 — Graph 初始化与设置热更新

- [ ] 在 `src/tests/test_agent/graph/test_run_loop_startup.py` 新增：
  - graph 构造时创建一个无状态 `AiApprovalService`；
  - settings update 替换 `self._settings` 后 service 实例可复用；
  - 更新模式/profile 后下一次授权读取新 settings；
  - graph settings=None 时安全回退人工。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_agent/graph/test_run_loop_startup.py -v -k ai_approval`
- [ ] 修改 `src/voidx/agent/graph/core/voidx_graph.py`：初始化 `_ai_approval`；保持 service 无状态；现有 `_apply_settings_update` 不缓存审批 profile/model。
- [ ] 运行同一命令确认 GREEN。

Acceptance：profile 删除、切换、key 更新无需重启 session。

### T9 — Gateway 配置契约

- [ ] 在 `src/tests/test_ui/gateway/test_gateway_v2_dispatch.py` 新增：
  - snapshot 返回 `permissions.ai_approval`；
  - update 对 profile_name/timeout 使用 merge 语义；
  - 空 profile_name 可保存；
  - 指定 profile 不存在或 key 为空时报 `MethodParamsError`；
  - timeout bool/NaN/Infinity/越界拒绝；
  - 更新触发 `_settings_update_handler`，当前 graph 可见新配置。
- [ ] 运行并确认 RED：
  - `./test.py --backend -- src/tests/test_ui/gateway/test_gateway_v2_dispatch.py -v -k ai_approval`
- [ ] 修改 `src/voidx/ui/gateway/session/method/settings.py`：在 permissions 内往返配置；async 精确校验 profile；构建 `AiApprovalConfig` 后同步 set；保持现有 settings 热更新回调。
- [ ] 运行同一命令确认 GREEN。

Acceptance：前后端只使用 `permissions.ai_approval`，不新增重复顶层字段。

### T10 — Frontend 设置页

- [ ] 在 `frontend/test/ui/settings.test.ts` 新增：
  - AI Approval 模式渲染；
  - profile 下拉只显示 `configured !== false` 的 profiles，并提供“当前主 profile”空值；
  - timeout 回显和保存；
  - 切换到其他模式不清空已有 ai_approval 配置；
  - patch 形状为 `permissions: { permission_mode, ai_approval: { profile_name, timeout_seconds } }`；
  - 页面显示“会将受限工具参数发送给所选模型”的说明。
- [ ] 运行并确认 RED：
  - `./test.py --frontend -- test/ui/settings.test.ts --reporter=verbose`
- [ ] 修改 `frontend/src/ui/settings.ts`：扩展 PermissionMode、snapshot 类型、模式配置、条件控件和 collect merge 数据。
- [ ] 运行同一命令确认 GREEN。

Acceptance：保存后 gateway snapshot 可无损回显。

### T11 — Frontend 快捷模式与状态 pill

- [ ] 在 `frontend/test/ui/workbench.test.ts`（现有实际路径）新增：
  - dropdown 显示 AI 审批选项；
  - 点击发送 `permissions.permission_mode="ai_approval"`；
  - pill 显示“AI 审批”及 `ai-approval` class；
  - settings snapshot 可同步状态。
- [ ] 运行并确认 RED：
  - `./test.py --frontend -- test/ui/workbench.test.ts --reporter=verbose`
- [ ] 修改 `frontend/src/ui/model.ts` 与 `frontend/src/services/state.ts`。
- [ ] 仅当缺少现有样式时修改 `frontend/css/composer.css`，不要改无关布局。
- [ ] 运行同一命令确认 GREEN。

Acceptance：切换模式不要求同时改 profile；未配置时后端安全回退人工。

### T12 — Focused Regression and Final Verification

- [ ] 运行 backend focused regression：
  - `./test.py --backend -- src/tests/test_permission src/tests/test_agent/graph/test_graph_authorization.py src/tests/test_agent/graph/test_run_loop_startup.py src/tests/test_ui/gateway/test_gateway_v2_dispatch.py -v`
- [ ] 运行 frontend focused regression：
  - `./test.py --frontend -- test/ui/settings.test.ts test/ui/workbench.test.ts --reporter=verbose`
- [ ] 运行完整 frontend：
  - `./test.py --frontend`
- [ ] 若 focused backend 通过且改动未触及 desktop，至少运行完整 backend：
  - `./test.py --backend`
- [ ] 确认所有命令退出码为 0，且没有跳过新增测试。

### T13 — Shell 语义审批上下文

- [ ] 先新增候选范围测试：dangerous/extreme + ask 进入 AI，normal、blocked、非 ask 与 risk=None 不进入 AI。
- [ ] 新增 shell 投影测试：只提供脱敏 command、shell 类型和 workspace-root cwd；不输出 network mode、SSH 副作用等静态语义推断。
- [ ] 新增敏感信息测试：认证 header、敏感环境变量、凭证参数与 URL userinfo 不出现在模型投影或 shell pattern。
- [ ] 运行聚焦测试确认 RED。
- [ ] 简化 `is_ai_approval_candidate` 为 action/risk 边界；删除 AI 模块内重复的 shell 语义分类规则。
- [ ] 更新 system policy：具体分析解释器、网络、SSH、包管理器与复合命令；无法理解或包含脱敏凭证时 deny 并回退人工。
- [ ] 运行聚焦测试确认 GREEN，再运行 permission/graph/gateway 回归。

Acceptance：权限层只提供确定性事实，模糊 shell 语义由 AI 判断；模型输入不包含可识别凭证，任意不确定状态继续回退人工。

## Manual Acceptance

- [ ] 主模型 A、审批 profile B：dangerous edit 可由 B 单次放行，dock 不泄露参数。
- [ ] extreme 触发 B；blocked 不触发 B。
- [ ] 删除 B、清空 B key、断网或模型超时均回退人工。
- [ ] 在不重启 session 的情况下切换 B→C，下一次审批使用 C。
- [ ] 同一 dangerous 调用成功后再次发生时跳过 AI；失败、参数变化、session clear 或权限模式切换后重新审批。

## Risks and Rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| 模型误放行 | blocked 前置、严格输出、语义不确定时 deny、单次授权、extreme 不缓存 | 从 frontend 隐藏模式并在 gateway 拒绝新选择；保留配置数据 |
| 参数泄露 | 安全投影、redaction、大小限制、不持久化 | 禁用 AI 模式，退化 safe |
| 延迟或供应商故障 | 有界 timeout/retry，失败人工 | 移除 graph 调用点即可恢复纯人工 |
| profile 热更新陈旧 | service 无状态、每次解析 | 无需重建 graph；回滚 service 接入 |
| metadata 兼容 | 新字段有兼容默认，旧数据可解析 | 保留 schema 字段，不再写 ai 值 |

## Forbidden Changes

- 不改 `sandbox_precheck_action`、RiskLevel/RiskAssessment 语义。
- 不让 normal、blocked 进入 AI review；extreme 与 dangerous 一样由 AI 做语义审批。
- 不新增 AI 专用 API key。
- 不缓存审批 profile、API key 或 BaseChatModel。
- 不接受缺项、未知/重复 ID 的部分成功。
- 不写 session allow/deny 或 persistent grants。
- 不记录 prompt、投影参数或模型理由。
- 不手工编辑生成的 `frontend/src/rpc/protocol.d.ts`；本功能当前不需要协议 schema 变更。
