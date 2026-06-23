# Persona 思维模式注册与运行时切换 — 技术设计文档

> **Status: Done**

## Context

voidx 设计了五种 persona（coordinate / explore / plan / implement / review）作为 voidx 的思维模式，但当前实现中：

1. **persona 永远是 `"voidx"`**：主循环 `state["persona"]` 硬编码为 `"voidx"`，没有任何代码修改它
2. **on_intent 是冗余的二次调用**：`goal_resolver.py` 在 turn 开始时已通过 LLM structured output 解析出 intent + goal_type，`on_intent` 工具让 LLM 在 turn 中再做一次同样的事
3. **available_tool_ids 在提示词和硬过滤中双重限制**：既在 RuntimeContext 中提示 LLM 可见工具列表，又在 `core.py` 中硬过滤 LLM 工具定义，但工具执行本应由 tool-engine 统一兜底
4. **denied_tools 泄露到提示词**：workflow gate 的 `denied_tools` 同时出现在 Workflow Context 渲染、RuntimeContext Current Task State、Tool Contract 三处提示词中，但 tool-engine 已有拦截逻辑
5. **workflow node 与 persona 脱节**：`WorkflowNode` schema 没有 persona 字段，进入 workflow 不会切换思维模式

## Goals and Non-Goals

### Goals

- `BASE_SYSTEM_PROMPT` 注册五种 persona，声明 persona 是 voidx 的思维模式
- `WorkflowNode` 支持挂多个 persona（`personas: list[str]`），进入 node 时这些 persona 激活
- 工作流 persona 注入 runtime state，runtime 仅标注 `current_persona`，不动态拼接 persona prompt
- 移除 `on_intent` 工具，workflow 激活完全由 runtime 自动完成
- 移除 `available_tool_ids` 的提示词渲染和运行时工具定义过滤；persona/workflow 工具限制统一由 tool-engine 兜底
- `denied_tools` 不进提示词，只由 tool-engine 拦截
- 明确 AgentDef id 与 runtime persona 解耦：主 agent 是 `voidx`，子 agent 是 `sub-voidx`，persona 只是思维模式标注

### Non-Goals

- 不改变子 agent（subagent）的调度与执行架构；本设计只收敛 agent 身份命名，不引入多套 persona AgentDef
- 不改变 `WorkflowNode.gate.denied_tools` 的数据定义，只改渲染行为
- 不通过 `available_tool_ids` 或 persona AgentDef 分配工具；`voidx` / `sub-voidx` 的基础工具 allowlist 仍由 AgentDef 定义
- 不改变消息层 `role` 字段
- 不在本设计中处理 persona prompt 内容优化

## Architecture

### Persona Prompt 架构

五种 persona prompt **全量固定在 system_prompt 中**，不根据当前 persona 动态拼接。

```
BASE_SYSTEM_PROMPT
  ├── 身份声明 + 通用规则
  ├── Persona Model（五种 persona 全量注册）
  │     ├── coordinate: 协调思维
  │     ├── explore: 探查思维
  │     ├── plan: 设计思维
  │     ├── implement: 构建思维
  │     └── review: 审视思维
  └── Workflow Runtime
```

runtime state 中只标注当前激活的 persona：

```
Current Task State
  - Current persona: coordinate,explore
  - Intent: coding
  - Goal type: feature
  ...
```

LLM 根据 `Current persona` 标注自行切换思维模式，不需要运行时动态替换 persona prompt。

### AgentDef 与 runtime persona 边界

voidx 运行时只有一个主 agent：`voidx`。子任务也不再拆成 `explore` / `plan` / `implement` / `review` 等独立 AgentDef，而是使用同一个 `sub-voidx` 执行架构（可带不同 runtime persona 标注）。

因此：

- `AgentDef.name` / tool allowlist / model loop identity 仍使用 `voidx` 或 `sub-voidx`
- `state["persona"]` 只表示当前激活的思维模式，例如 `coordinate`、`coordinate,explore`、`implement`
- `state["persona"]` 不参与 `get_agent()` lookup，也不决定 LLM 工具定义集合
- `_prepare_with_stream`、UI runtime snapshot、context frame 可以读取 `state["persona"]` 作为展示和提示词标注
- `_call_llm`、tool registry、subagent loop 使用 agent id，而不是 runtime persona 字符串

这避免 `coordinate,explore` 这类组合 persona 被误当成 agent id。

### Persona 切换链路

```
用户消息
  ↓
goal_resolver (LLM structured output)
  → GoalResolution: intent + goal_type
  ↓
turn_runner
  → 初始 persona = "coordinate"（默认）
  → 从 goal_map 查找 goal_type 对应的 workflow nodes
  → 收集这些 nodes 的 personas，写入 state["persona"]
  ↓
_prepare_with_stream
  → system_prompt 中已包含五种 persona 全量定义
  → runtime state 标注 current_persona: coordinate,explore
  → 不动态拼接 persona prompt
  ↓
tool-engine (permissions.py)
  → 根据 persona + workflow gate.denied_tools 统一判断
  → 执行 / 拒绝 / 询问
  ↓
advance_workflow
  → 进入新 node，新 node 的 personas 写入 ToolStatePatch
  → persona 随 workflow 切换（仅更新 runtime state 标注）
```

### Persona 与 Workflow 的关系

persona 是 voidx 的思维模式，workflow 是结构化流程。进入 workflow node 时，node 上挂的 persona 激活，在 runtime state 中标注。一个 node 可以挂多个 persona，表示该阶段需要多种思维协同。五种 persona prompt 全量固定在 system_prompt 中，LLM 根据 runtime state 的 `current_persona` 标注自行切换思维模式。

### 工具限制统一由 tool-engine 处理

LLM 看到 agent allowlist 内的工具定义，调用时由 tool-engine 根据 persona + workflow gate 统一判断：
- persona 级别：coordinate / explore / plan / review 默认不能直接写文件，implement 可以
- workflow gate 级别：brainstorm / plan / debug 等 node 可继续禁止写工具
- 权限级别：sandbox / approval policy

`AgentDef.tools` 保留为 agent 身份级静态工具目录可见性控制，例如 `voidx` 可以看到主循环工具，`compaction` / `title` 这类隐藏 agent 不暴露交互工具。这不是 persona/workflow 的运行时工具限制；运行时限制只在 tool-engine 授权阶段执行。

三层判断在 tool-engine 中统一执行。默认策略是遇到不确定或越权风险时返回 approval request（ask）作为兜底；只有超级危险操作、明确命中禁止规则的命令或工具，才直接 deny。

## Data Model

### WorkflowNode 新增字段

```
WorkflowNode
├── name: str
├── description: str
├── triggers: list[str]
├── priority: int
├── enabled: bool
├── core_rule: str
├── personas: list[str]          ← 新增：该 node 激活的 persona 列表
├── gate: NodeGate
│   ├── denied_tools: tuple[str, ...]
│   ├── description: str
│   └── required_before_transition: str
├── workflow: list[WorkflowStep]
├── decision_rules: list[DecisionRule]
├── anti_patterns: list[str]
├── allowed_exceptions: list[str]
└── extra_sections: dict[str, str]
```

### WorkflowRunState 新增字段

```
WorkflowRunState
├── name: str
├── status: WorkflowRunStatus
├── source: WorkflowActivationSource
├── reason: str
├── goal_type: str
├── scope: str
├── personas: list[str]          ← 新增：从 WorkflowNode.personas 继承
├── activated_turn: int
├── updated_turn: int
├── evidence: list[WorkflowEvidence]
├── blocked_reason: str
├── body_hash: str
└── transition_to: list[str]
```

### ToolStatePatch 新增字段

```
ToolStatePatch
├── task_intent: TaskIntent | None
├── goal: Goal | None
├── pending_approval: PendingApproval | None
├── persona: str | None          ← 新增：激活的 persona（逗号分隔多个）
├── workflow_runs: list[WorkflowRunState]
```

### AgentState persona 字段变更

```
AgentState
├── messages: list[BaseMessage]
├── workspace: str
├── persona: str                 ← 变更：runtime thinking-mode 标注，支持逗号分隔多个，不作为 AgentDef id
├── ...
```

### 移除的字段

| 位置 | 字段 | 原因 |
|------|------|------|
| `ToolStatePatch` | `available_tool_ids` | 工具限制改由 tool-engine 兜底 |
| `AgentState` | `available_tool_ids` | 同上 |
| `MessageRuntimeSnapshot` | `available_tool_ids` | 同上 |
| `RuntimeSnapshot` (DB) | `available_tool_ids_json` | 同上 |

## API Contract

### persona prompt 拼接

**不需要运行时拼接。** 五种 persona prompt 全量固定在 `BASE_SYSTEM_PROMPT` 的 `## Persona Model` 段落中。`_prepare_with_stream` 不再根据 `state["persona"]` 动态选择和拼接 persona prompt。

runtime state 中仅标注当前激活的 persona：

```python
# runtime_context.py _current_task_state()
lines.append(f"- Current persona: {self.persona}")
# 例如: - Current persona: coordinate,explore
```

LLM 看到全量 persona 定义 + 当前激活标注，自行切换思维模式。

### tool-engine persona 级别工具约束

```python
# permissions.py 中新增
PERSONA_WRITE_TOOL_IDS = {"write", "edit", "apply_patch", "lsp_format"}

PERSONA_TOOL_CONSTRAINTS: dict[str, set[str]] = {
    "coordinate": PERSONA_WRITE_TOOL_IDS,
    "explore": PERSONA_WRITE_TOOL_IDS,
    "plan": PERSONA_WRITE_TOOL_IDS,
    "implement": set(),
    "review": PERSONA_WRITE_TOOL_IDS,  # review 可读 + bash，但仍受命令权限规则约束
}

def _persona_denied_tools(personas: list[str]) -> set[str]:
    """根据 persona 列表返回该 persona 默认不应直接使用的工具。"""
    if not personas:
        personas = ["coordinate"]
    denied = set()
    for persona in personas:
        denied.update(PERSONA_TOOL_CONSTRAINTS.get(persona, set()))
    # 只要 implement 激活，就允许写工具；workflow gate 和 permission rules 仍可继续拦截。
    if "implement" in personas:
        denied.difference_update(PERSONA_WRITE_TOOL_IDS)
    return denied
```

permission/tool-engine 的判定顺序：

1. 明确危险或命令禁止规则命中：deny
2. workflow gate `denied_tools` 命中：作为 workflow hard gate 拦截，并返回清晰错误说明
3. persona 级写工具限制命中：ask；用户批准后可执行，或由 workflow transition 切到 implement 后执行
4. sandbox / approval policy 要求审批：ask
5. 其余允许执行

### turn_runner 初始 persona 推导

```python
# turn_runner.py 中
def _initial_persona_for_goal(goal_type: str | None, workflow_runs: list[WorkflowRunState]) -> str:
    """从 goal_type 和激活的 workflow runs 推导初始 persona。"""
    if workflow_runs:
        personas = []
        for run in workflow_runs:
            if run.status == WorkflowRunStatus.ACTIVE and run.personas:
                personas.extend(run.personas)
        if personas:
            return ",".join(dict.fromkeys(personas))  # 去重保序
    # 无 workflow 时根据 goal_type 推导
    goal_persona_map = {
        "inspect": "explore",
        "design": "plan",
        "doc": "plan",
        "bugfix": "implement",
        "feature": "implement",
        "refactor": "implement",
        "chore": "implement",
        "debug": "explore",
        "review": "review",
    }
    return goal_persona_map.get(goal_type or "", "coordinate")
```

`coordinate` 是 voidx 进入 turn 时的默认思维模式。workflow node 可以只激活某个专业 persona（如 `implement`），也可以组合 `coordinate`（如 `coordinate,explore`）。是否组合由 workflow node 的 `personas` 字段决定，不强制所有 node 都包含 coordinate。

## 各 Workflow Node 的 Persona 映射

| Node | Personas | 理由 |
|------|----------|------|
| brainstorm | `["coordinate", "explore"]` | 协调 + 探查上下文 |
| design-doc | `["plan"]` | 设计思维写文档 |
| plan | `["plan"]` | 设计思维出方案 |
| tdd | `["implement"]` | 构建思维写代码 |
| verify | `["review"]` | 审视思维验证 |
| review | `["review"]` | 审视思维复核 |
| review-feedback | `["implement"]` | 构建思维改代码 |
| debug | `["explore"]` | 探查思维排查 |

## 具体改造项

### 1. BASE_SYSTEM_PROMPT 注册五种 persona

在 `BASE_SYSTEM_PROMPT` 的 `## Global Rules` 后新增 `## Persona Model` 段落：

```
## Persona Model

voidx has five thinking modes (personas). The active persona is shown in Current Task State.
Switch persona automatically when entering a workflow node.

- **coordinate**: Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed.
- **explore**: Evidence gathering and codebase search. Search broadly, report with concrete paths and lines.
- **plan**: Design and architecture. Study existing patterns, output structured implementable plans.
- **implement**: Build and execute. Write minimal precise edits, run tests to verify.
- **review**: Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts.
```

### 2. WorkflowNode 加 personas 字段

- `workflow/schema.py`：`WorkflowNode` 新增 `personas: list[str] = Field(default_factory=list)`
- `workflow/nodes.py`：各 node 定义加上 `personas=[...]`

### 3. WorkflowRunState 加 personas 字段

- `workflow/runtime.py`：`WorkflowRunState` 新增 `personas: list[str] = Field(default_factory=list)`
- `WorkflowRunState.from_match()` 从 `WorkflowNode.personas` 继承

### 4. ToolStatePatch 加 persona 字段

- `runtime/task_state.py`：`ToolStatePatch` 新增 `persona: str | None = None`
- `tool_executor.py`：处理 `persona` 的 state update，写入 `state["persona"]`

### 5. RuntimeContextBuilder：persona prompt 固定在 system_prompt

- `persona_prompt` 参数不再用于动态拼接 persona prompt
- `BASE_SYSTEM_PROMPT` 已包含五种 persona 全量定义
- `Current Task State` 中 `- Persona:` 改为 `- Current persona:`，显示当前激活的 persona 列表
- `_prepare_with_stream` 不再根据 `state["persona"]` 选择 persona prompt，只传递 persona 标注到 runtime state

### 6. turn_runner 初始 persona 推导

- `turn_runner.py`：`"persona": "voidx"` 改为从 `goal_type` + `workflow_runs` 推导
- 默认 persona 为 `"coordinate"`（而非 `"voidx"`）
- persona 仅写入 `state["persona"]`，不驱动 persona prompt 拼接

### 7. 移除 on_intent 工具

| 文件 | 操作 |
|------|------|
| `tools/on_intent.py` | 删除整个文件 |
| `agent/graph/wiring.py` | 移除 `OnIntentTool` 注册和 `on_intent_resolver` 参数 |
| `agent/graph/core.py` | 移除 `_resolve_on_intent`、`on_intent_resolver` 参数 |
| `agent/intent_refinement.py` | 删除整个文件 |
| `agent/agents.py` | `BUILTIN_AGENTS["voidx"].tools` 中移除 `"on_intent"` |
| `permission/rules.py` | 移除 `on_intent` 的 allow rule |
| `ui/output/dock/nodes.py` | 移除 `on_intent` 的 UI 映射 |
| `ui/output/console/app.py` | 移除 `on_intent` 的 gerund 映射 |

### 8. 移除 available_tool_ids

| 文件 | 操作 |
|------|------|
| `runtime/task_state.py` | `ToolStatePatch` 移除 `available_tool_ids` 字段 |
| `agent/state.py` | `AgentState` 移除 `available_tool_ids` 字段 |
| `agent/runtime_context.py` | 移除 `available_tool_ids` 参数、渲染、字段 |
| `agent/graph/core.py` | 移除 `available_tool_ids` 对 LLM 工具定义的过滤；保留 `AgentDef.tools` 作为 agent 基础可见性 allowlist |
| `agent/graph/tool_executor.py` | 移除 `available_tool_ids` 的 state update 处理 |
| `agent/intent_refinement.py` | 移除 `_available_tools_for_goal()` 逻辑（文件整体删除） |
| `memory/runtime_state.py` | 移除 `available_tool_ids` 字段和持久化 |
| `memory/store.py` | 移除 `available_tool_ids_json` 列 |
| `agent/graph/turn_runner.py` | 移除 `available_tool_ids` 的传递 |

### 9. denied_tools 不进提示词

| 文件 | 位置 | 操作 |
|------|------|------|
| `workflow/render.py` | L68-69 | 移除 `Denied tools: {', '.join(node.gate.denied_tools)}` 行 |
| `agent/runtime_context.py` | L375-377 | 移除 `Workflow gate [{name}]: denied tools = ...` 行 |
| `agent/agents.py` | L236 | 移除 Tool Contract 中 `Constraint: this persona must not write or edit files.` 行 |

保留：
- `WorkflowNode.gate.denied_tools` 数据定义不变
- `permissions.py` 中 `workflow_denied_tools()` 拦截逻辑不变

### 10. agent id 与 persona 命名统一

当前 `"voidx"` 同时承担 agent id 和 persona 标识，改为：

| 位置 | 变更 |
|------|------|
| AgentDef | 主 agent id 固定为 `"voidx"`；子 agent id 固定为 `"sub-voidx"` |
| Persona Model | 在 `BASE_SYSTEM_PROMPT` 注册 `coordinate` / `explore` / `plan` / `implement` / `review` |
| `PERSONA_PROMPTS` | 只作为 AgentDef prompt registry 使用，不再把 runtime persona 注册成 agent id |
| `BUILTIN_AGENTS` | 移除 `explore` / `plan` / `implement` / `review` 等 persona AgentDef，统一为 `voidx` / `sub-voidx` |
| `state["persona"]` 默认值 | `"voidx"` → `"coordinate"` |
| `VOIDX_PROMPT` 常量名 | 可保留为主 agent 身份 prompt；coordinate 是 Persona Model 中的默认思维模式 |
| `persona_prompt_for_llm` | 不再根据 `state["persona"]` 为主 loop 动态选择 prompt；主 loop 固定使用 `voidx` agent id |
| Tool Contract | 使用 `Agent identity: voidx/sub-voidx` 标注静态 agent 身份；运行时思维模式只在 `Current persona: ...` 中标注 |

注意：`AgentDef.name = "voidx"` 是 agent id，不变。`persona` 是思维模式标识，从 `"voidx"` 改为 `"coordinate"`。两者是不同概念；组合 persona 也不得传给 `get_agent()`。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| persona 名称不在 Persona Model 中 | 作为未知 persona 忽略其额外约束，保留当前有效 persona；不动态注入 prompt |
| workflow node 的 personas 为空 | 不切换 persona，保持当前 persona |
| `advance_workflow` 进入新 node 时 persona 变更 | 通过 `ToolStatePatch.persona` 写入 state，下一轮 `_prepare_with_stream` 生效 |
| `goal_resolver` 超时或失败 | fallback 到 `resolve_turn_intent` 本地分类器，persona 默认 `"coordinate"` |
| persona 不允许的写工具被调用 | 默认 ask；如果 workflow gate 同时禁止则 deny |
| 超级危险或命令禁止规则命中 | 直接 deny，不进入 ask 兜底 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| persona 用逗号分隔存在 `state["persona"]` 字符串中 | 用 `list[str]` 字段 | 最小化 AgentState schema 变更，现有代码 `state.get("persona", "coordinate")` 兼容 |
| coordinate 默认激活但不强制包含 | coordinate 始终包含在 persona 列表中 | coordinate 是默认协调思维；workflow 可按节点需要组合 coordinate，例如 brainstorm 使用 `coordinate,explore` |
| 移除 on_intent 而非保留 | 保留但改为自动调用 | goal_resolver 已完成 intent 解析，on_intent 是冗余的 LLM 调用，浪费 token 和延迟 |
| denied_tools 保留数据定义只改渲染 | 完全移除 denied_tools | tool-engine 仍需 denied_tools 数据来拦截，只是不渲染到提示词 |
| persona 从 `"voidx"` 改名为 `"coordinate"` | 保持 `"voidx"` | `"voidx"` 是身份名，`"coordinate"` 是思维模式名，语义更清晰 |

## Open Questions

- [x] 全量 Persona Model 的固定 token 开销是否可接受？结论：可接受，五种 persona 固定放入 `BASE_SYSTEM_PROMPT`
- [ ] `WorkflowRunState.personas` 是否需要持久化到 DB？当前 `WorkflowRunState` 不单独持久化，随 `TaskState` 整体持久化
- [ ] persona 切换是否需要通知 UI？agent id 仍显示为 `voidx` / `sub-voidx`，runtime persona 可作为单独状态展示
