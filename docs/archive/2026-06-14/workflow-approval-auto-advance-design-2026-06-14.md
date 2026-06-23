# Workflow Turn Reconciliation 设计

> **Status: Done**
> 日期: 2026-06-14

## 1. 背景

一次真实交互中，用户在 `brainstorm` 阶段已经明确回复"可以，先写一个 spec"，但 agent 仍然走了额外几轮：

1. 先调用 `clarify` 让用户批准"设计方案是否批准"
2. 用户批准后，runtime 只更新了普通 task state
3. active workflow 仍停留在 `brainstorm`
4. agent 下一轮才意识到需要 `advance_workflow(workflow="brainstorm", condition="approved")`
5. `advance_workflow` 激活 `design-doc` 后，下一轮才开始写文件

`workflow-gate-deadlock-design-2026-06-13.md` 已经解决了后半段问题：`design-doc` 写文档不应被后继 `plan` gate 拦住。本 spec 解决前半段问题。

根因是 `GoalResolution.confirmed_approval` 与 workflow DAG 存在结构性冲突。解决方案是让 LLM resolver 直接返回 `next_workflow`，reconcile 验证 DAG 边后自动推进。

## 2. 现状分析

### 2.1 `confirmed_approval` 与 workflow 的语义冲突

`confirmed_approval` 的语义是"用户批准了实现计划"，它把 goal 从 DESIGN 升级为 FEATURE + `user_requested_write=True`。但 workflow gate 等的是"批准设计，进入下一个 workflow 阶段"——可能是写 spec（design-doc），也可能是写计划（plan），不一定是写代码。

用户说"可以，先写一个 spec"：
- `confirmed_approval` 理解为：批准实现 → goal=FEATURE, write=True
- workflow 需要：批准设计 → brainstorm→design-doc

**方向反了**。`confirmed_approval` 承载了"批准实现"的语义，但 workflow 需要的是"批准设计并进入下一阶段"。两层语义不该耦合在一个字段里。

### 2.2 关键字匹配对复合句无效

`is_approval_phrase` 底层是 `_is_approval_only()`，对 normalize 后的文本做全等匹配。复合句（"可以，先写一个 spec"）normalize 后变成"可以先写一个spec"，不在 `_APPROVAL_ONLY_HINTS` 集合里，匹配不上。

验证结果：

| 用户文本 | `is_approval_phrase` | `asks_for_design_doc` | `confirmed_approval=None` 时 reconcile |
|---|---|---|---|
| "可以" | ✅ True | ❌ False | 不触发（双条件安全阀） |
| "可以，先写一个 spec" | ❌ False | ✅ True | **不触发** |
| "好的，先写一个 spec" | ❌ False | ✅ True | **不触发** |
| "没问题，写 spec" | ❌ False | ✅ True | **不触发** |

`confirmed_approval=None` 时 reconcile 完全无法触发。只有 `resolve_turn_intent` 关键字路径设置了 `confirmed_approval`，但那条路径只匹配纯批准短句。

### 2.3 LLM resolver 已有语义理解能力，但没被 workflow 消费

`resolve_goal_for_turn` 已经用 LLM 理解用户意图，能正确返回 `goal.type=DOC` + `user_requested_write=True`。但 LLM prompt 没有指导 LLM 设置 `confirmed_approval`，reconcile 也无法从 goal 信息推导出 workflow transition。

LLM 的语义理解能力被浪费了——它理解了"用户要写 spec"，但这个信息没有传递到 workflow 层。

### 2.4 `confirmed_approval` 的消费者分析

`confirmed_approval` 被三处消费：

1. **`update_after_turn`**：把 goal 从 DESIGN 升级为 FEATURE + write=True + needs_confirmation=False
2. **`_next_pending_approval`**：清空 pending_approval
3. **`reconcile._approved_current_turn`**：判断用户是否批准

这三个效果都可以用更好的方式替代（见第 5 节）。

### 2.5 `asks_for_design_doc` 也是关键字匹配

`asks_for_design_doc` 用 `_DESIGN_DOC_HINTS` 做子串匹配，覆盖了"spec""设计文档""写文档"等词。但它和 `is_approval_phrase` 一样是关键字，无法理解语义。且这两个函数的组合逻辑（双条件守门）本质是在用关键字模拟 LLM 的语义判断。

## 3. 设计目标

- 让 LLM resolver 直接返回 workflow transition 意图，不再依赖关键字匹配
- 去掉 `confirmed_approval`，消除 TaskState 层与 workflow 层的语义冲突
- 用户明确表达下一步意图时，首个主 LLM call 前就让对应 workflow node active
- 减少 `clarify -> approved -> advance_workflow -> design-doc` 的额外往返
- 保留已有 active workflow 进度
- 保留 `brainstorm` gate 的安全性
- 让 runtime-driven workflow transition 可观察、可测试、可回放

## 4. 非目标

- 不取消 `brainstorm` gate
- 不让所有"可以"都自动写文件
- 不绕过用户批准；只桥接 LLM 已识别的明确意图
- 不改变 `advance_workflow` 工具本身
- 不把 `clarify` 改成通用 workflow transition 工具
- 不在每个 turn 盲目重建 workflow runs
- 不让新 goal resolution 覆盖尚未完成且仍相关的 active workflow
- 不让 reconcile 替 LLM 做语义推断；语义判断由 LLM 完成，reconcile 只做 DAG 验证

## 5. 推荐方案：`next_workflow` + 去掉 `confirmed_approval`

### 5.1 核心思路

让 LLM resolver 在返回 `GoalResolution` 时，直接指明 `next_workflow`——用户意图对应的下一个 workflow node。reconcile 只需验证 DAG 边存在，然后推进。

```text
LLM 返回: next_workflow="design-doc"
reconcile: brainstorm active? ✅ → brainstorm→design-doc edge exists? ✅ → condition="approved" → 推进
```

LLM 不需要知道 DAG 的 condition 名，只需要判断"用户接下来要做什么"。reconcile 从 active node + target 反查 condition，这是确定性逻辑。

### 5.2 `GoalResolution` 变更

```python
class GoalResolution(BaseModel):
    intent: TaskIntent = TaskIntent.CODING
    goal: Goal | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = ""
    next_workflow: str | None = None   # 替代 confirmed_approval
```

去掉 `confirmed_approval` 和 `title`，新增 `next_workflow`。

`title` 原本用于第一条消息时给 session 起标题。删除后不再让 resolver 同时承担标题职责，`turn_runner` 使用 `goal.target` 作为优先标题来源，fallback 到已有临时标题逻辑：

```python
title = (intent_resolution.goal.target
         if intent_resolution.goal and intent_resolution.goal.target.strip()
         else host._temporary_session_title(payload.title_text))
```

### 5.3 LLM resolver prompt 变更

在 `_resolver_messages` 的 system prompt 中新增规则：

```text
- If the user's intent clearly indicates which workflow should be active next
  (e.g. approved design → write spec, skip to plan, small change),
  set next_workflow to that node name (e.g. "design-doc", "plan", "tdd").
  Leave null when the intent does not imply a workflow transition.
- Do not set next_workflow based on vague or ambiguous approval.
```

同时去掉原有关于 `confirmed_approval` 的规则：

```text
# 删除
- If pending_approval is present and the user clearly approves it, use that scope as the goal target and set user_requested_write=true.
```

注意：`pending_approval` 相关的 prompt 规则（"If pending_approval is present and the user clearly approves it, return the next concrete goal directly."）已删除。该规则与 `next_workflow` 冗余——LLM 通过 `next_workflow` 字段即可表达用户批准后的下一步意图，`pending_approval` 作为输入上下文已足够让 LLM 理解当前状态。

LLM 应该通过 `goal` 字段直接返回正确的 goal type 和 `user_requested_write`，不需要 `confirmed_approval` 做 DESIGN→FEATURE 升级。

同时移除 `title_requested` prompt 规则和 resolver schema 中的 `title` 字段。首轮标题只由 `turn_runner` 从 `goal.target` 或 `_temporary_session_title()` 得出。

### 5.4 `update_after_turn` 变更

去掉 `confirmed_approval` 分支，直接用 `resolution.goal`：

```python
def update_after_turn(self, resolution, user_text, *, scope_text=None):
    self.previous_intent = self.current_intent
    self.current_intent = resolution.intent
    if resolution.goal is not None:
        self.current_goal = resolution.goal
    elif resolution.intent == TaskIntent.GENERAL:
        self.current_goal = None
    self._record_user_text(user_text)
    self.pending_approval = _next_pending_approval(resolution, self.current_goal)
```

LLM resolver 已经能直接返回 `Goal(type=DOC, user_requested_write=True)` 或 `Goal(type=FEATURE, user_requested_write=True)`，不需要从 DESIGN 升级。

### 5.5 `_next_pending_approval` 变更

去掉 `confirmed_approval` 判断。当 goal 是 DESIGN 且 `needs_confirmation=True` 时才创建 pending_approval：

```python
def _next_pending_approval(resolution, goal):
    if goal is not None and goal.type == GoalType.DESIGN and goal.needs_confirmation:
        return PendingApproval(
            scope=goal.label,
            source_goal_type=goal.type,
        )
    return None
```

### 5.6 `resolve_turn_intent` 变更

去掉 `_is_approval_only` 分支的 `confirmed_approval` 设置。纯批准短句（"可以"）在有关键字 fallback 时，改为返回正确的 goal 而非 `confirmed_approval`：

```python
def resolve_turn_intent(text, interaction_mode=None, task_state=None):
    mode = InteractionMode.parse(interaction_mode)
    state = task_state or TaskState()

    if mode == InteractionMode.PLAN:
        return _resolution(TaskIntent.CODING, "interaction mode forces coding")

    if _is_approval_only(text):
        if state.pending_approval:
            # 不再设 confirmed_approval，改为返回 goal
            return _resolution(
                TaskIntent.CODING,
                "user confirmed the pending implementation plan",
                goal=goal_from_text(
                    state.pending_approval.scope,
                    goal_type=GoalType.FEATURE,
                    user_requested_write=True,
                    needs_confirmation=False,
                ),
            )
        return _resolution(
            TaskIntent.GENERAL,
            "approval phrase without a pending implementation plan",
            confidence=0.6,
        )

    # ... 其余不变
```

### 5.7 `goal_resolver.py` 变更

1. 去掉 `fallback.confirmed_approval is not None` 的短路逻辑
2. `_normalize_resolution` 不再透传 `confirmed_approval`
3. LLM 返回的 `next_workflow` 经 DAG node 白名单校验后透传；未知 node 归一化为 `None`
4. 去掉 `title_requested` 参数、`_normalize_title()`、resolver prompt 中 title 相关上下文

```python
async def resolve_goal_for_turn(...):
    fallback = resolve_turn_intent(user_text, interaction_mode, task_state)
    # 不再短路，让 LLM 也有机会判断 next_workflow
    if model is None:
        return fallback
    # ... LLM 调用不变
    return _normalize_resolution(resolution, ...)


def _normalize_resolution(resolution, *, ...):
    # 去掉 fallback.confirmed_approval 覆盖逻辑
    # 直接用 resolution.goal，并规范化 resolution.next_workflow
    ...
    return GoalResolution(
        intent=...,
        goal=goal,
        confidence=confidence,
        reason=reason,
        next_workflow=_normalize_next_workflow(resolution.next_workflow),
    )
```

### 5.8 Reconcile 变更

`reconcile_workflow_runs_for_turn` 的签名和逻辑简化：

```python
def reconcile_workflow_runs_for_turn(
    *,
    goal_resolution: GoalResolution,
    after_state: TaskState,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    runs = [run.model_copy(deep=True) for run in (after_state.workflow_runs or {}).values()]
    events = _reconcile_events(
        goal_resolution=goal_resolution,
        runs=runs,
    )
    if not events:
        return runs
    return advance_workflow_states(runs, events, turn_count=turn_count)


def _reconcile_events(
    *,
    goal_resolution: GoalResolution,
    runs: list[WorkflowRunState],
) -> list[WorkflowStateEvent]:
    next_wf = goal_resolution.next_workflow
    if not next_wf:
        return []
    event = _resolve_auto_transition(runs, next_wf)
    if event is None:
        return []
    return [event]


def _resolve_auto_transition(
    active_runs: list[WorkflowRunState],
    next_workflow: str,
) -> WorkflowStateEvent | None:
    """从 active runs 中找到有指向 next_workflow 的 edge 的那个，生成 transition event。"""
    if _has_active(active_runs, next_workflow):
        return None
    for run in sorted(active_runs, key=lambda item: workflow_sort_key(item.name)):
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        for edge in DEFAULT_WORKFLOW_DAG.edges_from(run.name):
            if edge.target == next_workflow:
                return WorkflowStateEvent(
                    workflow=run.name,
                    kind=WorkflowStateEventKind.SATISFIED,
                    ref=f"auto:turn_reconcile:{run.name}_to_{next_workflow}",
                    ok=True,
                    summary=f"User intent implies transition from {run.name} to {next_workflow}.",
                    reason=f"next_workflow={next_workflow} from goal resolver",
                    condition=edge.condition,
                )
    return None
```

**不再需要**：`is_approval_phrase`、`asks_for_design_doc`、`_approved_current_turn`、`_should_advance_brainstorm_to_design_doc()`、`user_text`、`before_state` 参数。

### 5.9 `turn_runner.py` 接入变更

```python
# 之前
reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
    user_text=payload.title_text,
    goal_resolution=intent_resolution,
    before_state=base_task_state,
    after_state=turn_task_state,
)

# 之后
reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
    goal_resolution=intent_resolution,
    after_state=turn_task_state,
)
```

### 5.10 可删除的代码

- `GoalResolution.confirmed_approval` 字段
- `is_approval_phrase()` 函数
- `asks_for_design_doc()` 函数
- `GoalResolution.title` 字段
- `_normalize_title()` 函数
- `_should_request_resolver_title()` 函数
- `resolve_goal_for_turn(..., title_requested=...)` 参数
- `_DESIGN_DOC_HINTS` 常量
- reconcile 中的 `_approved_current_turn()`、`_should_advance_brainstorm_to_design_doc()`
- `resolve_turn_intent` 中 `_is_approval_only` 分支的 `confirmed_approval` 设置

注意：`_is_approval_only` 和 `_APPROVAL_ONLY_HINTS` 在 `resolve_turn_intent` 中仍用于识别纯批准短句（"可以"），不能删除。但它们不再产生 `confirmed_approval`，改为产生正确的 goal。

### 5.11 `PendingApproval` 保留

`PendingApproval` 模型本身保留，它仍用于：
- `TaskState.pending_approval`：标记当前有等待批准的设计
- `runtime_context` 渲染：提示 LLM 使用 `plan_checkpoint`
- `goal_resolver` 上下文：让 LLM 知道有 pending approval

但 `confirmed_approval` 字段从 `GoalResolution` 中移除。`PendingApproval` 不再作为 resolver 的输出，只作为输入上下文。

## 6. 数据流

### 6.1 新流程

```text
用户: "可以，先写一个 spec"
    │
    ▼
resolve_goal_for_turn (LLM resolver)
    │
    ├─ LLM 返回:
    │   GoalResolution(
    │       goal=Goal(type=DOC, target="写 spec", user_requested_write=True),
    │       next_workflow="design-doc",
    │   )
    │
    └─ fallback (关键字路径):
        GoalResolution(
            goal=Goal(type=FEATURE, target="...", user_requested_write=True),
            next_workflow=None,  ← 关键字路径无法判断 next_workflow
        )
    │
    ▼
update_after_turn: goal=DOC, write=True, pending=None
    │
    ▼
reconcile: next_workflow="design-doc"
    ├─ brainstorm active? ✅
    ├─ brainstorm→design-doc edge exists? ✅
    └─ condition="approved" → 生成 WorkflowStateEvent → 推进
    │
    ▼
第一次主 LLM 调用看到:
    Active workflow nodes: design-doc
    brainstorm=satisfied condition=approved
    design-doc=active
```

### 6.2 与旧流程对比

```text
旧: 用户"可以，先写一个 spec"
    → resolve_turn_intent: _is_approval_only=False → confirmed_approval=None
    → LLM resolver: 可能设 confirmed_approval（不确定）
    → update_after_turn: 可能升级 goal（取决于 confirmed_approval）
    → reconcile: is_approval_phrase=False, confirmed_approval=None → 不推进
    → 主 LLM 仍看到 brainstorm=active → 多一轮

新: 用户"可以，先写一个 spec"
    → LLM resolver: next_workflow="design-doc", goal=DOC
    → update_after_turn: goal=DOC, write=True
    → reconcile: next_workflow="design-doc" → 验证 DAG 边 → 推进
    → 主 LLM 看到 design-doc=active → 直接写文档
```

## 7. 安全性分析

### 7.1 为什么 `next_workflow` 不会误触

1. **LLM 做语义判断**：LLM 理解"可以"和"可以，先写一个 spec"的区别，不会对模糊确认设 `next_workflow`
2. **reconcile 做 DAG 验证**：即使 LLM 返回了 `next_workflow="design-doc"`，reconcile 也会验证当前有 active node 指向它
3. **`next_workflow` 是可选的**：LLM 可以不设，reconcile 就不推进，退化为现有行为
4. **不跳过 gate**：reconcile 只做 transition，不绕过目标 node 的 gate。`design-doc` 仍有自己的 gate（reader test）

### 7.2 与旧方案的安全性对比

| 场景 | 旧方案（关键字） | 新方案（next_workflow） |
|------|-----------------|----------------------|
| "可以" | `is_approval_phrase=True`，但 `asks_for_design_doc=False` → 不触发 | LLM 不设 `next_workflow` → 不触发 |
| "可以，先写一个 spec" | `is_approval_phrase=False` → 不触发 | LLM 设 `next_workflow="design-doc"` → 触发 |
| "可以，直接改代码" | `is_approval_phrase=False` → 不触发 | LLM 设 `next_workflow="tdd"` 或不设 → 取决于 LLM 判断 |
| "不用写 spec，直接写计划" | 不触发 | LLM 设 `next_workflow="plan"` → 触发 |

新方案覆盖了旧方案无法处理的复合句，同时不降低安全性。

### 7.3 LLM 返回错误 `next_workflow` 的防护

- resolver normalize 先验证 `next_workflow` 是 DAG 中已有 node name，未知值归一化为 `None`
- reconcile 验证 DAG 边存在，不存在的 transition 被忽略
- 如果当前没有 active node 指向该 target，reconcile 不产生 event
- 如果 target workflow 已经 active，reconcile 不产生 event，避免把 source 误标为 satisfied

## 8. 测试计划

### 8.1 Reconcile 纯函数测试

文件：`tests/test_workflow_reconcile.py`

用例：

1. `next_workflow="design-doc"` + active brainstorm → brainstorm=satisfied, design-doc=active
2. `next_workflow=None` → 不推进
3. `next_workflow="design-doc"` + 无 active brainstorm → 不推进
4. `next_workflow="design-doc"` + design-doc 已 active → 不重复激活
5. `next_workflow="plan"` + active brainstorm → brainstorm=satisfied, plan=active (condition=skip_to_plan)
6. `next_workflow="tdd"` + active brainstorm → brainstorm=satisfied, tdd=active (condition=small_change)
7. `next_workflow="nonexistent"` → 不推进
8. active verify/review + `next_workflow` 指向合法 target → 正常推进
9. 多个 active node 都有指向同一 target 的边 → 取第一个（按 DAG 顺序）
10. target workflow 已 active → 不推进 source

### 8.2 Goal resolver 测试

文件：`tests/test_agent/test_goal_resolver.py`

用例：

1. 用户说"可以，先写一个 spec" + pending_approval → `next_workflow="design-doc"`, goal=DOC
2. 用户说"可以" + pending_approval → `next_workflow=None` 或不设, goal=FEATURE
3. 用户说"不用写 spec，直接写计划" → `next_workflow="plan"`
4. 用户说"这个很小，直接改" → `next_workflow="tdd"` 或不设
5. 无 pending_approval + 普通请求 → `next_workflow=None`

### 8.3 `update_after_turn` 测试

文件：`tests/test_agent/test_task_state.py`

用例：

1. `GoalResolution(goal=Goal(type=DOC, write=True))` → current_goal=DOC, pending=None
2. `GoalResolution(goal=Goal(type=FEATURE, write=True))` → current_goal=FEATURE, pending=None
3. `GoalResolution(goal=Goal(type=DESIGN, needs_confirmation=True))` → current_goal=DESIGN, pending=PendingApproval(...)
4. 不再测试 `confirmed_approval` 相关断言

### 8.4 Turn runner 集成测试

文件：`tests/test_agent/test_run_loop.py`

场景：

1. brainstorm active + 用户"可以，先写一个 spec" → design-doc active
2. brainstorm active + 用户"可以" → brainstorm 仍 active

### 8.5 回归测试

- `plan_checkpoint` 的 `state_patch` 不受影响
- `advance_workflow` 手动调用仍可用
- `runtime_context` 的 `pending_approval` 渲染仍正常
- 已有 workflow evidence 不因新 turn goal resolution 丢失

## 9. 实现计划

### Task 1：`GoalResolution` 去掉 `confirmed_approval`，加 `next_workflow`

文件：`src/voidx/runtime/task_state.py`

改动：
- `GoalResolution` 去掉 `confirmed_approval` 字段，加 `next_workflow: str | None = None`
- `update_after_turn` 去掉 `confirmed_approval` 分支
- `_next_pending_approval` 去掉 `confirmed_approval` 判断
- `resolve_turn_intent` 的 `_is_approval_only` 分支改为返回 goal 而非 `confirmed_approval`
- 删除 `is_approval_phrase()`、`asks_for_design_doc()`、`_is_approval_only()` 的导出
- 保留 `_is_approval_only()` 和 `_APPROVAL_ONLY_HINTS` 供 `resolve_turn_intent` 内部使用
- 去掉 `title` 字段

### Task 2：`goal_resolver.py` 适配

文件：`src/voidx/agent/goal_resolver.py`

改动：
- 去掉 `fallback.confirmed_approval is not None` 的短路逻辑
- `_normalize_resolution` 去掉 `confirmed_approval` 和 `title` 透传，改为透传 `next_workflow`
- prompt 新增 `next_workflow` 规则，去掉 `confirmed_approval` 和 `title` 相关规则
- 去掉 `title_requested` 参数和 `_normalize_title` 函数
- `_resolver_messages` 不再传 `title_requested` 上下文
- 新增 `_normalize_next_workflow()`，只允许 `DEFAULT_WORKFLOW_DAG.nodes` 中存在的 node

### Task 3：Reconcile 简化

文件：`src/voidx/workflow/reconcile.py`

改动：
- 简化签名：去掉 `user_text`、`before_state`，只保留 `goal_resolution`、`after_state`、`turn_count`
- 核心逻辑改为 `_resolve_auto_transition`：从 active runs + `next_workflow` 反查 DAG edge
- 如果 target 已 active，直接不产生 event
- 多个 active source 可达同一 target 时，按 DAG sort key 选择第一个
- 删除 `_should_advance_brainstorm_to_design_doc`、`_approved_current_turn`
- 删除对 `is_approval_phrase`、`asks_for_design_doc` 的导入

### Task 4：`turn_runner.py` 适配

文件：`src/voidx/agent/graph/turn_runner.py`

改动：
- `reconcile_workflow_runs_for_turn` 调用签名适配
- session 标题改为从 `goal.target` 取值，fallback 到 `_temporary_session_title`
- 去掉 `_should_request_resolver_title` 函数
- `resolve_goal_for_turn` 调用去掉 `title_requested` 参数

### Task 5：测试更新

文件：
- `tests/test_workflow_reconcile.py`
- `tests/test_agent/test_task_state.py`
- `tests/test_agent/test_goal_resolver.py`
- `tests/test_agent/test_run_loop.py`

命令：

```bash
.venv/bin/python -m pytest tests/test_workflow_reconcile.py -v
.venv/bin/python -m pytest tests/test_agent/test_task_state.py -v
.venv/bin/python -m pytest tests/test_agent/test_goal_resolver.py -v
.venv/bin/python -m pytest tests/test_agent/test_run_loop.py -v
.venv/bin/python -m pytest tests/test_module_boundaries.py -v
```

## 10. 风险与防护

| 风险 | 防护 |
|------|------|
| LLM 对模糊确认误设 `next_workflow` | prompt 明确要求"不基于模糊批准设置"；reconcile 验证 DAG 边 |
| LLM 不设 `next_workflow` 导致退化为旧行为 | 退化为旧行为是安全的，只是不自动推进；LLM 仍可手动 `advance_workflow` |
| 关键字 fallback 路径无法设 `next_workflow` | fallback 返回 `next_workflow=None`，LLM 路径有机会补充 |
| 移除 `confirmed_approval` 后 `plan_checkpoint` 受影响 | `plan_checkpoint` 通过 `ToolStatePatch` 直接设 goal，不走 `confirmed_approval` |
| 移除 `asks_for_design_doc` 后其他消费者受影响 | grep 确认只有 reconcile 使用，可安全删除 |
| `PendingApproval` 仍被 `runtime_context` 渲染 | `PendingApproval` 模型保留，只是不再从 `GoalResolution` 输出 |

## 11. 验收标准

- 用户说"可以，先写一个 spec"后，第一次主 LLM 调用看到 `design-doc` active
- 用户说"可以"后，brainstorm 仍 active，不自动推进
- `confirmed_approval` 字段从 `GoalResolution` 中移除
- `is_approval_phrase` 和 `asks_for_design_doc` 不再被 reconcile 使用
- reconcile 只消费 `next_workflow` + DAG 验证
- `brainstorm` gate 仍能阻止未批准写入
- `plan_checkpoint` 和 `advance_workflow` 手动路径不受影响
- 纯函数和 turn runner 集成测试覆盖正向与安全防护
