> **Status: Done**

# Workflow 工具 `evidence` → `goal` 改造 — 技术设计文档

## Context

workflow 工具当前有一个 `evidence` 参数，语义是"为什么满足条件"的自由文本备注。实际使用中：

- `evidence` 存入 `WorkflowEvidence.summary` 和 `WorkflowStateEvent.reason`，但不影响任何状态决策
- `evidence` 字段 description 声称 "Required for 'advance' and 'done'"，但代码中**从未校验**——`_enter`、`_advance`、`_done` 都不检查 evidence 是否为空（测试 `test_workflow_advance_accepts_empty_evidence` 明确验证 advance 接受空 evidence）
- LLM 每次调用 workflow 都要编一句 evidence，增加无意义的 token 开销
- `WorkflowRunState` 有 `goal_type`（从 join 节点名映射来的类别）但没有 `goal`（具体目标描述）
- `TaskState.current_goal`（由 goal_resolver 解析）和 workflow run 之间没有联动——workflow run 不知道自己的目标是什么

与此同时，runtime 已有完整的 goal 回写机制：`ToolStatePatch.goal` → `_state_update_from_executed_tools` → `TaskState.current_goal`。但 workflow 工具从未设置过 `ToolStatePatch.goal`。

本次改造将 `evidence` 替换为 `goal`，定义为"当前工作流的目标"，并打通 workflow 工具与 runtime `current_goal` 的双向联动。

> **行为变更注意**：这不只是参数改名。当前 `evidence` 从未被强制校验（空值也能通过）。新设计要求 `enter` 的 `goal` 非空；`advance` 可省略 `goal`，但必须能从 active run 或 `TaskState.current_goal` 继承到有效目标，否则返回 guidance。这是从无校验到有语义校验的行为变更。

同时解决 resolver goal 文本过长的问题：当前 `ResolverGoal.goal` 无长度约束，resolver system prompt 只说"short summary"但无硬限制，导致 goal 经常是一整段用户原文，灌入 prompt 的 `- Goal:` 行和 `WorkflowRunState.scope` 后膨胀 token 开销。

## Goal 参数定义规范

`goal` 是"当前工作流要解决什么问题"的一句话描述，面向 LLM 自身和 runtime 状态消费。

### 长度约束

| 来源 | 约束 | 实现方式 |
|------|------|----------|
| `WorkflowInput.goal`（LLM 传入） | ≤ 120 字符 | Pydantic `max_length=120`（工具入口硬校验，超长直接报错） |
| `ResolverGoal.goal`（resolver 解析） | ≤ 120 字符 | resolver system prompt 加硬约束 + `_to_goal_resolution` 构造 `GoalSpec` 时截断 |
| `GoalSpec.desc`（runtime 存储） | ≤ 120 字符 | `GoalSpec` 的 `model_validator(mode="after")` 截断（覆盖所有构造路径） |
| `WorkflowRunState.goal` | ≤ 120 字符 | 继承自上述来源，不单独截断 |

> **为什么 `ResolverGoal.goal` 和 `GoalSpec.desc` 不用 Pydantic `max_length`？**
> `ResolverGoal` 由 LLM 结构化输出构造。`_coerce_resolution`（`goal_resolver.py:323-325`）在 `model_validate` 失败时返回 `None`，触发 fallback 到 `GoalSpec(desc=user_text)`——完整用户原文，比截断后的 LLM goal 更长。加 `max_length` 会让超长 goal 静默回退到更差的结果。
> `GoalSpec` 同理：`set_goal` 接收 `str` 时先构造 `GoalSpec(desc=goal)`，如果 `desc` 有 `max_length`，超长字符串会在构造时抛 `ValidationError`，截断代码永远不执行。
> 因此这两个字段用 `model_validator(mode="after")` 做截断而非 `max_length` 做拒绝。

### 语义规范

- 用一句话描述目标，不要复述用户原文
- 用动词开头（"修复…"、"实现…"、"重构…"、"审查…"）
- 不要包含实现细节、文件路径、代码片段
- 好例子：`"修复登录页 OAuth 回调的 500 错误"`
- 坏例子：`"用户说登录的时候点击 Google 登录按钮之后页面跳转到 /callback 然后报 500 错误，需要检查 callback handler 的异常处理逻辑"`

### 出现位置

goal 文本在 prompt 中出现在以下位置，过长会直接膨胀每轮 token：

1. `runtime_context.py:264` — `- Goal: {current_goal.desc}`（每轮 system prompt）
2. `llm.py:95` — `scope=goal_label(current_goal)` → `WorkflowRunState.scope`（workflow run 元数据）
3. `goal_resolver.py:288` — resolver 请求里的 `- goal: {goal}`（resolver prompt，`_resolver_request_markdown` 构造）

## Goals and Non-Goals

### Goals

- `WorkflowInput.evidence` → `goal`，语义为“当前工作流要解决什么问题”，不是 transition evidence/reason
- `enter` 必填 `goal` 且不能为空；`advance` 的 `goal` 可选，仅用于显式 retarget；`done` 不需要也不使用 `goal`
- workflow 工具在存在有效 goal 时通过 `ToolStatePatch.goal` 同步 `TaskState.current_goal`；`done` 和 guidance 路径不写 goal
- `WorkflowRunState` 新增 `goal: str` 字段，让 run 自身持有目标
- `advance` 时后继节点继承有效 goal：`inp.goal`（显式 retarget）→ 前驱 `run.goal` → `TaskState.current_goal.label`
- auto-advance / reconcile 触发的后继节点通过 runtime `_activate_transition_targets` 继承前驱 `run.goal`，避免目标断链
- `goal` 全链路长度约束 ≤ 120 字符（`WorkflowInput.goal` 用 `max_length`，`GoalSpec.desc` 用 `model_validator` 规范化并截断，`ResolverGoal.goal` 靠 prompt 约束 + `_to_goal_resolution` 构造 `GoalSpec` 时兜底）
- resolver system prompt 加 goal 长度和语义硬约束，减少 goal 文本过长
- `GoalSpec` 新增 `model_validator(mode="after")` 对 desc 做 `strip`、空白折叠和截断兜底（覆盖 `set_goal`、`_to_goal_resolution`、`update_after_turn` 等所有构造路径）

### Non-Goals

- 不改 `goal_resolver` 的解析流程和 LLM 调用方式——只改 system prompt 的 goal 字段约束
- 不改 `WorkflowEvidence` 数据结构——evidence trail 保留，`summary` 继续记录节点生命周期摘要
- 不改 `plan_checkpoint.py` 的 evidence 用法——它不通过 `WorkflowInput`，不受影响
- 不把 `goal` 当作"为什么满足 condition"的证据文本使用；transition 上下文继续由 `WorkflowStateEvent.summary`、`condition`、`ref` 表达
- 不改 `ToolContext` 数据模型——`ctx.goal_target` 已承载 `current_goal.label`（runtime 在 `executor.py:121` 注入），是 `_advance` 函数访问 `current_goal` 的唯一入口

## Architecture
### 数据流

#### Enter 设置目标

```
LLM 调用 workflow(action="enter", workflow="debug", goal="修复登录 bug")
    │
    ├─→ WorkflowInput.goal = "修复登录 bug"
    │
    ├─→ _activate_node: WorkflowRunState.goal = "修复登录 bug"
    │   └─ WorkflowEvidence.summary = "修复登录 bug" (保持 evidence trail)
    │
    └─→ ToolStatePatch(goal=GoalSpec(desc="修复登录 bug"))
            │
            └─→ _state_update_from_executed_tools
                    │
                    └─→ update["current_goal"] = {"desc": "修复登录 bug"}
                            │
                            └─→ TaskState.current_goal = GoalSpec(desc="修复登录 bug")
```

#### Advance 继承或 retarget

```
workflow(action="advance", condition="completed")
    │
    └─→ effective_goal = selected_run.goal or ctx.goal_target
            │                                                         
            │  注：ctx.goal_target 由 runtime 在工具执行前从               
            │  TaskState.current_goal.label 注入（executor.py:121）
            │  这是 _advance 函数中访问 current_goal 的唯一入口
            │
            ├─→ advance_workflow_states(...) 激活后继节点
            │
            ├─→ workflow 工具把 activated successors 的 WorkflowRunState.goal 设为 effective_goal
            │
            └─→ ToolStatePatch.goal = GoalSpec(desc=effective_goal)
```

```
workflow(action="advance", condition="completed", goal="实现 workflow goal 参数改造")
    │
    └─→ effective_goal = inp.goal  # 显式 retarget
            │
            ├─→ 前驱 completed run 保留原 goal
            ├─→ 后继 active run 使用新 goal
            └─→ current_goal 更新为新 goal
```

### 联动关系

| 方向 | 机制 | 触发点 |
|------|------|--------|
| resolver → workflow run | reconcile 时 `WorkflowRunState.goal_type` 从 join 映射，`scope` 仍来自 `goal_label(current_goal)` | 已有，不改 |
| workflow enter → TaskState.current_goal | `ToolStatePatch.goal` → `_state_update_from_executed_tools` → `update["current_goal"]` → `_apply_state_update` 写入 `runtime_task_state[1]` → 后续 `make_context()` 刷新 `ctx.goal_target` | 本次新增 |
| workflow advance → TaskState.current_goal | 解析 `effective_goal` 后条件式写入 `ToolStatePatch.goal`，同上写回路径 | 本次新增 |
| workflow advance → 后继节点 | 工具触发时对 activated successors 写入 `effective_goal`；auto-advance / reconcile 路径通过 `_activate_transition_targets` 继承前驱 `run.goal` | 本次新增 |
| auto-advance / reconcile → 后继节点 | `_activate_transition_targets` 构造新 `WorkflowRunState` 时新增 `goal=run.goal`（不需改函数签名，`goal` 字段有默认值 `""`） | 本次新增 |

### ToolStatePatch（不改）

```
ToolStatePatch
├── intent: IntentResolution | None
├── goal: GoalSpec | None           ← 已有，workflow 工具开始条件式使用
├── plan: PlanResolution | None
├── persona: str | None
└── workflow_runs: list[WorkflowRunState]
```

### GoalSpec（修改）

```
GoalSpec
├── desc: str (default="")              ← 不加 max_length，改用 model_validator 规范化并截断
│   └── @model_validator(mode="after")
│       self.desc = " ".join(self.desc.split())[:120]
│       return self
└── label: str (property, = desc.strip())
```

截断放在 `model_validator` 而非 `set_goal` 中，因为 resolver 路径不走 `set_goal`：
- resolver 路径：`_to_goal_resolution`（`goal_resolver.py:332`）→ `GoalSpec(desc=resolver.goal)` → `GoalResolution` → `update_after_turn`（`task_state.py:102`）→ `self.current_goal = resolution.goal`（直接赋值，不经过 `set_goal`）
- 工具路径：`_state_update_from_executed_tools` → `update["current_goal"]` → runtime 重建 `GoalSpec`

`model_validator` 覆盖两条路径的所有 `GoalSpec` 构造，无需在各调用点分别截断。

### ResolverGoal（修改）

```
ResolverGoal
├── intent: Literal["coding", "general"]
├── goal: str                           ← 不加 max_length（避免校验失败回退到 user_text）
├── workflow: WorkflowName | None
└── kind_hint: str | None
```

### Resolver system prompt（修改）

`_resolver_system_prompt` 的 goal 字段规则从：

> Always provide a short user-language summary of what the user wants this turn. Never null or empty.

改为：

> One-sentence goal of what the user wants this turn. Verb-first (e.g. "Fix…", "Implement…", "Refactor…"). Max 120 characters. Do not copy user text verbatim. No file paths or code.

## API Contract

### `workflow(action="enter", workflow=<node>, goal=<text>)`

- **Signature**: `WorkflowInput(action="enter", workflow="debug", goal="修复登录 bug")`
- **行为**: 激活指定节点，设置 `WorkflowRunState.goal`，通过 `ToolStatePatch.goal` 覆盖 `current_goal`
- **校验**: `goal` 非空，空则返回 guidance
- **Response payload**:
  ```json
  {
    "action": "enter",
    "workflow": "debug",
    "activated": ["debug"],
    "goal": "修复登录 bug",
    "next_hints": [...]
  }
  ```
- **Errors**: `goal_required` — goal 为空时返回 guidance
- **evidence→goal 改名**：`_enter` 函数体有 3 处 evidence 引用需处理：
  - `workflow.py:134`（already_active 分支）— `"evidence": inp.evidence.strip()` → `"goal": inp.goal.strip()`，并传入 `_success(goal=inp.goal.strip())`
  - `workflow.py:163`（正常路径）— `_activate_node(updated, node_name, evidence=inp.evidence.strip())` → `_activate_node(updated, node_name, goal=inp.goal.strip())`（`_activate_node` 的 `evidence` 参数改名为 `goal`，仍写入 `WorkflowEvidence.summary` 保持 evidence trail）
  - `workflow.py:170`（正常路径）— `"evidence": inp.evidence.strip()` → `"goal": inp.goal.strip()`
- **already_active 同步规则**：already_active 分支不能假设 run 上已有正确 goal。若传入 goal 与 active run 的 `goal` 不同，应更新该 run 的 `goal` 并通过 `_success(goal=...)` 同步 `current_goal`，避免 `TaskState.current_goal` 与 `WorkflowRunState.goal` 分叉。
- **`_activate_node` 新增 goal 设置**：`_activate_node` 函数体（`workflow.py:541-571`）在激活节点时需新增 `existing.goal = goal`，将 goal 写入 `WorkflowRunState.goal`。

### `workflow(action="advance", condition=<exit>, goal=<optional retarget>)`

- **Signature**: `WorkflowInput(action="advance", condition="completed")` 或 `WorkflowInput(action="advance", condition="completed", goal="实现 workflow goal 参数改造")`
- **行为**: 用 condition 驱动转换；`goal` 为空时继承当前目标，`goal` 非空时作为后继节点的显式 retarget 目标
- **有效目标解析顺序**: `inp.goal.strip()` → `selected_run.goal.strip()` → `ctx.goal_target`
- **Note**: `ctx.goal_target` 由 runtime 在 `ToolContext` 构造时注入（`goal_target=goal_label(current_goal)`，见 `executor.py:121`），是 `_advance` 函数访问 `TaskState.current_goal.label` 的唯一入口。本 spec 不修改 `ToolContext` 数据模型。
- **校验**: 若上述解析后仍无有效 goal，返回 guidance（`reason="goal_required"`），不执行转换
- **Response payload**:
  ```json
  {
    "action": "advance",
    "from": "design",
    "condition": "completed",
    "activated": ["plan"],
    "goal": "实现 workflow goal 参数改造",
    "goal_source": "input",
    "next_hints": [...]
  }
  ```
- **Errors**: `goal_required` — `inp.goal`、前驱 `run.goal`、`ctx.goal_target` 都为空时返回 guidance
- **evidence→goal 改名**：`_advance` 函数体（`workflow.py:208-234`）需避免把 goal 当 evidence 使用：
  - `workflow.py:208` — `evidence = inp.evidence.strip()` → 解析 `retarget_goal = inp.goal.strip()` 和 `effective_goal`
  - `workflow.py:215` — `reason=evidence` → 不再写入 goal；保持空 reason 或固定生命周期 reason，转换上下文由 `summary` / `condition` / `ref` 表达
  - `workflow.py:234` — `"evidence": evidence` → `"goal": effective_goal`，并可加入 `"goal_source"`
  - `workflow.py:214` — `summary=f"Workflow node {selected.name} completed."` 已是固定文本，不改
- **后继写入规则**：`advance_workflow_states(...)` 返回后，workflow 工具应只对本次新激活的 successors 写入 `goal=effective_goal`；前驱 completed run 保留原 `goal`，除非后续有明确需求要记录 retarget 历史。
- **runtime 继承规则**：`_activate_transition_targets` 构造新 run 时新增 `goal=run.goal`，用于 auto-advance / reconcile 等不经过 workflow 工具 retarget 的路径。

### `workflow(action="done")`

- **Signature**: `WorkflowInput(action="done")`
- **行为**: 结束当前节点，不激活后继，不设 `ToolStatePatch.goal`
- **Response payload**:
  ```json
  {
    "action": "done",
    "from": ["verify"],
    "activated": []
  }
  ```
- **注意**: payload 不含 `goal` 字段。`_done` 函数体（`workflow.py:257-295`）有 4 处 evidence 引用需处理：
  - `workflow.py:258` — `evidence = inp.evidence.strip()` → 删除（done 不读 goal）
  - `workflow.py:264` — `"evidence": evidence`（no_active_nodes 分支 payload）→ 删除该字段
  - `workflow.py:278` — `summary=evidence or "Workflow node completed."`（`_satisfy_active_runs` 的 summary 参数）→ `summary="Workflow node completed."`（改用固定文本）。当前 `done` 时用户传的 evidence 会写入 `WorkflowEvidence.summary`，改成固定文本后这个信息丢失。这是可接受的：`done` 的语义是“结束节点”，`WorkflowStateEvent` 已记录 `condition`（terminal condition）和 `ref`（`tool:workflow`），evidence trail 上下文足够。
  - `workflow.py:287` — `"evidence": evidence`（正常分支 payload）→ 删除该字段

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `enter` 时 goal 为空 | 返回 guidance，`reason="goal_required"`，不激活节点 |
| `advance` 时 goal 为空但可从 active run/current_goal 继承 | 允许转换，payload 返回继承后的 `goal` 和 `goal_source` |
| `advance` 时无任何有效 goal | 返回 guidance，`reason="goal_required"`，不执行转换 |
| `done` 时传了 goal | 忽略，不报错，不写 payload，不更新 `current_goal` |
| reconcile 产生的 run 无 goal | `WorkflowRunState.goal` 默认 `""`，不报错；后继继续继承空 goal |
| `enter`/`advance` 时 goal 超 120 字符 | Pydantic `max_length=120` 校验失败，返回 `Invalid arguments` 错误 |
| resolver 产出 goal 超 120 字符 | `_to_goal_resolution` 构造 `GoalSpec` 时由 `model_validator` 规范化并截断到 120 字符，不报错 |

### Guidance 文本同步

`workflow.py` 中以下位置包含 `evidence`，改名后需同步更新：

- `WorkflowInput.evidence` 字段（`workflow.py:50-53`）：字段名 `evidence` → `goal`，description 从 `"Brief evidence that the condition is satisfied. Required for 'advance' and 'done'."` 改为 `"One-sentence goal of the current workflow. Verb-first, no implementation details. Required for 'enter'. Optional retarget for 'advance'. Ignored for 'done'."`，新增 `max_length=120`
- `_suggested_advance_call`（`workflow.py:537`）：`evidence="..."` → 删除 goal 或使用继承路径示例；retarget 示例可显式传 `goal="..."`
- `_suggested_done_call`（`workflow.py:538`）：`evidence="..."` → 删除（`done` 不需要 goal）
- `_select_advance_run` guidance（`workflow.py:475`）：`evidence="..."` → 删除 goal 或使用 optional retarget wording

## Test Impact

### 需修改的测试文件

| 文件 | 改动 |
|------|------|
| `src/tests/test_tools/test_workflow_tool.py` | `enter` 调用参数从 `"evidence"` 改为 `"goal"`；`advance` 用例可省略 goal 测继承，也可传 goal 测 retarget；`test_workflow_advance_accepts_empty_evidence` 改为验证 advance 在有继承目标时接受空 goal、在无继承目标时返回 guidance；payload 不再断言 `evidence` |
| `src/tests/test_agent/graph/test_workflow_done.py` | `done` 调用中的 `"evidence"` 删除；payload 中不再有 `evidence`/`goal` 字段；新增断言 `current_goal` 不变 |
| `src/tests/test_agent/graph/test_workflow_review.py` | `"evidence": "red-green cycle completed"` 不再适合作为 goal；删除或改为 retarget 语义的 `"goal"` |
| `src/tests/test_agent/graph/test_workflow_transactions_barrier.py` | `"evidence": "stale design gate cleared"` 不再适合作为 goal；删除或改为 retarget 语义的 `"goal"` |

### 不需修改的测试文件

| 文件 | 理由 |
|------|------|
| `src/tests/test_workflow/test_workflow_reconcile.py` | `.evidence[-1]` 断言访问的是 `WorkflowEvidence` 对象属性（`run.evidence` 列表），不是 `WorkflowInput.evidence` 参数，不受改名影响；但新增 `WorkflowRunState.goal` 默认字段后如有 exact dump 断言需补字段 |

### 新增测试

- `test_workflow_enter_requires_goal`：验证 `enter` 时 goal 为空返回 guidance（`reason="goal_required"`）
- `test_workflow_enter_sets_run_goal_and_current_goal`：验证 `enter` 后 `WorkflowRunState.goal` 和 `TaskState.current_goal` 被设置
- `test_workflow_enter_already_active_updates_goal`：验证 already_active 分支会同步不同的新 goal
- `test_workflow_advance_inherits_goal_without_input`：验证 `advance` 不传 goal 时继承前驱 run goal
- `test_workflow_advance_retargets_successor_only`：验证 `advance(goal=...)` 只 retarget 后继节点，前驱 completed run 保留原 goal
- `test_workflow_advance_requires_goal_when_no_context`：验证无 `inp.goal`、无前驱 goal、无 current_goal 时返回 guidance（`reason="goal_required"`）
- `test_workflow_done_preserves_current_goal`：验证 `done` 后 `current_goal` 不变且 state patch 不含 goal
- `test_goal_spec_normalizes_and_truncates`：验证 `GoalSpec(desc=long_multiline_string)` 空白折叠并截断到 120 字符
- `test_auto_advance_inherits_run_goal`：验证 auto-advance / reconcile 路径通过 `_activate_transition_targets` 继承前驱 `run.goal`

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 新增 `WorkflowRunState.goal` 字段 | 复用 `scope` 字段 | `scope` 语义是“变更范围摘要”，和“工作流目标”不同；分开更清晰 |
| goal 存入 `WorkflowEvidence.summary`（enter 激活摘要） | 新增 `WorkflowEvidence.goal` 字段 | evidence trail 的 summary 本就是自由文本，复用即可，避免改 schema |
| `advance.goal` 作为 optional retarget | `advance.goal` 必填并当作 transition evidence | 必填会迫使模型编造“设计已批准”这类非目标文本，污染 `current_goal`；optional retarget 才符合 goal 语义 |
| 后继节点继承有效 goal | 后继节点 goal 留空 | 工具 advance 可显式 retarget；未 retarget 时继承前驱/current goal；auto-advance/reconcile 路径也需要继承保证不断链 |
| `_success` 条件式 include `"goal"` | 总是 include `"goal"` / 全量 dump `ToolStatePatch` | 条件式 include 避免 `done` 或无 goal 路径清空/污染 `current_goal`；精确 include 避免误传 intent/plan |
| `done` 不覆盖 current_goal | done 时清空 current_goal | done 只是结束节点，不代表目标消失；下轮 resolver 会重新解析 |
| goal 长度限制 120 字符 | 不限制 / 用更短的 80 字符 | 120 字符够装一句中文目标（约 40-60 个汉字）；80 太短容易截断语义；不限制则 resolver 经常产出整段用户原文 |
| `GoalSpec.desc` 用 `model_validator` 规范化并截断 | Pydantic `max_length` 报错 / `set_goal` 中截断 | `max_length` 会导致 `ResolverGoal` 校验失败回退到完整 `user_text`（更差）；`set_goal` 覆盖不到 resolver 路径；`model_validator` 覆盖所有 `GoalSpec` 构造路径 |
| resolver prompt 加 verb-first 约束 | 只加长度约束 | 长度解决“太长”，verb-first 解决“太散”——强制 LLM 提炼成动作目标而非复述原文 |

## Resolved Questions

- [x] `advance` 时 LLM 传的 goal 和前驱 run 的 goal 不一致时，是否覆盖前驱 goal？**决策：不覆盖**。`advance.goal` 表示后继工作流 retarget；前驱 completed run 保留原 `goal`，后继 active run 和 `current_goal` 使用新 goal。
- [x] `advance` 不传 goal 时是否允许？**决策：允许**，但必须能从 active run 或 `TaskState.current_goal` 继承到有效 goal；否则返回 `goal_required` guidance。
- [x] `done` 是否清空 current_goal？**决策：不清空**。`done` 只结束 workflow node，不代表用户目标完成或作废。
