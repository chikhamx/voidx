# workflow 工具改造 — 技术设计文档

## Context

当前 `advance_workflow` 工具只支持一种操作：选择退出条件推进当前活跃节点。LLM 无法主动"进入"某个工作流节点——进入由 `goal_resolver` 在 turn 开始时自动决定。这导致：

1. LLM 在对话中途判断需要切换工作流时（如用户说"帮我 review 一下"），只能依赖 goal_resolver 在下一 turn 才能生效。
2. 工具名 `advance_workflow` 只表达了"推进"语义，无法承载"进入"等操作。
3. `condition="done"` 作为退出条件混在业务 condition 中，语义不够清晰。

## Goals and Non-Goals

### Goals

- 将 `advance_workflow` 重命名为 `workflow`，tool id 从 `"advance_workflow"` 改为 `"workflow"`。
- 新增 `action` 参数，支持 `enter` / `advance` / `done` 三种操作。
- `enter`：LLM 主动激活一个工作流节点，无需等待 goal_resolver。**进入新节点前先关闭当前所有活跃节点**，保证任意时刻只有一个 active workflow。
- `advance`：选择退出条件推进到后继节点（等价于旧版 `condition=xxx`）。
- `done`：关闭当前所有活跃节点，不激活后继（等价于旧版 `condition="done"`）。
- 更新所有引用 `advance_workflow` 的代码和配置。
- 降低 LLM 调用时的报错率（详见防错机制章节）。

### Non-Goals

- 不改变 workflow DAG 结构、节点定义、auto_advance 逻辑。
- 不改变 goal_resolver 的行为——`enter` 是 LLM 主动行为，与 goal_resolver 并行存在。
- 不改变 reconcile 逻辑——reconcile 在 turn 开始时运行，`enter` 在 turn 中执行，两者不冲突。
- 不改变 `advance_workflow_states` 函数名（它是内部 runtime 函数，不是 LLM 面向的 API）。

## Architecture

### 单一 Active Workflow 约束

**核心规则：任意时刻最多只有一个 active workflow 节点。**

- `enter`：进入新节点前，先关闭当前所有活跃节点（SATISFIED + 级联跳过下游），再激活目标节点。
- `advance`：推进当前活跃节点，后继节点自动成为唯一 active。
- `done`：关闭当前所有活跃节点，不激活后继。

这消除了多活跃节点歧义问题——`workflow` 参数在 `advance`/`done` 中不再需要指定目标节点，因为只有一个活跃节点。

```
workflow(action="enter", workflow="debug")
  → 关闭当前所有活跃节点（SATISFIED + 级联跳过下游）
    旧节点 evidence 自动生成："replaced by enter:debug"
  → 校验节点存在于 DAG nodes（忽略大小写；subworkflow 内部名称如 "TDD Cycle" 不在 DAG nodes 中，自然被拒绝）
  → 创建 WorkflowRunState(status=ACTIVE, source=MANUAL)
  → 返回 state_patch（含 persona 切换）

workflow(action="advance", condition="nontrivial_fix", evidence="...")
  → 校验有活跃节点
  → 校验 condition 匹配出边（忽略大小写）
  → 校验 evidence 非空（gate 要求）
  → 构建 SATISFIED 事件，调用 advance_workflow_states
  → 返回 state_patch（含后继节点激活 + persona 切换）

workflow(action="done", evidence="...")
  → 关闭当前所有活跃节点（SATISFIED + 级联跳过下游）
  → 返回 state_patch
```

### 与现有系统的交互

```
goal_resolver (turn start)
  ↓ 自动激活节点
reconcile (turn start)
  ↓ 协调状态
LLM turn
  ↓ 调用 workflow(action="enter", ...)
  ↓ 或 workflow(action="advance", ...)
tool_executor
  ↓ 处理 state_patch
auto_advance (post-tool)
  ↓ 自动推进可检测的条件
```

`enter` 不受 route 边界限制——它是 LLM 的主动行为，类似用户直接说"进入 debug"。route 边界只在 auto_advance 和 reconcile 中生效。

## 防错与降错机制

> 核心原则：**让 LLM 难以犯错，而非犯错后纠正**。通过 schema 约束、智能默认值、忽略大小写匹配、参数互斥校验，将报错率降到最低。

### 1. Schema 层面：消除默认值陷阱

**问题**：旧版 `condition` 默认值为 `"done"`，LLM 只传 `workflow="xxx"` 不传 condition 时，会意外触发 done。

**方案**：
- `action` 无默认值，LLM 必须显式选择 `enter` / `advance` / `done`。
- `condition` 默认值从 `"done"` 改为 `""`（空字符串），不再有隐式行为。
- `workflow` 默认值保持 `""`，但 description 中明确标注何时必填。
- 移除 `summary` 参数——与 `evidence` 语义重叠，LLM 经常只填 summary 不填 evidence，导致 gate 质量保证失效。统一使用 `evidence` 即可。

```python
class WorkflowInput(BaseModel):
    action: Literal["enter", "advance", "done"] = Field(
        description="Workflow operation."
    )
    workflow: str = Field(
        default="",
        description=(
            "Workflow node name. Required for 'enter'. "
            "For 'advance'/'done', auto-resolved when only one active node exists."
        ),
    )
    condition: str = Field(
        default="",
        description=(
            "Exit condition for 'advance'. Must match an outgoing edge condition "
            "in the workflow DAG (case-insensitive). Ignored for 'enter' and 'done'."
        ),
    )
    evidence: str = Field(
        default="",
        description="Brief evidence that the condition is satisfied. Required for 'advance' and 'done'.",
    )
```

### 2. 参数互斥校验：提前拦截无效组合

**问题**：LLM 可能传 `action="advance"` 但不传 `condition`，或 `action="enter"` 但传了 `condition`。

**方案**：在 execute 入口做参数组合校验，返回结构化错误信息：

| action | 缺失参数 | 多余参数 | 处理 |
|--------|---------|---------|------|
| `enter` | `workflow` 为空 | `condition` 非空 | 报错 + 忽略 condition |
| `advance` | `condition` 为空 | — | 报错，列出可用 condition |
| `advance` | `evidence` 为空 | — | 报错（gate 要求） |
| `done` | `evidence` 为空 | `condition` 非空 | 报错 + 忽略 condition |

对于"多余参数"场景，**不报错**，只忽略多余参数并在 response 中提示。这避免了 LLM 因传了不该传的参数而被拒绝。

### 3. condition 忽略大小写匹配：容忍大小写差异

**问题**：LLM 可能传入大小写不一致的 condition，如 `"NonTrivial_Fix"` 而非 `"nontrivial_fix"`。

**方案**：condition 匹配时忽略大小写，但**不做模糊匹配**（如下划线/连字符/空格差异、前缀匹配等）。匹配不上时返回错误，列出可用选项让 LLM 自行修正。

```python
def _match_condition(condition: str, edges: tuple[Edge, ...]) -> Edge | None:
    normalized = condition.strip().lower()
    for edge in edges:
        if edge.condition.strip().lower() == normalized:
            return edge
    return None
```

忽略大小写命中时，在 response 中使用 DAG 中的原始 condition 值（而非 LLM 传入的值），确保后续状态一致。

匹配失败时，错误信息列出所有可用退出条件：

```
Invalid condition 'Non_Trivial_Fix' for node 'debug'.
Available exits:
  - nontrivial_fix -> tdd (fix requires TDD)
  - trivial_fix -> verify (fix is trivial)
  - done -> end the current workflow node
Correct usage: workflow(action="advance", condition="nontrivial_fix", evidence="...")
```

### 4. enter 节点名忽略大小写匹配

**问题**：LLM 可能传入大小写不一致的节点名，如 `"Debug"` 而非 `"debug"`。

**方案**：与 condition 一致，节点名匹配时忽略大小写，不做模糊匹配。匹配不上时返回错误，列出所有可用节点名。

```python
def _match_node(name: str, dag_nodes: dict[str, WorkflowNode]) -> str | None:
    normalized = name.strip().lower()
    for node_name in dag_nodes:
        if node_name.strip().lower() == normalized:
            return node_name
    return None
```

### 5. 重复调用幂等保护

**问题**：LLM 可能在同一 turn 内重复调用 `workflow`。

**不需要并发锁**：`workflow` 是 barrier tool（见 `tool_executor.py` `_is_barrier_tool`），同一 turn 内多个 tool calls 时，`workflow` 会作为分割点串行执行，不存在真正的并发竞态。因此不需要 `_state_lock` 或原子操作。

**方案**：只处理重复调用（幂等）：

- `enter` 同一节点已 ACTIVE → 返回成功（幂等），response 中标记 `"already_active": true`
- `advance` 同一 condition 已 SATISFIED → 返回成功（幂等），response 中标记 `"already_satisfied": true`
- `done` 无活跃节点 → 返回成功（幂等），response 中标记 `"no_active_nodes": true`

### 6. 错误信息自修复引导

**问题**：旧版错误信息只说"invalid condition"，LLM 需要额外一轮调用才能修正。

**方案**：所有错误信息都包含**可直接复制使用的正确调用示例**：

```
Invalid condition 'non_trivial_fix' for node 'debug'.
Available exits:
  - nontrivial_fix -> tdd (fix requires TDD)
  - trivial_fix -> verify (fix is trivial)
  - done -> end the current workflow node
Correct usage: workflow(action="advance", condition="nontrivial_fix", evidence="...")
```

### 7. 成功响应包含上下文提示

**问题**：LLM 完成一次 workflow 调用后，不知道下一步该做什么。

**方案**：成功响应中增加 `next_hints` 字段：

```json
{
  "action": "advance",
  "from": "debug",
  "condition": "nontrivial_fix",
  "activated": ["tdd"],
  "next_hints": ["tdd is now active. Write a failing test first, then implement."],
  "evidence": "Root cause confirmed: off-by-one in parser"
}
```

`next_hints` 的用途是**在 tool result 中提示 LLM 刚激活的节点期望什么行为**，减少 LLM 在节点切换后"不知道该做什么"而进行无效调用的情况。它是给 LLM 的行为引导，不是给用户的。

提取规则：
- `next_hints[0]` = 激活节点的 `WorkflowNode.goal`（如 `"Locate root cause and confirm fix direction"`）
- `next_hints[1]` = 激活节点的 `WorkflowNode.rules[0]`（如 `"Find the root cause before changing code."`），如果 rules 为空则省略
- 不超过 2 条

### 防错机制汇总

| 机制 | 解决的出错模式 | 降错效果 |
|------|--------------|---------|
| action 无默认值 | 旧版 condition="done" 陷阱 | 消除隐式行为 |
| condition 默认值改空 | LLM 不传 condition 意外触发 done | 消除默认值陷阱 |
| 移除 summary 参数 | summary/evidence 语义重叠，LLM 只填 summary | 统一为 evidence，消除混淆 |
| 参数互斥校验 | action 与参数组合错误 | 提前拦截，忽略多余参数 |
| condition 忽略大小写 | condition 大小写不一致 | 容忍大小写差异 |
| enter 节点名忽略大小写 | 节点名大小写不一致 | 容忍大小写差异 |
| 重复调用幂等 | 同一 turn 内重复操作 | 返回成功，不报错 |
| 错误信息含修复示例 | 报错后 LLM 不知道怎么改 | 一轮修正而非两轮 |
| 成功响应含 next_hints | LLM 不知道下一步做什么 | 减少无效调用 |

## Data Model

### WorkflowInput（新）

```
WorkflowInput
├── action: Literal["enter", "advance", "done"]  ← 无默认值，必须显式选择
│   enter   = 激活一个工作流节点（先关闭当前活跃节点）
│   advance = 通过退出条件推进到后继节点
│   done    = 关闭当前所有活跃节点，不激活后继
├── workflow: str  ← 默认 ""
│   enter:   必填，要进入的节点名（忽略大小写）
│   advance: 可选，指定活跃节点（单一 active 约束下通常不需要）
│   done:    可选，指定活跃节点（单一 active 约束下通常不需要）
├── condition: str  ← 默认 ""（不再默认 "done"）
│   enter:   忽略（传了不报错）
│   advance: 必填，忽略大小写匹配
│   done:    忽略（传了不报错）
└── evidence: str  ← 默认 ""
    enter:   可选
    advance: 必填
    done:    必填
```

### enter 创建的 WorkflowRunState

```
WorkflowRunState
├── name:         <workflow 参数值>
├── status:       ACTIVE
├── source:       MANUAL
├── reason:       "manual:enter"
├── goal_type:    ""
├── scope:        ""
├── personas:     [node.persona]  ← 从 DAG 节点定义读取
├── activated_turn: ctx 当前 turn
├── updated_turn:   ctx 当前 turn
├── evidence:     [WorkflowEvidence(kind="activated", ref="tool:workflow", ok=True, summary="", condition="enter")]
├── blocked_reason: ""
├── body_hash:    ""
└── transition_to: [后继节点名列表]
```

### enter 对已有节点状态的处理

| 当前状态 | 行为 |
|---------|------|
| 不存在于 runs | 创建新 ACTIVE 节点 |
| ACTIVE | 返回成功（幂等），标记 `already_active: true` |
| SATISFIED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| SKIPPED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| BLOCKED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| PENDING | 更新为 ACTIVE |

### enter 关闭旧节点的 evidence 策略

`enter` 进入新节点前需要关闭当前所有活跃节点，关闭旧节点时需要 evidence。策略：**自动生成固定文本**，不依赖 LLM 传入的 evidence。

- 旧节点 evidence：`"replaced by enter:{new_node}"`（如 `"replaced by enter:debug"`）
- 旧节点 condition：`"done"`（表示被替换终止）
- 级联跳过的下游节点 evidence：沿用现有 `_skip_downstream_active_runs` 逻辑

选择自动生成而非复用 `enter` 的 evidence 参数，因为：
1. `enter` 的 evidence 是可选的，不应因为没传 evidence 就无法关闭旧节点。
2. 旧节点关闭的原因是"被新节点替换"，而非"满足某个条件"，自动生成更准确。
3. LLM 传入的 evidence 描述的是新节点的激活理由，语义上不属于旧节点的关闭理由。

### done 关闭所有活跃节点

`done` 操作关闭当前所有活跃节点（虽然单一 active 约束下通常只有一个），对每个活跃节点：
1. 标记为 SATISFIED
2. 级联跳过其下游所有 ACTIVE 节点（标记为 SKIPPED）
3. 记录 evidence

## API Contract

### workflow 工具

- **Tool ID**: `workflow`
- **Description**: Manage workflow node lifecycle. Use `enter` to activate a workflow node, `advance` to transition via an exit condition, or `done` to end a node without activating successors.
- **Parameters**: `WorkflowInput` schema（见 Data Model）
### action="enter"

- **Request**: `workflow(action="enter", workflow="debug")`
- **Response**:
  ```json
  {
    "action": "enter",
    "workflow": "debug",
    "activated": ["debug"],
    "next_hints": ["Locate root cause and confirm fix direction before changing code."],
    "evidence": "User requested debug workflow"
  }
  ```
- **Already active (idempotent)**:
  ```json
  {
    "action": "enter",
    "workflow": "debug",
    "already_active": true,
    "activated": ["debug"],
    "next_hints": ["Locate root cause and confirm fix direction before changing code."],
    "evidence": ""
  }
  ```
- **Errors**:
  - 节点不存在且忽略大小写也无法匹配 → `workflow: invalid node`，列出所有可用节点名
  - workflow 为空 → `workflow: node required`

### action="advance"

- **Request**: `workflow(action="advance", condition="nontrivial_fix", evidence="Root cause confirmed: off-by-one in parser")`
- **Response**:
  ```json
  {
    "action": "advance",
    "from": "debug",
    "condition": "nontrivial_fix",
    "activated": ["tdd"],
    "next_hints": ["Write a failing test first, then implement minimal code to pass it."],
    "evidence": "Root cause confirmed: off-by-one in parser"
  }
  ```
- **Errors**:
  - 无活跃节点 → 返回空结果（同旧版）
  - condition 为空 → `workflow: condition required`，列出可用退出条件
  - condition 不匹配出边且忽略大小写也无法匹配 → `workflow: invalid exit`，列出可用退出条件 + 修复示例
  - evidence 为空 → `workflow: evidence required`

### action="done"

- **Request**: `workflow(action="done", evidence="User cancelled the feature request")`
- **Response**:
  ```json
  {
    "action": "done",
    "from": "brainstorm",
    "activated": [],
    "evidence": "User cancelled the feature request"
  }
  ```
- **No active nodes (idempotent)**:
  ```json
  {
    "action": "done",
    "no_active_nodes": true,
    "activated": [],
    "evidence": ""
  }
  ```
- **Errors**:
  - evidence 为空 → `workflow: evidence required`

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `enter` 节点不存在 | 忽略大小写匹配 → 匹配则继续，否则返回错误 + 可用节点列表 |
| `enter` 节点已 ACTIVE | 返回成功（幂等），标记 `already_active: true` |
| `enter` workflow 为空 | 返回错误，提示必填 |
| `enter` 传了 condition | 忽略 condition，不报错 |
| `advance` condition 为空 | 返回错误，列出可用退出条件 + 修复示例 |
| `advance` condition 不匹配 | 忽略大小写匹配 → 匹配则继续，否则返回错误 + 可用退出条件 + 修复示例 |
| `advance`/`done` 无活跃节点 | 返回空结果 payload（同旧版） |
| `advance`/`done` evidence 为空 | 返回 gate 错误 |
| `done` 传了 condition | 忽略 condition，不报错 |
| 重复调用（同一操作） | 返回成功（幂等） |

## Migration

### 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/advance_workflow.py` | 重命名为 `src/voidx/tools/workflow.py`；类名 `AdvanceWorkflowTool` → `WorkflowTool`；id 改为 `"workflow"`；重写 `WorkflowInput` 和 `execute`；新增忽略大小写匹配、幂等逻辑 |
| `src/voidx/tools/registry.py` | import 从 `advance_workflow` → `workflow`；类名更新 |
| `src/voidx/permission/rules.py` | 4 处更新：L36 `BASIC_RULES` 中 `"advance_workflow"` → `"workflow"`；L105 `repair_tool_name` 映射 `"AdvanceWorkflow": "advance_workflow"` → `"AdvanceWorkflow": "workflow"`；L357 `capability_for_tool` 集合中 `"advance_workflow"` → `"workflow"` |
| `src/voidx/ui/output/display_policy.py` | `DEFAULT_DISPLAY_RULES` key `"advance_workflow"` → `"workflow"` |
| `src/voidx/agent/graph/runtime_guards.py` | `LOW_VALUE_REPETITIVE_TOOLS` frozenset 中 `"advance_workflow"` → `"workflow"` |
| `src/voidx/agent/agents.py` | `BUILTIN_AGENTS["voidx"].tools` 列表中 `"advance_workflow"` → `"workflow"` |
| `src/voidx/agent/graph/tool_executor.py` | 4 处字符串引用：L830 `tool_call.get("name") != "advance_workflow"` → `"workflow"`；L849 `ref="tool:advance_workflow"` → `"tool:workflow"`；L1012 同 L830；L1077 `_is_barrier_tool` frozenset 中 `"advance_workflow"` → `"workflow"` |
| `src/voidx/agent/todo_state.py` | `_REPLAY_SANITIZED_TOOL_NAMES` 当前为 `{"todo"}`，不包含 `"advance_workflow"`，无需更新 |

### 不变的内部函数

以下函数名保持不变，它们是内部 runtime 函数，不是 LLM 面向的 API：

- `advance_workflow_states()` — `src/voidx/workflow/runtime.py`
- `advance_workflow_states()` — `src/voidx/workflow/service.py`（re-export）
- `auto_advance_events()` — `src/voidx/workflow/auto_advance.py`
- `reconcile_workflow_runs_for_turn()` — `src/voidx/workflow/reconcile.py`

### 测试迁移

- `tests/test_tools/test_basic.py`：tool id 断言更新，advance_workflow 相关测试用例更新为 workflow
- `tests/test_workflow/test_auto_advance.py`：内部函数调用不变
- `tests/test_agent/test_core_flow.py`：tool call name 更新
- `tests/test_agent/test_stream_llm.py`：parametrize 更新
- `tests/test_agent/test_run_loop.py`：引用更新
- `tests/test_agent/test_module_boundaries.py`：路径更新

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `action` 用 Literal 枚举 | 用 `condition` 的特殊值区分（如 `condition="enter:debug"`） | Literal 更清晰，LLM 理解成本更低，schema 自文档 |
| `done` 作为独立 action | 保留 `condition="done"` | 语义更明确——done 不是"退出条件"，而是"终止操作" |
| `enter` 不受 route 边界限制 | `enter` 只能进入 route 内节点 | LLM 主动行为应等同于用户直接请求，不应受 route 约束 |
| `advance_workflow_states` 内部函数名不变 | 一并重命名 | 它是 runtime 内部函数，不是 LLM API，重命名收益低、影响面大 |
| `enter` 时 source=MANUAL | source=EXPLICIT | MANUAL 更准确——是 LLM 主动发起而非用户显式指定 |
| `enter` 允许重新进入 SATISFIED/SKIPPED/BLOCKED 节点 | 只允许进入不存在于 runs 的节点 | 实际场景中 LLM 可能需要重新进入已完成的工作流（如再次 debug），禁止会限制灵活性 |
| condition 忽略大小写匹配 | 严格匹配 + 报错 / 模糊匹配 | 忽略大小写是最低成本的容错，模糊匹配（下划线/连字符/前缀）容易误匹配，不如让 LLM 看到正确选项后自行修正 |
| 移除 summary 参数 | 保留 summary + evidence 兜底 | summary 与 evidence 语义重叠，LLM 经常只填 summary 导致 gate 失效；统一为 evidence 更清晰 |
| 单一 active workflow 约束 | 允许多个 active 节点 | 单一 active 消除歧义问题，简化 LLM 调用；`enter` 自动关闭旧节点，`done` 关闭所有 |
| `done` 关闭所有活跃节点 | 只关闭指定节点 | 单一 active 约束下通常只有一个，关闭所有更安全，避免遗漏 |
| 重复调用幂等返回成功 | 重复调用报错 | 幂等更友好，LLM 不需要记住"是否已经调用过" |
| `enter` 关闭旧节点 evidence 自动生成 | 复用 LLM 传入的 evidence 参数 | `enter` 的 evidence 是可选的，不应因没传而无法关闭旧节点；旧节点关闭原因是"被替换"，自动生成更准确 |
| 不需要并发锁 | 加 `_state_lock` 原子操作 | `workflow` 是 barrier tool，tool_executor 保证串行执行，不存在并发竞态 |
