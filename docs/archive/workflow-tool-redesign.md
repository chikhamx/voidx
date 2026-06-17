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
- `enter`：LLM 主动激活一个工作流节点，无需等待 goal_resolver。
- `advance`：选择退出条件推进到后继节点（等价于旧版 `condition=xxx`）。
- `done`：终止当前节点，不激活后继（等价于旧版 `condition="done"`）。
- 更新所有引用 `advance_workflow` 的代码和配置。
- 降低 LLM 调用时的报错率（详见防错机制章节）。

### Non-Goals

- 不改变 workflow DAG 结构、节点定义、auto_advance 逻辑。
- 不改变 goal_resolver 的行为——`enter` 是 LLM 主动行为，与 goal_resolver 并行存在。
- 不改变 reconcile 逻辑——reconcile 在 turn 开始时运行，`enter` 在 turn 中执行，两者不冲突。
- 不改变 `advance_workflow_states` 函数名（它是内部 runtime 函数，不是 LLM 面向的 API）。

## Architecture

### 操作流程

```
workflow(action="enter", workflow="debug")
  → 校验节点存在于 DAG
  → 校验节点未处于 ACTIVE 状态
  → 创建 WorkflowRunState(status=ACTIVE, source=MANUAL)
  → 返回 state_patch（含 persona 切换）

workflow(action="advance", workflow="debug", condition="nontrivial_fix", evidence="...")
  → 校验节点处于 ACTIVE 状态
  → 校验 condition 匹配出边
  → 校验 evidence 非空（gate 要求）
  → 构建 SATISFIED 事件，调用 advance_workflow_states
  → 返回 state_patch（含后继节点激活 + persona 切换）

workflow(action="done", workflow="debug", evidence="...", summary="...")
  → 校验节点处于 ACTIVE 状态
  → 校验 evidence 非空
  → 标记 SATISFIED，级联跳过下游
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

> 核心原则：**让 LLM 难以犯错，而非犯错后纠正**。通过 schema 约束、智能默认值、模糊匹配、参数互斥校验，将报错率降到最低。

### 1. Schema 层面：消除默认值陷阱

**问题**：旧版 `condition` 默认值为 `"done"`，LLM 只传 `workflow="xxx"` 不传 condition 时，会意外触发 done。

**方案**：
- `action` 无默认值，LLM 必须显式选择 `enter` / `advance` / `done`。
- `condition` 默认值从 `"done"` 改为 `""`（空字符串），不再有隐式行为。
- `workflow` 默认值保持 `""`，但 description 中明确标注何时必填。

```python
class WorkflowInput(BaseModel):
    action: Literal["enter", "advance", "done"] = Field(
        description="Workflow operation."
    )
    workflow: str = Field(
        default="",
        description=(
            "Workflow node name. Required for 'enter'. "
            "For 'advance'/'done', required when multiple nodes are active."
        ),
    )
    condition: str = Field(
        default="",
        description=(
            "Exit condition for 'advance'. Must match an outgoing edge condition "
            "in the workflow DAG. Ignored for 'enter' and 'done'."
        ),
    )
    evidence: str = Field(
        default="",
        description="Brief evidence that the condition is satisfied. Required for 'advance' and 'done'.",
    )
    summary: str = Field(
        default="",
        description="What was accomplished in the current workflow node.",
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

### 3. condition 模糊匹配：容忍拼写错误

**问题**：LLM 需要精确匹配 edge condition（如 `nontrivial_fix`、`review_has_issues`），但 schema 里没有枚举值，LLM 只能靠 prompt 上下文猜。常见错误：
- `non_trivial_fix` → 正确 `nontrivial_fix`
- `review_has_issue` → 正确 `review_has_issues`
- `feedback_verified` → 正确 `feedback_verified`（这个容易对）

**方案**：当 `condition` 精确匹配失败时，做模糊匹配：

```python
def _fuzzy_match_condition(condition: str, edges: tuple[Edge, ...]) -> Edge | None:
    normalized = condition.strip().lower().replace("-", "_").replace(" ", "_")
    for edge in edges:
        edge_norm = edge.condition.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized == edge_norm:
            return edge
    # 去掉下划线再试一次（non_trivial_fix → nontrivialfix）
    stripped = normalized.replace("_", "")
    for edge in edges:
        edge_stripped = edge.condition.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        if stripped == edge_stripped:
            return edge
    return None
```

模糊匹配命中时，在 response 中增加 `"fuzzy_matched": true` 和 `"matched_condition": "nontrivial_fix"` 字段，让 LLM 知道实际匹配的 condition 是什么，下次可以传正确的值。

### 4. workflow 智能默认：减少歧义报错

**问题**：多个活跃节点时 LLM 忘填 `workflow` 参数，导致歧义报错。

**方案**：
- 当只有一个活跃节点时，`workflow` 自动默认为该节点，无需 LLM 显式指定。
- 当有多个活跃节点时，如果 `action="advance"` 且 `condition` 只匹配其中一个节点的出边，自动选择该节点。
- 只有真正无法推断时才报歧义错误。

```python
def _resolve_target_run(
    active: list[WorkflowRunState],
    *,
    action: str,
    workflow: str,
    condition: str,
) -> WorkflowRunState | None:
    # 1. 显式指定
    if workflow:
        return next((run for run in active if run.name == workflow), None)
    # 2. 只有一个活跃节点
    if len(active) == 1:
        return active[0]
    # 3. advance 时按 condition 匹配
    if action == "advance" and condition:
        matched = [run for run in active
                    if any(e.condition == condition for e in workflow_edges(run.name))]
        if len(matched) == 1:
            return matched[0]
    # 4. 无法推断
    return None
```

### 5. enter 节点名模糊匹配

**问题**：LLM 可能传入不存在的节点名，如 `"debugging"` 而非 `"debug"`。

**方案**：当 `enter` 的 `workflow` 不在 DAG 中时，做模糊匹配：

```python
def _fuzzy_match_node(name: str, dag_nodes: dict[str, WorkflowNode]) -> str | None:
    normalized = name.strip().lower()
    # 精确匹配
    if normalized in dag_nodes:
        return normalized
    # 前缀匹配（debugging → debug）
    for node_name in dag_nodes:
        if node_name.startswith(normalized[:4]) or normalized.startswith(node_name[:4]):
            return node_name
    return None
```

模糊匹配命中时，在 response 中增加 `"fuzzy_matched": true` 和 `"matched_node": "debug"` 字段。

### 6. evidence 智能降级：用 summary 兜底

**问题**：LLM 经常只填 `summary` 不填 `evidence`，导致 gate 报错。

**方案**：当 `evidence` 为空但 `summary` 非空时，用 summary 作为 evidence 的兜底值，而非直接报错。在 response 中增加 `"evidence_from_summary": true` 字段提示 LLM。

只有 `evidence` 和 `summary` 都为空时才报错。

### 7. 错误信息自修复引导

**问题**：旧版错误信息只说"invalid condition"，LLM 需要额外一轮调用才能修正。

**方案**：所有错误信息都包含**可直接复制使用的正确调用示例**：

```
Invalid condition 'non_trivial_fix' for node 'debug'.
Did you mean 'nontrivial_fix'?
Available exits:
  - nontrivial_fix -> tdd (fix requires TDD)
  - trivial_fix -> verify (fix is trivial)
  - done -> end the current workflow node
Correct usage: workflow(action="advance", workflow="debug", condition="nontrivial_fix", evidence="...")
```

### 8. 成功响应包含上下文提示

**问题**：LLM 完成一次 workflow 调用后，不知道下一步该做什么。

**方案**：成功响应中增加 `next_hints` 字段：

```json
{
  "action": "advance",
  "from": "debug",
  "condition": "nontrivial_fix",
  "activated": ["tdd"],
  "next_hints": ["tdd is now active. Write a failing test first, then implement."],
  "summary": "debug -> nontrivial_fix"
}
```

`next_hints` 从激活节点的 `WorkflowNode.goal` 和 `WorkflowNode.rules[:2]` 中提取，不超过 2 条。

### 防错机制汇总

| 机制 | 解决的出错模式 | 降错效果 |
|------|--------------|---------|
| action 无默认值 | 旧版 condition="done" 陷阱 | 消除隐式行为 |
| condition 默认值改空 | LLM 不传 condition 意外触发 done | 消除默认值陷阱 |
| 参数互斥校验 | action 与参数组合错误 | 提前拦截，忽略多余参数 |
| condition 模糊匹配 | condition 拼写错误 | 容忍下划线/连字符/空格差异 |
| workflow 智能默认 | 多活跃节点歧义 | 单节点自动推断，condition 匹配推断 |
| enter 节点名模糊匹配 | 节点名拼错 | 容忍前缀差异 |
| evidence 用 summary 兜底 | 忘填 evidence | 降级而非报错 |
| 错误信息含修复示例 | 报错后 LLM 不知道怎么改 | 一轮修正而非两轮 |
| 成功响应含 next_hints | LLM 不知道下一步做什么 | 减少无效调用 |

## Data Model

### WorkflowInput（新）

```
WorkflowInput
├── action: Literal["enter", "advance", "done"]  ← 无默认值，必须显式选择
│   enter   = 激活一个工作流节点
│   advance = 通过退出条件推进到后继节点
│   done    = 终止当前节点，不激活后继
├── workflow: str  ← 默认 ""
│   enter:   必填，要进入的节点名
│   advance: 可选，指定活跃节点（智能默认：单节点自动推断，condition 匹配推断）
│   done:    可选，指定活跃节点（智能默认：单节点自动推断）
├── condition: str  ← 默认 ""（不再默认 "done"）
│   enter:   忽略（传了不报错）
│   advance: 必填，支持模糊匹配
│   done:    忽略（传了不报错）
├── evidence: str  ← 默认 ""
│   enter:   可选
│   advance: 必填（summary 兜底）
│   done:    必填（summary 兜底）
└── summary: str
    所有 action 均可选
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
├── evidence:     [WorkflowEvidence(kind="activated", ref="tool:workflow", ok=True, summary=..., condition="enter")]
├── blocked_reason: ""
├── body_hash:    ""
└── transition_to: [后继节点名列表]
```

### enter 对已有节点状态的处理

| 当前状态 | 行为 |
|---------|------|
| 不存在于 runs | 创建新 ACTIVE 节点 |
| ACTIVE | 返回错误：already active |
| SATISFIED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| SKIPPED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| BLOCKED | 创建新 ACTIVE 节点（覆盖旧状态，允许重新进入） |
| PENDING | 更新为 ACTIVE |

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
    "summary": "Entered workflow node: debug"
  }
  ```
- **Fuzzy match response**:
  ```json
  {
    "action": "enter",
    "workflow": "debug",
    "fuzzy_matched": true,
    "matched_node": "debug",
    "activated": ["debug"],
    "next_hints": ["Locate root cause and confirm fix direction before changing code."],
    "summary": "Entered workflow node: debug (matched 'debugging' -> 'debug')"
  }
  ```
- **Errors**:
  - 节点不存在且无法模糊匹配 → `workflow: invalid node`，列出所有可用节点名
  - 节点已 ACTIVE → `workflow: already active`
  - workflow 为空 → `workflow: node required`

### action="advance"

- **Request**: `workflow(action="advance", workflow="debug", condition="nontrivial_fix", evidence="Root cause confirmed: off-by-one in parser")`
- **Response**:
  ```json
  {
    "action": "advance",
    "from": "debug",
    "condition": "nontrivial_fix",
    "activated": ["tdd"],
    "next_hints": ["Write a failing test first, then implement minimal code to pass it."],
    "summary": "debug -> nontrivial_fix",
    "evidence": "Root cause confirmed: off-by-one in parser"
  }
  ```
- **Fuzzy match response**:
  ```json
  {
    "action": "advance",
    "from": "debug",
    "condition": "nontrivial_fix",
    "fuzzy_matched": true,
    "matched_condition": "nontrivial_fix",
    "activated": ["tdd"],
    "next_hints": ["Write a failing test first, then implement minimal code to pass it."],
    "summary": "debug -> nontrivial_fix",
    "evidence": "Root cause confirmed: off-by-one in parser"
  }
  ```
- **Errors**:
  - 无活跃节点 → 返回空结果（同旧版）
  - condition 为空 → `workflow: condition required`，列出可用退出条件
  - condition 不匹配出边且无法模糊匹配 → `workflow: invalid exit`，列出可用退出条件 + 修复示例
  - 多个活跃节点无法推断 → `workflow: ambiguous target`
  - evidence 和 summary 都为空 → `workflow: evidence required`

### action="done"

- **Request**: `workflow(action="done", workflow="brainstorm", evidence="User cancelled the feature request", summary="Feature cancelled by user")`
- **Response**:
  ```json
  {
    "action": "done",
    "from": "brainstorm",
    "condition": "done",
    "activated": [],
    "summary": "brainstorm completed",
    "evidence": "User cancelled the feature request"
  }
  ```
- **Errors**:
  - 同 advance 的错误场景（无活跃节点、歧义、evidence/summary 都为空）

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `enter` 节点不存在 | 模糊匹配 → 匹配则继续，否则返回错误 + 可用节点列表 |
| `enter` 节点已 ACTIVE | 返回错误，提示节点已活跃 |
| `enter` workflow 为空 | 返回错误，提示必填 |
| `enter` 传了 condition | 忽略 condition，不报错 |
| `advance` condition 为空 | 返回错误，列出可用退出条件 + 修复示例 |
| `advance` condition 不匹配 | 模糊匹配 → 匹配则继续（标记 fuzzy_matched），否则返回错误 + 可用退出条件 + 修复示例 |
| `advance`/`done` 无活跃节点 | 返回空结果 payload（同旧版） |
| `advance`/`done` 多活跃节点无法推断 | 返回歧义错误（同旧版） |
| `advance`/`done` evidence 为空但 summary 非空 | 用 summary 兜底，标记 `evidence_from_summary` |
| `advance`/`done` evidence 和 summary 都为空 | 返回 gate 错误 |
| `done` 传了 condition | 忽略 condition，不报错 |

## Migration

### 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/advance_workflow.py` | 重命名为 `src/voidx/tools/workflow.py`；类名 `AdvanceWorkflowTool` → `WorkflowTool`；id 改为 `"workflow"`；重写 `WorkflowInput` 和 `execute`；新增模糊匹配、智能默认、evidence 兜底逻辑 |
| `src/voidx/tools/registry.py` | import 从 `advance_workflow` → `workflow`；类名更新 |
| `src/voidx/permission/rules.py` | Rule permission `"advance_workflow"` → `"workflow"`；`_CAPABILITY_MAP` 中 key 更新；`_ALLOWED_WITHOUT_READ` 更新 |
| `src/voidx/ui/output/display_policy.py` | `DEFAULT_DISPLAY_RULES` key `"advance_workflow"` → `"workflow"` |
| `src/voidx/agent/graph/runtime_guards.py` | `LOW_VALUE_REPETITIVE_TOOLS` frozenset 更新 |
| `src/voidx/agent/agents.py` | `BUILTIN_AGENTS["voidx"].tools` 列表更新 |
| `src/voidx/agent/graph/tool_executor.py` | 所有 `"advance_workflow"` 字符串引用更新为 `"workflow"` |
| `src/voidx/agent/todo_state.py` | 如需加入 replay sanitize 则更新 `_REPLAY_SANITIZED_TOOL_NAMES` |

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
| `enter` evidence kind 用 `"activated"` | 用 `"satisfied"` | `satisfied` 语义是"完成"，`activated` 更准确表达"激活" |
| condition 模糊匹配 | 严格匹配 + 报错 | LLM 拼写错误是高频问题，模糊匹配容忍下划线/连字符/空格差异，降错效果显著 |
| evidence 用 summary 兜底 | 严格校验 evidence 非空 | LLM 经常只填 summary，用 summary 兜底比报错更实用 |
| 多余参数不报错 | 严格校验参数组合 | LLM 传了不该传的参数（如 enter 时传 condition），忽略比报错更友好 |
| 错误信息含修复示例 | 只列出可用选项 | 一轮修正而非两轮，减少 LLM 反复调用 |
| 成功响应含 next_hints | 只返回状态变更 | 减少 LLM 不知道下一步做什么的无效调用 |

## Open Questions

- [ ] `enter` 是否需要 evidence 参数？当前设计为可选，但 gate 语义上要求"进入前满足条件"。考虑 enter 场景通常是 LLM 自主判断，不需要 evidence。
- [ ] 模糊匹配的阈值是否需要调整？当前前缀匹配用 4 字符，对于短节点名（如 `tdd`）可能不够精确。
