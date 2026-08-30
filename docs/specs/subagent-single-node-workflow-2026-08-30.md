# 子 Agent 单节点工作流收敛 — 技术规格

> **Status: Proposed**  
> **Date: 2026-08-30**  
> **Audience: Human + LLM**

## TL;DR

子 agent 应执行边界明确的工作，而不应参与主 agent 的跨节点工作流编排。将 `review`、`debug`、`implement` 三种模式分别收敛为 `review`、`debug`、`tdd` 单节点；从子 agent 工具面移除通用 `workflow` 工具，并且不向子 agent 展示 DAG 出口边。子 agent 只返回结构化事实与结果，父 agent 独占 `feedback`、`verify`、`review` 等后续路由决策和最终 fresh verification。

## Context

当前 `agent` 工具将模式映射为一段 workflow route：

| Mode | Current `join` | Current `leave` |
|------|----------------|-----------------|
| `review` | `review` | `review` |
| `debug` | `debug` | `debug` |
| `implement` | `tdd` | `verify` |

映射定义在 `src/voidx/agent/adapters/tools/subagent.py` 的 `_MODE_ROUTES`，并由 `normalize_agent_input()` 写入 `PlanResolution(join, leave)`。子 agent 运行时使用 `child_workflow_runtime()` 注入 route 节点定义，同时 `RuntimeContextBuilder._current_task_state()` 根据完整 DAG 展示活动节点的出口边。

这导致两层职责混合：

1. 子 agent 的 route boundary 要求它在固定节点结束；
2. 子 agent 同时持有 `workflow` 工具，并能看到主 DAG 的出口；
3. 例如 review 子 agent 能看到 `review --review_has_issues--> feedback`，因而可能发起进入 `feedback` 的 advance；
4. 执行器再根据 `leave=review` 截断该转移，只满足 `review` 而不激活 `feedback`。

`src/voidx/agent/adapters/langgraph/runtime/tool_executor/workflow.py` 的 route-terminal 逻辑保证了状态边界，但模型侧仍存在不必要的路由认知和工具调用。`implement` 的 `tdd -> verify` 也把实施验证与父 agent 的独立完成验证混在一起；而 `tdd` 节点本身已经要求 RED、GREEN 和 broader test set。

## Goals / Non-Goals

### Goals

- 三种子 agent 模式均使用一个固定工作流节点：
  - `review -> review`
  - `debug -> debug`
  - `tdd -> tdd`
- 从所有子 agent 的工具面移除通用 `workflow` 工具。
- 子 agent 上下文只包含自身单节点定义，不显示任何 DAG 出口边。
- 子 agent 以既有结果 contract 返回事实、结论和实施证据。
- 父 agent 独占跨节点路由，并根据子 agent 结果决定 `feedback`、`verify`、`review` 或结束。
- 父 agent 在声明完成前执行 fresh verification；implement 子 agent 的测试结果不能替代该验证。

### Non-Goals

- 不修改主 agent 的默认 workflow DAG 或节点语义。
- 不删除 `WorkflowRoute`、`PlanResolution` 或通用 `workflow` 工具。
- 不重做子 agent 结构化汇报协议；该议题继续由 `docs/design/subagent-report-protocol.md` 管理。
- 不改变 `agent` 工具公开输入：`mode` 仍为 `review | debug | implement`。
- 不允许 review/debug 子 agent 自动修复问题。
- 不在本变更中处理 AGENTS.md / Project Instructions 是否自动注入子 agent 的独立问题。

## Proposed Design

### 1. 固定单节点模式

`_MODE_ROUTES` 调整为：

```python
_MODE_ROUTES = {
    "review": ("review", "review", "review"),
    "debug": ("debug", "debug", "debug"),
    "implement": ("feature", "tdd", "tdd"),
}
```

三元组继续表示 `(goal_type, join, leave)`，避免扩大数据模型变更。`join == leave` 成为所有标准子 agent 的不变量。

### 2. 单节点工作流提示

`child_workflow_runtime()` 的模式映射与 `_MODE_ROUTES` 保持一致：

```python
routes = {
    "review": ("review", "review"),
    "debug": ("debug", "debug"),
    "implement": ("tdd", "tdd"),
}
```

当 `join == leave` 时，同一个节点定义只能渲染一次，不得重复注入两份 `tdd`、`review` 或 `debug` 正文。

子 agent 仍可看到：

- 基础系统规则；
- persona 模型和当前 persona；
- 当前单节点的完整定义；
- `Goal`、`Scope`、`Details`；
- 当前 route，如 `tdd -> tdd`；
- 结果 contract、运行环境、权限和自身 todo。

子 agent 不应看到：

- 当前节点的 DAG 出口摘要；
- 非当前节点的完整定义；
- 父 agent 的完整对话或私有工作流状态。

### 3. 隐藏出口边

为 `RuntimeContextBuilder` 增加显式上下文策略，而不是修改全局 DAG。例如增加语义明确的布尔选项：

```python
show_workflow_transitions: bool = True
```

主 agent 保持默认 `True`；`run_subagent()` 构建上下文时传入 `False`。`_current_task_state()` 仅在该选项为真时调用 `workflow_exit_summaries()` 并渲染：

```text
Workflow transitions [<node>]: ...
```

不得通过向子 agent 传入裁剪或伪造 DAG 来隐藏出口，因为 workflow state reconciliation 和 route-terminal 判断仍可能需要真实 DAG。

### 4. 移除子 agent 的 `workflow` 工具

将 `workflow` 加入固定 child blocked tools：

```python
_BLOCKED_CHILD_TOOLS = {"agent", "clarify", "checkpoint", "workflow"}
```

约束适用于三种 mode，且不受 `AgentDef.can_delegate` 或父 registry 内容影响。主 agent 的工具 registry 不得被修改；过滤必须继续发生在子 agent 的 registry 副本上。

子 agent 的完成不依赖 `workflow.done()` 或 `workflow.advance()`。其终止依据是：

- 模型返回符合结果 contract 的最终结果；
- 现有 result-contract 重试与 convergence guard；
- 运行预算、取消或错误等既有终止路径。

### 5. 父子职责边界

```text
review child
  -> verdict + findings + risks + next_actions
  -> parent decides PASS/end or feedback

debug child
  -> root_cause + evidence + reproduction + fix_direction
  -> parent decides done, trivial handling, plan, or implementation

implement child
  -> TDD cycle + relevant broader tests
  -> files_changed + tests_run + risks + followups
  -> parent runs fresh verification
  -> parent decides review, feedback, debug, or completion
```

子 agent 输出可以作为父 agent 路由的证据，但不能直接修改父 agent workflow state。父 agent 消费 review verdict 后触发 `feedback` 的既有能力必须保留；实现不应再依赖子 agent 内部 `workflow.advance()`。

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| 三种模式全部单节点 | implement 保留 `tdd -> verify` | `tdd` 已含 RED/GREEN 和 broader tests；最终独立验证应由父 agent执行。 |
| 子 agent 不持有 `workflow` 工具 | 保留工具并依赖 route-terminal 截断 | 移除错误行动空间，避免模型与执行器语义冲突。 |
| 隐藏出口展示但保留真实 DAG | 给子 agent 构造单节点 DAG | 保持执行器 reconciliation、自动事件和边界判断的数据一致性。 |
| 保留 `join/leave` 数据模型 | 新增 `workflow_node` 并迁移 | 本次只收敛行为，避免不必要的状态与持久化迁移。 |
| 父 agent负责 fresh verification | 信任 implement 子 agent 测试 | 满足“证据先于完成声明”，并维持独立复验边界。 |

## Data Model / Migration

不需要持久化 schema 或数据迁移。标准新建子 agent route 会从 `tdd -> verify` 变为 `tdd -> tdd`。

兼容要求：

- `run_subagent()` 仍应正确执行调用方显式传入的历史/非标准 route；本规格只改变 `agent(mode=...)` 的标准映射和其可见工具面。
- 已运行或恢复的旧子 agent 若携带 `tdd -> verify`，不得因本次变更在反序列化时失败。
- route-terminal helper 的通用行为保留，不为单节点模式删除。

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `src/voidx/agent/adapters/tools/subagent.py` | 将 implement route 改为 `tdd -> tdd` | 保持公开 schema 和结果 preset 不变。 |
| `src/voidx/agent/application/prompts.py` | implement prompt route 改为单节点并去重节点渲染 | review/debug 输出不得回归。 |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | 屏蔽 `workflow`；构建 context 时关闭出口展示 | 不修改父 registry。 |
| `src/voidx/agent/application/runtime_context.py` | 增加是否展示 workflow transitions 的显式策略 | 默认保持主 agent 现有行为。 |
| `src/tests/test_agent/adapters/tools/test_agent_contract.py` | 覆盖三种 mode 的标准 route | 断言 implement leave 为 `tdd`。 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_workflow_prompt.py` | 覆盖单节点注入和去重 | implement 不含 `verify` 正文。 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_prepare_workflow.py` | 更新 implement 子 agent prompt/tool 断言 | 断言无 `workflow` 工具和无出口摘要。 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_runner.py` | 需要时补充 runtime context 回归测试 | 覆盖父/子展示策略差异。 |
| `src/tests/test_agent/application/test_runtime_context.py` 或现有对应测试文件 | 覆盖 transitions 开关 | 以仓库实际测试布局为准，禁止另建重复测试层。 |

### Invariants

- 主 agent 继续看到活动节点的合法出口，并继续使用 `workflow` 工具。
- 子 agent 始终不能使用 `agent`、`clarify`、`checkpoint`、`workflow`。
- 子 agent 的文件、bash、LSP、todo、message 等其他工具权限保持原状。
- review/debug/implement 的结果 contract 字段保持不变。
- review verdict 驱动父层后续处理的能力保持不变。
- `workflow.py` 中 route-terminal 保护及其测试不得删除。
- `agent_control(wait|cancel)`、父子 message 通信和 run status 不变。
- 不依赖自然语言中是否出现 `feedback` 来判断子 agent 是否完成。

### Edge Cases / Failure Paths

| Case | Expected Behavior | Required Coverage |
|------|-------------------|-------------------|
| implement 子 agent 初始化 | route 为 `tdd -> tdd`，只注入一份 tdd 定义 | route + prompt unit test |
| review 返回 FAIL/NEEDS_CHANGE | 子 agent结束；父层可消费 verdict 决定 feedback | existing/new integration test |
| debug 找到非简单修复 | 子 agent只汇报修复方向，不自行进入 tdd | tool-surface/prompt test |
| 父 registry 包含 workflow | 子 registry 过滤掉，父 registry 仍保留 | registry isolation test |
| 子 agent 有真实完整 DAG | Current Task State 不渲染出口，route boundary 仍可读取 DAG | runtime-context test |
| 主 agent 有活动 workflow | 继续显示出口摘要 | regression test |
| 恢复旧 `tdd -> verify` route | 可正常加载和执行，不发生 schema 错误 | compatibility test where existing coverage allows |
| result contract 不完整 | 继续使用既有 retry/convergence 处理，不尝试 workflow 跳转 | existing contract tests remain green |

### Forbidden Changes

- 不修改 `DEFAULT_WORKFLOW_DAG` 的边或节点定义。
- 不从主 agent 工具面移除 `workflow`。
- 不删除通用 route-terminal 逻辑或旧 route 的兼容支持。
- 不改变 `AgentInput` 字段、mode 枚举或 `AgentResultContract` preset。
- 不让子 agent 直接修改父 agent 的 workflow state。
- 不把父 agent fresh verification 合并回 implement 子 agent 的完成声明。
- 不顺手重构 subagent loop、权限系统、结构化汇报协议或 UI。
- 不修改无关文件，不新增依赖。

## Test Plan

按项目规则使用 `./test.py`，先 focused 后 regression。

| Scenario | Command | Expected Result |
|----------|---------|-----------------|
| agent mode contract/routes | `./test.py --backend -- src/tests/test_agent/adapters/tools/test_agent_contract.py` | 三种 mode schema 不变，标准 route 均为单节点。 |
| child workflow prompt | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_workflow_prompt.py` | review/debug/tdd 各只注入自身节点；implement 不含 verify。 |
| subagent prompt and tool surface | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_prepare_workflow.py -k "subagent"` | 子 agent 无 workflow 工具、无出口边摘要。 |
| subagent runtime state | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_runner.py -k "workflow"` | route/context 刷新与终止行为正常。 |
| route boundary regression | `./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_workflow_done.py` | 通用 leave 截断行为保持通过。 |
| relevant backend regression | `./test.py --backend -- src/tests/test_agent/adapters/tools src/tests/test_agent/adapters/langgraph/runtime` | 子 agent 与 workflow runtime 相关测试全绿。 |

若实际 runtime-context 测试文件名不同，实现者必须先用 `find`/`search` 找到现有测试归属，再运行对应 focused command；不得直接假设路径并跳过验证。

## Acceptance Criteria

- [ ] `review` 标准 route 为 `review -> review`。
- [ ] `debug` 标准 route 为 `debug -> debug`。
- [ ] `implement` 标准 route 为 `tdd -> tdd`。
- [ ] 三种子 agent 的 LLM tool definitions 均不包含 `workflow`。
- [ ] 子 agent Current Task State 不包含 `Workflow transitions [...]`。
- [ ] 主 agent Current Task State 仍包含活动节点出口摘要。
- [ ] 单节点 workflow 正文只渲染一次。
- [ ] review/debug/implement 结果 contract 保持兼容。
- [ ] review 有问题时，父 agent仍能获得完整结果并决定是否进入 feedback。
- [ ] implement 结束后，由父 agent运行 fresh verification，再作完成声明。
- [ ] focused 与相关 regression 测试通过。
- [ ] 没有修改默认 DAG、公开 agent API 或无关模块。

## Open Questions

无阻塞性开放问题。后续若结构化 `agent_report` 协议获批，可将父 agent 的路由判断从文本 fallback 进一步迁移到结构化 metadata；该迁移不属于本规格范围。
