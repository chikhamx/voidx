# Workflow Gate 死锁修复设计

> **Status: Done**
> 日期: 2026-06-13

## 1. 背景与问题

一次真实交互中，用户先要求“写 spec 文档”，agent 进入 `brainstorm` gate，提出设计方向并等待确认。用户回复“可以啊，写文档”后，agent 正确推进到 `design-doc`，但随后出现反复自我纠错：

1. `brainstorm` 被标记完成，并激活 `design-doc`
2. `design-doc` 需要写入 `docs/specs/...md`
3. `plan` 同时处于 active 状态
4. `plan` gate 拒绝 `write/edit/lsp_format`
5. 权限层对所有 active workflow 的 denied tools 取并集，导致 `design-doc` 的写文档动作被 `plan` gate 拦截
6. agent 试图推进 `plan` 或检查 `design-doc` gate，形成“用户已批准，但系统仍拒绝写文档”的循环体验

这不是单纯提示词问题，而是 workflow 激活策略和 gate 生效范围的语义混淆。

## 2. 现状分析

### 2.1 Node 定义存在职责越界

`brainstorm` 是设计确认节点，gate 禁止写工具，但 workflow step 5 仍写着 “Write design doc”。这让 agent 容易把“设计确认”和“写设计文档”混在同一个 gate 中。

当前应由 `design-doc` 节点负责写文档。`design-doc` 本身没有拒绝写工具，语义上允许写 `docs/specs/...md`。

### 2.2 `plan` 被过早激活

当前有多个路径会提前激活 `plan`：

- `goal_map` 中 `refactor` 直接激活 `brainstorm + plan`
- `agent_name == "plan"` 时无条件激活 `plan`
- plan mode 直接激活 `brainstorm + plan`
- `goal == "design"` 且文本包含 plan 相关词时激活 `plan`

这些入口绕过了 DAG 中的顺序边：

```text
brainstorm --approved--> design-doc --completed--> plan
brainstorm --skip_to_plan--> plan
```

因此 `plan` 在 `design-doc` 尚未完成时就可能成为 active gate。

### 2.3 Gate 生效模型过粗

权限层只看 active workflow 名称，然后对所有 active node 的 `denied_tools` 取并集：

```text
effective_denied_tools = union(active_node.gate.denied_tools)
```

这对单节点流程有效，但在多个 active node 混用时会产生误伤：

- `design-doc` 需要写文档
- `plan` 禁止开始实现前写文件
- 由于工具名都是 `write/edit`，系统无法区分“写文档”和“写代码”

### 2.4 状态展示误导 LLM

Current Task State 将所有 active workflow 平铺展示。LLM 看到 `design-doc` 和 `plan` 同时 active，会自然尝试满足所有 gate，而不是把 `plan` 当成后续阶段。这会放大循环：

```text
design-doc 是当前任务
plan gate 也 active
write 被 plan gate 拦
LLM 尝试先推进 plan
plan 又需要文档/计划完成
```

## 3. 设计目标

- 当前执行节点的 gate 才能阻塞当前工具调用
- 后继节点可以被提示为 next/queued，但不能提前参与权限拒绝
- `brainstorm` 只负责设计确认，不负责写设计文档
- `design-doc` 可以在受控范围内写 `docs/specs/` 文档
- `plan` 只能在前置节点完成后进入 active，不能因 persona 或 refactor goal 提前抢占
- 出现 gate 拒绝时，错误信息能指出具体阻塞节点和可恢复动作

## 4. 非目标

- 不重写整个 workflow DAG
- 不改变 `advance_workflow` 的 barrier 语义
- 不引入新的用户审批 UI
- 不实现完整路径级权限系统；本 spec 只定义最小必要的文档写入例外
- 不改变 plan mode 的总体约束：实现代码仍必须等计划批准

## 5. 方案

### 5.1 Phase 1：短期止血

#### 5.1.1 移除 `brainstorm` 的写文档步骤

将 `brainstorm` 的 workflow step 5 删除或改为：

```text
If a design document is needed, transition to design-doc.
```

`brainstorm` 的输出是“用户批准的设计方向”，不是文件。

#### 5.1.2 `refactor` goal 不再初始激活 `plan`

将 `refactor` 的 `goal_map` 从：

```python
GoalEntry(goal_type="refactor", nodes=["brainstorm", "plan"], reason="goal:refactor")
```

改为：

```python
GoalEntry(goal_type="refactor", nodes=["brainstorm"], reason="goal:refactor")
```

是否进入 `plan` 由 `brainstorm` 的 exit condition 决定：

- `approved` -> `design-doc`
- `skip_to_plan` -> `plan`
- `small_change` -> `tdd`

#### 5.1.3 限制 plan persona 自动激活

`agent_name == "plan"` 不应无条件添加 `plan`。它只能在以下情况激活：

- 没有 active workflow runs
- 用户显式点名 plan/writing-plans，且当前不是 plan mode 的初始 brainstorm 阶段
- 当前 goal 类型就是 implementation plan，且没有 `brainstorm` / `design-doc` 前置节点 active
- 前置节点通过 `advance_workflow` transition 激活

如果已有 `brainstorm` 或 `design-doc` active，plan persona 只影响 persona prompt，不新增 `plan` run。

#### 5.1.4 plan mode 不再同时激活 `brainstorm + plan`

plan mode 当前应进入 `brainstorm`，由用户确认后再通过 DAG 进入 `plan`。即使用户明确说“直接写实施计划”，初始激活也仍然只有 `brainstorm`；agent 应在 `brainstorm` 内判断这是详细 spec / 明确计划请求，然后调用 `advance_workflow(workflow="brainstorm", condition="skip_to_plan")` 进入 `plan`。

### 5.2 Phase 2：active/queued 语义

Phase 1 修正后，`plan` 不再需要为了“下一步预告”提前 active，因此 `QUEUED` 不是当前修复的必要条件。只有当 runtime 未来需要持久化“已确定的后继节点，但尚未开始执行”的状态时，再引入 `QUEUED`。

推荐模型：

```python
class WorkflowRunStatus(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
```

触发场景限定为：

- DAG transition 已确定后继，但当前节点仍需要完成本轮收尾展示
- UI 需要显示“next workflow node”但不希望它参与 gate
- 恢复旧 session 时发现后继节点只应作为下一阶段提示，不应阻塞当前工具

`workflow_denied_tools()` 只读取当前 `ACTIVE` / `BLOCKED` 节点，不读取 `QUEUED`。

语义：

| status | 是否展示 | 是否展开指令 | 是否 gate 生效 |
|--------|----------|--------------|----------------|
| queued | 是 | 摘要或 next steps | 否 |
| active | 是 | 完整指令 | 是 |
| blocked | 是 | 完整指令 + blocker | 是 |
| satisfied | 可摘要 | 否 | 否 |
| skipped | 可摘要 | 否 | 否 |

当前实现暂不持久化 `QUEUED`；用“只激活当前节点 + Current Task State 只展示当前节点”解决截图死锁。

### 5.3 Phase 3：Node 工具能力声明

延续 `workflow-node-refactor-design-2026-06-11.md` 中的工具白名单方向，为 node 增加工具声明：

```python
class WorkflowNode(BaseModel):
    tools: list[str] = []
```

实际可用工具：

```text
role.tools ∩ node.tools ∩ not(node.gate.denied_tools)
```

`design-doc` 应声明：

```python
tools=[
    "read",
    "glob",
    "grep",
    "write",
    "edit",
    "load_doc_template",
    "repo_map",
]
```

`plan` 可以继续限制实现写入，但不应阻止 `docs/specs/` 文档写入。

### 5.4 Phase 4：文档写入例外

在最小范围内区分“写文档”和“写实现代码”：

```python
class NodeGate(BaseModel):
    denied_tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
```

`plan` gate 可以表达：

```python
NodeGate(
    denied_tools=("write", "edit", "lsp_format"),
    allowed_paths=("docs/specs/**", "docs/design/**"),
)
```

权限层判断流程：

1. 工具名不在 denied_tools 中：继续普通权限判断
2. 工具名在 denied_tools 中，但路径匹配 allowed_paths：继续普通权限判断
3. 否则由 workflow gate 拒绝

路径提取结论：

- `write`、`edit`、`lsp_format` 都必须通过 `classify_tool_call()` 的结构化 `file_path` 提取路径
- `edit` 不从 `old_string` / `new_string` 中猜路径；缺少 `file_path` 时不适用 allowed_paths 例外，仍按 gate 拒绝
- `allowed_paths` 只作用于 workflow gate，不能绕过 sandbox、plan mode 或用户权限策略

这不是替代 sandbox；只是 workflow gate 的范围收窄。

### 5.5 多 active 节点的 gate 语义

合法多 active 场景仍会出现，例如 `debug + tdd + verify` 共同描述一个 bugfix 生命周期，或旧 session 恢复出 `design-doc + plan`。权限层不能对所有 active node 的 `denied_tools` 取并集；否则后继节点会误伤当前节点。

当前规则：

```text
current_workflow = active workflows sorted by workflow priority, take first
effective_gate = current_workflow.gate
```

只有当前 workflow node 的 gate 能拒绝工具。后续 active node 的 gate 不参与本次工具拒绝；如果需要它阻塞，必须先通过 `advance_workflow` 或状态恢复让它成为当前优先节点。

## 6. 状态与提示词改动

Current Task State 应尽量只保留当前 active 节点和必要的退出信息：

```text
- Active workflow node: design-doc
- Workflow exits [design-doc]: completed -> plan; done -> end
```

不要把 queued / future 节点提前写进当前状态，除非 runtime 真的引入并持久化了 queued 语义。

如果内部状态中仍存在多个 active workflow，Current Task State 只展示按优先级选出的当前节点；完整 workflow run 列表保留在结构化 `voidx_workflow_context` / runtime snapshot 中，避免 LLM 同时尝试满足多个 gate。

权限拒绝信息不在 Current Task State 里预告，直接由 tool-engine 返回：

```text
Blocked by workflow gate for tool 'write':
- blocker: plan
- reason: plan is active before design-doc completed
- recovery: advance or complete design-doc first, or mark plan queued instead of active
```

这样模型只在真正被挡住时才看到 gate 细节，减少状态面板噪音。

## 7. 实现计划

### Task 1：修正 node 定义

文件：

- `src/voidx/workflow/nodes.py`
- `tests/test_agent/test_core_flow.py`

改动：

- 删除 `brainstorm` 的 “Write design doc” step
- 确认 `design-doc` 是文档写入唯一节点
- 增加测试：`brainstorm` 指令不包含写文件动作

验证：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v
```

### Task 2：修正激活策略

文件：

- `src/voidx/workflow/dag.py`
- `src/voidx/workflow/policy.py`
- `tests/test_agent/test_core_flow.py`

改动：

- `refactor` 初始只激活 `brainstorm`
- plan persona 不再无条件激活 `plan`
- plan mode 初始只激活 `brainstorm`，显式 plan 请求除外

验证：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_skills.py -v
```

### Task 3：补回归测试复现截图场景

文件：

- `tests/test_agent/test_core_flow.py`

测试场景：

1. 初始 active: `brainstorm`
2. 执行 `advance_workflow(workflow="brainstorm", condition="approved")`
3. 后续执行 `write(file_path="docs/specs/example-design-2026-06-13.md")`
4. 断言 write 不被 `plan` gate 拒绝
5. 断言此时 active 只有 `design-doc`，`plan` 尚未 active

### Task 4：引入 queued 状态（Deferred）

文件：

- `src/voidx/workflow/types.py`
- `src/voidx/workflow/runtime.py`
- `src/voidx/agent/runtime_context.py`
- `src/voidx/agent/graph/permissions.py`

改动：

- 本轮不增加 `QUEUED`
- 当前实现改为 `workflow_denied_tools()` / permission gate 仅考虑当前优先级最高的 active node
- Runtime context 暂不展示 queued；未来真正持久化 queued 时再分开展示 active 和 queued

验证：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_compaction.py -v
```

### Task 5：文档写路径例外

文件：

- `src/voidx/workflow/schema.py`
- `src/voidx/agent/graph/permissions.py`
- `tests/test_agent/test_permission.py`

改动：

- `NodeGate` 增加 allowed path pattern
- gate 拒绝前检查文件路径是否在允许范围
- `plan` gate 允许 `docs/specs/**` 和 `docs/design/**`

验证：

```bash
.venv/bin/python -m pytest tests/test_agent/test_permission.py tests/test_agent/test_core_flow.py -v
```

## 8. 测试矩阵

| 场景 | 预期 |
|------|------|
| 用户批准 brainstorm 后写 design doc | 允许写 `docs/specs/**` |
| design-doc 未完成时 | `plan` 不应 active gate |
| design-doc completed 后 | 激活 `plan` |
| plan active 时写源码 | 被 gate 拒绝 |
| plan active 时 write/edit/lsp_format `docs/specs/**` 文档 | workflow gate 允许，继续普通权限判断 |
| plan active 时 edit 源码 | 被 gate 拒绝 |
| 旧 session 残留 `design-doc + plan` active | 当前节点为 `design-doc`，`plan` gate 不误拦文档写入 |
| 用户手动 advance_workflow 导致多 active 并存 | 只让当前优先节点的 gate 阻塞工具调用 |
| plan mode 普通请求 | 先进入 brainstorm |
| 用户明确“直接写实施计划” | 初始仍只有 brainstorm；由 agent 显式 `skip_to_plan` |
| `advance_workflow(..., condition="done")` 且多 active | 仍要求显式 workflow 名称 |

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 放宽 plan gate 后误写代码 | 只允许文档路径例外，源码仍被拒绝 |
| queued 状态引入迁移复杂度 | Phase 1 先通过激活策略止血，queued 可后续独立实现 |
| plan persona 不再自动激活导致计划遗漏 | 对显式 plan 请求保留激活；并依赖 DAG transition |
| 旧 session 中已有多 active 状态 | Runtime context 给出恢复建议；必要时允许 `advance_workflow(workflow=..., condition="done")` 清理 |

## 10. 成功标准

- 截图中的交互路径不再出现 `plan gate` 拦截 `design-doc` 写文件
- 用户批准设计后，agent 能一次性完成 spec 文档写入
- `plan` gate 仍能阻止未批准计划前的源码实现
- Current Task State 明确区分当前节点和后续节点
- 回归测试覆盖多 active gate 的误伤场景

## 11. 开放问题

1. 是否要把 `docs/archive/**` 也加入文档写例外？当前结论：不加入。archive 表示已完成文档，归档动作应在实现完成后由普通权限处理。
2. `edit` 工具的路径参数如何提取？当前结论：统一通过 `classify_tool_call().args["file_path"]` 结构化提取；不从 `old_string` / `new_string` 中猜路径。
3. queued 状态是否需要持久化到 session？当前结论：本轮不持久化。只有未来需要展示 next node 但不让它阻塞时再引入。
4. plan mode 是否应保留“只读”强约束？当前结论：保留。`allowed_paths` 只收窄 workflow gate，不绕过 plan mode 的权限层。

## 12. 关联问题：固定 AgentMaxSteps 不适合主代理和子代理

### 12.1 现象

review 子代理很容易被启动，并且 UI 中经常显示类似 `Reviewer (1/100)`、`Reviewer (2/100)`。用户感知上像是 review 每次都会朝 100 步上限跑，而实际也难以看出它究竟走了几步、为什么还在继续。

同类问题也存在于主代理：`voidx` 自身带着固定 `max_steps=100`，LangGraph recursion limit 又按这个值派生。这个上限既不能表达“这个任务实际需要几步”，也容易让提示词和 UI 围绕数字收敛，而不是围绕任务是否完成、阻塞或需要用户确认来收敛。

### 12.2 根因

当前步数限制有两层：

```python
BUILTIN_AGENTS["voidx"].max_steps = 100
BUILTIN_AGENTS["sub-voidx"].max_steps = 100
AgentMaxSteps(voidx=100, explore=25, plan=30, implement=100, review=30)
```

主代理从 `AgentMaxSteps.voidx` 取值，子代理则先从 `AgentDef.max_steps` 取值，再尝试用 `_apply_max_steps_override()` 覆盖。但 `_apply_max_steps_override()` 只按 `agent_def.name` 查配置：

```python
configured = getattr(steps_map, agent_def.name, None)
```

对子代理来说，`agent_def.name == "sub-voidx"`，配置里没有这个字段，所以 persona 级字段永远不会影响子代理。最终 review 继承 `sub-voidx.max_steps=100`。

更深的问题是：即使修成按 persona 读取 `AgentMaxSteps.review`，也只是把“所有 review 子任务”固定成同一个预算。实际需要的是由 voidx 根据当前任务动态分配预算：

- 小型 review：只检查 1-2 个文件，3-5 步即可
- 常规 review：检查 diff、相关测试和风险点，6-10 步
- 深度 review：跨模块或安全敏感，12-20 步
- 主代理普通问答：不需要步数预算
- 主代理长任务：应由当前 plan/todo/workflow 决定阶段边界

因此 `AgentMaxSteps` 和 `AgentDef.max_steps` 都不应该继续作为主/子代理的固定配置。步数应是一次任务运行的动态预算，而不是 agent/persona 的静态属性。

子代理循环本身并不是强制跑满：`run_subagent()` 在没有 `tool_calls` 时会提前 return。但由于：

- review 实际预算是 100
- convergence hint 只在最后 4 步才出现，过晚
- UI step header 显示的是 `step/agent_def.max_steps`，暴露静态上限
- review workflow 要求收集 evidence / verdict，容易继续读工具

所以 review 的收敛压力来得太晚，用户看到的预算也过大。

### 12.3 修复方案

#### Phase A：移除静态 AgentMaxSteps 与 AgentDef.max_steps

删除或废弃：

- `AgentDef.max_steps`
- `AgentDef.with_max_steps()`
- `AgentMaxSteps`
- settings 中的 `agent_max_steps`
- `_apply_max_steps_override()`
- 基于 `max_steps` 推导 recursion limit 的逻辑

主代理不再有固定 `max_steps`。它的停止条件改为：

- 已满足用户目标并完成必要验证
- workflow gate 要求用户确认
- 工具执行明确阻塞
- context/usage/safety guard 触发
- 用户中断

子代理也不再从配置或 `AgentDef` 读取固定上限，而是每次主 agent 调用 `agent` tool 时显式指定本次 delegation 的 `max_steps`。

#### Phase B：由主 agent 在 `agent` tool 调用时指定 `max_steps`

`AgentInput` 增加必填字段：

```python
class AgentInput(BaseModel):
    agent: str = "sub-voidx"
    persona: str = "explore"
    description: str
    model: str | None = None
    max_steps: int = Field(
        ge=3,
        description=(
            "Step budget chosen by voidx for this delegated task. "
            "Use a small number for narrow review/explore work and a larger "
            "number only for broad or implementation-heavy tasks."
        ),
    )
```

主 agent 必须在每次 `agent` tool 调用中传入 `max_steps`。不传时不 fallback，`agent` tool 直接失败并返回可读错误，例如：

```text
Missing required argument: max_steps. The main agent must choose a step budget for each delegated task.
```

不新增单独的预算提示词。主 agent 的工具 schema 已经描述 `max_steps` 的含义；最后几步收敛约束继续由现有 runtime convergence hint 注入。

工具到运行循环的数据流：

```text
main agent tool call
  agent(persona="review", description="...", max_steps=5)
        │
        ▼
AgentInput.max_steps
        │
        ▼
_subagent_runner(..., max_steps=5)
        │
        ▼
run_subagent(..., max_steps=5)
```

`run_subagent()` 不再从 `agent_def.max_steps` 读取预算；预算作为独立参数传入。

#### Phase C：runtime safety guard 取代固定步数配置

Phase C 已从本设计中拆出为独立待实现 spec：

- `docs/specs/runtime-safety-guards-design-2026-06-13.md`

本设计完成的范围是移除静态 `AgentMaxSteps` / `AgentDef.max_steps` runtime 预算路径，并保留已有 interrupt、context overflow、compaction、prune guard。新的 tool failure loop、no-progress、wall-clock guard 不属于本归档文档的完成标准。

#### Phase D：预算进入工具结果和 UI，不进入 LLM 上下文

`AgentTool` metadata 记录：

```python
metadata={
    "agent": agent_name,
    "persona": runtime_persona,
    "max_steps": inp.max_steps,
}
```

预算不注入子代理 Runtime State。子代理不需要从 Current Task State 读取“自己有多少步”来决定是否继续；循环控制和最后几步 convergence hint 已在 runtime 层处理。

验证结论：子代理最后几步的 convergence hint 会作为临时 `HumanMessage` 进入当次 LLM payload，内容包含 `[Step n/max]`。这属于 runtime 控制提示，不进入持久 Runtime State，也不会写入 `sub_messages` 回传给主代理。主代理不再注入基于静态 `max_steps` 的 convergence hint。

UI 和事件层可以显示预算，因为这是用户可见的运行状态，不是 LLM 推理上下文。

主代理 Runtime State 不显示固定 `Max steps`，只显示当前 todo/workflow/goal 状态和 runtime guard 状态。

#### Phase E：改 UI 展示真实结束原因

`SubagentFinished` 增加可选字段：

```python
final_step: int | None = None
max_steps: int | None = None
finish_reason: str = ""
```

`run_subagent()` 在返回时记录：

- `finish_reason="final_answer"`：模型主动无工具结束
- `finish_reason="step_limit"`：达到最后无工具步或 fallback summary
- `finish_reason="error"`：异常退出

UI 完成态显示：

```text
Reviewer completed (4/12, final answer, 18.2s)
Reviewer completed (12/12, step limit, 74.0s)
voidx paused (no progress guard, 6 calls, needs user confirmation)
```

这样用户能区分“正常 4 步结束”和“真的跑到上限”。

### 12.4 回归测试

文件：

- `tests/test_agent/test_core_flow.py`
- `tests/test_ui_events.py`

测试：

1. `test_agent_tool_uses_delegated_max_steps`
   - `agent(persona="review", max_steps=5, description="Review one file")`
   - 断言传入 `run_subagent(..., max_steps=5)`

2. `test_agent_tool_fails_when_max_steps_missing`
   - 不传 `max_steps`
   - `agent` tool 返回错误
   - 不启动 `run_subagent()`

3. `test_subagent_finished_reports_final_step`
   - 模拟 `SubagentFinished(final_step=3, max_steps=12, finish_reason="final_answer")`
   - 断言 UI header 包含 `3/12`

4. `test_main_agent_has_no_static_max_steps`
   - `AgentDef` 不再暴露 `max_steps`
   - main run state 不再注入固定 `max_steps`

5. `test_agent_max_steps_settings_removed_or_ignored`
   - settings 中旧 `agent_max_steps` 不影响主代理或子代理预算
   - 旧配置存在时不报错，但不参与 runtime budget

### 12.5 成功标准

- review 子代理默认不再显示 `Reviewer (.../100)`
- review 子代理预算由 voidx 在 `agent` 工具调用中分配
- 缺少 `max_steps` 时 `agent` tool 失败，不 fallback
- 主代理不再由 `AgentMaxSteps.voidx` 或 `AgentDef.max_steps` 限制
- 子代理不再由 `AgentMaxSteps.review/explore/plan/implement` 限制
- `AgentMaxSteps` 从 runtime 预算路径中移除
- 子代理预算不注入 LLM 上下文
- UI 能显示子代理实际结束步数和结束原因
- implement 子代理可以分配较大预算，但必须由 voidx 在 delegation 时明确选择

## 13. 关联问题：子代理委托必须有硬门槛

### 13.1 现象

主 agent 目前很容易把普通搜索、单文件阅读、简单 review 交给 `agent` tool。虽然 prompt 已经写了“只有并行独立任务或用户明确要求时才委托”，但这仍是软约束。LLM 一旦觉得 review/explore 是“更稳妥”的动作，就会启动子代理，带来额外 token、时间和 UI 噪音。

### 13.2 原则

子代理是昂贵的隔离执行单元，不是普通工具调用。默认应由主 agent 自己完成，只有满足以下条件之一才允许委托：

- **用户显式要求**：用户说“开个子代理 / 让 reviewer 看 / 并行查”等
- **并行独立任务**：至少两个互不依赖的任务可以并发推进，且开启 parallel subagents
- **隔离审查**：主 agent 已完成实质变更，需要独立 review，且提供 changed files、验证结果和风险点
- **上下文隔离有收益**：任务足够大，独立上下文能减少主上下文污染，且主 agent 给出明确边界

不允许委托的场景：

- 单文件 read/grep/glob
- 简单事实确认
- 主 agent 还没自己看过关键文件
- 只是为了“更保险”而启动 review
- 没有明确 expected output
- 没有指定 `max_steps`

### 13.3 Tool schema 硬约束

`AgentInput` 增加必填字段：

```python
class AgentInput(BaseModel):
    agent: str = "sub-voidx"
    persona: str
    description: str
    max_steps: int
    delegation_reason: Literal[
        "user_requested",
        "parallel_independent",
        "isolated_review",
        "context_isolation",
    ]
    expected_output: str
    parent_evidence: str
    model: str | None = None
```

字段语义：

| 字段 | 要求 |
|------|------|
| `delegation_reason` | 主 agent 为什么不能直接做 |
| `expected_output` | 子代理必须返回什么结构化结果 |
| `parent_evidence` | 主 agent 已经掌握的事实，例如读过的路径、变更文件、测试结果 |
| `max_steps` | 主 agent 为本次委托分配的预算 |

缺少任一必填字段时，`agent` tool 直接失败，不启动子代理。

### 13.4 Tool runtime 校验

`AgentTool.execute()` 做最小硬校验：

1. `max_steps` 缺失或小于 3：失败
2. `description` 过短或不是自包含 brief：失败
3. `expected_output` 为空：失败
4. `parent_evidence` 为空：失败
5. `delegation_reason == "parallel_independent"` 但 parallel subagents 未启用：失败
6. `delegation_reason == "isolated_review"` 且 `persona != "review"`：失败
7. `persona == "implement"` 且当前目标不是 `feature`、`bugfix` 或 `refactor`：失败

这些校验不试图判断 LLM 的理由是否“真实”，但能迫使主 agent 在工具调用里显式承担委托决策，并拦住最随意的调用。

### 13.5 Review 子代理专门门槛

review 子代理还需要额外要求：

- `parent_evidence` 必须包含 changed files 或明确 review target
- 必须包含验证命令或说明未验证原因
- `expected_output` 必须要求 `verdict: PASS | FAIL | NEEDS_CHANGE`
- `max_steps` 通常应选择较小预算，由主 agent 根据 review 范围显式指定

如果只是“看看这个文件有没有问题”，主 agent 应自己 read/grep 并回答，不启动 review 子代理。

### 13.6 UI 与可观测性

子代理节点显示委托原因和预算，但不暴露在 LLM Runtime State：

```text
Reviewer running (reason: isolated_review, budget: 6)
```

完成后显示：

```text
Reviewer completed (4/6, final answer, 18.2s)
```

这样用户能看出为什么开了子代理，以及它是否按预算收敛。

### 13.7 回归测试

文件：

- `tests/test_agent/test_core_flow.py`
- `tests/test_tools/test_basic.py`

测试：

1. `test_agent_input_requires_delegation_budget_and_evidence`
   - 不传 `delegation_reason` / `parent_evidence` / `expected_output` / `max_steps`
   - Pydantic validation 失败，`agent` tool 不启动子代理

2. `test_agent_tool_fails_when_max_steps_missing`
   - 不传 `max_steps`
   - 返回可读错误

3. `test_agent_tool_rejects_missing_delegation_reason`
   - 不传 `delegation_reason`
   - 返回可读错误，不启动子代理

4. `test_agent_tool_rejects_missing_parent_evidence`
   - 不传 `parent_evidence`
   - 返回可读错误，不启动子代理

5. `test_agent_tool_rejects_parallel_reason_when_parallel_disabled`
   - `delegation_reason="parallel_independent"` 且 parallel disabled
   - 断言失败

6. `test_agent_tool_accepts_isolated_review_with_evidence`
   - `persona="review"`
   - 提供 changed files、verification、expected verdict format、max_steps
   - 断言启动子代理

7. `test_agent_tool_rejects_review_without_verdict_contract`
   - `expected_output` 未要求 verdict 三态
   - 断言失败

8. `test_agent_tool_rejects_review_without_target_and_verification`
   - `parent_evidence` 缺少 changed files / review target / verification
   - 断言失败

9. `test_agent_tool_rejects_implement_for_chore_goal`
   - `persona="implement"` 且 `goal_type="chore"`
   - 断言失败

10. `test_orchestrator_prompt_mentions_delegation_gate`
   - prompt 中明确要求优先自己执行简单 read/grep
   - agent tool schema 要求 `reason/evidence/output/max_steps`

### 13.8 成功标准

- 简单 read/grep 不再触发子代理
- `agent` tool 缺少 reason/evidence/output/max_steps 时失败
- review 子代理只在有明确审查对象和父级证据时启动
- UI 能显示子代理委托原因和实际步数
- 主 agent 的 delegation 决策可从 tool args 审计
