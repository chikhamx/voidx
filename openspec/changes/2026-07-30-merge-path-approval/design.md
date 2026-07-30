# 技术设计：合并 workspace 外路径审批到单层

## 请求流（合并后）

```
LLM 返回 tool_call（read/write/replace/manage/lsp_format，workspace 外路径）
  │
  ├─① authorize_tool_call（engine.py）
  │   classify_tool_call → sandbox_precheck_action
  │     → resolve_access 返回一个或多个 AccessIntent
  │   → 返回 PermissionDecision(action="ask", access_intents=(...))
  │
  ├─② _ask_and_apply_permission（permission_flow.py）
  │   ├─ AI 审批模式？→ host._ai_approval.review(candidates)
  │   │   ├─ 通过 → approved, 写 access_approval metadata
  │   │   └─ 未通过 → fallback 到用户审批
  │   └─ 用户审批 → _ask_tool_permission
  │       ├─ 仅当本审批批次外部 AccessIntent 总数为 1 时显示 grant 选项
  │       │   options: [Allow once, This file this session, This folder this session,
  │       │             Always allow this file, Always allow this folder, Deny]
  │       ├─ 用户选择 → _ask_and_apply_permission 处理 choice
  │       │   ├─ "once" → approved, 为每个外部 intent 写 access_approval metadata
  │       │   ├─ "session_file" → add_grant(file, session) + approved + access_approval
  │       │   ├─ "session_dir"  → add_grant(dir, session)  + approved + access_approval
  │       │   ├─ "persistent_file" → add_grant(file, persistent) + approved + access_approval
  │       │   ├─ "persistent_dir"  → add_grant(dir, persistent)  + approved + access_approval
  │       │   └─ "deny" → denied
  │       └─ 返回 approved/denied
  │
  ├─③ executor 提取 access_approval → ctx.approved_access
  │
  └─④ 工具执行（read/write/replace/manage/lsp_format）
      _resolve_tool_path_for_access（base.py）
        → resolve_access 返回 "defer"
        → ctx.has_access_approval(actual_tool_name, normalized_path, access) == True
        → 直接返回路径，不再 ctx.interact()
```

## 数据传递链

### access intents 从 engine 到 permission_flow

```
resolve_access() → AccessResolution(action="defer", intent=AccessIntent(...))
  ↓
sandbox_precheck_action() → ("defer", reason, (intent1, intent2, ...))
  ↓
authorize_tool_call() → _decision(classified, "ask", ..., access_intents=intents)
  ↓
PermissionDecision(access_intents=(...))
  ↓
_ask_and_apply_permission() → 从 access_intents 构造 grant 和 access approval token
```

`read` 在 `workspace-write` 下也走这条链：`sandbox_precheck_action` 对 read 使用 `resolve_access(..., access="read", require_exists=True)`，workspace 外且未授权时返回 `defer` 和 read intent。

### 统一路径预检

`workspace-write` 下，路径工具统一用 `resolve_access()` 产出 intent：

| 工具 | access | require_exists | allow_missing_write_file | 路径来源 |
|------|--------|----------------|--------------------------|----------|
| read | read | true | false | `file_path` |
| write | write | false | true | `file_path` |
| replace | write | false | true | `file_path` |
| manage create | write | false | true | `paths` |
| manage delete | write | true | false | `paths` |
| manage move | write | src=true, dest=false | dest=true | `moves[].src`, `moves[].dest` |
| lsp_format | write | true | false | `file_path` |

当前 manage/lsp_format 只通过 `check_sandbox_filepath()` 返回 defer reason，不会产生 `AccessIntent`；本设计要求改为 `resolve_access()`，否则 grant 和 access approval 无法绑定 normalized path/access。

### access approval 从 permission_flow 到工具执行层

`approved_risk` 当前按 shell 风险建模，字段名和语义都偏命令风险；路径授权需要精确匹配工具名、access 和 normalized path。新增专用 metadata token：

```python
# 单路径允许写 dict；多路径允许写 list[dict]
tool_call.metadata["access_approval"] = {
    "tool_name": decision.name,                 # read / write / replace / manage / lsp_format
    "normalized_path": str(intent.normalized_path),
    "access": intent.access,                    # read / write
    "approved_by": "user" | "ai",
}
```

多路径 tool call（例如 manage move）写入 token 列表：

```python
tool_call.metadata["access_approval"] = [
    {"tool_name": "manage", "normalized_path": str(src_intent.normalized_path), "access": "write", "approved_by": "user"},
    {"tool_name": "manage", "normalized_path": str(dest_intent.normalized_path), "access": "write", "approved_by": "user"},
]
```

executor 读取链路：

```
executor._approved_access_for_call(tc)
  → 读取 metadata["access_approval"] dict 或 list[dict]
  → 转成 list[ApprovedAccess]
  ↓
ctx.approved_access = [...]
  ↓
_resolve_tool_path_for_access
  → resolve_access(...).intent.normalized_path / intent.access
  → ctx.has_access_approval(actual_tool_name, str(normalized_path), access)
```

`approved_risk` 保留给 bash/powershell 风险审批使用；路径审批不再塞进 `ApprovedToolRisk`，避免污染风险语义，也避免 access 字段无法校验。

## 关键设计决策

### 1. sandbox_precheck_action 返回值变更

**当前**：`tuple[Action, str | None]`
**变更后**：`tuple[Action, str | None, tuple[AccessIntent, ...]]`

所有返回分支都补齐第三个元素：
- `allow` / `deny`：返回空 tuple
- workspace 外路径 `defer`：返回一个或多个 unresolved external intents
- read-only sandbox 的非路径型 defer：返回空 tuple

`authorize_tool_call` 和 `sandbox_denial_reason` 同步解包三元组；`authorize_tool_call` 在 defer→ask 分支把 intents 传入 `_decision`。

### 2. PermissionDecision 增加 access_intents 字段

```python
@dataclass(frozen=True)
class PermissionDecision:
    ...
    access_intents: tuple[AccessIntent, ...] = ()

    @property
    def primary_access_intent(self) -> AccessIntent | None:
        return self.access_intents[0] if len(self.access_intents) == 1 else None
```

`_decision(..., access_intents=())` 负责把 intents 写入 `PermissionDecision`。

### 3. 外部 intent 计数决定 grant UX

当前 `_permission_choices(decisions)` 是批量统一选项，而 file/dir grant 是路径级决策。为避免一次选择错误覆盖多个不同路径，grant 选项仅在以下条件全部满足时显示：

- 审批批次中所有 decision 的外部 intents 合计数量为 1
- 唯一 intent 的 `is_workspace_path is False`
- 对应 decision 的 name 在 `{ "read", "write", "replace", "manage", "lsp_format" }`
- intent.access in `{ "read", "write" }`

否则保持现有批量选项：`Allow once`、`Deny`，如果所有 decision 都支持 session scope，则保留 `Allow for this session`。

> 注意：不能只用 `len(decisions) == 1`。`manage move` 可能一个 decision 内包含 src/dest 两个外部 intents，必须禁用 grant UX。

选项文案使用用户可理解的授权范围，value 保持稳定：

```python
verb = "read" if intent.access == "read" else "edit"
choices = [
    ("Allow once", "once", f"Allow this {verb} one time"),
    ("This file this session", "session_file", f"Allow this file for {verb} during this session"),
    ("This folder this session", "session_dir", f"Allow files in this folder for {verb} during this session"),
    ("Always allow this file", "persistent_file", f"Remember this file for {verb}"),
    ("Always allow this folder", "persistent_dir", f"Remember this folder for {verb}"),
    ("Deny", "deny", f"Do not {verb} this file"),
]
```

说明：
- read intent 构造 readable file/dir grant；write/replace/manage/lsp_format intent 构造 writable file/dir grant。
- UI label 避免 “Session dir / Persistent file” 这类内部术语。
- value 使用 `deny`，并兼容旧的 `n` / `no`。

### 4. grant 构造与锁

使用 `grant_for_intent(intent, persistence, object_type=...)` 构造 grant；grant 的 access 来自唯一 external intent：

- `session_file` → `grant_for_intent(intent, "session", object_type="file")`
- `session_dir` → `grant_for_intent(intent, "session", object_type="dir")`
- `persistent_file` → `grant_for_intent(intent, "persistent", object_type="file")`
- `persistent_dir` → `grant_for_intent(intent, "persistent", object_type="dir")`

permission_flow 层写 grant 时复用 permission service 的 grant lock，并遵循工具执行层已有的两阶段目标策略：

```python
lock = await host._permission.acquire_grant_targets(
    [intent.normalized_path],
    final_paths=[grant.path],
)
try:
    access_grants = host._permission.get_access_grants()
    precondition = _approval_precondition(access_grants)
    # 重新 resolve，避免锁等待期间 grant/revoke 状态变化
    resolution = resolve_access(..., access_grants=access_grants, ...)
    if resolution.action == "allow":
        approve without add_grant
    else:
        await host._permission.add_grant(grant, precondition=precondition)
finally:
    await lock.release()
```

如果 permission_flow 写入 grant 后工具执行层再次 resolve，正常应返回 `allow`；`access_approval` 仍作为 once 审批和兼容 fallback 的跳过凭证。

### 5. 批量允许时的 once token

多 decision 或多路径审批不显示 grant 选项，但用户选择批量 `Allow once` 后，permission_flow MUST 为每个 approved path decision 写入对应 `access_approval`：

- 单路径 decision：写入 dict token
- 多路径 decision：写入 list token
- 无 external intent 的非路径 decision：不写 access_approval，沿用现有审批逻辑

这样批量 Yes 不会写长期 grant，但工具执行层仍能识别本次审批，避免第二次 `ctx.interact()`。

### 6. 工具执行层跳过逻辑

新增专用模型和上下文方法：

```python
class ApprovedAccess(BaseModel):
    tool_name: str
    normalized_path: str
    access: Literal["read", "write"]
    approved_by: Literal["user", "ai"] = "user"

class ToolContext(BaseModel):
    approved_access: list[ApprovedAccess] = Field(default_factory=list)

    def has_access_approval(self, tool_name: str, normalized_path: str, access: str) -> bool:
        return any(
            item.tool_name == tool_name
            and item.normalized_path == normalized_path
            and item.access == access
            for item in self.approved_access
        )
```

`_resolve_tool_path_for_access` 需要知道真实工具名：

```python
async def _resolve_tool_path_for_access(..., tool_name: str | None = None):
    access = "write" if write else "read"
    actual_tool_name = tool_name or access
    ...
    if resolution.action == "defer" and resolution.intent is not None:
        normalized = str(resolution.intent.normalized_path)
        if ctx.has_access_approval(actual_tool_name, normalized, access):
            return resolution.intent.normalized_path, None
```

各调用方传入：
- read 工具：`tool_name="read"`
- write 工具：`tool_name="write"`
- replace 工具：`tool_name="replace"`
- manage 工具：`tool_name="manage"`
- lsp_format 工具：`tool_name="lsp_format"`

不要使用 `"edit"` 作为 tool_name；`edit` 是 permission rule 的抽象权限名，不是 approved token 的工具名。

### 7. access approval 写入点

新增 helper：

```python
def _tool_call_with_access_approval(decision: PermissionDecision, *, approved_by: Literal["user", "ai"] = "user") -> dict:
    if not decision.access_intents:
        return decision.tool_call
    tokens = [
        {
            "tool_name": decision.name,
            "normalized_path": str(intent.normalized_path),
            "access": intent.access,
            "approved_by": approved_by,
        }
        for intent in decision.access_intents
        if not intent.is_workspace_path
    ]
    if not tokens:
        return decision.tool_call
    metadata = dict(decision.tool_call.get("metadata") or {})
    metadata["access_approval"] = tokens[0] if len(tokens) == 1 else tokens
    return {**decision.tool_call, "metadata": metadata}
```

使用位置：
- AI 审批通过：路径 decision 用 `_tool_call_with_access_approval(..., approved_by="ai")`，shell decision 继续用 `_tool_call_with_approval_risk`
- 用户选择 `once` / grant / 批量 allow once：路径 decision 用 `_tool_call_with_access_approval(..., approved_by="user")`
- cached approval：本变更不支持路径 approval cached；现有 dangerous shell cached 分支继续使用 `_tool_call_with_approval_risk(..., approved_by="cached")`

`_tool_call_with_execution_approval` 不承担路径工具 metadata 写入职责；它继续服务 bash/powershell 的执行期风险授权。

### 8. manage 与 lsp_format 范围

- `manage create/delete/move` 都按 `FILE_WRITE` 处理；`move` 的 `src` 和 `dest` 都会经 `file_paths_for_tool` 枚举并触发 sandbox 预检。
- 如果一个 `manage move` 同时涉及多个 workspace 外路径，grant 选项不显示，使用现有批量 Yes/No；批量允许后写入 list `access_approval`，确保 src/dest 执行层都不二次审批。
- `lsp_format` 按 `FILE_FORMAT` 处理，workspace 外路径也可使用同一 access approval token；grant 本质仍是 write access grant。

## TDD 测试清单

### engine 层

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | sandbox_precheck_action 返回 write intent | workspace 外 write 路径 | 返回 `(defer, reason, (intent,))` 且 `intent.access="write"` |
| 2 | sandbox_precheck_action 返回 read intent | workspace 外 read 路径 | 返回 `(defer, reason, (intent,))` 且 `intent.access="read"` |
| 3 | sandbox_precheck_action 返回 manage 多 intent | manage move 外部 src/dest | 返回两个 write intents |
| 4 | sandbox_precheck_action 返回 lsp_format intent | workspace 外 lsp_format | 返回 write intent |
| 5 | sandbox_precheck_action workspace 内不返回 intent | workspace 内 write 路径 | 返回 `(allow, None, ())` |
| 6 | PermissionDecision 携带 access_intents | defer → ask decision | `decision.access_intents` 非空 |
| 7 | sandbox_denial_reason 兼容三元组 | path traversal | 返回 deny reason，不因解包失败崩溃 |

### permission_flow 层

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 8 | 单个 workspace 外读路径生成友好 grant 选项 | one read decision with one external intent | choices 包含 `Allow once` / `This file this session` / `Always allow this folder` |
| 9 | 单个 workspace 外写路径生成友好 grant 选项 | one write decision with one external intent | choices 包含相同友好 label，description 使用 edit 语义 |
| 10 | 单个 decision 但多个 external intents 不生成 grant 选项 | manage move external src/dest | choices 保持批量 Yes/No |
| 11 | 多个 decision 不生成 grant 选项 | two external path decisions | choices 保持批量 Yes/No |
| 12 | workspace 内路径不生成 grant 选项 | decision with no external intents | choices 保持原选项 |
| 13 | choice=session_file 调用 add_grant | mock permission service | add_grant 被调用，access 来自 intent，persistence=session, object_type=file |
| 14 | choice=persistent_dir 调用 add_grant | mock permission service | add_grant 被调用，access 来自 intent，persistence=persistent, object_type=dir |
| 15 | choice=once 不调用 add_grant | mock permission service | add_grant 未调用，写入 access_approval |
| 16 | 批量 allow once 为每个路径 decision 写 token | two path decisions or manage move | 不写 grant；每个 external intent 都有 access_approval |
| 17 | choice=deny 返回 denied | choice="deny" | denied 列表包含该 tool_call，且不进入工具执行 |
| 18 | AI allow 写入 access_approval | AI approval 允许 path decision | metadata["access_approval"] 存在，approved_by="ai" |

### tool 执行层

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 19 | access_approval dict 被 executor 转为 ApprovedAccess | metadata["access_approval"] dict | `ctx.approved_access[0].normalized_path == normalized_path` |
| 20 | access_approval list 被 executor 转为多个 ApprovedAccess | manage move metadata list | `ctx.approved_access` 含 src/dest 两个 token |
| 21 | 已授权 read 跳过 interact | tool_name=read, path/access 匹配 | 不调用 ctx.interact，返回路径 |
| 22 | 已授权 write 跳过 interact | tool_name=write, path/access 匹配 | 不调用 ctx.interact，返回路径 |
| 23 | access 不匹配不跳过 | approved read，执行 write | fallback 调用 ctx.interact |
| 24 | tool_name 不匹配不跳过 | approved write，执行 replace | fallback 调用 ctx.interact |
| 25 | 未授权时保留 fallback | 无 access_approval | 调用 ctx.interact |
| 26 | workspace 内路径不受影响 | resolve_access 返回 allow | 直接返回路径 |

### 集成

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 27 | 端到端：workspace 外 read 只弹一次审批 | 用户选择 Allow once | `_ask_tool_permission` 调用 1 次，`ctx.interact` 调用 0 次 |
| 28 | 端到端：workspace 外 write 只弹一次审批 | 用户选择 Allow once | `_ask_tool_permission` 调用 1 次，`ctx.interact` 调用 0 次 |
| 29 | 端到端：session_file 授权后同文件不再审批 | 第一次 This file this session，第二次同文件 | 第二次无需用户审批 |
| 30 | manage move 批量 allow 不显示 grant 且不二次审批 | manage move 外部 src/dest | 保持批量 Yes/No，执行层 src/dest 都不调用 interact |
| 31 | 回归：workspace 内文件行为不变 | workspace 内 read/write/replace/manage | 不审批 |
| 32 | 回归：bash/powershell 行为不变 | dangerous shell | 继续使用 approved_risk |
