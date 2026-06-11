# Workflow Node 重构设计

> **Status: In Progress**
> 日期: 2026-06-11

## 1. 背景与问题

voidx 当前有 7 个 workflow node，定义在 `src/voidx/workflow/nodes.py`，由 `WorkflowDAG` 编排。存在以下结构性问题：

1. **Node 缺少 I/O 契约** — 上下游之间没有明确的输入输出定义，数据传递靠隐含约定
2. **Node 缺少 goal** — 只有 core_rule（原则）和 description（描述），没有"这个 node 要达成什么"
3. **Node 缺少 tools 声明** — 不知道每个 node 该用什么工具，gate 的 denied_tools 是黑名单但无白名单
4. **闭环缺失** — 部分 node 没有完成验证步骤，或包含越界步骤
5. **冗余** — gate 和 role 重复限制、extra_sections 内容重复
6. **缺少 node_type** — 无法区分 task/decision/gateway/subworkflow
7. **缺少 subworkflow** — node 无法嵌套引用另一个 workflow

## 2. 设计目标

- Node 是 LLM 的最小可执行单元，内部闭环
- Node 之间正交组合，通过 I/O 契约衔接
- Role 限制能力边界，Workflow 限制过程约束，两者正交不重复
- 每个 Node 有明确的 goal、input、output、tools、规则、流转

## 3. Schema 变更

### 3.1 新增 NodeIO

```python
class NodeIO(BaseModel):
    input: dict[str, str] = Field(default_factory=dict)   # name -> description
    output: dict[str, str] = Field(default_factory=dict)  # name -> description
```

### 3.2 WorkflowNode 新增字段

```python
class WorkflowNode(BaseModel):
    # 现有字段保持不变
    name: str
    description: str
    triggers: list[str]
    priority: int
    enabled: bool
    core_rule: str
    gate: NodeGate
    workflow: list[WorkflowStep]
    decision_rules: list[DecisionRule]
    anti_patterns: list[str]
    allowed_exceptions: list[str]
    extra_sections: dict[str, str]

    # 新增字段
    node_type: str = "task"           # task | decision | gateway | subworkflow
    goal: str = ""                    # 这个 node 要达成什么
    io: NodeIO = Field(default_factory=NodeIO)
    tools: list[str] = []             # 白名单，与 role.tools 取交集生效
    subworkflow: str | None = None    # 引用另一个 WorkflowDAG 的 name
```

### 3.3 工具生效规则

```
实际可用工具 = Role.tools ∩ Node.tools ∩ (¬Node.gate.denied_tools)
```

- `Node.tools` 为空时，不限制（兼容现有行为）
- `Node.tools` 非空时，只允许列出的工具
- `gate.denied_tools` 仍然作为最终黑名单生效
- Role 的 `can_write=False` 等约束在 agent 层面已强制，不需要 gate 重复

### 3.4 node_type 语义

| node_type | 含义 | 典型用途 |
|-----------|------|---------|
| `task` | 执行具体工作 | TDD、写文档、debug |
| `decision` | 做选择，不执行工作 | brainstorming、receiving-code-review |
| `gateway` | 路由分发 | intent-classification |
| `subworkflow` | 委派到子 workflow | delegate-explore |

## 4. 逐 Node 审计与改动

### 4.1 brainstorming

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `确认需求和设计方案，获得用户批准` | 补缺失 |
| +node_type | `decision` | 本质是做选择，不执行工作 |
| +io.input | `{"user_request": "用户原始请求"}` | 补缺失 |
| +io.output | `{"design": "批准的设计方案或确认的变更范围", "scope": "确认的变更边界"}` | 补缺失 |
| +tools | `["read","glob","grep","repo_map","clarify","plan_checkpoint","webfetch","websearch"]` | 补缺失 |
| -workflow step 5 | 删除 "Write design doc" | 越界，写文档是 writing-design-docs 的职责 |
| gate.denied_tools | 保留 | orchestrator 有写权限，此阶段需禁止写代码 |

**闭环检查：** step 4 "Present design for approval" 即闭环——用户批准 = 完成。

### 4.2 writing-design-docs

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `产出通过读者测试的结构化文档` | 补缺失 |
| +node_type | `task` | 执行具体工作 |
| +io.input | `{"design": "设计方案", "doc_type": "文档类型(prd/tech-design/rfc/api-doc/readme)"}` | 补缺失 |
| +io.output | `{"doc_path": "文档保存路径", "doc_type": "实际文档类型"}` | 补缺失 |
| +tools | `["read","glob","grep","write","load_doc_template","repo_map","webfetch","websearch"]` | 补缺失 |
| 合并 extra_sections | "How to Request" 和 "Review Brief" 合并为 "Review Brief" | 内容重复 |

**闭环检查：** ✅ 已有 reader test（step 6）+ verify accuracy（step 7）。

### 4.3 writing-plans

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `产出可执行的实施计划，获得用户批准` | 补缺失 |
| +node_type | `task` | 执行具体工作 |
| +io.input | `{"spec": "设计文档或需求规格", "scope": "变更范围"}` | 补缺失 |
| +io.output | `{"plan": "实施计划(含任务列表、文件结构、测试定义)", "tasks": "有序任务清单"}` | 补缺失 |
| +tools | `["read","glob","grep","repo_map","webfetch","websearch","write"]` | 补缺失，write 允许写 .md 计划 |
| -gate.denied_tools | 去掉 `("write","edit","apply_patch","lsp_format")` | plan agent 本身只读已限制，双重限制导致 orchestrator 在此阶段也写不了计划文档 |
| +workflow step | 加 "Verify plan is executable — every task has a file path and a test command" | 补闭环 |

**闭环检查：** 新增可执行性验证步骤。

### 4.4 test-driven-development

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `按 TDD 循环完成实现，测试全绿` | 补缺失 |
| +node_type | `task` | 执行具体工作 |
| +io.input | `{"plan": "实施计划", "task": "当前要实现的任务"}` | 补缺失 |
| +io.output | `{"files_changed": "修改的文件列表", "tests_written": "编写的测试列表", "test_result": "测试运行结果"}` | 补缺失 |
| +tools | `["read","write","edit","bash","glob","grep","repo_map","lsp_diagnostics","lsp_format"]` | 补缺失 |
| allowed_exceptions | 改措辞：明确 exception 只适用于非代码变更 | 边界模糊 |

**闭环检查：** ✅ 已有 step 6 "Run broader test set"。

### 4.5 verification-before-completion

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `用可复现的证据证明变更达到预期状态` | 补缺失 |
| +node_type | `decision` | 本质是判断是否通过 |
| +io.input | `{"claim": "声称完成的状态(done/fixed/passing)", "files_changed": "变更的文件", "test_commands": "相关测试命令"}` | 补缺失 |
| +io.output | `{"evidence": "验证证据(命令+输出)", "verified": "是否通过", "scope": "变更影响范围(substantial/routine)"}` | 补缺失 |
| +tools | `["bash","read","glob","grep","repo_map","lsp_diagnostics"]` | 补缺失 |
| 合并 extra_sections | "Common Failure Modes" 和 "Red Flags" 合并 | 内容重叠 |

**闭环检查：** ✅ gate 要求必须跑验证命令。

### 4.6 requesting-code-review

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `发起结构化的代码审查请求` | 补缺失 |
| +node_type | `task` | 执行审查请求 |
| +io.input | `{"files_changed": "变更文件", "verification_evidence": "验证证据", "risks": "风险点"}` | 补缺失 |
| +io.output | `{"review_brief": "审查简报", "review_result": "审查结果(PASS/FAIL/NEEDS_CHANGE)"}` | 补缺失 |
| +tools | `["agent","read","glob","grep"]` | 补缺失 |
| +workflow step | 加 "Confirm review is completed and collect verdict" | 补闭环 |
| 合并 extra_sections | "How to Request" 和 "Review Brief" 合并 | 内容重复 |

**闭环检查：** 新增确认 review 完成步骤。

### 4.7 receiving-code-review

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `验证并实施有效的审查反馈` | 补缺失 |
| +node_type | `decision` | 本质是判断反馈是否有效 |
| +io.input | `{"feedback": "审查反馈内容", "source": "反馈来源(human/external)"}` | 补缺失 |
| +io.output | `{"changes_made": "根据反馈做的变更", "feedback_status": "每条反馈的处理状态(accepted/rejected/deferred)"}` | 补缺失 |
| +tools | `["read","write","edit","bash","glob","grep","repo_map"]` | 补缺失 |

**闭环检查：** ✅ 已有 step 7 "Verify"。

### 4.8 systematic-debugging

**改动清单：**

| 项目 | 改动 | 原因 |
|------|------|------|
| +goal | `定位根因并修复，验证修复有效` | 补缺失 |
| +node_type | `task` | 执行具体工作 |
| +io.input | `{"error": "错误信息或异常表现", "reproduction": "复现步骤"}` | 补缺失 |
| +io.output | `{"root_cause": "根因描述", "fix": "修复内容", "fix_type": "修复类型(trivial/nontrivial)"}` | 补缺失 |
| +tools | `["read","glob","grep","bash","repo_map","lsp_diagnostics","lsp_symbols","lsp_definition"]` | 补缺失 |
| -extra_sections | 删除 "Four Phases" | 与 workflow steps 重复 |

**闭环检查：** ✅ 已有 step 5-6 验证修复。

## 5. Subworkflow 内部闭环

### 5.1 设计原则

Node 是最小可执行单元，但某些 node 内部逻辑复杂，需要嵌套子流程。Subworkflow 的设计原则：

1. **subworkflow 是 prompt 级别的引用** — 在 node 的 extra_sections 中用 `Subworkflow: <name>` key 定义，渲染时展开到 prompt
2. **内部闭环** — subworkflow 有自己的退出条件，执行完毕后产出回到父 node
3. **不增加运行时复杂度** — 不需要嵌套 DAG 执行，只是 prompt 里的结构化指引
4. **LLM 自由发挥** — subworkflow 内部步骤是建议性的，LLM 可以根据实际情况调整

### 5.2 现有 Node 的 subworkflow 识别

| Node | 内部子流程 | 是否需要 subworkflow |
|------|-----------|---------------------|
| brainstorming | 无 | 否 |
| writing-design-docs | 文档类型选择 → 模板加载 → 写作 → 读者测试 | 可选，当前 workflow steps 已覆盖 |
| writing-plans | 目标定义 → 架构描述 → 任务拆分 → 风险识别 | 可选，当前 workflow steps 已覆盖 |
| test-driven-development | 红-绿-重构循环 | **是** — TDD 循环是经典子流程，可能多次迭代 |
| verification-before-completion | 选择验证命令 → 执行 → 判断结果 | 可选 |
| requesting-code-review | 构造 brief → 委派 review agent → 收集 verdict | **是** — 涉及子 agent 交互 |
| receiving-code-review | 逐条验证反馈 → 实施 → 验证 | 可选 |
| systematic-debugging | 四阶段调试 | **是** — 调试循环可能多次迭代 |

### 5.3 Subworkflow 定义方式

在 `extra_sections` 中用 `Subworkflow: <name>` key 定义，渲染时展开到 prompt：

```python
TEST_DRIVEN_DEVELOPMENT = WorkflowNode(
    ...
    extra_sections={
        "Subworkflow: TDD Cycle": (
            "Repeat until all tasks are complete:\n"
            "1. Pick the next task from the plan.\n"
            "2. Write a failing test for that task.\n"
            "3. Run the test — confirm it fails for the expected reason (RED).\n"
            "4. Write the minimal implementation to make the test pass.\n"
            "5. Run the test — confirm it passes (GREEN).\n"
            "6. Refactor if needed — test must stay green.\n"
            "7. Run the broader test set — confirm no regressions.\n"
            "8. If all tasks done, exit. Otherwise, go to step 1."
        ),
    },
)
```

### 5.4 Subworkflow 闭环规则

- 每个 subworkflow 必须有明确的退出条件
- subworkflow 的 output 汇总到父 node 的 output
- subworkflow 内部步骤不计入 DAG edge，纯 prompt 级别
- subworkflow 可以引用父 node 的 input，不需要重复声明

### 5.5 需要加 subworkflow 的 Node

**test-driven-development — Subworkflow: TDD Cycle**

```
Repeat until all tasks are complete:
1. Pick the next task from the plan
2. Write a failing test for that task
3. Run the test — confirm it fails for the expected reason (RED)
4. Write the minimal implementation to make the test pass
5. Run the test — confirm it passes (GREEN)
6. Refactor if needed — test must stay green
7. Run the broader test set — confirm no regressions
8. If all tasks done, exit. Otherwise, go to step 1
Exit condition: all plan tasks implemented, broader test set green
```

**systematic-debugging — Subworkflow: Debug Cycle**

```
Repeat until root cause is confirmed and fix is verified:
1. Read the full error → reproduce consistently
2. Find working examples → compare differences
3. Form one concrete hypothesis → test it minimally
4. Hypothesis confirmed? → implement smallest fix. No? → step 1 with new observations
5. Run reproduction command → error gone?
6. Run broader test set → no regressions
Exit condition: original error gone, broader tests pass
```

**requesting-code-review — Subworkflow: Review Cycle**

```
1. Construct review brief (what changed, verification, risks)
2. Delegate to review agent with brief
3. Collect verdict (PASS/FAIL/NEEDS_CHANGE)
4. If PASS → exit. If FAIL/NEEDS_CHANGE → transition to receiving-code-review
Exit condition: review verdict is PASS, or feedback handed off to receiving-code-review
```

## 6. 改动汇总

### 6.1 Schema 变更

| 文件 | 变更 |
|------|------|
| `src/voidx/workflow/schema.py` | 新增 `NodeIO`；`WorkflowNode` 新增 `node_type`、`goal`、`io`、`tools`、`subworkflow` |

### 6.2 Node 定义变更

| Node | +goal | +node_type | +io | +tools | -冗余 | +subworkflow | +闭环 |
|------|-------|-----------|-----|--------|-------|-------------|-------|
| brainstorming | ✅ | decision | ✅ | ✅ | — | — | 删 step 5 |
| writing-design-docs | ✅ | task | ✅ | ✅ | 合并重复段 | — | — |
| writing-plans | ✅ | task | ✅ | ✅ | 去 denied_tools | — | +可执行性验证 |
| test-driven-development | ✅ | task | ✅ | ✅ | 改 exceptions | +TDD Cycle | — |
| verification-before-completion | ✅ | decision | ✅ | ✅ | 合并重复段 | — | — |
| requesting-code-review | ✅ | task | ✅ | ✅ | 合并重复段 | +Review Cycle | +确认步骤 |
| receiving-code-review | ✅ | decision | ✅ | ✅ | — | — | — |
| systematic-debugging | ✅ | task | ✅ | ✅ | 删重复段 | +Debug Cycle | — |

### 6.3 渲染变更

| 文件 | 变更 |
|------|------|
| `src/voidx/workflow/render.py` | `render_node_markdown` 渲染 goal、io、node_type、tools、subworkflow |

### 6.4 向后兼容

- 所有新字段有默认值，现有 node 定义无需立即修改
- `tools` 为空时行为不变（不限制）
- `node_type` 默认 `task`，不影响现有逻辑
- `io` 为空时不渲染
- `subworkflow` 为 None 时不渲染

## 7. Orchestrator Workflow 结构化

### 7.1 现状问题

Orchestrator 的编排逻辑全部硬编码在 `ORCHESTRATOR_PROMPT` 的 Decision Flow 里（`src/voidx/agent/agents.py`），存在以下问题：

1. **不可观测** — 编排决策在 prompt 里，无法追踪 orchestrator 走了哪条路径
2. **不可调试** — 编排逻辑和 LLM 推理混在一起，出问题无法定位
3. **不可扩展** — 新增 intent 或 role 需要改 prompt，容易遗漏
4. **与 workflow 脱节** — orchestrator 自己没有 workflow node，但它在编排其他 role 进入 workflow
5. **intent_map 不完整** — `inspect` intent 没有映射到任何 node

### 7.2 设计：Orchestrator Workflow DAG

将 orchestrator 的 Decision Flow 结构化为一个独立的 WorkflowDAG：

```
ORCHESTRATOR_WORKFLOW = WorkflowDAG(
    name="orchestrator",
    nodes=[
        intent-classification,   # gateway: 分类用户意图
        direct-handle,            # task: orchestrator 自己处理(chat/inspect)
        delegate-explore,         # subworkflow: 委派探索
        delegate-plan,            # subworkflow: 委派设计/计划
        delegate-implement,       # subworkflow: 委派实现
        delegate-review,          # subworkflow: 委派审查
        delegate-debug,           # subworkflow: 委派调试
    ],
    edges=[
        intent-classification --chat--> direct-handle
        intent-classification --inspect--> direct-handle
        intent-classification --explore--> delegate-explore
        intent-classification --design--> delegate-plan
        intent-classification --implement--> delegate-implement
        intent-classification --review--> delegate-review
        intent-classification --debug--> delegate-debug
        intent-classification --ambiguous--> direct-handle
    ],
    intent_map=[
        IntentEntry(intent="chat", nodes=["intent-classification", "direct-handle"]),
        IntentEntry(intent="inspect", nodes=["intent-classification", "direct-handle"]),
        IntentEntry(intent="design", nodes=["intent-classification", "delegate-plan"]),
        IntentEntry(intent="implement", nodes=["intent-classification", "delegate-implement"]),
        IntentEntry(intent="review", nodes=["intent-classification", "delegate-review"]),
        IntentEntry(intent="debug", nodes=["intent-classification", "delegate-debug"]),
    ],
)
```

### 7.3 Orchestrator Node 定义

**intent-classification** (gateway)

| 字段 | 值 |
|------|----|
| node_type | `gateway` |
| goal | `分类用户意图，决定走哪条路径` |
| io.input | `{"user_request": "用户原始请求", "context": "当前对话上下文"}` |
| io.output | `{"intent": "分类后的意图(chat/inspect/design/implement/review/debug/ambiguous)", "confidence": "分类置信度"}` |
| tools | `["on_intent"]` |
| gate | 无限制 |

**direct-handle** (task)

| 字段 | 值 |
|------|----|
| node_type | `task` |
| goal | `orchestrator 直接处理小任务：回答问题、查看代码、小范围编辑` |
| io.input | `{"user_request": "用户请求", "intent": "意图(chat/inspect)"}` |
| io.output | `{"response": "回答或操作结果"}` |
| tools | `["read","glob","grep","repo_map","bash","write","edit","clarify","webfetch","websearch","lsp_diagnostics","lsp_symbols","lsp_definition","lsp_references"]` |

**delegate-explore** (subworkflow)

| 字段 | 值 |
|------|----|
| node_type | `subworkflow` |
| goal | `委派探索任务给 explore agent` |
| io.input | `{"task_description": "探索任务描述", "thoroughness": "探索深度(quick/medium/thorough)"}` |
| io.output | `{"findings": "探索结果摘要"}` |
| tools | `["agent"]` |
| subworkflow | `explore-workflow` |

**delegate-plan** (subworkflow)

| 字段 | 值 |
|------|----|
| node_type | `subworkflow` |
| goal | `委派设计/计划任务，激活 brainstorming → writing-plans 流程` |
| io.input | `{"user_request": "用户请求", "scope": "变更范围"}` |
| io.output | `{"plan": "批准的实施计划"}` |
| tools | `["agent","plan_checkpoint","clarify"]` |
| subworkflow | `default` |

**delegate-implement** (subworkflow)

| 字段 | 值 |
|------|----|
| node_type | `subworkflow` |
| goal | `委派实现任务，激活 TDD → verification 流程` |
| io.input | `{"plan": "实施计划", "task": "当前任务"}` |
| io.output | `{"files_changed": "变更文件", "test_result": "测试结果"}` |
| tools | `["agent","todo"]` |
| subworkflow | `default` |

**delegate-review** (subworkflow)

| 字段 | 值 |
|------|----|
| node_type | `subworkflow` |
| goal | `委派审查任务，激活 requesting-code-review 流程` |
| io.input | `{"files_changed": "变更文件", "risks": "风险点"}` |
| io.output | `{"review_result": "审查结果(PASS/FAIL/NEEDS_CHANGE)"}` |
| tools | `["agent"]` |
| subworkflow | `default` |

**delegate-debug** (subworkflow)

| 字段 | 值 |
|------|----|
| node_type | `subworkflow` |
| goal | `委派调试任务，激活 systematic-debugging 流程` |
| io.input | `{"error": "错误信息", "reproduction": "复现步骤"}` |
| io.output | `{"root_cause": "根因", "fix": "修复内容"}` |
| tools | `["agent"]` |
| subworkflow | `default` |

### 7.4 Orchestrator Workflow 与 Default Workflow 的关系

```
Orchestrator Workflow (编排层)
  │
  ├── direct-handle → orchestrator 自己干活，不进入 default workflow
  │
  └── delegate-* → 委派给子 agent，子 agent 进入 default workflow
                      │
                      ├── explore agent → explore-workflow
                      ├── plan agent → brainstorming → writing-plans
                      ├── implement agent → TDD → verification → review
                      └── review agent → requesting-code-review
```

关键规则：
- orchestrator 自己在 `direct-handle` 时，不激活 default workflow 的 node
- orchestrator 委派子 agent 时，子 agent 进入 default workflow 对应的 node
- orchestrator 的 `intent-classification` 是所有请求的入口，替代现有 prompt 里的 Decision Flow

### 7.5 对现有代码的影响

| 文件 | 变更 |
|------|------|
| `src/voidx/workflow/nodes.py` | 新增 orchestrator workflow 的 7 个 node 定义 |
| `src/voidx/workflow/dag.py` | 新增 `ORCHESTRATOR_WORKFLOW_DAG` |
| `src/voidx/workflow/policy.py` | `workflow_activations` 支持 orchestrator workflow 的激活 |
| `src/voidx/agent/agents.py` | `ORCHESTRATOR_PROMPT` 的 Decision Flow 简化，编排逻辑移到 workflow |
| `src/voidx/workflow/context.py` | `render_workflow_context` 支持多 DAG 渲染 |
| `src/voidx/runtime/intent.py` | `TaskIntent` 新增 `EXPLORE` 值（当前 inspect 和 explore 共用） |

### 7.6 迁移策略

1. **Phase 1**：新增 orchestrator workflow DAG 和 node 定义，不改现有逻辑
2. **Phase 2**：`workflow_activations` 支持 orchestrator workflow，与现有逻辑并行
3. **Phase 3**：`ORCHESTRATOR_PROMPT` 的 Decision Flow 简化为"遵循 orchestrator workflow"，移除硬编码逻辑

## 8. 缺少的工作流类型

### 8.1 现有覆盖分析

| 场景 | 现有 node | 覆盖 |
|------|----------|------|
| 需求探索与设计 | brainstorming | ✅ |
| 写文档 | writing-design-docs | ✅ |
| 写计划 | writing-plans | ✅ |
| TDD 实现 | test-driven-development | ✅ |
| 完成验证 | verification-before-completion | ✅ |
| 代码审查 | requesting/receiving-code-review | ✅ |
| 调试 | systematic-debugging | ✅ |
| 探索/理解代码 | ❌ | ❌ 缺失 |
| 发布/部署 | ❌ | ❌ 缺失 |
| 紧急修复/回滚 | ❌ | ❌ 缺失（systematic-debugging 无回滚出口） |
| 持续集成失败 | ❌ | ❌ 缺失（triggers 不覆盖 CI 场景） |

### 8.2 新增 Node：exploring-codebase

**为什么需要**：`inspect` intent 没有对应 node，explore agent 没有专属 workflow，用户最常见的"看看代码"场景缺少过程约束。

| 字段 | 值 |
|------|----|
| name | `exploring-codebase` |
| node_type | `task` |
| goal | `理解代码结构、定位关键逻辑、回答代码相关问题` |
| priority | 3 |
| core_rule | `先读再问，先搜索再推断` |
| io.input | `{"question": "用户的问题或探索目标", "scope": "探索范围(模块/文件/函数)"}` |
| io.output | `{"findings": "探索发现摘要", "key_paths": "关键文件和符号路径", "architecture_notes": "架构理解笔记"}` |
| tools | `["read","glob","grep","repo_map","lsp_symbols","lsp_definition","lsp_references","webfetch","websearch"]` |
| triggers | `["look at","analyze","explain","understand","check","how does","看看","分析","梳理","了解","检查","是什么","为什么"]` |

**workflow steps：**

1. Clarify the question — 确认用户想了解什么
2. Map the scope — 用 repo_map/glob 定位相关文件
3. Read key files — 读取核心文件，理解结构
4. Trace references — 用 lsp_definition/lsp_references 追踪调用链
5. Summarize findings — 组织发现，回答用户

**闭环检查：** step 5 即闭环——用户的问题被回答。

**DAG 变更：**
- 新增 `Edge(source="exploring-codebase", target="brainstorming", condition="needs_design")` — 探索后发现需要设计
- `intent_map` 新增 `IntentEntry(intent="inspect", nodes=["exploring-codebase"])`

### 8.3 新增 Node：releasing

**为什么需要**：项目有 `docs/releasing.md` 但没有工作流覆盖，从 verification 到发布之间有断裂。

| 字段 | 值 |
|------|----|
| name | `releasing` |
| node_type | `task` |
| goal | `完成版本发布流程，确保版本号、changelog、构建、测试一致` |
| priority | 70 |
| core_rule | `发布前必须验证版本号、changelog、构建产物三者一致` |
| io.input | `{"version": "目标版本号", "changes": "本次变更内容"}` |
| io.output | `{"release_version": "发布的版本号", "artifacts": "构建产物列表", "changelog_path": "changelog 路径"}` |
| tools | `["read","write","edit","bash","glob","grep","repo_map"]` |
| triggers | `["release","publish","deploy","version bump","发布","上线","部署","版本更新"]` |

**workflow steps：**

1. Verify version file — 检查版本号是否已更新
2. Verify changelog — 检查 changelog 是否包含本次变更
3. Run full test suite — 跑全量测试
4. Build artifacts — 构建产物
5. Verify artifacts — 验证产物版本号与目标一致
6. Tag and push — 打 tag 并推送

**闭环检查：** step 5-6 即闭环——产物验证通过 + tag 推送成功。

**DAG 变更：**
- 新增 `Edge(source="verification-before-completion", target="releasing", condition="ready_to_release")`
- 新增 `Edge(source="requesting-code-review", target="releasing", condition="review_passed_release")`
- `intent_map` 新增 `IntentEntry(intent="implement", nodes=[..., "releasing"])`

### 8.4 systematic-debugging 补充：回滚出口

**为什么需要**：当前 systematic-debugging 只能走 TDD 或 verification，没有"修不了先回滚"的出口。

**DAG 变更：**
- 新增 `Edge(source="systematic-debugging", target="verification-before-completion", condition="rollback", label="unable to fix, reverting to last known good state")`
- `systematic-debugging` 的 decision_rules 新增：`DecisionRule(condition="rollback", description="Use when the bug cannot be fixed within reasonable effort and a revert to a known good state is safer than continuing.")`

### 8.5 systematic-debugging 补充：CI triggers

**DAG 变更：**
- `systematic-debugging` 的 triggers 新增：`"CI failed", "pipeline failed", "CI 挂了", "流水线失败"`

### 8.6 新增 Workflow：explore-workflow

为 explore agent 提供专属 workflow：

```
EXPLORE_WORKFLOW = WorkflowDAG(
    name="explore-workflow",
    nodes=[
        exploring-codebase,
    ],
    edges=[],
    intent_map=[
        IntentEntry(intent="inspect", nodes=["exploring-codebase"]),
    ],
)
```

### 8.7 完整 DAG 变更汇总

**新增 node：**
- `exploring-codebase` (priority=3)
- `releasing` (priority=70)

**新增 edge：**
- `exploring-codebase --needs_design--> brainstorming`
- `verification-before-completion --ready_to_release--> releasing`
- `requesting-code-review --review_passed_release--> releasing`
- `systematic-debugging --rollback--> verification-before-completion`

**intent_map 变更：**
- 新增 `inspect → exploring-codebase`
- `implement` nodes 追加 `releasing`

**新增 workflow DAG：**
- `explore-workflow`
- `orchestrator-workflow`（第 7 节）

## 9. 实施路线

### Phase 1：Schema + 现有 Node 补全
- `schema.py` 新增 `NodeIO`、`node_type`、`goal`、`io`、`tools`
- `nodes.py` 7 个 node 补 goal/node_type/io/tools
- `render.py` 渲染新字段
- 裁剪冗余（去 denied_tools、合并重复段、删越界 step）
- 补闭环（writing-plans、requesting-code-review）
- 补 subworkflow（TDD Cycle、Debug Cycle、Review Cycle）

### Phase 2：新增 Node + DAG
- 新增 `exploring-codebase` node
- 新增 `releasing` node
- `systematic-debugging` 补 rollback 出口和 CI triggers
- `dag.py` 新增 edge 和 intent_map
- 新增 `explore-workflow` DAG

### Phase 3：Orchestrator Workflow
- 新增 orchestrator workflow DAG 和 7 个 node
- `policy.py` 支持多 DAG 激活
- `context.py` 支持多 DAG 渲染
- `agents.py` 简化 ORCHESTRATOR_PROMPT
- `intent.py` TaskIntent 新增 EXPLORE

### Phase 4：运行时集成
- `workflow_activations` 支持 Node.tools 白名单
- `workflow_denied_tools` 与 Node.tools 取交集
- 子 agent 自动进入对应 workflow
- auto_advance 支持新 node 的自动流转
