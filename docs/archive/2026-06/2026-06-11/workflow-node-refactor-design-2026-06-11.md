# Workflow Node 重构设计

> **Status: Done**
> 日期: 2026-06-11

## 1. 背景与问题

voidx 当前内置 workflow node 定义在 `src/voidx/workflow/nodes.py`，由 `WorkflowDAG` 编排。现有代码已经有 DAG、edge、gate、runtime state 和 `advance_workflow`，但 node 本身仍然偏 prompt 片段，缺少明确的执行契约。

主要问题：

1. **Node 缺少 I/O 契约**：上下游数据传递依赖隐含约定。
2. **Node 缺少 goal**：只有 `core_rule` 和 `description`，没有明确声明当前阶段要达成什么。
3. **Node 缺少工具白名单**：目前主要靠 role tools 和 gate denied tools，不能表达“当前 node 应该用哪些工具”。
4. **Node 字段混杂 runtime 策略**：`triggers`、`priority`、`enabled` 这类选择/排序/开关信息不应该放在 node 执行契约里。
5. **闭环表达不清**：`workflow` 混合了主流程和内部循环；TDD/debug/review 这类循环需要结构化的内部 subworkflow。
6. **LLM 与 runtime 边界不清**：LLM 可以判断下一步，但 workflow state 必须由 runtime 校验和持久化。

## 2. 设计目标

- `WorkflowDAG` 管 node 之间怎么流转。
- `WorkflowNode` 管当前阶段怎么执行。
- `NodeSubworkflow` 管当前阶段内部怎么循环。
- `Gate` 管当前 node 没满足前不能离开。
- `Edge condition` 管当前 node 满足后走哪条路。
- `Terminal condition` 管当前 workflow 什么时候结束。
- LLM 可以选择出口 condition，但 runtime 必须校验 active node、gate、edge 和最终 state。

核心模型：

```text
DAG 管“去哪里”
Node 管“当前阶段怎么做”
Subworkflow 管“当前阶段内部怎么循环”
Gate 管“没满足前不许离开”
Edge condition 管“满足后走哪条路”
Terminal condition 管“到这里结束”
```

## 3. 最终结构

### 3.1 WorkflowDAG

`WorkflowDAG` 是跨 node 的状态机。

```python
class WorkflowDAG(BaseModel):
    name: str
    nodes: dict[str, WorkflowNode]
    edges: list[Edge]
    goal_map: list[GoalEntry]
    terminal_exit: TerminalExit
```

字段作用：

| 字段 | 作用 |
|------|------|
| `name` | workflow 名称，例如 `default` |
| `nodes` | DAG 内所有可执行 node |
| `edges` | node 之间的合法流转规则 |
| `goal_map` | 初始入口映射，只决定从哪个 node 开始 |
| `terminal_exit` | 无后继结束条件，例如 `done -> end` |

`goal_map` 只负责入口，不负责后续流转。后续流转必须通过 `advance_workflow(condition=...)` 和 `edges` 决定。

### 3.2 WorkflowNode

`WorkflowNode` 是 LLM 的最小可执行阶段。

```python
class WorkflowNode(BaseModel):
    name: str
    goal: str
    description: str
    io: NodeIO
    tools: list[str]
    persona: str
    gate: NodeGate
    workflow: list[WorkflowStep]
    subworkflow: NodeSubworkflow | None = None
    rules: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
```

字段作用：

| 字段 | 作用 |
|------|------|
| `name` | 稳定 node ID，edge、runtime state、`advance_workflow` 都引用它 |
| `goal` | 当前 node 要达成什么 |
| `description` | 当前 node 的用途说明，给人和 LLM 读 |
| `io` | 上下游契约，说明输入来自哪里、输出交给谁 |
| `tools` | 当前 node 允许使用的工具白名单 |
| `persona` | 当前 node 的主执行姿态，例如 `explore`、`plan`、`implement`、`review` |
| `gate` | 当前 node 的硬约束和离开前置条件 |
| `workflow` | 当前 node 的线性主流程 |
| `subworkflow` | 当前 node 内部闭环流程，不参与 DAG、不引用其它 node |
| `rules` | 当前 node 必须遵守的规则和禁止行为 |
| `exceptions` | 允许跳过默认规则的例外 |

删除旧字段：

| 旧字段 | 处理 | 原因 |
|--------|------|------|
| `triggers` | 删除 | 用户文本命中属于 policy/intent 层，不属于 node 执行契约 |
| `priority` | 删除 | DAG 和 policy 决定顺序，node 不自带排序策略 |
| `enabled` | 删除 | 内置 node 不做半启用；开关放配置层 |
| `node_type` | 删除 | default workflow 当前不需要 gateway/subworkflow 类型；orchestrator 另起 spec |
| `core_rule` | 合入 `rules` | 避免 goal/rule 两套概念重复 |
| `decision_rules` | 挪到 `Edge.description` | condition 是 edge 语义，不是 node 内部字段 |
| `anti_patterns` | 合入 `rules` | 避免 prompt section 过碎 |
| `allowed_exceptions` | 改名 `exceptions` | 名称更短，语义不变 |
| `extra_sections` | 删除 | 不保留非结构化逃生口；需要表达的内容结构化建模 |

### 3.3 NodeIO

```python
class NodeIO(BaseModel):
    input: dict[str, str]
    output: dict[str, str]
```

`io` 是 prompt 级契约，先不要求 runtime 做数据管道校验，但每个内置 node 必须声明非空输入和输出。

示例：

```python
io=NodeIO(
    input={
        "spec": "设计文档或需求规格",
        "scope": "确认的变更范围",
    },
    output={
        "plan": "实施计划，包含文件结构、任务和测试命令",
        "tasks": "有序任务清单",
    },
)
```

### 3.4 NodeGate

现有 `NodeGate` 保留，并继续支持 `allowed_paths`。

```python
class NodeGate(BaseModel):
    denied_tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    description: str = ""
    required_before_transition: str = ""
```

字段作用：

| 字段 | 作用 |
|------|------|
| `denied_tools` | 当前 node 激活时禁止的工具 |
| `allowed_paths` | 对 denied tools 的路径例外，例如允许计划阶段写 `docs/specs/**` |
| `description` | gate 的自然语言说明 |
| `required_before_transition` | 离开当前 node 前必须满足的条件 |

工具最终可用性：

```text
available_tools = role.tools ∩ node.tools - gate.denied_tools
```

如果工具命中 `gate.denied_tools`，但目标路径匹配 `gate.allowed_paths`，则允许通过。

### 3.5 WorkflowStep

```python
class WorkflowStep(BaseModel):
    order: int
    action: str
    description: str = ""
```

`workflow` 使用 `WorkflowStep` 表示当前 node 的主流程。主流程应该是线性的阶段指引，不表达循环。

### 3.6 NodeSubworkflow

`subworkflow` 是当前 `WorkflowNode` 内部闭环 workflow prompt，不引用其它 `WorkflowNode`，也不引用其它 `WorkflowDAG`。

```python
class NodeSubworkflow(BaseModel):
    name: str
    description: str = ""
    steps: list[WorkflowStep]
    exit_condition: str
```

字段作用：

| 字段 | 作用 |
|------|------|
| `name` | 内部闭环名称，例如 `TDD Cycle` |
| `description` | 内部闭环什么时候使用 |
| `steps` | 内部循环步骤 |
| `exit_condition` | 内部循环退出条件 |

规则：

- `subworkflow` 不参与 DAG edge。
- `subworkflow` 不激活其它 node。
- `subworkflow` 可以循环多次。
- `subworkflow` 的结果汇总进当前 node 的 `io.output`。
- 渲染时作为当前 node 的 `### Internal Subworkflow` 展开。

适用场景：

| Node | Subworkflow |
|------|-------------|
| `tdd` | Red-Green-Refactor loop |
| `debug` | Hypothesis/debug loop |
| `review` | Review request loop |

不适用场景：

```python
subworkflow="default"
subworkflow="explore-workflow"
subworkflow="another-node"
```

如果未来 orchestrator 需要委派到其它 workflow，应另起字段或独立 spec，例如 `delegates_to_workflow`，不要复用 `NodeSubworkflow`。

### 3.7 Edge

`Edge` 表达 node 到 node 的合法流转。

```python
class Edge(BaseModel):
    source: str
    target: str
    condition: str
    label: str = ""
    description: str = ""
```

字段作用：

| 字段 | 作用 |
|------|------|
| `source` | 当前 node |
| `target` | condition 命中后激活的下一个 node |
| `condition` | LLM 调用 `advance_workflow` 时选择的出口 |
| `label` | 短说明，用于 UI 或 prompt |
| `description` | 详细说明，替代旧 `decision_rules` |

## 4. WorkflowNode 如何流转

### 4.1 入口选择

runtime 根据 `goal_map`、用户 intent、当前 task state 选择初始 node。

例如：

```text
goal_type=feature  -> brainstorm
goal_type=design   -> brainstorm
goal_type=doc      -> design-doc
goal_type=debug    -> debug
goal_type=bugfix   -> debug
goal_type=chore    -> tdd
goal_type=review   -> review
```

入口选择只发生在 workflow run 开始时。进入 node 后，后续必须走 edge。

### 4.2 当前 node 执行

runtime 将 active node 渲染给 LLM：

```text
Goal
Description
Input
Output
Tools
Persona
Gate
Workflow
Internal Subworkflow
Rules
Exceptions
Available Exits
```

LLM 执行当前 node，必要时使用当前 node 允许的工具。

### 4.3 继续到下一个 node

当前 node 满足 gate 后，LLM 从 `Available Exits` 中选择一个 condition，并调用：

```python
advance_workflow(
    workflow="brainstorm",
    condition="approved",
    evidence="User approved the proposed design.",
)
```

runtime 校验：

1. `workflow` 是否是 active node。
2. 当前 node 的 gate 是否满足。
3. `condition` 是否是该 node 的合法 outgoing edge。
4. `target` node 是否存在。
5. 当前 node 标记为 `satisfied`。
6. target node 标记为 `active`。

LLM 可以选择下一条 edge，但不能直接写 workflow state。

### 4.4 结束当前 workflow

当前 node 满足 gate 后，如果不需要后继 node，LLM 使用 terminal condition：

```python
advance_workflow(
    workflow="verify",
    condition="done",
    evidence="Targeted verification command passed and no review is required.",
)
```

runtime 校验：

1. `workflow` 是否 active。
2. gate 是否满足。
3. `condition` 是否等于 `terminal_exit.condition`。
4. 当前 node 标记为 `satisfied`。
5. 不激活后继 node。

### 4.5 阻塞

如果 gate 未满足，当前 node 不能继续，也不能结束。

示例：

```text
brainstorm gate: design approved by user
用户还没批准 -> 不能进入 design-doc / plan / tdd
```

```text
verify gate: verification command run with evidence
没有新验证证据 -> 不能 done / passed_substantial
```

### 4.6 回流

edge 可以指向前面的 node，用于验证失败、审查反馈或调试发现问题。

示例：

```text
verify --failed_implementation--> tdd
verify --failed_bug--> debug
review --review_has_issues--> feedback
feedback --feedback_valid--> tdd
debug --nontrivial_fix--> tdd
debug --trivial_fix--> verify
```

回流仍然必须通过 `advance_workflow(condition=...)`，由 runtime 校验 edge 合法性。

## 5. Default Workflow 结构

当前 default DAG 保持现有主结构：

```text
brainstorm --approved--> design-doc
brainstorm --skip_to_plan--> plan
brainstorm --small_change--> tdd
design-doc --completed--> plan
plan --approved--> tdd
tdd --implemented--> verify
verify --passed_substantial--> review
verify --failed_implementation--> tdd
verify --failed_bug--> debug
review --review_has_issues--> feedback
feedback --feedback_valid--> tdd
feedback --feedback_verified--> verify
debug --nontrivial_fix--> tdd
debug --trivial_fix--> verify
done -> end
```

## 6. Node 定义目标

### 6.1 brainstorm

```python
WorkflowNode(
    name="brainstorm",
    goal="确认需求和设计方案，获得用户批准",
    description="Use before creating features, building components, or modifying behavior.",
    persona="explore",
    io=NodeIO(
        input={"user_request": "用户原始请求"},
        output={
            "design": "批准的设计方案或确认的变更范围",
            "scope": "确认的变更边界",
        },
    ),
    tools=["read", "glob", "grep", "repo_map", "clarify", "plan_checkpoint", "webfetch", "websearch"],
    gate=NodeGate(
        denied_tools=("write", "edit", "lsp_format"),
        required_before_transition="design approved by user",
    ),
    workflow=[
        WorkflowStep(order=1, action="Explore context"),
        WorkflowStep(order=2, action="Ask clarifying questions"),
        WorkflowStep(order=3, action="Propose approaches"),
        WorkflowStep(order=4, action="Present design for approval"),
    ],
    rules=[
        "Do not write code before the design is approved.",
        "Do not write the design doc inside brainstorm; transition to design-doc if a document is needed.",
    ],
)
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `approved` | `design-doc` | 设计批准，且需要写设计文档 |
| `skip_to_plan` | `plan` | 用户给出详细 spec 或明确跳过设计文档 |
| `small_change` | `tdd` | 本地、机械、小范围变更 |
| `done` | end | 只需要讨论，不进入实现 |

### 6.2 design-doc

Goal: `产出通过读者测试的结构化文档`

I/O:

```python
input={
    "design": "批准的设计方案",
    "doc_type": "文档类型(prd/tech-design/rfc/api-doc/readme)",
}
output={
    "doc_path": "文档保存路径",
    "doc_type": "实际文档类型",
}
```

Tools:

```python
["read", "glob", "grep", "write", "edit", "load_doc_template", "repo_map", "webfetch", "websearch"]
```

Workflow:

1. Identify the scenario
2. Identify the document type
3. Gather context
4. Load the template
5. Write the first draft
6. Reader test
7. Verify accuracy

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `completed` | `plan` | 文档通过 reader test 和 accuracy check |
| `done` | end | 只需要产出文档，不需要继续计划/实现 |

### 6.3 plan

Goal: `产出可执行的实施计划，获得用户批准`

I/O:

```python
input={
    "spec": "设计文档或需求规格",
    "scope": "变更范围",
}
output={
    "plan": "实施计划，含任务列表、文件结构、测试定义",
    "tasks": "有序任务清单",
    "test_commands": "相关验证命令",
}
```

Tools:

```python
["read", "glob", "grep", "repo_map", "webfetch", "websearch", "write", "edit"]
```

Gate:

```python
NodeGate(
    denied_tools=("write", "edit", "lsp_format"),
    allowed_paths=("docs/specs/**", "docs/design/**"),
    required_before_transition="plan is executable and approved",
)
```

`plan` 可以写计划/设计文档，但不能开始实现代码。

Workflow:

1. Define goal
2. Describe architecture
3. List tech stack
4. Define file structure
5. Write tasks
6. Define tests
7. Identify risks
8. Verify plan is executable: every task has a file path and a test command

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `approved` | `tdd` | 用户批准计划 |
| `done` | end | 只需要计划，不进入实现 |

### 6.4 tdd

Goal: `按 TDD 循环完成实现，测试全绿`

I/O:

```python
input={
    "plan": "实施计划",
    "task": "当前要实现的任务",
}
output={
    "files_changed": "修改的文件列表",
    "tests_written": "编写的测试列表",
    "test_result": "测试运行结果",
}
```

Tools:

```python
["read", "write", "edit", "bash", "glob", "grep", "repo_map", "lsp_diagnostics", "lsp_format"]
```

Subworkflow:

```python
NodeSubworkflow(
    name="TDD Cycle",
    description="Repeat until all implementation tasks are complete.",
    steps=[
        WorkflowStep(order=1, action="Pick the next task from the plan"),
        WorkflowStep(order=2, action="Write a failing test"),
        WorkflowStep(order=3, action="Run the test and confirm RED"),
        WorkflowStep(order=4, action="Implement minimal code"),
        WorkflowStep(order=5, action="Run the test and confirm GREEN"),
        WorkflowStep(order=6, action="Refactor if needed"),
        WorkflowStep(order=7, action="Run the broader test set"),
    ],
    exit_condition="all plan tasks implemented and broader test set green",
)
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `implemented` | `verify` | 实现完成并通过相关测试 |

Exceptions:

```text
Pure documentation, prompt-only edits, generated assets, or configuration-only changes.
```

### 6.5 verify

Goal: `用可复现的证据证明变更达到预期状态`

I/O:

```python
input={
    "claim": "声称完成的状态(done/fixed/passing)",
    "files_changed": "变更文件",
    "test_commands": "相关测试命令",
}
output={
    "evidence": "验证证据，包含命令和输出",
    "verified": "是否通过",
    "scope": "变更影响范围(substantial/routine)",
}
```

Tools:

```python
["bash", "read", "glob", "grep", "repo_map", "lsp_diagnostics"]
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `passed_substantial` | `review` | 验证通过，且变更足够大需要审查 |
| `failed_implementation` | `tdd` | 验证失败，原因是实现问题 |
| `failed_bug` | `debug` | 验证暴露新的 bug 或根因不明 |
| `done` | end | 验证通过，且不需要继续审查 |

### 6.6 review

Goal: `发起结构化的代码审查请求并收集 verdict`

I/O:

```python
input={
    "files_changed": "变更文件",
    "verification_evidence": "验证证据",
    "risks": "风险点",
}
output={
    "review_brief": "审查简报",
    "review_result": "审查结果(PASS/FAIL/NEEDS_CHANGE)",
}
```

Tools:

```python
["agent", "read", "glob", "grep"]
```

Subworkflow:

```python
NodeSubworkflow(
    name="Review Cycle",
    steps=[
        WorkflowStep(order=1, action="Construct review brief"),
        WorkflowStep(order=2, action="Delegate to review agent"),
        WorkflowStep(order=3, action="Collect verdict"),
        WorkflowStep(order=4, action="Route verdict"),
    ],
    exit_condition="review verdict is PASS, or feedback is handed off to feedback",
)
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `review_has_issues` | `feedback` | 审查返回问题 |
| `done` | end | 审查 PASS |

### 6.7 feedback

Goal: `验证并实施有效的审查反馈`

I/O:

```python
input={
    "feedback": "审查反馈内容",
    "source": "反馈来源(human/external)",
}
output={
    "changes_made": "根据反馈做的变更",
    "feedback_status": "每条反馈的处理状态(accepted/rejected/deferred)",
}
```

Tools:

```python
["read", "write", "edit", "bash", "glob", "grep", "repo_map"]
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `feedback_valid` | `tdd` | 反馈有效，且需要按 TDD 修改实现 |
| `feedback_verified` | `verify` | 反馈已实施，需要验证 |
| `done` | end | 反馈不需要改动或已处理完 |

### 6.8 debug

Goal: `定位根因并修复，验证修复有效`

I/O:

```python
input={
    "error": "错误信息或异常表现",
    "reproduction": "复现步骤",
}
output={
    "root_cause": "根因描述",
    "fix": "修复内容",
    "fix_type": "修复类型(trivial/nontrivial)",
}
```

Tools:

```python
["read", "glob", "grep", "bash", "repo_map", "lsp_diagnostics", "lsp_symbols", "lsp_definition"]
```

Subworkflow:

```python
NodeSubworkflow(
    name="Debug Cycle",
    description="Repeat until root cause is confirmed and fix direction is known.",
    steps=[
        WorkflowStep(order=1, action="Read the full error and reproduce consistently"),
        WorkflowStep(order=2, action="Find working examples and compare differences"),
        WorkflowStep(order=3, action="Form one concrete hypothesis"),
        WorkflowStep(order=4, action="Test the hypothesis minimally"),
        WorkflowStep(order=5, action="Implement the smallest supported fix"),
        WorkflowStep(order=6, action="Run reproduction and broader tests"),
    ],
    exit_condition="root cause confirmed and original symptom no longer reproduces",
)
```

Exits:

| condition | target | 使用时机 |
|-----------|--------|----------|
| `nontrivial_fix` | `tdd` | 修复需要测试驱动实现 |
| `trivial_fix` | `verify` | 修复很小，可直接验证 |
| `done` | end | 只需要定位/解释，不需要修改 |

## 7. LLM 与 Runtime 的编排边界

不让 LLM 完全自己编排 workflow。采用 hybrid 模型：

```text
runtime selects entry node
        ↓
LLM executes active node
        ↓
LLM chooses exit condition
        ↓
runtime validates gate + edge
        ↓
runtime activates next node or ends
```

Runtime 负责：

- 决定初始入口。
- 持久化 active/satisfied/blocked/skipped state。
- 校验当前 node 是否 active。
- 校验 gate 是否满足。
- 校验 condition 是否是合法 edge 或 terminal condition。
- 激活下一个 node 或结束当前 workflow。

LLM 负责：

- 执行当前 active node。
- 判断当前 node 是否满足 gate。
- 从 runtime 渲染的 `Available Exits` 中选择 condition。
- 调用 `advance_workflow(workflow=..., condition=..., evidence=...)`。

LLM 不允许：

- 编造不存在的 node。
- 直接把某个 node 标记 active/satisfied。
- 跳过 gate。
- 使用不在 `Available Exits` 中的 condition。
- 把内部 `subworkflow` 当作 DAG node 激活。

## 8. 渲染变更

`render_node_markdown` 渲染 active node 时必须包含：

```text
## Workflow Node: <name>
Description
Goal
Persona
Input
Output
Tools
Gate
Workflow
Internal Subworkflow
Rules
Exceptions
Available Exits
```

Inactive node summary 只需要：

```text
## Workflow Node Summary: <name>
Description
Goal
Exits
```

`subworkflow` 渲染为当前 node 内部 section：

```text
### Internal Subworkflow: TDD Cycle
Description: Repeat until all implementation tasks are complete.
1. Pick the next task from the plan
2. Write a failing test
...
Exit condition: all plan tasks implemented and broader test set green
```

## 9. 权限与工具规则

工具白名单必须参与实际工具绑定或权限过滤：

```text
available_tools = role.tools ∩ node.tools - gate.denied_tools
```

规则：

- `node.tools` 是必填字段。
- `node.tools=[]` 表示当前 node 不允许调用工具。
- role tools 仍然是能力上限。
- gate denied tools 是最终黑名单。
- `allowed_paths` 只对文件路径类工具放行，不改变工具白名单。

权限拒绝时，错误信息必须指出阻塞 node：

```text
Blocked by workflow gate for tool 'edit': brainstorm requires design approved by user
```

## 10. 实施路线

### Phase 1: Schema

- `src/voidx/workflow/schema.py`
  - 新增 `NodeIO`
  - 新增 `NodeSubworkflow`
  - 精简 `WorkflowNode`
  - `Edge` 新增 `description`

### Phase 2: Default Nodes

- `src/voidx/workflow/nodes.py`
  - 移除 `triggers`、`priority`、`enabled`、`core_rule`、`decision_rules`、`anti_patterns`、`allowed_exceptions`、`extra_sections`
  - 补齐 8 个内置 node 的 `goal`、`io`、`tools`、`persona`、`rules`、`exceptions`
  - 为 `tdd`、`debug`、`review` 增加 `NodeSubworkflow`

### Phase 3: DAG

- `src/voidx/workflow/dag.py`
  - 保持现有 default DAG 主结构
  - 将旧 `decision_rules` 的说明移动到 `Edge.description`
  - `goal_map` 继续只负责入口

### Phase 4: Render

- `src/voidx/workflow/render.py`
  - active node 渲染完整执行契约
  - inactive summary 渲染 description、goal、exits
  - 渲染 `Internal Subworkflow`

### Phase 5: Runtime/Permissions

- 工具绑定或权限层应用 `role.tools ∩ node.tools - gate.denied_tools`
- `advance_workflow` 校验 active node、gate、edge condition、terminal condition
- LLM 只能选择 `Available Exits`，不能直接写 workflow state

## 11. 验收测试

Schema tests:

- 每个内置 node 都有非空 `goal`。
- 每个内置 node 都有非空 `io.input` 和 `io.output`。
- 每个内置 node 都声明 `tools`。
- `NodeSubworkflow` 必须有 `exit_condition`。
- `Edge.description` 可选但 condition 必须唯一。

Render tests:

- active node 渲染 `Goal`、`Persona`、`Input`、`Output`、`Tools`、`Gate`、`Workflow`、`Available Exits`。
- 有 `subworkflow` 的 node 渲染 `Internal Subworkflow` 和 exit condition。
- inactive summary 不渲染完整 workflow/gate。

Policy/runtime tests:

- `goal_map` 只激活入口 node。
- `advance_workflow(condition=<edge>)` 激活合法 target。
- `advance_workflow(condition="done")` 结束当前 node，不激活 successor。
- 非 active node 不能 advance。
- gate 未满足时不能 advance。
- 非法 condition 被拒绝。
- 内部 `subworkflow.name` 不能作为 workflow node advance。

Permission tests:

- node tools 限制实际可用工具。
- role tools 仍是能力上限。
- gate denied tools 覆盖 node tools。
- `allowed_paths` 允许计划阶段写 `docs/specs/**` 和 `docs/design/**`。

Regression tests:

- `brainstorm` 不包含写设计文档步骤。
- `refactor` goal 初始只进入 `brainstorm`。
- plan mode 初始只进入 `brainstorm`。
- 设计文档阶段不被 plan gate 死锁。

## 12. 非目标

- 不实现 orchestrator workflow DAG。
- 不新增 `exploring-codebase` node。
- 不新增 `explore-workflow`。
- 不新增 `TaskIntent.EXPLORE`。
- 不让 `subworkflow` 引用其它 `WorkflowNode` 或 `WorkflowDAG`。
- 不保留旧 schema 字段的兼容逻辑。
