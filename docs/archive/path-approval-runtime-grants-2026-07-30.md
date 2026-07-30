> **Status: Done** — Archived on 2026-07-30. Commit `34dd3886`.

---

# 合并 workspace 外路径审批到单层

## 问题

workspace 外路径访问（read/write/replace/manage/lsp_format）会触发**两次审批**：permission_flow 层弹一次，工具执行层 `resolve_access` 返回 defer 后又弹一次。根因是 permission_flow 层审批后没有写入路径级 grant，工具执行层看不到已授权状态。

## 方案

在 permission_flow 层一次性完成审批，所有路径授权统一用 `AccessGrant` 表达。`Allow once` → `persistence="runtime"` grant；session/persistent 选项 → 对应长期 grant。工具执行层继续只调 `resolve_access()`，看到 grant 后自然返回 `allow`。

### 数据流

```
resolve_access() → AccessResolution(action="defer", intent=AccessIntent(...))
  ↓
sandbox_precheck_action() → ("defer", reason, (intent1, intent2, ...))
  ↓
authorize_tool_call() → _decision(..., access_intents=intents)
  ↓
PermissionDecision(access_intents=(...))
  ↓
_ask_and_apply_permission() → 从 access_intents 构造 grants
  ↓
host._permission.add_grant(AccessGrant(...))
  ↓
工具执行层 resolve_access() → allow（不再弹第二次审批）
```

### 统一路径预检

`workspace-write` 下，路径工具统一用 `resolve_access()` 产出 intent：

| 工具 | access | require_exists | allow_missing_write_file |
|------|--------|----------------|--------------------------|
| read | read | true | false |
| write | write | false | true |
| replace | write | false | true |
| manage create | write | false | true |
| manage delete | write | true | false |
| manage move | write | src=true, dest=false | dest=true |
| lsp_format | write | true | false |
| lsp | read | true | false |

### Grant UX 规则

- **单外部 intent**：显示 `Allow once / This file this session / This folder this session / Always allow this file / Always allow this folder / Deny`
- **多外部 intent**（如 manage move）：只显示 `Allow once / Deny`，避免一个 file/folder grant 误表达多个路径
- **无外部 intent**：保留原有 `Yes / No / Yes, always` 选项

### Grant 映射

| 用户选择 | persistence | object_type |
|----------|-------------|-------------|
| Allow once | runtime | 按 intent |
| This file this session | session | file |
| This folder this session | session | dir |
| Always allow this file | persistent | file |
| Always allow this folder | persistent | dir |
| Deny | — | — |

### Runtime grant 生命周期

- `get_access_grants()` 立即包含 runtime grants（通过 `extra_grants` 合并）
- `execution_lease_for_tool` 的 `finally` 中调用 `_clear_runtime_grants()` 清理
- session/persistent grants 不受影响

### AI approval

AI 允许路径 decision 时，为相关 external intents 写入 runtime grants（`_apply_runtime_grant`，根据 `intent.object_type` 动态选择 file/dir）。

## 改动文件

| 文件 | 变更 |
|------|------|
| `src/voidx/permission/engine.py` | `sandbox_precheck_action` 返回三元组 `(Action, str|None, tuple[AccessIntent, ...])`；read/write/replace/manage/lsp_format/lsp 统一用 `resolve_access()` 产出 intent；`_collect_external_access_intents` 收集外部路径 intent；`_decision` 接受 `access_intents` kwarg |
| `src/voidx/permission/context.py` | `PermissionDecision` 增加 `access_intents` 字段和 `primary_access_intent` property |
| `src/voidx/permission/service.py` | `execution_lease_for_tool` 结束时清理 runtime grants |
| `src/voidx/agent/infrastructure/langgraph/runtime/permission_flow.py` | `_permission_choices` 按 external intent 数量生成友好 grant 选项；`_ask_and_apply_permission` 处理 grant 选项值写入对应 persistence grant；`_apply_runtime_grant` 动态选择 file/dir；AI approval 写 runtime grant |

## 测试

31 个新测试覆盖 7 个文件：
- `test_permission_access_intents.py` — engine 层 intent 生成（6 tests）
- `test_permission_flow_choices.py` — grant 选项 UX（5 tests）
- `test_permission_flow_grants.py` — grant 写入 session/persistent/runtime/deny（4 tests）
- `test_permission_flow_ai_grants.py` — AI approval 写 runtime grant（1 test）
- `test_runtime_grant_lifecycle.py` — runtime grant 可读 + 执行后清理（4 tests）
- `test_tool_exec_with_grants.py` — 工具执行层有 grant 时跳过交互（2 tests）
- `test_manage_lsp_boundary.py` — manage move 多路径 + lsp/lsp_format 边界（9 tests）

验证：`./test.py --backend -- src/tests/test_agent/` → 1308 passed, 0 failed。

## Non-goals

- 不改变 workspace 内路径行为（始终 allow）
- 不改变 bash/powershell 的 `approved_risk` 审批流程
- 不重构 session_rules 的 pattern 匹配
- 不设计多路径 file/folder grant UX
- 不把路径 approval 塞进 tool_call metadata
