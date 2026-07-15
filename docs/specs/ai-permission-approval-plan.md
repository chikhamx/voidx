---
name: ai-permission-approval-plan
display_name: AI 权限审批实现计划
description: 把 ai-permission-approval 设计拆成 TDD 小任务
doc_type: tasks
audience: llm
---

# AI 权限审批 — 实现计划

依据设计文档 `docs/specs/ai-permission-approval.md`。每个任务先写失败测试，再实现，最后跑指定测试命令确认绿。

## 任务列表

- [ ] T1: 新增 `PermissionMode.AI_APPROVAL` 枚举
  - 文件: `src/voidx/config/enums.py`
  - 改动: 在 `PermissionMode` 增加 `AI_APPROVAL = "ai_approval"`；`sandbox_mode` 返回 `"workspace-write"`，`approval_policy` 返回 `"untrusted"`。
  - 测试: `src/tests/test_permission/test_ai_approval.py::test_ai_approval_mode_sandbox`
  - 命令: `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`

- [ ] T2: 新增 `AiApprovalConfig` 配置模型
  - 文件: `src/voidx/config/models.py`
  - 改动: 新增 Pydantic 模型 `AiApprovalConfig(enabled: bool=False, profile_name: str="", max_risk: str="dangerous", timeout_seconds: float=12.0)`。
  - 测试: `test_ai_approval_config_defaults`
  - 命令: 同上

- [ ] T2.5: `presets.resolve_mode_decision` 增加 AI_APPROVAL 分支
  - 文件: `src/voidx/permission/presets.py`
  - 改动: 在 SAFE 分支后增加 `if preset == PermissionMode.AI_APPROVAL: return _ask_scoped(risk, (ONCE, SESSION))`，使 dangerous 风险返回 session scope（与 safe 一致）。extreme 仍走前面的 `_ask_once`。
  - 测试: `test_presets_ai_approval_dangerous_scopes`, `test_presets_ai_approval_extreme_ask_once`
  - 命令: `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`

- [ ] T3: Settings 读写 `ai_approval` 配置
  - 文件: `src/voidx/config/settings_permissions.py`, `src/voidx/config/settings.py`
  - 改动:
    - 新增 `get_ai_approval_config() -> AiApprovalConfig`（同步读 `_effective_data().get("ai_approval", {})`）。
    - 新增 `set_ai_approval_config(cfg) -> Path`（同步写 `_data["ai_approval"]`，不校验 profile）。
    - `settings.py` 的 `WORKSPACE_ONLY_KEYS` 增加 `"ai_approval"`。
    - profile_name 校验放到 T6 gateway 层 async 调 `list_profiles`。
  - 测试: `test_get_set_ai_approval_config`
  - 命令: `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`

- [ ] T4: 实现 `AiApprovalService`
  - 文件: `src/voidx/permission/ai_approval.py`（新建）
  - 改动:
    - `AiApprovalResult(BaseModel)`: `allowed_ids: set[str]`, `reason: str`
    - `AiApprovalService.review(decisions, context, settings) -> AiApprovalResult`:
      - 过滤条件：`risk.level in {NORMAL, DANGEROUS}` 且 `action != BLOCKED_ACK`（显式检查 risk.level，不靠 action）。
      - 解析 profile_name（空则用当前主模型 profile）；无 profile/api_key → 返回空 allowed_ids + reason="ai_unavailable"。
      - 构造 prompt（SystemMessage + HumanMessage），要求 JSON `{"decisions":[{"id","allow":bool,"reason":str}]}`。
      - 用 `create_chat_model` + `create_resolver_model` + `with_structured_output` 调用，12s 超时 + retry_async。
      - 异常/超时/解析失败 → 返回空 allowed_ids + reason。
  - 测试: `test_ai_review_allow`, `test_ai_review_deny`, `test_ai_review_unavailable`, `test_ai_review_skips_extreme`
  - 命令: `./test.py --backend -- src/tests/test_permission/test_ai_approval.py -v`

- [ ] T5: Graph 审批链路接入 AI 审批
  - 文件: `src/voidx/agent/graph/permissions.py`, `src/voidx/agent/graph/contracts.py`
  - 改动:
    - `GraphPermissionHost` Protocol 增加可选 `_ai_approval: AiApprovalService | None` 与 `_settings: Settings | None`。
    - `_ask_and_apply_permission`: 当 `context.permission_mode == "ai_approval"` 且 approvable 非空且 `_ai_approval` 可用时，先调 `review`；allow 的决策移入 approved（带 approved_risk + approved_by=ai metadata），剩余继续人工。
    - `_tool_call_with_approval_risk` 扩展：新增可选 `approved_by` 参数，写入 metadata["approved_by"]。
    - AI allow 后 dock 提示 "AI 审批: allow <tool>"。
    - 注意：AI 过滤在 `AiApprovalService.review` 内部做，显式检查 `risk.level not in {EXTREME, BLOCKED}`，不依赖 action。
  - 测试: `test_graph_ai_approval_allow`, `test_graph_ai_approval_fallback_on_deny`, `test_graph_ai_approval_skips_blocked`, `test_graph_ai_approval_skips_extreme`, `test_graph_ai_approval_no_session_persist`
  - 命令: `./test.py --backend -- src/tests/test_agent/graph/test_graph_authorization.py -v`

- [ ] T6: Gateway settings 往返 `ai_approval`
  - 文件: `src/voidx/ui/gateway/session/method/settings.py`
  - 改动: `_desktop_settings_snapshot` 增加 `ai_approval` 字段（调 `get_ai_approval_config().model_dump()`）；`_method_settings_update` 处理 `patch["ai_approval"]`，async 校验 profile_name（调 `settings.list_profiles()`，不存在则 `MethodParamsError`），通过后调 `set_ai_approval_config`。
  - 测试: `test_settings_snapshot_includes_ai_approval`, `test_settings_update_ai_approval`, `test_settings_update_ai_approval_invalid_profile`
  - 命令: `./test.py --backend -- src/tests/test_ui -v -k ai_approval`

- [ ] T7: 前端设置页支持 AI Approval 模式与审批 profile 选择
  - 文件: `frontend/src/ui/settings.ts`, `frontend/src/ui/model.ts`, `frontend/src/services/state.ts`
  - 改动:
    - `settings.ts`: `PermissionMode` 类型加 `"ai_approval"`；`PERMISSION_MODES` 加配置项（label "AI approval", description "AI 自动审批低中风险操作，高风险仍需人工确认"）；`renderPermissionsTab` 增加 AI 审批 profile 下拉（来自 `snapshot.profiles`）；`collectPermissionsPatch` 收集 `ai_approval.profile_name`。
    - `model.ts`: 权限下拉 `options` 数组增加 ai_approval 项（title "AI 审批", desc "AI 自动审批低中风险，高风险仍需确认", icon 用 shield-svg）；否则用户无法从 composer pill 切换。
    - `state.ts`: `permissionMode` 类型扩展；`updateStatusBar` 的 pill 文案 if-elif 增加 `ai_approval` 分支（text "AI 审批", colorClass "ai-approval"）；否则 pill 显示默认"安全模式"。
  - 测试: `frontend/test/workbench.test.ts` 调整权限 pill 断言；新增 ai_approval 模式渲染测试
  - 命令: `./test.py --frontend -- --reporter=verbose`

- [ ] T8: 回归验证
  - 命令: `./test.py --backend -- src/tests/test_permission -v` + `./test.py --backend -- src/tests/test_agent/graph/test_graph_authorization.py -v` + `./test.py --backend -- src/tests/test_ui -v` + `./test.py --frontend`
  - 预期: 全绿

## 风险与回滚

- 若审批模型调用导致 graph 卡顿，回滚 T5 即可恢复纯人工审批；T1-T4 为纯新增，不影响现有模式。
- `PermissionMode.AI_APPROVAL` 新增值对旧 settings.json 透明（未知值 fallback safe）。
