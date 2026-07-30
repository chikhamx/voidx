# 合并 workspace 外路径审批到单层

## Why

当前 workspace 外路径访问（read/write/replace/manage/lsp_format）可能触发**两次审批**：

1. **permission_flow 层**（`permission_flow.py`）：`authorize_tool_call` → sandbox 返回 `defer` → 转为 `ask` → 弹出 AI 审批或用户审批
2. **工具执行层**（`base.py:_resolve_tool_path_for_access`）：工具执行时再次调用 `resolve_access` → 返回 `defer` → 通过 `ctx.interact()` 弹出第二次用户审批

其中 read 工具在 `workspace-write` 下也会触发外部路径审批：`sandbox_precheck_action` 对 read 使用 `resolve_access(..., access="read")`，workspace 外且未授权时会返回 `defer`。

根因：两层审批之间没有路径级“已授权”状态传递。当前 `approved_risk` 主要服务 bash/powershell 风险审批，不适合表达 read/write 路径授权；路径授权需要精确校验 tool_name、normalized_path 和 access。

## What Changes

**核心思路**：在 permission_flow 层一次性完成 workspace 外路径审批；路径审批写入专用 `access_approval` metadata，executor 将其转成 `ToolContext.approved_access`，工具执行层用 `has_access_approval(tool_name, normalized_path, access)` 跳过重复 `ctx.interact()`。

### 变更内容

1. **engine 层返回路径 intents**：`sandbox_precheck_action` 返回 `AccessIntent` 列表，覆盖 read/write/replace/manage/lsp_format；manage move 这类多路径 tool call 可携带多个 intent
2. **PermissionDecision 携带 intents**：`PermissionDecision.access_intents` 保存本次 tool call 需要审批的 workspace 外路径 intent；兼容单路径 helper 时可提供 `primary_access_intent`
3. **permission_flow 层增加友好 grant 选项**：仅当当前审批批次的外部 intent 总数为 1 时显示 `Allow once / This file this session / This folder this session / Always allow this file / Always allow this folder / Deny`；多个外部 intent 继续使用批量 Yes/No，避免一个 grant choice 误表达多个路径
4. **permission_flow 层处理 grant choice**：用户选择 session/persistent 选项时，基于唯一 intent 调用 `host._permission.add_grant()` 写入对应 read/write grant，并使用 grant lock 避免并发冲突
5. **新增专用 access approval**：路径 decision 审批通过后写入 `tool_call.metadata["access_approval"]`；该字段支持单个 token 或 token 列表，每个 token 包含 `{tool_name, normalized_path, access, approved_by}`
6. **executor 读取 access_approval**：新增 `ApprovedAccess` 和 `ToolContext.approved_access`；executor 将 metadata 转成 approved access token，legacy `approved_risk` 保持只服务 bash/powershell
7. **工具执行层按 access 精确校验**：`_resolve_tool_path_for_access` 新增 `tool_name` 参数，在 `resolve_access` 返回 `defer` 时用 `ctx.has_access_approval(actual_tool_name, str(intent.normalized_path), access)` 判断是否跳过二次审批
8. **批量 Yes 不写 grant 但写 once token**：多 decision 或多路径批量允许时，不写 session/persistent grant，但每个 approved path call 都写入对应 once `access_approval`，确保工具执行层不再二次弹窗
9. **cached path approval 不纳入本次实现**：`approved_by` 仅使用 `user` / `ai`；现有 cached dangerous shell approval 继续使用 `approved_risk`，路径审批缓存另行设计

### 影响层次

| 层次 | 文件 | 变更 |
|------|------|------|
| permission/engine | `engine.py` | `sandbox_precheck_action` 返回 access intents；read/write/replace/manage/lsp_format 统一用 `resolve_access` 产出 intent |
| permission/context | `context.py` | `PermissionDecision` 增加 `access_intents` 字段，可选提供 `primary_access_intent` helper |
| permission_flow | `permission_flow.py` | 按外部 intent 总数决定 grant UX；友好选项文案；grant choice 处理；新增 `_tool_call_with_access_approval` |
| tool executor | `tool_executor/executor.py` | 读取 `access_approval` 到 `ToolContext.approved_access`，不复用 `approved_risk` |
| tools/base | `base.py` | 新增 `ApprovedAccess`、`ToolContext.approved_access`、`has_access_approval()`；`_resolve_tool_path_for_access` 按 tool/path/access 校验 |
| path tools | read/write/replace/manage/lsp_format 调用方 | 调用 `_resolve_tool_path_for_access` 时传入真实工具名 |

## Impact

- **用户体验**：workspace 外路径访问从两次审批减少为一次，单路径场景提供更清晰的授权范围选项
- **安全性**：access approval token 绑定真实工具名、normalized path 和 access；read approval 不能放行 write，write approval 也不能跨工具误放行 replace/manage
- **兼容性**：工具执行层的 `ctx.interact()` 路径保留作为 fallback；`approved_risk` 继续服务 bash/powershell，不改变 shell 审批语义
- **风险**：多路径 manage/move 暂不提供 grant UX，只保留批量 Yes/No；后续如需多路径 grant，需要单独设计 UI

## Non-goals

- 不改变 workspace 内路径的行为（始终 allow）
- 不改变 bash/powershell 的 `approved_risk` 审批流程
- 不重构 session_rules 的 pattern 匹配机制
- 不在本变更中设计多路径 grant UX
- 不在本变更中实现路径 approval 的 cached/session silent allow；持久或 session 路径授权通过 grants 表达

## 测试方案

### 成功路径
- workspace 外文件 read → 只弹一次审批，选择 Allow once → 文件读取成功
- workspace 外文件 write → 只弹一次审批，选择 Allow once → 文件写入成功
- workspace 外文件 replace → 只弹一次审批，选择 This file this session → 同文件再次操作不再审批
- workspace 外文件 write → 选择 Always allow this file → 重启后仍生效
- AI 审批允许 workspace 外路径访问 → 写入 `access_approval`，工具执行层不再弹第二次审批
- 多路径批量 Yes → 每个路径 tool call 写入 once `access_approval`，不写 grant，也不二次弹窗

### 失败路径
- workspace 外文件 read/write → 审批选择 Deny → 工具执行层不执行，不弹第二次
- workspace 外路径含 path traversal（`../`）→ 直接 deny，不弹审批
- `access_approval.access` 不匹配当前访问类型 → 不跳过，走工具执行层 fallback
- `access_approval.tool_name` 不匹配实际工具名 → 不跳过，走工具执行层 fallback

### 边界
- workspace 外目录授权后，该目录下子文件访问不再审批
- 同一轮 LLM response 中多个工具调用操作不同 workspace 外路径 → 不显示 grant 选项，仍只做批量 Yes/No
- manage move 涉及外部 src/dest 多路径 → 不显示 grant 选项，避免单个 grant 误表达多个路径
- lsp_format workspace 外路径 → 按 `FILE_FORMAT` 处理，使用 write access grant 语义

### 回归
- workspace 内文件操作行为不变
- bash/powershell 审批行为不变
- session allow/deny 规则不受影响
