---
name: ai-permission-approval
display_name: AI 权限审批模式
description: 新增 AI 审批权限模式，允许配置专门模型对工具调用做自动安全审批
doc_type: tech-design
audience: human+llm
---

# AI 权限审批模式 — 技术设计文档

## TL;DR

在现有四种 `PermissionMode`（read_only / safe / project_trusted / full_access）之外新增 `ai_approval` 模式。该模式下，工具调用经过 sandbox 与 risk 评估后，若决策为 `ask`，先调用一个可单独配置的“审批模型”做自动审批；模型返回 `allow` 的低/中风险调用直接放行，`deny` 或不可解析、调用失败、风险为 extreme/blocked 的调用仍回退到人工确认。审批模型复用现有 profiles/API key 体系，不引入新密钥存储。

## Context

当前权限链路（`src/voidx/permission/engine.py` → `presets.py` → `agent/graph/permissions.py`）对 `ask` 决策统一走 `_ask_tool_permission`，由用户在 UI/TUI 手动选择 y/n/a。用户希望减少高频低风险确认的打断，同时不放弃对高风险操作的人工把关。`goal_resolver.py` 已有“用独立模型 + 结构化输出做轻量判定”的成熟范式（`create_resolver_model` + `with_structured_output` + retry/timeout），本设计沿用同一范式。

## Goals / Non-Goals

### Goals

- 新增 `PermissionMode.AI_APPROVAL`，sandbox 行为等同 safe（workspace-write），approval_policy 仍为 untrusted。
- 支持为 AI 审批单独指定一个已配置的 model profile（provider/model/base_url/protocol/api_key 全部复用现有 profile）。
- AI 只对 risk.level ∈ {normal, dangerous} 且 action=ask 的决策尝试自动审批；extreme/blocked 一律不交给 AI。
- AI 调用失败、超时、输出不可解析时默认回退人工确认，绝不静默放行。
- 前端设置页可切换到 AI Approval 模式并选择审批 profile。

### Non-Goals

- 不让 AI 审批绕过 sandbox 预检或 blocked_ack。
- 不新增独立的审批模型密钥存储；审批模型必须来自已配置 profile。
- 不在本期实现 AI 审批的审计日志持久化（仅运行时 dock 提示）。
- 不改变 session_allow/session_deny 语义；AI allow 不写入 session 白名单，仅本次放行。

## Proposed Design

### Request / Data Flow

1. `_authorize_tool_calls` 仍先做 sandbox 预检 + risk 评估，得到 approved / denied / need_ask 三类。
2. 当 `context.permission_mode == ai_approval` 且 need_ask 非空：
   - 过滤出 `approvable`（action != BLOCKED_ACK 且 risk.level ∈ {normal, dangerous}）的决策。
   - 对这批决策调用 `AiApprovalService.review(decisions, context)`，传入工具名、参数摘要、风险标签、pattern、workspace。
   - AI 返回 `allow` 的决策 → 直接 append 到 approved（带 approved_risk metadata）。
   - AI 返回 `deny` 或 `unclear` 的决策 → 仍进入 `_ask_and_apply_permission` 走人工确认（不直接 deny，避免误伤）。
   - BLOCKED_ACK / extreme 风险的决策不调用 AI，直接走原 blocked 人工流程。
3. AI 不可用（无 profile / 无 api_key / 模型 None）时，整个 ai_approval 模式退化为 safe 行为：全部 ask 走人工。

### API / Function Contract

| Name | Input | Output | Error Behavior |
|------|-------|--------|----------------|
| `AiApprovalService.review` | `decisions: list[PermissionDecision]`, `context: PermissionContext`, `settings: Settings` | `AiApprovalResult(allowed_ids: set[str], reason: str)` | 任何异常 → 返回空 allowed_ids + reason="ai_unavailable"，调用方回退人工 |
| `Settings.get_ai_approval_config` | — | `AiApprovalConfig` | 缺失字段返回默认（enabled=False, profile_name=""） |
| `Settings.set_ai_approval_config` | `AiApprovalConfig` | `Path` | 校验 profile_name 已配置，否则 ValueError |

`AiApprovalConfig`（新增于 `config/models.py`）：

```text
AiApprovalConfig
├── enabled: bool = False
├── profile_name: str = ""        # 空 = 复用当前主模型 profile
├── max_risk: str = "dangerous"   # 允许 AI 审批的最高风险等级
└── timeout_seconds: float = 12.0
```

## Data Model / Migration

- `PermissionMode` 枚举新增 `AI_APPROVAL = "ai_approval"`，`sandbox_mode` 返回 `"workspace-write"`，`approval_policy` 返回 `"untrusted"`。
- `settings.json` 新增顶层键 `ai_approval`（dict），归入 `WORKSPACE_ONLY_KEYS`。
- 无数据迁移：旧 settings.json 不含该键，`get_ai_approval_config` 返回默认值。

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| AI 只审 normal/dangerous，extreme/blocked 强制人工 | 让 AI 审全部风险 | 用户选择“自动安全审批”，极端风险必须人工把关 |
| AI deny 不直接拒绝，回退人工确认 | AI deny 直接进 denied | 避免审批模型误判导致任务卡死，保留人工覆盖权 |
| 复用现有 profile 体系，不新增密钥存储 | 新增 ai_approval_api_key | 单一密钥来源，避免重复与泄露面 |
| 审批模型用 `with_structured_output` + JSON schema | 自由文本解析 | 与 goal_resolver 一致，降低解析失败率 |
| AI allow 不写 session 白名单 | 写入 session_allow | AI 审批是单次判定，不应放大授权范围 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| 审批模型误放行危险操作 | 安全风险 | 限制 max_risk=dangerous，extreme/blocked 不交 AI；sandbox 预检仍前置 |
| 审批模型调用慢/超时 | 任务卡顿 | 12s 超时 + retry，超时回退人工 |
| 审批模型未配置 | 功能不可用 | 退化为 safe，不阻断 |
| AI allow 被误认为用户授权 | 审计混淆 | metadata 标注 `approved_by: "ai"`，dock 提示“AI 审批放行” |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/config/enums.py` | 新增 `AI_APPROVAL` 枚举值及 sandbox/approval 属性 | 不改现有四值 |
| `src/voidx/config/models.py` | 新增 `AiApprovalConfig` Pydantic 模型 | |
| `src/voidx/config/settings_permissions.py` | 新增 `get_ai_approval_config`（同步读）与 `set_ai_approval_config`（同步写，不校验 profile） | profile_name 校验放到 gateway 层 async 调 `list_profiles`，避免 mixin 同步方法调 async |
| `src/voidx/permission/ai_approval.py` | 新建：`AiApprovalService` + prompt 构造 + 结构化输出解析 | 复用 `create_chat_model`/`create_resolver_model` |
| `src/voidx/permission/context.py` | `PermissionContext` 无需新字段；`from_service` 已带 permission_mode | |
| `src/voidx/permission/presets.py` | `resolve_mode_decision` 增加 `AI_APPROVAL` 分支：dangerous 走 `_ask_scoped((ONCE, SESSION))`，extreme 走 `_ask_once` | 不加则 fallback 到 `_ask_once`，用户无法选 session scope |
| `src/voidx/agent/graph/permissions.py` | `_ask_and_apply_permission` 前插入 AI 审批分支；`_tool_call_with_approval_risk` 扩展支持 `approved_by` metadata | 仅当 mode=ai_approval 且 approvable 非空；AI 过滤必须显式检查 `risk.level` 不只是 action |
| `src/voidx/agent/graph/contracts.py` | `GraphPermissionHost` 增加可选 `_ai_approval: AiApprovalService \| None` 与 `_settings` 访问 | 保持 Protocol 兼容 |
| `src/voidx/ui/gateway/session/method/settings.py` | settings_get/update 往返 `ai_approval` 配置；update 时 async 校验 profile_name | |
| `frontend/src/ui/settings.ts` | PERMISSION_MODES 增加 ai_approval；permissions tab 增加审批 profile 下拉 | |
| `frontend/src/ui/model.ts` | 权限下拉 `options` 数组增加 ai_approval 项（title/desc/icon） | 不加则用户无法从 composer pill 切换到 AI 审批 |
| `frontend/src/services/state.ts` | `permissionMode` 类型加 `"ai_approval"`；pill 文案 if-elif 增加 ai_approval 分支 | 不加则 pill 显示默认"安全模式" |

### Existing Behavior

- `_ask_and_apply_permission` 先处理 blocked（BLOCKED_ACK），再对 approvable 调 `_ask_tool_permission`。
- `authorize_tool_call` 返回的 `PermissionDecision` 带 `risk: RiskAssessment | None` 与 `allowed_scopes`。
- `presets.resolve_mode_decision` 对 safe 返回 `_ask_scoped((ONCE, SESSION))`。

### Target Behavior

- ai_approval 模式下 `resolve_mode_decision` 行为等同 safe（ask_scoped ONCE/SESSION），但 `_ask_and_apply_permission` 在调 `_ask_tool_permission` 前先尝试 AI 审批。
- AI allow 的决策从 approvable 移入 approved，剩余继续人工。
- AI 审批结果通过 dock 输出“AI 审批: allow/deny <tool>”提示。

### Invariants

- sandbox 预检与 blocked_ack 流程不可被 AI 绕过。
- extreme/blocked 风险的决策绝不传入 `AiApprovalService.review`。注意：extreme 风险在 safe/ai_approval 模式下返回 `action="ask"`（非 BLOCKED_ACK），会进入 approvable 列表，因此 AI 过滤必须显式检查 `risk.level not in {EXTREME, BLOCKED}`，不能仅靠 `action != BLOCKED_ACK`。
- AI 审批失败必须回退人工，不得默认 allow。
- AI allow 仅本次有效，不修改 session_allow/session_deny/persistent grants。
- `PermissionMode.AI_APPROVAL.sandbox_mode == "workspace-write"`，`approval_policy == "untrusted"`。

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| 审批模型返回 allow | 决策进 approved，带 approved_risk + approved_by=ai | `test_ai_approval_allow` |
| 审批模型返回 deny | 决策回退人工确认 | `test_ai_approval_deny_fallback` |
| 审批模型超时/异常 | allowed_ids 为空，全部回退人工 | `test_ai_approval_unavailable` |
| extreme/blocked 决策 | 不调用 AI，走原 blocked 人工流程 | `test_ai_approval_skips_extreme` |
| 未配置审批 profile | 退化为 safe，全部人工 | `test_ai_approval_no_profile` |
| AI allow 不写 session 白名单 | 同一工具下次仍需审批 | `test_ai_approval_no_session_persist` |

### Forbidden Changes

- 不改 sandbox 预检逻辑与 `sandbox_precheck_action`。
- 不改 `RiskAssessment` / `RiskLevel` 语义。
- 不改 session_allow/session_deny/persistent grants 写入路径。
- 不为 AI 审批新增独立密钥存储字段。
- 不在 `PermissionContext` 新增可变状态字段（保持 frozen dataclass）。

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| Unit | `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v` | 全绿 |
| Graph | `./test.py --backend -- src/tests/test_agent/graph/test_graph_authorization.py -v` | 全绿 |
| Frontend | `./test.py --frontend -- --reporter=verbose` | 全绿 |
| Smoke | 手动切到 AI Approval 模式 + 选 profile，触发一次 edit | 低风险自动放行，dock 提示 AI 审批 |

## Open Questions

- [x] AI deny 是回退人工还是直接拒绝？决策：回退人工（保留覆盖权）。
- [x] extreme 风险过滤靠 action 还是 risk.level？决策：显式检查 `risk.level not in {EXTREME, BLOCKED}`，不靠 action（extreme 在 safe/ai_approval 下 action=ask）。
- [x] set_ai_approval_config 同步还是 async？决策：同步写，profile 校验放 gateway 层 async。
- [x] 12s timeout 是否够？决策：先用 12s，与 goal_resolver 的 20s 区分（审批 prompt 更短）；后续可配置。
