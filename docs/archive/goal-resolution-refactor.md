# GoalResolution 重构 — 技术设计文档

> **Status: Done**

## Context

当前 `GoalResolution` 返回 6 个扁平字段（`intent`, `goal`, `confidence`, `reason`, `workflow_start`, `workflow_end`），存在以下问题：

0. **命名不当**：`workflow_start`/`workflow_end` 暗示时间顺序，实际语义是"加入哪个节点"和"离开哪个节点后停止"，应改为 `join`/`leave`
1. **结构冗余**：`confidence` 无消费点，`reason` 的信息可由各对象 `desc` 承载
2. **职责越界**：`user_requested_write` / `needs_confirmation` 完全多余，workflow 节点本身已明确写操作和确认需求，不需要额外字段
3. **命名模糊**：`goal.target` / `goal.expected_result` 语义重叠，不如单一 `desc` 清晰
4. **结构割裂**：`goal` 和 `plan` 分离为两个独立对象，但语义上 goal 和 plan 是绑定关系——有 goal 必有 plan（join），不存在有 goal 无 plan 或有 plan 无 goal 的情况；`workflow_start`/`workflow_end` 应归入 `plan` 并重命名为 `join`/`leave`
5. **fallback 冗余**：`resolve_turn_intent` 做了一套基于规则的 intent 分类，但 LLM 调用失败时直接默认 intent=general 即可，不需要额外分类逻辑

## Goals and Non-Goals

### Goals

- 简化 `GoalResolution` 为 `intent` / `goal` / `plan` 三个顶层字段，goal 与 plan 绑定（有 goal 必有 plan）
- 移除 `user_requested_write` / `needs_confirmation`，不再需要推断，workflow 节点本身已明确
- 移除 `resolve_turn_intent`，LLM 调用失败时 intent 默认 fallback 到 general；其承载的审批确认、plan mode、goal mode 等场景逻辑迁移至 `_normalize_resolution`（见"resolve_turn_intent 移除"章节）
- 修复节点 `done` 后下游不级联 skip 的 bug
- 验证 LLM 没选 `workflow_start` 时 trigger 匹配已正确处理（主循环走 `select_from_start`，subagent 走 `service.select()`）
- 明确 `workflow_route` 对自动推进的终止作用

### Non-Goals

- 不重构 DAG 边定义和节点结构
- 不改变 subagent 的 workflow 激活逻辑（subagent 无 `workflow_start`，仍走 `service.select()`）

## Architecture

### 当前结构

```
GoalResolution (6 扁平字段)
├── intent: TaskIntent (coding|general)
├── goal: Goal | None
│   ├── type: GoalType
│   ├── target: str
│   ├── expected_result: str
│   ├── user_requested_write: bool
│   └── needs_confirmation: bool
├── confidence: float
├── reason: str
├── workflow_start: str | None
└── workflow_end: str | None
```

### 目标结构

```
GoalResolution (3 顶层字段)
├── intent: IntentResolution
│   ├── type: coding | general
│   └── desc: str
├── goal: GoalSpec | None
│   ├── type: bugfix | debug | refactor | feature | chore | inspect | design | doc | review
│   └── desc: str
└── plan: PlanResolution | None
    ├── join: str          (必填，进入哪个工作流节点)
    └── leave: str | None  (可选，离开哪个节点后停止自动推进)
```

**核心约束：goal 和 plan 绑定，有 goal 必有 plan（join）。**
- `goal` 为 null → `plan` 也为 null，无工作流，agent 自由发挥
- `goal` 非 null → `plan` 非 null，`plan.join` 必填，`plan.leave` 可选
- 不存在有 goal 无 plan 或有 plan 无 goal 的情况

### 数据流

```
用户输入
  │
  ▼
resolve_goal_for_turn()
  │ LLM 返回 GoalResolution { intent, goal, plan }
  │ LLM 失败 → intent=general, goal=null, plan=null
  │
  ▼
_normalize_resolution()
  │ 校验 join/leave 是否在 DAG 中
  │ plan mode: 强制 goal.type=design, plan.join=brainstorm
  │ goal mode: 保持 current_goal 不变
  │
  ▼
task_state.update_after_turn()
  │ 写入 current_intent, current_goal, workflow_route
  │
  ▼
reconcile_workflow_runs_for_turn()
  │ 用 plan.join 激活初始节点
  │
  ▼
workflow_context_for()
  │ 有 plan.join → select_from_start()
  │ 无 plan → 不注入工作流
  │
  ▼
advance_workflow() (轮内)
  │ 节点完成时选择 exit condition
  │ done → 级联 skip 下游 active 节点
  │ 非 done → 激活下游节点
```

## Data Model

### 新增类型

```
IntentResolution
├── type: TaskIntent (coding|general)
└── desc: str

GoalSpec (原 Goal 简化，重命名以避免与顶层 GoalResolution 冲突)
├── type: GoalType
└── desc: str

PlanResolution
├── join: str          (必填)
└── leave: str | None  (可选)
```

### IntentResolution 别名迁移

当前 `IntentResolution = GoalResolution`（task_state.py:271）是 `GoalResolution` 的别名，两者完全等价。重构后 `IntentResolution` 变为独立类型 `{type, desc}`，不再是 `GoalResolution` 的别名。

**迁移步骤**：
1. 删除 `IntentResolution = GoalResolution` 别名定义
2. 新增 `IntentResolution` 独立 Pydantic 模型（`type: TaskIntent`, `desc: str`）
3. 搜索所有 `IntentResolution` 的消费点，将 `GoalResolution` 用法改为 `IntentResolution` 独立用法

**消费点迁移**：

| 文件 | 当前用法 | 迁移后 |
|------|---------|--------|
| `task_state.py:271` | `IntentResolution = GoalResolution` | 删除别名，新增独立模型 |
| `runtime/__init__.py` | 导出 `IntentResolution` | 不变（仍导出，但类型定义变了） |
| `goal_resolver.py` | 不直接使用 `IntentResolution` | 通过 `GoalResolution.intent` 间接使用 |
| `runtime_context.py` | 渲染 `intent` 字段 | 改为渲染 `intent.type` + `intent.desc` |

### 字段映射

| 旧字段 | 新位置 | 说明 |
|--------|--------|------|
| `intent` | `intent.type` | 不变 |
| `reason` | `intent.desc` | 语义更清晰 |
| `goal.type` | `goal.type` | 不变 |
| `goal.target` | `goal.desc` | 合并 |
| `goal.expected_result` | `goal.desc` | 合并 |
| `goal.user_requested_write` | 删除 | workflow 节点已明确写操作 |
| `goal.needs_confirmation` | 删除 | workflow 节点已明确确认需求 |
| `confidence` | 删除 | 无消费点 |
| `workflow_start` | `plan.join` | 归入 plan，重命名，必填 |
| `workflow_end` | `plan.leave` | 归入 plan，重命名，可选 |
| `resolve_turn_intent` | 删除 | LLM 失败时 intent 默认 fallback 到 general；场景逻辑迁移至 `_normalize_resolution` |

## API Contract

### resolver_goal 提示词

**System Message**（简化后）：

```
You are voidx resolving the current user's goal before normal work begins.
Return only structured data matching the GoalResolution schema.

Rules:
- intent.type=general only for non-code, non-workspace conversation.
- intent.type=coding for codebase inspection, design, docs, review, debugging, or edits.
- Pick exactly one goal.type when intent is coding and a concrete workspace goal exists.
- plan.join is the workflow node the agent should enter. Required when goal is set; null when goal is null.
- plan.leave is the workflow node after which automatic progression stops. Optional.
- Available join values: debug, brainstorm, design-doc, plan, tdd, review, feedback.
- Choose join based on the user's primary intent:
  - debug: user reports a bug, error, crash, or unexpected behavior to investigate.
  - brainstorm: user wants to explore requirements, design a feature, or discuss approach before coding.
  - design-doc: user asks to write or revise a design/spec/PRD/RFC/API doc.
  - plan: user asks to turn a spec or requirements into an implementation plan.
  - tdd: user explicitly asks to implement an already detailed spec or continue an approved implementation.
  - review: user asks for code review or pre-merge review.
  - feedback: user provides review feedback or reviewer comments to act on.
- If the user's intent does not clearly match any join value, set goal to null and plan to null. The agent will work without workflow constraints.
- Do not choose brainstorm when the request already contains an approved or sufficiently detailed spec.
- Do not set join or leave based on vague or ambiguous approval.
- goal and plan are bound: if goal is set, plan must be set with join; if goal is null, plan must be null.
GoalResolution JSON schema:
{schema}
```

**Human Message**（不变）：

```json
{
  "workspace": "/path/to/workspace",
  "session_time": "2026-06-15 CST",
  "interaction_mode": "auto|plan|goal",
  "current_intent": "coding|general",
  "current_goal": { "type": "...", "desc": "..." },
  "pending_approval": null,
  "recent_user_texts": ["prev1", "prev2"],
  "latest_user_text": "current user input"
}
```

### LLM 返回结构

有工作流时：

```json
{
  "intent": {
    "type": "coding",
    "desc": "user wants to review code before merge"
  },
  "goal": {
    "type": "review",
    "desc": "review the auth module changes"
  },
  "plan": {
    "join": "review",
    "leave": "review"
  }
}
```

无工作流时（goal 为 null）：

```json
{
  "intent": {
    "type": "coding",
    "desc": "user is asking a question about the codebase"
  },
  "goal": null,
  "plan": null
}
```

## resolve_turn_intent 移除

`resolve_turn_intent` 是一套基于规则的 intent 分类函数，用于 LLM 调用失败时的 fallback。移除后：
- LLM 调用失败 → intent 默认 `general`，goal=null，plan=null
- 不再需要 `goal_from_text`、`resolve_turn_intent` 及相关测试
- `goal_resolver.py` 中 `fallback = resolve_turn_intent(...)` 替换为直接构造 `GoalResolution(intent=IntentResolution(type=TaskIntent.GENERAL, desc=""), goal=None, plan=None)`

### 场景迁移

当前 `resolve_turn_intent` 承载了以下场景逻辑，删除后需迁移至 `_normalize_resolution`：

| 场景 | 当前逻辑（resolve_turn_intent） | 迁移后（_normalize_resolution） |
|------|-------------------------------|-------------------------------|
| plan mode 强制 coding | `mode==PLAN → intent=CODING` | `mode==PLAN → intent.type=CODING, goal.type=design, plan.join=brainstorm` |
| goal mode 保持当前 goal | `mode==GOAL + current_goal → intent=CODING` | `mode==GOAL + current_goal → intent.type=CODING, goal=GoalSpec(type=current_goal.type, desc=current_goal.desc)` |
| 审批确认（"对，可以"） | `_is_approval_only + pending_approval → intent=CODING, goal=feature` | 删除：`pending_approval` 机制整体移除（见 PendingApproval 章节），审批确认由 workflow gate 驱动 |
| 短命令写操作 | `_is_direct_write_command → intent=CODING` | 删除：LLM 已能识别短命令写操作，无需规则兜底 |

### 消费点迁移

| 消费点 | 当前逻辑 | 迁移后 |
|--------|---------|--------|
| `goal_resolver.py` fallback | `resolve_turn_intent(user_text, mode, state)` | 直接构造 intent=general, goal=null, plan=null |
| `goal_from_text` | 规则分类 goal type | 删除 |
| `resolve_turn_intent` 测试 | 多个测试文件 | 删除 |

## user_requested_write / needs_confirmation 移除

这两个字段完全删除，不再由 LLM 返回也不由 runtime 推断。原因：
- `user_requested_write`：workflow 节点本身已明确写操作（tdd/debug/feedback = 写，review/brainstorm/plan = 不写）
- `needs_confirmation`：workflow 节点的 gate 机制已处理确认需求（brainstorm 的 gate 要求用户批准，design-doc 的 gate 要求 reader test）

### 消费点迁移

| 消费点 | 当前逻辑 | 迁移后 |
|--------|---------|--------|
| `_next_pending_approval` | `goal.needs_confirmation` | 删除函数及 `PendingApproval` 模型（见下方） |
| `default_workflow_end_for_goal` | `goal.user_requested_write` | 删除函数，由 `plan.leave` 直接指定（见下方） |
| `_workflow_start_for_goal` | 按 `goal.type` 推断 `workflow_start` | 删除函数，由 `plan.join` 直接指定（见下方） |
| `_has_explicit_write_intent` (reconcile) | `goal.user_requested_write` | 从 `plan.join` 直接判断（见下方） |
| `_resolve_intent_override` (reconcile) | `goal.user_requested_write` | 从 `plan.join` 直接判断 |
| `runtime_context` 提示 | `goal.user_requested_write` / `goal.needs_confirmation` | 删除，替换渲染代码（见下方） |
| `_copy_goal` | 依赖 `user_requested_write` / `needs_confirmation` | 删除函数（见下方） |

### PendingApproval 完整处理方式

删除 `needs_confirmation` 后，`_next_pending_approval` 函数无输入条件，整个审批确认机制需重新处理：

- **删除**：`PendingApproval` 模型、`TaskState.pending_approval` 字段、`_next_pending_approval` 函数
- **删除**：`resolve_turn_intent` 中的 `_is_approval_only` 分支（审批确认路径）
- **替代方案**：审批确认由 workflow gate 驱动。brainstorm 节点的 gate 要求"design approved by user"，design-doc 节点的 gate 要求"doc passes reader test"——这些 gate 本身就是确认机制，不需要额外的 `pending_approval` 状态
- **影响文件**：`task_state.py`（删除 `PendingApproval`、`pending_approval` 字段、`_next_pending_approval`）、`goal_resolver.py`（删除 `_is_approval_only` 分支）、`core.py`（删除 `pending_approval` 传递）

### _workflow_start_for_goal 处理方式

当前 `_workflow_start_for_goal`（task_state.py:420-433）在 LLM 没返回 `workflow_start` 时，按 `goal.type` 推断 start：

```python
def _workflow_start_for_goal(goal: Goal | None) -> str:
    if goal is None:
        return ""
    return {
        GoalType.BUGFIX: "debug", GoalType.DEBUG: "debug",
        GoalType.REFACTOR: "brainstorm", GoalType.FEATURE: "brainstorm",
        GoalType.DESIGN: "brainstorm", GoalType.DOC: "design-doc",
        GoalType.REVIEW: "review", GoalType.CHORE: "tdd",
        GoalType.INSPECT: "",
    }.get(goal.type, "")
```

新结构下 `plan.join` 由 LLM 直接返回，不再需要此 fallback：

- **删除 `_workflow_start_for_goal` 函数**
- **`_normalize_resolution` 中补充默认规则**：当 LLM 返回了 `goal` 但 `plan.join` 为空时，按 goal.type 推断默认 join（逻辑与当前 `_workflow_start_for_goal` 相同，但作为 normalize 步骤而非独立函数）
- **`_workflow_route_from_resolution` 中**：不再调用 `_workflow_start_for_goal`，直接使用 `plan.join`

```python
def _default_join_for_goal_type(goal_type: GoalType) -> str:
    return {
        GoalType.BUGFIX: "debug", GoalType.DEBUG: "debug",
        GoalType.REFACTOR: "brainstorm", GoalType.FEATURE: "brainstorm",
        GoalType.DESIGN: "brainstorm", GoalType.DOC: "design-doc",
        GoalType.REVIEW: "review", GoalType.CHORE: "tdd",
        GoalType.INSPECT: "",
    }.get(goal_type, "")
```

### default_workflow_end_for_goal 处理方式

当前逻辑依赖 `goal.user_requested_write`：
- `review + user_requested_write → end=verify`
- `tdd + user_requested_write → end=verify`
- 否则 `end=start`

删除 `user_requested_write` 后的新方案：

- **删除 `default_workflow_end_for_goal` 函数**
- **由 LLM 在 `plan.leave` 中直接指定**：提示词已包含 `plan.leave` 的语义说明（"离开哪个节点后停止自动推进"），LLM 可根据 goal type 和用户意图判断是否需要 verify 终态
- **`_normalize_resolution` 中补充默认规则**：当 LLM 返回了 `plan.join` 但未返回 `plan.leave` 时，按 goal.type 推断默认 leave：
  - `goal.type ∈ {bugfix, debug, refactor, feature, chore}` → `leave=verify`（写操作类 goal 需验证终态）
  - `goal.type ∈ {design, doc, review, inspect}` → `leave=join`（非写操作类 goal 到 join 节点即止）
- **`_workflow_route_from_resolution` 中**：`end = plan.leave`，不再调用 `default_workflow_end_for_goal`

### _normalize_resolution 中 plan mode 处理

当前 `_normalize_resolution`（goal_resolver.py:166-181）在 plan mode 下强制设置 `needs_confirmation=True` 和 `user_requested_write=False`。删除这两个字段后：

```python
# _normalize_resolution 中 plan mode 分支
if mode == InteractionMode.PLAN:
    goal = GoalSpec(
        type=GoalType.DESIGN,
        desc=user_text if resolution.goal is None else resolution.goal.desc,
    )
    plan = PlanResolution(
        join="brainstorm",  # plan mode 强制从 brainstorm 开始
        leave=resolution.plan.leave if resolution.plan else None,
    )
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="plan mode forces design goal"),
        goal=goal,
        plan=plan,
    )
```

关键变化：
- `needs_confirmation=True` → 不再需要，brainstorm 节点的 gate 已要求用户批准
- `user_requested_write=False` → 不再需要，brainstorm 节点本身不涉及写操作
- `goal_type=DESIGN` → 保留，plan mode 强制 design goal
- `plan.join=brainstorm` → 新增，plan mode 强制从 brainstorm 开始

### _has_explicit_write_intent 迁移

当前 `_has_explicit_write_intent`（reconcile.py:145-150）依赖 `goal.user_requested_write`：

```python
def _has_explicit_write_intent(
    goal_resolution: GoalResolution,
    after_state: TaskState,
) -> bool:
    goal = goal_resolution.goal or after_state.current_goal
    return bool(goal is not None and goal.user_requested_write)
```

删除 `user_requested_write` 后，从 `plan.join` 直接判断：

```python
_WRITE_INTENT_JOINS = {"tdd", "debug", "feedback"}

def _has_explicit_write_intent(
    goal_resolution: GoalResolution,
    after_state: TaskState,
) -> bool:
    plan = goal_resolution.plan
    if plan is not None and plan.join in _WRITE_INTENT_JOINS:
        return True
    goal = goal_resolution.goal or after_state.current_goal
    if goal is not None and goal.type in {GoalType.BUGFIX, GoalType.DEBUG, GoalType.CHORE}:
        return True
    return False
```

判断逻辑：`plan.join ∈ {tdd, debug, feedback}` = 写操作节点。这些节点本身涉及代码修改，等价于 `user_requested_write=True`。

### WorkflowRoute 结构更新

当前 `WorkflowRoute(start=str, end=str)` 需同步修改：

- **字段重命名**：`start` → `join`，`end` → `leave`
- **`leave` 改为可选**：`leave: str | None = None`（原 `end` 必填，新结构下 `plan.leave` 可选）
- **`_route_target` 迁移**：`reconcile.py:226` 中 `_route_target` 读 `goal_resolution.workflow_start`，改为读 `goal_resolution.plan.join`
- **`_workflow_route_from_resolution` 迁移**：`task_state.py:405-417` 中构造 `WorkflowRoute(start=..., end=...)`，改为 `WorkflowRoute(join=plan.join, leave=plan.leave)`
- **数据库持久化**：`memory/runtime_state.py` 中 `workflow_route_json` 列存储的是 `WorkflowRoute` 的 JSON，字段重命名后需数据库迁移（见持久化章节）

### ToolStatePatch 更新

当前 `ToolStatePatch`（task_state.py:274-281）：

```python
class ToolStatePatch(BaseModel):
    task_intent: TaskIntent | None = None
    goal: Goal | None = None
    pending_approval: PendingApproval | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)
```

重构后：

```python
class ToolStatePatch(BaseModel):
    intent: IntentResolution | None = None
    goal: GoalSpec | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)
```

变更：
- `task_intent: TaskIntent | None` → `intent: IntentResolution | None`（类型升级，含 desc）
- `goal: Goal | None` → `goal: GoalSpec | None`（类型重命名）
- `pending_approval: PendingApproval | None` → 删除

### runtime_context.py 消费点

当前 `_current_task_state()`（runtime_context.py:438-473）渲染了 `user_requested_write`、`needs_confirmation`、`pending_approval`、`goal.target`、`goal.expected_result`。

替换后的渲染代码：

```python
# 旧
if goal:
    lines.append(f"- Goal type: {goal.type.value}")
    lines.append(f"- Goal target: {goal.target}")
    if goal.expected_result:
        lines.append(f"- Expected result: {goal.expected_result}")
    lines.append(f"- User requested write: {goal.user_requested_write}")
    lines.append(f"- Needs confirmation: {goal.needs_confirmation}")
if state.pending_approval:
    lines.append(f"- Pending approval: {state.pending_approval.scope}")

# 新
if goal:
    lines.append(f"- Goal type: {goal.type.value}")
    lines.append(f"- Goal: {goal.desc}")
```

删除的渲染项：
- `goal.target` + `goal.expected_result` → 合并为 `goal.desc`
- `goal.user_requested_write` → 删除（workflow 节点已明确）
- `goal.needs_confirmation` → 删除（workflow gate 已覆盖）
- `pending_approval` → 删除（整个机制移除）

### memory/runtime_state.py 持久化

`pending_approval_json` 和 `workflow_route_json` 列存在于数据库中。

**数据库迁移方案**：

1. **`pending_approval_json` 列**：删除。由于 `PendingApproval` 模型整体删除，此列不再需要。迁移脚本中 `ALTER TABLE DROP COLUMN pending_approval_json`。
2. **`workflow_route_json` 列**：保留，但字段名从 `{start, end}` 变为 `{join, leave}`。由于 Pydantic 模型字段已重命名，反序列化时 `start`/`end` 会被忽略，`join`/`leave` 正常读取。迁移脚本只需确保新数据写入 `join`/`leave`，旧数据在下次写入时自动更新格式。
3. **迁移脚本位置**：`src/voidx/memory/migrations/`，版本号递增。

### _copy_goal 函数处理

当前 `_copy_goal`（goal_resolver.py:207-223）依赖 `user_requested_write` / `needs_confirmation`：

```python
def _copy_goal(
    goal: Goal | None,
    *,
    fallback_text: str,
    goal_type: GoalType,
    user_requested_write: bool,
    needs_confirmation: bool,
) -> Goal:
    target = goal.target if goal is not None and goal.target.strip() else fallback_text
    expected_result = goal.expected_result if goal is not None else ""
    return goal_from_text(
        target,
        goal_type=goal_type,
        user_requested_write=user_requested_write,
        needs_confirmation=needs_confirmation,
        expected_result=expected_result,
    )
```

重构后删除此函数。`_normalize_resolution` 中 plan mode 分支直接构造 `GoalSpec`：

```python
# 旧（_normalize_resolution 中调用 _copy_goal）
goal = _copy_goal(
    resolution.goal,
    fallback_text=user_text,
    goal_type=GoalType.DESIGN,
    user_requested_write=False,
    needs_confirmation=True,
)

# 新（直接构造 GoalSpec）
goal = GoalSpec(
    type=GoalType.DESIGN,
    desc=resolution.goal.desc if resolution.goal is not None and resolution.goal.desc.strip() else user_text,
)
```

### _normalize_resolution 完整代码

```python
def _normalize_resolution(
    resolution: GoalResolution,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)

    # 校验 join/leave 是否在 DAG 中
    plan = resolution.plan
    if plan is not None:
        if plan.join and plan.join not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = None  # 无效 join，整个 plan 作废
        elif plan.leave and plan.leave not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = PlanResolution(join=plan.join, leave=None)  # 无效 leave，保留 join

    # plan mode: 强制 design goal + brainstorm
    if mode == InteractionMode.PLAN:
        goal = GoalSpec(
            type=GoalType.DESIGN,
            desc=resolution.goal.desc if resolution.goal is not None and resolution.goal.desc.strip() else user_text,
        )
        plan = PlanResolution(
            join="brainstorm",
            leave=plan.leave if plan is not None else None,
        )
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="plan mode forces design goal"),
            goal=goal,
            plan=plan,
        )

    # goal mode: 保持 current_goal 不变
    if mode == InteractionMode.GOAL and task_state.current_goal is not None:
        current = task_state.current_goal
        goal = GoalSpec(type=current.type, desc=current.desc)
        if plan is None:
            plan = PlanResolution(
                join=_default_join_for_goal_type(current.type),
                leave=None,
            )
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="goal mode keeps the turn scoped to the current goal"),
            goal=goal,
            plan=plan,
        )

    # general intent: 无 goal 无 plan
    if resolution.intent.type == TaskIntent.GENERAL:
        return GoalResolution(
            intent=resolution.intent,
            goal=None,
            plan=None,
        )

    # coding intent: 补充默认 join/leave
    goal = resolution.goal
    if goal is not None:
        if plan is None:
            plan = PlanResolution(join=_default_join_for_goal_type(goal.type), leave=None)
        if not plan.join:
            plan.join = _default_join_for_goal_type(goal.type)
        if not plan.leave:
            plan.leave = _default_leave_for_goal_type(goal.type)

    return GoalResolution(
        intent=resolution.intent,
        goal=goal,
        plan=plan,
    )
```

### goal_map 中 inspect 补充

当前 DAG 的 `goal_map`（dag.py:30-38）没有 `GoalEntry(goal_type="inspect", ...)`，但 `GoalType` 有 `INSPECT`。

补充：

```python
GoalEntry(goal_type="inspect", nodes=[], reason="goal:inspect"),
```

`inspect` 的 `plan.join` 为空——inspect 是纯观察/查询，不进入任何工作流节点。`_default_join_for_goal_type` 中 `GoalType.INSPECT` 返回 `""`，与当前 `_workflow_start_for_goal` 行为一致。

## Bug 修复

### Bug 1: 节点 done 后下游不级联 skip

**根因**：`advance_workflow_states` 中，节点以 `done`（terminal condition）退出时，`_activate_transition_targets` 返回空列表，但不会 skip 已存在的下游 active 节点。

**修复**：在 `advance_workflow_states` 中，当节点以 terminal condition 退出时，级联 skip 所有"仅由此节点可达且仍为 ACTIVE"的下游节点。

```python
# runtime.py — advance_workflow_states 中 SATISFIED 分支
if event.kind == WorkflowStateEventKind.SATISFIED:
    run.status = WorkflowRunStatus.SATISFIED
    run.blocked_reason = ""
    if is_workflow_terminal_condition(condition):
        _cascade_skip_downstream(states, run, turn_count=turn_count)
    else:
        _activate_transition_targets(states, run, turn_count=turn_count, condition=condition)
```

```python
def _cascade_skip_downstream(
    states: dict[str, WorkflowRunState],
    run: WorkflowRunState,
    *,
    turn_count: int,
) -> None:
    """Skip all downstream nodes that are still ACTIVE and only reachable from this node."""
    downstream = _reachable_downstream(run.name)
    for name in downstream:
        target = states.get(_workflow_key(name))
        if target is None or target.status != WorkflowRunStatus.ACTIVE:
            continue
        if _has_other_active_precursor(states, name, exclude=run.name):
            continue
        target.status = WorkflowRunStatus.SKIPPED
        target.updated_turn = turn_count
        target.blocked_reason = ""
        target.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SKIPPED.value,
                ref=f"cascade:upstream_{run.name}_done",
                ok=True,
                summary=f"Upstream node {run.name} exited with done; downstream skipped.",
                condition="done",
            )
        )
```

#### 辅助函数算法说明

**`_reachable_downstream(name: str) -> list[str]`**

从 `name` 出发，沿 DAG 边做 BFS 遍历，收集所有可达的下游节点名。遍历范围为当前 DAG 定义的所有边（`WORKFLOW_EDGES`），不限于当前 `states` 中已有的节点。

```
算法：BFS
1. queue = [name], visited = {name}, result = []
2. while queue not empty:
   a. current = queue.popleft()
   b. for each edge (current → target) in WORKFLOW_EDGES:
      - if target not in visited: visited.add(target), result.append(target), queue.append(target)
3. return result
```

**`_has_other_active_precursor(states: dict, name: str, *, exclude: str) -> bool`**

检查 `name` 是否有除 `exclude` 之外的其他 ACTIVE 前置节点。用于判断下游节点是否仅由当前 done 节点可达——如果有其他 active 前置，说明该下游节点还有其他入口路径，不应被 skip。

```
算法：反向边遍历
1. for each edge (source → name) in WORKFLOW_EDGES:
   a. if source == exclude: continue
   b. precursor_state = states.get(_workflow_key(source))
   c. if precursor_state exists and precursor_state.status == ACTIVE: return True
2. return False
```

### Bug 2: LLM 没选 workflow_start 时仍走 trigger 匹配

**根因**：`workflow_context_for` 的 else 分支仍走 `service.select()`（含 trigger 匹配），与"LLM 没选就不进工作流"的设计矛盾。

**验证结论**：当前代码已正确处理——主循环调用点（`_prepare_with_stream`）传入 `workflow_start`，当它为 None 时 `workflow_context_for` 返回空 matches；subagent 调用点没有 `workflow_start`，需保留 `service.select()` 回退。**无需额外修改**。

### Bug 3: advance_workflow 无可推进时报错给 LLM

**根因**：`advance_workflow` 在当前节点无可推进的下游时，返回错误信息给 LLM（如 "No active workflow node is available to advance."）。但 LLM 不应关心 workflow 内部状态，无可推进就是完成了。

**修复**：无可推进时，自动将当前节点以 `done` 退出，不报错。

```python
# advance_workflow.py — execute() 中
active = _active_runs(runs)
if not active:
    # 无活跃节点，直接 done，不报错
    return _done_result(runs, turn_count=turn_count)
```

### Bug 4: verify→review 循环无终态

**根因**：verify 的 `passed_substantial` exit 指向 review，review 的 `review_has_issues` 又指向 feedback→tdd→verify，形成循环。

**修复**：verify 判定 pass 后直接 `done` 退出，不进入 review。**verify pass = 终态，不循环。**

```python
# verify 节点规则（写入 workflow context）
# - 验证通过 → done（直接结束，不进入 review）
# - 验证失败（实现问题）→ failed_implementation → tdd
# - 验证失败（bug）→ failed_bug → debug
```

#### DAG 边修改

**删除** `Edge(source="verify", target="review", condition="passed_substantial")`。

当前 dag.py:19 定义了这条边：

```python
Edge(source="verify", target="review", condition="passed_substantial",
     label="verification passed after substantial work",
     description="Use when verification passed and the change merits code review."),
```

删除后：
- verify 节点的 `passed_substantial` exit 不再对应任何 DAG 边，`_activate_transition_targets` 返回空列表
- `advance_workflow` 中 verify 以 `passed_substantial` 退出时，走 terminal condition 路径（与 Bug1 的 `_cascade_skip_downstream` 一致），节点直接 SATISFIED
- review 节点仍可通过 `plan.join=review` 直接激活（用户明确要求 review 时），不受此边删除影响
- verify 节点的 `done` exit 也走 terminal condition，行为与 `passed_substantial` 一致

**注意**：verify 节点定义中 `passed_substantial` 仍作为可用 exit condition 保留（workflow context 提示词中仍列出），只是不再有对应的 DAG 边。agent 选择 `passed_substantial` 时等同于 `done`——节点 SATISFIED，不激活下游。

### Bug 3/Bug 4 测试影响分析

| 测试文件 | 受影响测试 | 变更说明 |
|---------|-----------|---------|
| `tests/test_tools/test_basic.py` | `test_advance_workflow_done_satisfies_without_successor` | Bug3: 无 active 节点时不再报错，改为 done |
| `tests/test_tools/test_basic.py` | `test_advance_workflow_done_requires_evidence` | Bug3: 同上 |
| `tests/test_tools/test_basic.py` | `test_advance_workflow_done_requires_workflow_when_ambiguous` | Bug3: 同上 |
| `tests/test_tools/test_basic.py` | `test_advance_workflow_done_with_explicit_workflow` | Bug3: 同上 |
| `tests/test_tools/test_basic.py` | `test_advance_workflow_reports_invalid_condition` | Bug3: 可能受影响 |
| `tests/test_workflow_reconcile.py` | `test_reconcile_advances_verify_to_review_when_workflow_start_requests_review` | Bug4: verify pass 不再进 review，需重写或删除 |
| `tests/test_agent/test_goal_resolver.py` | `test_goal_resolver_defaults_review_write_route_end_to_verify` | Bug4: verify pass 行为变化 |
| `tests/test_auto_advance.py` | `test_auto_advance_events_flow_through_advance_workflow_states` | Bug3/Bug4: 自动推进行为变化 |
| `tests/test_agent/test_core_flow.py` | `test_advance_workflow_route_end_satisfies_without_successor` | Bug3: route end 处理逻辑变化 |
| `tests/test_agent/test_core_flow.py` | `test_advance_workflow_route_end_satisfies_non_review_without_successor` | Bug3: 同上 |
| `tests/test_agent/test_core_flow.py` | `test_advance_workflow_done_stops_before_followup_llm_when_workflow_complete` | Bug3: done 行为变化 |

### 数据模型重构测试影响分析

除 Bug3/Bug4 外，数据模型重构（GoalResolution → intent/goal/plan、删除 user_requested_write/needs_confirmation/pending_approval、WorkflowRoute 字段重命名）影响以下测试文件：

#### test_agent/test_task_state.py

整个文件围绕 `resolve_turn_intent` 和 `pending_approval` 编写，重构后大部分测试需删除或重写：

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_inspect_turn_is_coding_without_implementation_approval` | 删除：`resolve_turn_intent` 整体移除 |
| `test_design_turn_opens_one_pending_implementation_approval` | 删除：`pending_approval` 机制移除 |
| `test_approval_phrase_confirms_pending_design` | 删除：`pending_approval` 机制移除 |
| `test_confirm_phrase_confirms_pending_design` | 删除：同上 |
| `test_approval_phrase_without_pending_design_is_general_confirmation_needed` | 删除：同上 |
| `test_direct_implementation_request_does_not_need_pending_design` | 删除：`user_requested_write` 移除 |
| `test_short_modify_command_is_explicit_write_request` | 删除：`user_requested_write` 移除 |
| `test_design_question_with_change_word_stays_design_goal` | 删除：`resolve_turn_intent` 移除 |
| `test_intent_classifier_uses_recent_two_turn_window_for_short_input` | 删除：`resolve_turn_intent` 移除 |
| `test_intent_window_keeps_only_two_recent_user_inputs` | 删除：同上 |
| `test_intent_window_does_not_override_approval_without_pending_plan` | 删除：`pending_approval` 移除 |
| `test_intent_window_does_not_override_direct_short_command` | 删除：`user_requested_write` 移除 |
| `test_general_turn_clears_pending_approval` | 删除：`pending_approval` 移除 |
| `test_goal_mode_uses_task_state_current_goal` | 重写：改用 `_normalize_resolution` goal mode 分支 |
| `test_set_goal_resets_previous_workflow_context` | 重写：`WorkflowRoute(start→join, end→leave)`，`goal_from_text` → `GoalSpec` |
| `test_goal_mode_confirmation_clears_pending_approval` | 删除：`pending_approval` 移除 |
| `test_design_goal_only_creates_pending_approval_when_confirmation_needed` | 删除：`pending_approval` 移除 |
| `test_design_goal_needing_confirmation_creates_pending_approval` | 删除：同上 |
| `test_clear_goal_resets_goal_state` | 重写：`goal_from_text` → `GoalSpec`，删除 `pending_approval` 断言 |

#### test_agent/test_goal_resolver.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_goal_resolver_uses_structured_llm_result` | 重写：`GoalResolution` 结构变化（`workflow_start/end` → `plan.join/leave`，删除 `user_requested_write/needs_confirmation`） |
| `test_goal_resolution_schema_excludes_approval_and_title_fields` | 重写：schema 字段变化 |
| `test_goal_resolver_propagates_review_only_route` | 重写：`workflow_start/end` → `plan.join/leave`，`user_requested_write` 移除 |
| `test_goal_resolver_propagates_review_and_fix_route` | 重写：同上 |
| `test_goal_resolver_defaults_review_write_route_end_to_verify` | 重写：`workflow_end` → `plan.leave`，默认规则变化 |
| `test_goal_resolver_propagates_valid_workflow_start` | 重写：`workflow_start` → `plan.join` |
| `test_goal_resolver_drops_unknown_workflow_route` | 重写：校验逻辑从 `workflow_start` → `plan.join` |
| `test_goal_resolver_plan_mode_forces_design_goal` | 重写：`needs_confirmation` 移除，`plan.join=brainstorm` |
| `test_goal_resolver_falls_back_when_structured_output_fails` | 重写：fallback 从 `resolve_turn_intent` → 直接构造 `intent=general` |
| `test_goal_resolver_plain_approval_with_pending_approval_returns_no_workflow_route` | 删除：`pending_approval` 移除 |
| `test_goal_resolver_normal_request_returns_no_workflow_route` | 重写：`workflow_start` → `plan.join` |

#### test_agent/test_run_loop.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_run_once_uses_local_goal_fallback_when_structured_resolver_fails` | 重写：fallback 逻辑变化 |
| `test_run_once_does_not_preadvance_workflow_without_resolver_workflow_start` | 重写：`workflow_start` → `plan.join`，`pending_approval` 移除 |
| `test_run_once_clears_stale_completed_workflow_when_resolver_has_no_workflow_start` | 重写：`workflow_start` → `plan.join`，`user_requested_write/needs_confirmation` 移除 |
| `test_run_once_preadvances_workflow_from_resolver_workflow_start` | 重写：`workflow_start` → `plan.join`，`pending_approval` 移除 |
| `test_run_once_activates_workflow_start_from_resolver_route` | 重写：`workflow_start` → `plan.join` |
| `test_run_once_overrides_stale_brainstorm_when_resolver_requests_tdd` | 重写：`workflow_start` → `plan.join` |
| `test_resume_restores_structured_runtime_state` | 重写：`pending_approval` 移除，`WorkflowRoute` 字段重命名 |

#### test_agent/test_session.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| 会话持久化相关测试（~6个） | 重写：`pending_approval` 移除，`WorkflowRoute(start→join, end→leave)`，`goal_from_text` → `GoalSpec` |

#### test_agent/test_runtime_context.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_runtime_context_applies_task_context_before_current_user` | 重写：`goal.target/expected_result/user_requested_write/needs_confirmation` 渲染变化 |
| `test_runtime_context_inserts_goal_resolution_guide_before_current_user` | 重写：`GoalResolution` schema 变化 |
| `test_runtime_context_reuses_goal_resolution_guide_message_incrementally` | 重写：同上 |

#### test_agent/test_slash_model.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `/model` slash 测试中 `pending_approval` 相关 | 重写：`pending_approval` 移除 |

#### test_workflow_reconcile.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_reconcile_activates_workflow_start_when_no_workflow_is_active` | 重写：`workflow_start` → `plan.join`，`user_requested_write` 移除 |
| `test_reconcile_clears_completed_workflow_runs_when_next_turn_has_no_workflow_start` | 重写：`workflow_start` → `plan.join` |
| `test_reconcile_advances_brainstorm_to_plan_when_workflow_start_requests_plan` | 重写：`workflow_start` → `plan.join` |
| 其他 reconcile 测试 | 重写：`GoalResolution` 结构变化，`goal_from_text` → `GoalSpec` |

#### test_tools/test_basic.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `TestToolStatePatch`（4个测试） | 重写：`task_intent` → `intent`，`pending_approval` 移除，`goal_from_text` → `GoalSpec` |
| `test_plan_checkpoint_approval_clears_pending_approval` | 删除：`pending_approval` 移除 |
| `_dump_pending_approval` 测试（4个） | 删除：`_dump_pending_approval` 随 `pending_approval` 移除 |
| `test_none_pending_approval_clears_state` | 删除：`pending_approval` 移除 |
| ToolStatePatch 序列化测试（~7个） | 重写：字段名变化 |

#### test_runtime_intent_classifier.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `resolve_turn_intent` 相关测试（3个） | 删除：`resolve_turn_intent` 移除 |

#### test_intent_classifier_phase_a.py

| 受影响测试 | 变更说明 |
|-----------|---------|
| `test_resolve_turn_intent_keeps_design_behavior_after_classifier_integration` | 删除：`resolve_turn_intent` 移除 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `goal.desc` 替代 `target`+`expected_result` | 保留两个字段 | 语义重叠，desc 更简洁 |
| goal/plan 保持独立字段 | 合一为 `GoalPlan` | 用户偏好独立字段；绑定约束由校验保证，不靠结构合并 |
| 删除 `user_requested_write` / `needs_confirmation` | 保留但改为推断 | 推断不可靠，workflow 节点已明确 |
| 嵌套类命名 `GoalSpec` | 保留 `Goal` | `Goal` 与顶层 `GoalResolution` 在重构后语义不同但易混淆；`GoalSpec` 明确表示"goal 的规格描述"，与 `IntentResolution`/`PlanResolution` 命名风格一致 |
| 删除 `PendingApproval` 机制 | 改为 workflow gate 驱动 | gate 已覆盖确认需求，`pending_approval` 是冗余状态 |
| 删除 `default_workflow_end_for_goal` | 保留并改写 | 函数仅有一行有效逻辑（`user_requested_write` 判断），删除后由 LLM `plan.leave` + `_normalize_resolution` 默认规则替代更清晰 |
| Bug2 不修改代码 | 修改代码强制跳过 trigger | 当前代码已正确处理（主循环传 `workflow_start`，subagent 走 `service.select()`），无需额外修改 |
| verify pass → done | verify pass → review | 打破循环，verify pass 是终态 |
