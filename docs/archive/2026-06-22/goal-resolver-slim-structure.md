# GoalResolver 精简结构规范 — 技术设计文档

> **Status: Done**

## Context

当前 `GoalResolution` 返回 3 个顶层字段（`intent`, `goal`, `plan`），但内部结构存在冗余和逻辑断裂：

1. **`intent.desc`** 从未被消费——所有消费点只读 `intent.type`，desc 是死字段
2. **`goal.type`** 与 `plan.join` 语义重叠——真正决定路由的是 join，type 只是标签，且无逻辑分支依赖
3. **`plan.leave`** 在 resolver 层无实际意义——LLM 几乎不返回 leave，唯一实际设置 leave 的是工具层（plan_checkpoint、agent tool）
4. **`_normalize_resolution` 的 `interaction_mode` 参数** 是死代码
5. **短续接规则** 在提示词中硬编码，但 LLM 有历史上下文能自然理解，且 normalize 已有保底逻辑
6. **`verify`** 在 resolver workflow 列表中但不在 `_ALLOWED_JOIN_NODES` 中，导致逻辑断裂

本次重构将 resolver 的 LLM 返回结构精简为必要字段，同时删除 `GoalSpec.type` 的 canonical state 地位，将 goal 简化为纯文本描述。`GoalType`、`infer_goal_type()`、`goal_type_value()` 不再作为 runtime/agent task state 的公开契约导出；仅保留 resolver 边界对旧 dict 输入的容错读取，方便日志回放和过渡期模型结果转换。LLM 可返回 `kind_hint` 作为非权威语义观察，但该字段不进入 `GoalResolution`、`TaskState` 或 workflow 路由。下游消费点中依赖 `goal.type` 的逻辑改为从 `plan.join` 或 `workflow_route.join` 派生。

## Goals and Non-Goals

### Goals

- 定义 resolver 的精简 LLM 返回结构 `ResolverGoal`（4 字段：`intent`, `goal`, `workflow`, `kind_hint`）
- 定义 `ResolverGoal → GoalResolution` 的转换映射
- 将 LLM-facing 的 `workflow` 映射为 runtime-facing 的 `PlanResolution.join`
- 将 resolver 请求改为结构化 Markdown：历史对话仅保留 content，schema 放在最后
- 从 resolver 提示词中移除 `plan.leave`、`goal.type`、`intent.desc`、短续接规则
- 保留 `kind_hint` 作为 LLM 语义提示字段，但当前不用于处理、路由、persona、状态更新或 goal 比较
- 将 `verify` 加入 `_ALLOWED_JOIN_NODES`
- 删除 `_normalize_resolution` 的 `interaction_mode` 死代码参数
- 清理 `IntentResolution.desc` 死字段
- 删除 `GoalSpec.type` 字段，`GoalSpec` 简化为 `{desc: str}`
- 从 runtime source 中移除 `GoalType`、`infer_goal_type()`、`goal_type_value()` 依赖和公开导出
- 下游消费点中 `goal.type.value` 改为从 `join` 派生或直接移除

### Non-Goals

- 不重构 `WorkflowRunState.goal_type` 字段（保留为 `str`，工具层按需设置）
- 不重构 `ToolStatePatch` 接口
- 工具层（clarify、agent、plan_checkpoint）不再用 `GoalType` 构造 `GoalSpec`；需要保留语义标签时使用普通字符串写入 `WorkflowRunState.goal_type`
- 不修改 workflow DAG 结构

## Architecture

### 数据流

```
用户输入 + recent_exchanges + task_state
    │
    ▼
_resolver_messages_from_exchanges()  ← 生成结构化 Markdown request
    │
    ▼
LLM structured output → ResolverGoal {intent, goal, workflow, kind_hint}
    │
    ▼
_coerce_resolution()  ← 解析 ResolverGoal
    │
    ▼
_to_goal_resolution()  ← 转换为 GoalResolution
    │
    ▼
_normalize_resolution()  ← 校验 join、保底逻辑
    │
    ▼
GoalResolution {intent: IntentResolution, goal: GoalSpec | None, plan: PlanResolution | None}
    │
    ▼ （下游适配）
task_state.update_after_turn()
reconcile_workflow_runs_for_turn()
```

### 调用链与职责划分

| 函数 | 输入 | 输出 | 职责 |
|------|------|------|------|
| `_coerce_resolution` | `object`（LLM 原始返回） | `ResolverGoal \| None` | 解析多种输入类型为 `ResolverGoal` |
| `_to_goal_resolution` | `ResolverGoal, TaskState` | `GoalResolution` | 精简结构 → 下游结构转换，设置 leave |
| `_normalize_resolution` | `GoalResolution, user_text, TaskState` | `GoalResolution` | plan.join 校验、general intent 保底、goal-plan 绑定校验 |

注意：`_normalize_resolution` 的输入从 `GoalResolution`（LLM 直接返回）变为 `_to_goal_resolution` 的输出。`interaction_mode` 参数移除后，签名简化为 `(resolution, user_text, task_state)`。

### 关键变化

1. LLM 返回 `ResolverGoal`（精简结构），而非直接返回 `GoalResolution`
2. `_to_goal_resolution()` 负责将精简结构转换为下游兼容的 `GoalResolution`
3. `GoalSpec` 简化为 `{desc: str}`，不再有 `type` 字段
4. `workflow: str` 由转换层映射为 `PlanResolution.join`，`leave` 由转换层按需设置
5. `kind_hint` 仅保留在 resolver 层作为非权威提示；转换为下游结构时丢弃
6. 下游消费点中 `goal.type.value` 改为从 `join` 派生

## Data Model

### 新增：ResolverGoal（LLM 返回结构）

```
ResolverGoal
├── intent: 'coding' | 'general'
├── goal: string | null
├── workflow: string | null
└── kind_hint: string | null
```

- `intent`：直接用枚举值字符串，无 desc
- `goal`：用户请求的简短描述（1-2 句），用用户语言。null 表示无工作流目标
- `workflow`：要进入的 workflow 节点名。null 表示无工作流。转换层将其映射为 `PlanResolution.join`
- `kind_hint`：LLM 对请求语义类型的非权威提示（如 `review`、`debug`、`feature`、`inspect`）。当前仅保留在 resolver 原始输出/诊断中，不进入 `GoalResolution`，不参与任何处理逻辑

**约束**：`goal` 和 `workflow` 绑定——有 goal 必有 workflow，有 workflow 必有 goal；两者都为 null 表示自由模式。

**`kind_hint` 约束**：`kind_hint` 与 `goal`/`workflow` 不绑定；它可以表达与 `workflow` 不同粒度的语义观察（例如 `kind_hint=inspect`、`workflow=review`），但不能覆盖 `workflow`，不能驱动 workflow、persona、权限、工具选择或 `_same_goal`。

**类型约束**：`intent` 与 `workflow` 应使用 `Literal` 或等价枚举约束，避免 LLM 返回任意字符串后把校验推迟到运行时。

### 变更：GoalResolution（下游结构）

```
GoalResolution
├── intent: IntentResolution {type: TaskIntent}    ← desc 移除
├── goal: GoalSpec | None {desc: str}              ← type 移除，仅保留 desc
└── plan: PlanResolution | None {join, leave}       ← 保留，leave 由转换层设置
```

### 删除的模型

- `GoalSpec.type` 作为 runtime state 字段
- runtime/agent task state 对 `GoalType`、`infer_goal_type()`、`goal_type_value()` 的公开导出和内部依赖
- `GoalSpec.type` 字段
- `IntentResolution.desc` 字段

### 转换映射：ResolverGoal → GoalResolution

```python
def _to_goal_resolution(resolver: ResolverGoal, task_state: TaskState) -> GoalResolution:
    intent_type = TaskIntent(resolver.intent)
    # resolver.kind_hint is intentionally not mapped into runtime state.

    if resolver.goal is None or resolver.workflow is None:
        # 无工作流目标
        return GoalResolution(
            intent=IntentResolution(type=intent_type),
            goal=None,
            plan=None,
        )

    # goal: str → GoalSpec（纯文本，无 type）
    goal_spec = GoalSpec(desc=resolver.goal)

    # workflow → PlanResolution.join，leave 由转换层按场景设置
    leave = _infer_leave(resolver.workflow, task_state)
    plan = PlanResolution(join=resolver.workflow, leave=leave)

    return GoalResolution(
        intent=IntentResolution(type=intent_type),
        goal=goal_spec,
        plan=plan,
    )
```

### 兼容输入

`_coerce_resolution()` 应继续接受旧形态输入，并映射为新结构，避免测试、日志回放和过渡期模型立即失效：

```json
{
  "intent": {"type": "coding"},
  "goal": {"type": "review", "desc": "review code"},
  "plan": {"join": "review", "leave": null}
}
```

转换为：

```json
{
  "intent": "coding",
  "goal": "review code",
  "workflow": "review",
  "kind_hint": "review"
}
```

legacy `goal.type` 只在兼容层读取，不进入 runtime state。

### leave 推断规则

`_infer_leave` 仅在转换层使用，不在提示词中暴露：

| join | leave | 场景 |
|------|-------|------|
| brainstorm | None | brainstorm 可流向 design/plan/tdd，不预设终止 |
| design | None | design → plan，不预设终止 |
| plan | None | plan → tdd，不预设终止 |
| tdd | None | tdd → verify，不预设终止 |
| verify | None | verify → review/debug/tdd，不预设终止 |
| review | None | review → feedback，不预设终止 |
| feedback | None | feedback → tdd/verify/brainstorm/plan，不预设终止 |
| debug | None | debug → tdd/verify，不预设终止 |

**默认 leave = None**。工具层（plan_checkpoint、agent tool）在运行时按需覆盖 leave 值，这是 leave 的正确设置时机——工具知道自己的终止边界，resolver 不应预设。

## API Contract

### Resolver 请求结构（精简后）

resolver 请求统一使用 Markdown 结构。历史对话不再按 `HumanMessage` / `AIMessage` 逐条追加给 resolver，而是提取 recent exchanges 的 content，按时间顺序放入同一个 Markdown request 中。schema 始终放在最后，避免规则、状态和历史内容打断输出契约。

**System Message**：

```
Resolve this turn into intent, goal, workflow, and kind_hint.
Read the Markdown request. Return only structured data matching the schema at the end.
```

**Human Message（Markdown）**：

````
# Goal Resolver Request

## Current State

- intent: {current_intent}
- goal: {current_goal.desc or current_goal.label or "none"}
- active workflows: {active_workflow_names or "none"}

## Recent Conversation Content

```text
{recent_exchanges content only, in chronological order; omit roles and metadata}
```

## Current User Content

```text
{current user_text}
```

## Available Workflows

- brainstorm: Confirm requirements and design, get user approval
- debug: Locate root cause and confirm fix direction
- design: Produce a structured document that passes the reader test
- feedback: Verify and implement valid review feedback
- plan: Produce an executable implementation plan, get user approval
- review: Initiate structured code review request and collect verdict
- tdd: Complete implementation via TDD cycle, all tests green
- verify: Prove changes reach expected state with reproducible evidence

## Return Fields

- intent: "coding" for codebase/workspace work; "general" for non-code conversation.
- goal: short user-language summary, or null when no workflow should start.
- workflow: workflow to start, or null. Must be set exactly when goal is set.
- kind_hint: optional semantic hint such as review/debug/feature/inspect. Advisory only; never overrides workflow.

## ResolverGoal Schema

- intent: 'coding' | 'general'
- goal: null or string (short summary of the user's request in their language, 1-2 sentences)
- workflow: null or one of [brainstorm, debug, design, feedback, plan, review, tdd, verify]
- kind_hint: null or string (non-authoritative semantic hint; not used for routing)
````

**Recent Conversation Content 格式**：

```
### Content 1

{exchange.user_text}

{exchange.assistant_text}

### Content 2

{exchange.user_text}

{exchange.assistant_text}
```

只保留内容本身，不保留 role、message id、tool metadata 或其他 transport 结构。空内容跳过。历史内容和当前用户内容使用 fenced block 包裹，防止用户文本中的标题、schema 或代码块破坏外层 request 结构。

注意：不再暴露 `goal.type`，只展示 goal 描述文本。`ResolverGoal Schema` 必须是 Markdown request 的最后一节。

### 确定性兜底

移除短续接 prompt 规则后，`_normalize_resolution()` 需要保留确定性 fallback：当用户输入明显是短续接（如 `ok`、`continue`、`改`、`go on`），且存在 active workflow，但 resolver 没有返回 workflow 时，继续当前 workflow。这个兜底属于 runtime 逻辑，不放回 prompt。

### 注入边界

Markdown request 中的用户内容和历史内容必须视为纯文本，不得让其中的标题、代码块或 schema 片段覆盖外层契约。实现时应优先使用 fenced block 包裹原文。

### LLM 返回示例

有工作流：
```json
{
  "intent": "coding",
  "goal": "用户想在合并前做代码审查",
  "workflow": "review",
  "kind_hint": "review"
}
```

无工作流：
```json
{
  "intent": "general",
  "goal": null,
  "workflow": null,
  "kind_hint": null
}
```

### 转换后 GoalResolution 示例

有工作流：
```json
{
  "intent": {"type": "coding"},
  "goal": {"desc": "用户想在合并前做代码审查"},
  "plan": {"join": "review", "leave": null}
}
```

## 下游适配：goal.type 消费点

删除 `GoalSpec.type` 后，以下消费点需要适配。核心原则：**路由和 persona 由 join 决定，goal.type 原本就是 join 的冗余标签**。

### join → goal_type 派生映射

当下游需要 goal_type 字符串（如 `WorkflowRunState.goal_type`、日志、UI 显示）时，从 join 派生：

```python
_JOIN_GOAL_TYPE_MAP: dict[str, str] = {
    "brainstorm": "design",
    "debug": "debug",
    "design": "doc",
    "feedback": "review",
    "plan": "design",
    "review": "review",
    "tdd": "feature",
    "verify": "feature",
}

def goal_type_from_join(join: str | None) -> str:
    if not join:
        return ""
    return _JOIN_GOAL_TYPE_MAP.get(join, "")
```

此映射仅用于需要 goal_type 字符串的场景（日志、WorkflowRunState、UI），不用于路由决策。

### 逐文件适配

| 文件 | 当前用法 | 适配方案 |
|------|---------|---------|
| `runtime_context.py:258` | `self.current_goal.type.value` | 移除该行——下一行 `Goal: {desc}` 已提供更详细的用户意图信息，type 标签冗余 |
| `llm.py:79` | `goal_type_value(current_goal)` | 改为 `goal_type_from_join(workflow_start)`，workflow_start 已在上下文中可用 |
| `_voidx_graph.py:358` | `goal.type.value if goal is not None else ""` | 改为 `goal_type_from_join(plan.join) if plan is not None else ""`，plan 已在上下文中可用 |
| `run_loop.py:186` | `goal_type_value(getattr(..., "current_goal", None))` | 改为 `goal_type_from_join(getattr(..., "workflow_route", None).join) if getattr(..., "workflow_route", None) else ""` |
| `turn_runner.py:70` | `goal.type.value` → persona 映射 | 改为从 `workflow_route.join` 或 `workflow_runs` 的 persona 派生。当有 active workflow 时已有 persona（L62-67），无 active workflow 时用 join 映射：`{"brainstorm": "plan", "debug": "explore", "design": "plan", "feedback": "review", "plan": "plan", "review": "review", "tdd": "implement", "verify": "review"}.get(join, "coordinate")` |
| `slash/handler.py:217` | `goal_type_value(task_state.current_goal)` | 改为 `goal_type_from_join(task_state.workflow_route.join) if task_state.workflow_route else ""` |
| `reconcile.py:260` | `goal.type.value if goal is not None else ""` | 改为 `goal_type_from_join(target)`，target 即 join 值，已在上下文中 |
| `goal_resolver.py:145` | `goal.type.value if goal is not None else ""` | 改为 `goal_type_from_join(plan.join) if plan is not None else ""` |
| `goal_resolver.py:241` | `task_state.current_goal.type.value — {label}` | 改为 `task_state.current_goal.label`，不再展示 type |
| `task_state.py:187` | `GoalSpec(type=infer_goal_type(goal), desc=goal)` | 改为 `GoalSpec(desc=goal)` |
| `task_state.py:248` | `_same_goal` 比较 `left.type == right.type and left.desc == right.desc` | 改为只比较 `left.desc == right.desc` |

### 工具层适配

工具层当前直接构造 `GoalSpec(type=GoalType.XXX, desc=...)`，删除 type 后改为 `GoalSpec(desc=...)`：

| 文件 | 当前 | 适配 |
|------|------|------|
| `clarify.py:117` | `GoalSpec(type=goal_type_map[normalized], desc=answer)` | `GoalSpec(desc=answer)` |
| `agent.py:254` | `GoalSpec(type=goal_type, desc=...)` | `GoalSpec(desc=...)` |
| `plan_checkpoint.py:126,141,156,162` | `GoalSpec(type=GoalType.FEATURE/DOC, desc=...)` | `GoalSpec(desc=...)` |
| `goal_resolver.py:38` | `GoalSpec(type=GoalType.DESIGN, desc=desc)` | `GoalSpec(desc=desc)` |
| `goal_resolver.py:49` | `GoalSpec(type=GoalType.FEATURE, desc=user_text)` | `GoalSpec(desc=user_text)` |

工具层内部若仍需表示语义类型（如 `agent.py` 的 `_MODE_ROUTES`），使用字符串而非 `GoalType`，用于设置 `WorkflowRunState.goal_type` 等场景，但不再用于构造 `GoalSpec`。

### 不变的部分

- `PlanResolution` 模型保留（`{join, leave}`），工具层仍使用
- `WorkflowRunState.goal_type` 保留（`str` 类型，工具层按需设置）
- `ToolStatePatch` 接口不变
- `GoalSpec.label` 属性保留，简化为 `return self.desc.strip() or ""`

## 变更清单

### goal_resolver.py

| 变更 | 说明 |
|------|------|
| 新增 `ResolverGoal` Pydantic 模型 | `{intent: Literal["coding", "general"], goal: str \| None, workflow: WorkflowName \| None, kind_hint: str \| None}` |
| `with_structured_output` 改用 `ResolverGoal` | LLM 返回精简结构 |
| 新增 `_to_goal_resolution()` | ResolverGoal → GoalResolution 转换 |
| 重写 `_resolver_messages_from_exchanges()` | 生成结构化 Markdown request，历史对话只保留 content，schema 放在最后 |
| 精简 `_resolver_system_prompt()` | 移除 goal.type、plan.leave、intent.desc、短续接规则 |
| `_coerce_resolution()` 兼容旧结构 | 允许 legacy `goal.type` / `plan.join` 输入过渡到新 schema |
| `_ALLOWED_JOIN_NODES` 加入 `verify` | 修复逻辑断裂 |
| `_normalize_resolution` 移除 `interaction_mode` 参数 | 死代码清理 |
| `_normalize_resolution` 增加短续接 fallback | 短输入 + active workflow + 空 workflow 时继续当前 workflow |
| `_normalize_resolution` 简化 | 不再需要 leave 校验、goal.type 校验（转换层已处理） |
| `_coerce_resolution` 改为解析 `ResolverGoal` | 返回 `ResolverGoal \| None`，后续由 `_to_goal_resolution` 转换 |
| `_log_goal_resolver_decision` 适配新字段 | goal_desc 从 goal.desc 取，goal_type 从 join 派生；可附带 resolver `kind_hint` 作为诊断字段 |
| `resolve_plan_mode` 移除 `IntentResolution` 的 desc 参数 | `IntentResolution(type=TaskIntent.CODING)` |
| `resolve_goal_mode` 移除 `IntentResolution` 的 desc 参数 | `IntentResolution(type=TaskIntent.CODING)` |
| `resolve_plan_mode` / `resolve_goal_mode` 中 `GoalSpec` 移除 type | `GoalSpec(desc=desc)` / `GoalSpec(desc=user_text)` |

### task_state.py

| 变更 | 说明 |
|------|------|
| `IntentResolution` 移除 `desc` 字段 | 死字段清理 |
| `IntentResolution` 默认工厂更新 | `default_factory=lambda: IntentResolution(type=TaskIntent.CODING)` |
| `GoalSpec` 移除 `type` 字段 | 简化为 `{desc: str}` |
| `GoalSpec.label` 简化 | `return self.desc.strip() or ""` |
| `GoalType` / `infer_goal_type()` / `goal_type_value()` 降级为 deprecated compatibility exports | runtime source 不再依赖，后续可单独删除 |
| `_coerce_goal()` 保留兼容旧 dict | `goal_label()` 可继续安全读取旧数据 |
| `goal_label()` 简化 | `return goal.desc.strip() if goal and goal.desc else ""` |
| `_same_goal` 简化 | 只比较 `left.desc == right.desc` |
| `TaskState.set_goal` 简化 | `GoalSpec(desc=goal)` 不再调用 `infer_goal_type` |
| 新增 `goal_type_from_join()` 函数 | join → goal_type 字符串派生，供下游使用 |

### 下游适配（机械替换）

| 文件 | 变更 |
|------|------|
| `runtime_context.py:258` | `Goal type: {type.value}` → 移除该行（下一行 `Goal: {desc}` 已提供信息） |
| `llm.py:79` | `goal_type_value(current_goal)` → `goal_type_from_join(workflow_start)` |
| `_voidx_graph.py:358` | `goal.type.value if goal is not None else ""` → `goal_type_from_join(plan.join) if plan is not None else ""` |
| `run_loop.py:186` | `goal_type_value(...)` → `goal_type_from_join(...)` |
| `turn_runner.py:70` | `goal.type.value` → persona 映射改为从 join 派生 |
| `slash/handler.py:217` | `goal_type_value(task_state.current_goal)` → `goal_type_from_join(task_state.workflow_route.join)` |
| `reconcile.py:260` | `goal.type.value if goal is not None else ""` → `goal_type_from_join(target)` |
| `workflow.py:55` | `value.get("type")` 不变（IntentResolution 仍有 type） |

### 工具层适配

| 文件 | 变更 |
|------|------|
| `clarify.py:117` | `GoalSpec(type=..., desc=answer)` → `GoalSpec(desc=answer)` |
| `agent.py:254` | `GoalSpec(type=goal_type, desc=...)` → `GoalSpec(desc=...)` |
| `plan_checkpoint.py:126,141,156,162` | `GoalSpec(type=GoalType.XXX, desc=...)` → `GoalSpec(desc=...)` |

### 导出兼容

| 文件 | 变更 |
|------|------|
| `runtime/__init__.py` | 新增 `goal_type_from_join` 导出；`GoalType`, `infer_goal_type`, `goal_type_value` 可短期保留为 deprecated compatibility exports |
| `agent/task_state.py` | 新增 `goal_type_from_join` 导出；`GoalType`, `infer_goal_type`, `goal_type_value` 可短期保留为 deprecated compatibility exports |

### 测试适配

以下测试文件需要同步更新：

| 文件 | 变更 |
|------|------|
| `test_goal_resolver.py:35` | `assert schema is GoalResolution` → `assert schema is ResolverGoal` |
| `test_goal_resolver.py:52` | `IntentResolution(type=..., desc=...)` → `IntentResolution(type=...)` |
| `test_goal_resolver.py:53` | `GoalSpec(type=GoalType.REVIEW, desc=...)` → `GoalSpec(desc=...)` |
| `test_goal_resolver.py:66` | `assert result.intent.desc == "review requested"` → 移除 |
| `test_goal_resolver.py:79` | `"goal: null or {type:"` → `"goal: null or string"` |
| `test_goal_resolver.py` | 新增：`ResolverGoal Schema` 是 Markdown request 最后一节 |
| `test_goal_resolver.py` | 新增：历史对话只保留 content，不保留 role/tool metadata |
| `test_goal_resolver.py` | 新增：legacy `GoalResolution` shape 可被 `_coerce_resolution()` 转为 `ResolverGoal` |
| `test_goal_resolver_advanced.py:56` | `IntentResolution(type=..., desc=...)` → `IntentResolution(type=...)` |
| `test_goal_resolver_advanced.py:57` | `GoalSpec(type=..., desc=...)` → `GoalSpec(desc=...)` |
| `test_goal_resolver_advanced.py:196` | `assert result.intent.desc == "continuation of active workflow"` → 移除 |
| `test_goal_resolver_advanced.py:347-367` | 短续接规则测试 → 改为 normalize fallback 测试（提示词不再包含短续接规则） |
| `test_goal_resolver_advanced.py` | 新增：resolver 返回 `kind_hint` 时不写入 `GoalResolution` / `TaskState` |
| `test_goal_resolver_advanced.py` | 新增：resolver 返回 `workflow` 时转换为 `PlanResolution.join` |
| `test_task_state.py:27` | `IntentResolution(type=intent, desc="test resolution")` → `IntentResolution(type=intent)` |
| `test_tool_state_patch.py:76,82` | `IntentResolution(type=..., desc=...)` → `IntentResolution(type=...)` |
| `test_state_update_from_executed_tools.py:78,104,105` | `IntentResolution(type=..., desc=...)` → `IntentResolution(type=...)` |
| 运行时行为测试中 `GoalSpec(type=GoalType.XXX, desc=...)` | → `GoalSpec(desc=...)` 或只断言 `desc` |
| 兼容性测试中 `GoalType` 引用 | 可暂留，验证旧输入不会进入 runtime state |

### 不变的部分

- `PlanResolution` 模型保留（`{join, leave}`），工具层仍使用
- `WorkflowRunState.goal_type` 保留（`str` 类型，工具层按需设置）
- `ToolStatePatch` 接口不变
- 工具层内部的语义映射可保留为字符串（用于 `WorkflowRunState.goal_type` 等场景）

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| ResolverGoal 独立于 GoalResolution | 直接修改 GoalResolution | 解耦 LLM 返回结构和下游接口，避免级联修改 |
| 保留 `kind_hint` 但不用于处理 | 彻底删除 LLM 语义类型 / 继续使用 `goal.type` | 保留 LLM 的语义观察用于诊断，同时避免非权威字段进入 canonical runtime state |
| 删除 GoalSpec.type 的 canonical state 地位 | 保留 type 由 infer_goal_type 推断 | type 与 join 语义重叠，infer_goal_type 是不可靠的启发式，删除后所有路由由 join 决定，更简单更可靠 |
| 新增 goal_type_from_join 派生映射 | 继续从 GoalSpec/infer_goal_type 派生 | 下游部分场景仍需 goal_type 字符串（日志、WorkflowRunState、UI），从 join 派生比从文本推断更可靠——join 是确定值，文本推断是概率性的 |
| leave 默认 None | resolver 推断 leave | resolver 不了解执行上下文，工具层按需设置更准确 |
| 移除 intent.desc | 保留并补充消费点 | desc 与 goal 语义重复，无消费点证明其价值 |
| 移除短续接规则 | 保留 | LLM 有历史上下文能自然理解续接，normalize 有保底逻辑 |
| verify 加入 ALLOWED_JOIN | 从提示词移除 verify | verify 是合法入口（tdd→verify→review），应允许直接进入 |
| runtime_context.py Goal type 行移除 | 改用 goal_type_from_join | 下一行 `Goal: {desc}` 已提供更详细的用户意图信息，type 标签冗余 |

## Open Questions

- [x] ~~`IntentResolution` 移除 desc 后，`ToolStatePatch.intent` 的消费点是否需要同步清理~~ — 已确认无影响：`workflow.py:55` 只读 `value.get("type")`，不读 desc
- [ ] `_infer_leave` 当前默认全部返回 None，是否需要为特定 join 值设置非 None leave（如 brainstorm → leave=brainstorm 限制在 brainstorm 内？目前 plan_mode 这样做，但 auto 模式不需要）
- [ ] `goal_type_from_join` 映射表是否需要与工具层的 `_MODE_ROUTES` / `goal_type_map` 统一管理，避免三处维护同一映射关系
