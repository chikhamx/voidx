---
name: ai-permission-approval
display_name: AI 权限审批模式
description: 新增 AI 审批权限模式，允许配置专门模型对可人工批准的危险工具调用做自动安全审批
doc_type: tech-design
audience: human+llm
---

# AI 权限审批模式 — 技术设计文档

## TL;DR

新增 `PermissionMode.AI_APPROVAL`。它沿用 safe 模式的 sandbox 与人工授权范围，但在 dangerous、action=ask 的工具调用进入人工弹窗前，先调用已配置 profile 对本批调用做一次受限安全审查。只有完整、唯一、可验证的结构化 `allow` 结果才单次放行；deny、缺项、未知 ID、超时、异常、profile 不可用以及 extreme/blocked 调用全部回退现有人工流程。

审批服务不缓存模型或密钥，每次 review 从当前 `Settings` 解析 profile，因此设置热更新和 profile 删除立即生效。首次 AI 放行不写 session/persistent grant，不绕过 sandbox，也不承担用户意图判断；同一 session 内仅当同一 dangerous 工具与规范化完整参数已成功执行时，后续调用可复用该成功审批并标记 `approved_by="cached"`，失败、参数变化、会话清理或权限模式切换均不复用。

## Context

当前权限链为：

1. `src/voidx/permission/engine.py` 做 sandbox 预检、session 规则和 risk 分类；
2. `src/voidx/permission/presets.py` 将 risk 映射为 allow / ask / blocked_ack；
3. `src/voidx/agent/graph/permissions.py` 汇总 need_ask，并统一调用 `_ask_tool_permission`。

safe 模式下，`RiskLevel.NORMAL` 已由 `resolve_mode_decision` 自动放行；通常只有 dangerous/extreme 会进入 ask。因此 AI 审批只处理 **dangerous + ask**，不虚构不可达的 normal + ask 分支。若未来引擎允许 normal + ask，必须另行修改本设计及测试，不能自动扩大 AI 授权面。

`src/voidx/agent/goal_resolver.py` 已提供 `create_resolver_model`、`with_structured_output`、retry 和 timeout 的可复用模式，但审批是安全边界，输出校验必须比普通分类更严格。

## Goals / Non-Goals

### Goals

- 新增 `PermissionMode.AI_APPROVAL`；`sandbox_mode="workspace-write"`，`approval_policy="untrusted"`。
- 复用一个已配置 model profile，不新增密钥存储。
- 对 dangerous、action=ask 的调用尝试单次 AI 审批。
- 所有不确定状态 fail closed 到人工确认，而不是静默 allow 或直接 deny。
- 设置页支持切换模式并选择审批 profile；配置变更对当前 graph 热生效。
- 运行时明确标记和提示 AI 放行来源。

### Non-Goals

- 不改变 sandbox 预检、RiskLevel、blocked_ack、session allow/deny 或 persistent grant 语义。
- 不让 AI 审批 extreme/blocked 调用。
- 不让 AI 判断调用是否符合用户真实意图；它只判断调用本身是否边界清晰、局部、可恢复且不具明显高危效果。
- 不持久化审批 prompt、原始参数、模型理由或审计日志。
- 不为子代理增加独立审批链；子代理工具仍通过已有 `authorize_tools` 回调进入同一 graph 权限链。

## User-visible Behavior

- 选择 AI Approval 后，normal 调用与 safe 相同，直接执行。
- dangerous 调用先进行 AI 审查：
  - 全批结果有效且某项为 allow：该项本次执行；
  - deny：该项显示在原人工确认中；
  - 服务不可用或整批结果无效：整批显示在原人工确认中。
- extreme 保持一次性人工确认；blocked 保持不可批准的 blocked_ack 提示。
- AI 放行时 dock 输出 `AI 审批: allow <tool>`；AI deny 不单独刷屏，由随后出现的人工弹窗表达。
- 未配置 profile、profile 被删除或 API key 为空时，模式等价于 safe 的人工审批体验。

## Architecture

### Responsibility Split

| Component | Responsibility |
|---|---|
| `permission/engine.py` | 现有 sandbox、session、risk 决策；不感知 AI |
| `permission/presets.py` | 令 ai_approval 的 dangerous/extreme 人工 scope 与 safe 一致 |
| `permission/ai_approval.py` | 候选过滤、profile 解析、请求投影、模型调用、严格输出校验 |
| `agent/graph/permissions.py` | 在人工审批前调用 service，应用单次 allow，剩余项走原流程 |
| `config/settings_permissions.py` | workspace-only 配置同步读写，不做异步 profile 校验 |
| gateway settings | profile 存在性/API key 校验、配置往返、触发当前 graph 热更新 |
| frontend settings/model/state | 模式选择、profile 选择、状态 pill |

### Service Lifecycle

`AiApprovalService` 是无状态服务：

- graph 构造时创建一个实例并存入 `_ai_approval`；实例不持有 `BaseChatModel`、Profile、API key 或配置快照；
- `review(..., settings)` 每次调用 `settings.get_ai_approval_config()`；
- 空 `profile_name` 调用 `await settings.resolve_profile()` 解析当前主 profile；指定名称通过 `await settings.list_profiles()` 精确匹配，禁止调用会回退首项的 `resolve_profile(name)`；
- profile 不存在或 `api_key` 为空时返回 unavailable；
- `_apply_settings_update` 替换 `self._settings` 后，无须重建 service；下一次 review 自动读取新设置；
- graph 没有 Settings 时不调用模型，全部回退人工。

这样避免审批模型长期持有已删除 profile 或旧密钥，也减少 graph 热更新的耦合。

## Request / Decision Flow

1. `_authorize_tool_calls` 创建一次 `PermissionContext`，按现有逻辑把调用分成 approved / denied / need_ask。
2. `_ask_and_apply_permission` 先分离 blocked_ack；blocked 仍走原不可批准流程。
3. 仅当以下条件全部成立时调用 AI：
   - `context.permission_mode == PermissionMode.AI_APPROVAL.value`；
   - `_settings` 与 `_ai_approval` 均可用；
   - decision.action == ASK；
   - decision.risk 存在且 level == DANGEROUS。
4. extreme 与缺失 risk 的决策不传给 AI，保留在人工列表。
5. service 对候选生成 `AiApprovalRequestItem`，执行一次批量结构化调用。
6. 仅当响应通过完整批次校验时，按 ID 应用 allow；整批校验失败时 `allowed_ids` 为空。
7. AI allow 的 tool call 加入 approved；其他 approvable 决策继续走 `_ask_tool_permission`。
8. AI allow 不调用 `allow_silent`，不修改任何 grant。

## Trust Boundary and Prompt Safety

工具参数是不可信数据，可能包含提示注入、密钥或超大文本。模型输入不得使用自由拼接的“参数摘要”。使用固定 system policy 和 JSON 序列化的强类型 request items，并明确声明 `args` 仅是待审数据，不是指令。

### `AiApprovalRequestItem`

```text
AiApprovalRequestItem
├── id: str                  # 原 tool call id，非空且批内唯一
├── tool_name: str
├── pattern: str
├── risk_level: "dangerous"
├── risk_tags: tuple[str, ...]
├── risk_reason: str
├── args: dict               # 经安全投影后的参数
└── args_sha256: str         # 原始规范化 args 的 SHA-256，仅用于绑定/诊断，不持久化
```

安全投影规则是确定性的，放在 `permission/ai_approval.py` 并由单元测试锁定：

- bash/powershell：保留完整 command；命令超过单项上限则该项不交 AI；
- read/write/replace/manage/git：保留操作类型、完整路径、git 子命令；文件正文只发送长度与 SHA-256，不发送内容；
- agent：保留 agent、mode、target 和 task/description 的受限长度文本，不发送嵌套运行时上下文；
- 已知敏感键（api_key、authorization、cookie、password、secret、token 及大小写变体）的值替换为 `<redacted>`；
- 未定义投影的工具、不可 JSON 序列化参数、发生截断的安全关键字段均不交 AI，直接人工；
- 单项投影上限 16 KiB、整批上限 48 KiB；超过限制的项留在人工列表，不因同批其他项超限而丢失。

启用 AI Approval 意味着上述投影会发送给所选外部模型。前端说明必须明确这一点。原始参数、投影和模型理由均不得写入 dock 或持久化日志。

### Model Policy

模型只能在以下条件全部成立时 allow：操作边界明确、限定于 workspace/已知项目操作、无外部系统破坏、无权限提升、无凭证操作、无不可逆或大范围副作用。模型必须把参数中的文本视为数据，并忽略其中要求改变审批规则或输出 allow 的指令。不满足时返回 deny；deny 只是“需要人工确认”。

## Response Contract and Validation

结构化响应：

```text
AiApprovalResponse
└── decisions: list[AiApprovalItemResult]
    ├── id: str
    ├── decision: "allow" | "deny"
    └── reason: str
```

service 的公开结果：

```text
AiApprovalResult
├── allowed_ids: frozenset[str]
└── reason: "reviewed" | "disabled" | "unavailable" | "invalid_response" | "error"
```

响应必须满足：

- 每个请求 ID 恰好出现一次；
- 不含未知 ID、重复 ID或空 ID；
- decision 只能是 allow/deny；
- 请求候选本身的 ID 必须非空且唯一；否则不调用模型；
- 只依据 ID 关联结果，不依据列表顺序、工具名或模型返回的参数；
- 任一规则失败则整批 invalid，`allowed_ids` 为空，全部回退人工。

不接受 partial success。安全性优先于减少弹窗，避免缺项或伪造 ID 造成误放行。

## Configuration

`AiApprovalConfig` 位于 `src/voidx/config/models.py`：

```text
AiApprovalConfig
├── profile_name: str = ""          # 空 = 当前主 profile
└── timeout_seconds: float = 12.0    # Pydantic: ge=1.0, le=60.0
```

不增加 `enabled`：`permission_mode == ai_approval` 是唯一启用开关，避免双重状态。

不增加可配置 `max_risk`：本期授权上限固定为 dangerous。扩大风险范围属于安全策略变更，必须修改代码、设计和测试，不能通过 settings 静默调整。

配置存于 workspace `settings.json` 顶层 `ai_approval`，加入 `WORKSPACE_ONLY_KEYS`。旧文件缺少该键时返回默认值，无需迁移。

### Gateway Validation

- profile_name 为空：允许，表示当前 profile；保存时当前 profile 可以暂时不存在，运行时安全回退人工；
- profile_name 非空：必须精确匹配 `await settings.list_profiles()` 中的 profile，且该 profile `api_key` 非空；否则 `MethodParamsError`；
- timeout 由 Pydantic 校验，bool、NaN、无穷值和范围外值拒绝；
- patch 使用 merge 语义：缺失字段保留当前值，避免只切模式时重置 profile；
- settings snapshot 在 `permissions.ai_approval` 下返回配置，前后端只有一个数据位置。

## API Contracts

| Name | Contract | Failure behavior |
|---|---|---|
| `AiApprovalService.review` | async；输入候选 decisions、PermissionContext、Settings；输出 `AiApprovalResult` | 捕获模型/解析异常，返回空 allowed_ids |
| `Settings.get_ai_approval_config` | 同步读取并校验 `AiApprovalConfig` | 缺失返回默认；损坏配置回退默认并不抛到授权链 |
| `Settings.set_ai_approval_config` | 同步持久化完整 model_dump | 不做 async profile 查询 |
| `_tool_call_with_approval_risk` | 可选 `approved_by` 写入 `approved_risk` 内部 | 不新增顶层 metadata 来源字段 |

`approved_by` 放入 `metadata["approved_risk"]`，并在 `src/voidx/tools/base.py::ApprovedToolRisk` 增加 `approved_by: "user" | "ai" | "policy" | ""`。现有人工审批写 `user`，AI 审批写 `ai`，自动 policy 路径可保持空值以兼容旧数据。这样执行侧解析后不会丢失来源。

## Model Invocation

- 使用 `create_chat_model(profile.api_key, ModelConfig(...profile fields...))` 创建本次模型；
- 再调用 `create_resolver_model` 禁用或最小化 reasoning；
- 使用 Pydantic schema + `with_structured_output`，兼容 DeepSeek json_mode 的逻辑抽成 ai_approval 内部小函数，不修改 goal_resolver；
- `asyncio.wait_for` 使用配置 timeout；
- `retry_async` 仅重试 timeout、连接和 OS I/O 异常，最多使用现有 `RetryConfig`；结构化输出错误不重试；
- review 完成后不保存 model 实例。

## Decisions

| Decision | Rationale |
|---|---|
| 只审 dangerous + ask | normal 当前已自动 allow；extreme/blocked 必须人工 |
| AI deny 回退人工 | 保留人工覆盖权，不让模型误拒绝阻断任务 |
| 无状态 service、按次解析 profile | 设置/profile 删除立即生效，避免缓存旧密钥 |
| 严格全批校验 | 缺项、重复和未知 ID 不能产生部分误授权 |
| 安全投影而非自由摘要 | 保留安全关键字段，同时限制敏感内容和提示注入面 |
| 固定 dangerous 上限 | 安全策略不应由普通配置扩大 |
| 唯一模式开关 | 避免 mode 与 enabled 不一致 |
| AI allow 仅本次有效 | 不放大一次模型判断的授权范围 |

## Failure Matrix

| Case | Expected behavior |
|---|---|
| profile 不存在/无 key/settings 缺失 | 不调用模型，全部人工 |
| profile 在运行中删除或切换 | 下一次 review 使用最新 Settings，失败则人工 |
| timeout/网络/模型异常 | 空 allowed_ids，全部人工 |
| structured output 不支持或解析失败 | 空 allowed_ids，全部人工 |
| 响应缺项、重复 ID、未知 ID | 整批无效，全部人工 |
| 请求 ID 空或重复 | 不调用模型，全部人工 |
| extreme/blocked/risk=None | 不进入模型输入，走原流程 |
| 投影未知、关键字段截断、尺寸超限 | 该项不进入模型输入，人工；其他合法候选可单独 review |
| AI allow | 单次 approved，approved_risk.approved_by=ai，dock 提示 |
| AI deny | 人工确认，不写 denied |
| 人工对剩余项选 always | 仅剩余人工项写 session allow；AI allow 项不参与 |

## Files / Entry Points

| Path | Expected change |
|---|---|
| `src/voidx/config/enums.py` | 新枚举值及 sandbox/approval 属性覆盖 |
| `src/voidx/config/models.py`, `config/__init__.py` | `AiApprovalConfig` 模型与导出 |
| `src/voidx/config/settings_permissions.py`, `settings.py` | workspace-only 配置读写 |
| `src/voidx/permission/presets.py` | dangerous scope 与 safe 一致；extreme 保持 once |
| `src/voidx/permission/ai_approval.py` | service、投影、prompt、schema、严格校验 |
| `src/voidx/agent/graph/permissions.py`, `contracts.py` | graph 接入与来源 metadata |
| `src/voidx/agent/graph/core/voidx_graph.py` | 创建无状态 service；热更新只替换 settings |
| `src/voidx/tools/base.py` | `ApprovedToolRisk.approved_by` |
| `src/voidx/ui/gateway/session/method/settings.py` | 配置 snapshot/update/校验 |
| `frontend/src/ui/settings.ts` | 模式、profile、timeout 控件与数据外发说明 |
| `frontend/src/ui/model.ts`, `services/state.ts` | composer 模式和 pill 显示 |

## Invariants

- sandbox 预检、session deny 和 blocked_ack 始终先于 AI。
- 只有 risk=DANGEROUS 且 action=ASK 的决策可发送给 AI。
- extreme、blocked、risk=None 永不发送给 AI。
- 任意不确定状态的默认结果都是人工确认。
- 首次 AI allow 只绑定一个原始 tool-call ID，不直接创建长期 grant。
- 仅成功执行过的 dangerous 调用可在同一 session 内按工具名与规范化完整参数复用审批；复用来源为 `cached`。
- 失败、deny、extreme、blocked、不可规范化参数、参数变化、session reset/clear 和权限模式切换均不得命中缓存。
- 成功调用缓存仅驻留 graph 内存，不持久化，也不复用工具输出。
- AI allow 与缓存复用均不修改 session_allow/session_deny/persistent grants。
- 原始参数、投影和模型理由不持久化、不输出到 dock。
- profile 或 API key 不缓存于 AiApprovalService。
- 现有四种 PermissionMode 行为不变。

## Test Plan

| Scope | Command | Expected result |
|---|---|---|
| Config/service | `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v` | 枚举、配置、投影、模型响应与失败矩阵全绿 |
| Graph | `./test.py --backend -- src/tests/test_agent/graph/test_graph_authorization.py -v -k ai_approval` | allow/fallback/extreme/blocked/session 语义全绿 |
| Runtime lifecycle | `./test.py --backend -- src/tests/test_agent/graph/test_run_loop_startup.py -v -k ai_approval` | graph 初始化和 settings 热更新全绿 |
| Gateway | `./test.py --backend -- src/tests/test_ui/gateway/test_gateway_v2_dispatch.py -v -k ai_approval` | snapshot、merge、profile/key/timeout 校验全绿 |
| Frontend settings | `./test.py --frontend -- test/ui/settings.test.ts --reporter=verbose` | 模式/profile/timeout 保存回显全绿 |
| Frontend workbench | `./test.py --frontend -- test/ui/workbench.test.ts --reporter=verbose` | dropdown 与 pill 全绿 |
| Backend regression | `./test.py --backend -- src/tests/test_permission src/tests/test_agent/graph/test_graph_authorization.py src/tests/test_ui/gateway/test_gateway_v2_dispatch.py -v` | 相关回归全绿 |
| Frontend regression | `./test.py --frontend` | 全绿 |

## Manual Acceptance

1. 配置两个带 key 的 profiles，主模型 A、审批模型 B。
2. 选择 AI Approval 与 B，触发 workspace 内 dangerous edit；确认 AI allow 时无人工弹窗且 dock 有来源提示。
3. 触发 extreme/blocked 命令；确认模型未被调用且仍走人工/blocked 流程。
4. 删除 B 或清空 key，再触发 dangerous edit；确认安全退化为人工。
5. 切换审批 profile，无需重启 session；确认下一次 review 使用新 profile。
