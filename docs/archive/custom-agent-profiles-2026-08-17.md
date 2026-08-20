# 自定义智能体架构设计：可组合的 Agent Profile

> **Status: Done** — Archived on 2026-08-20.

Date: 2026-08-17


## 目标

把 voidx 的智能体基建抽象为**可自定义组合的配置体系**：任何 agent = 四层配置的组合。
现有 coding / chat / goal / loop 四种模式降级为该体系下的 4 个内置 preset，
不再拥有任何特权代码路径之外的特权。

非目标：

- 不重构 goal/loop 内部状态机、outbox 协议、UI 事件（已稳定且有持久化数据）。
- 不在 TUI 提供自定义配置界面（TUI 只做消费端，见「客户端能力分层」）。
- 不开放 RunConfig 的自由拼装（内部固定预设目录，用户按 id 选择）。

## 现状锚点（关键代码路径）

实现前必须先读这些文件，本文所有改造点都以此为基准：

| 关注点 | 文件 | 现状 |
|---|---|---|
| 智能体抽象 | `src/voidx/agent/domain/profile.py:12` | `RuntimeProfile` 9 字段（profile_id/revision/name/protocol/system_prompt/constraints/persona/continuation_policy/prompt_policy），coding/chat 为硬编码实例 |
| goal/loop preset | `src/voidx/agent/domain/automation/goal.py:47`、`loop.py:77` | 各自硬编码 `RuntimeProfile(protocol=..., prompt_policy=...)` |
| prompt 策略 | `src/voidx/agent/domain/prompt_policy.py` | `PromptPolicy` Protocol：base_system_spec / profile_sections / suppress_sections |
| 身份 prompt | `src/voidx/agent/application/prompts.py:36` | `BaseSystemPrompt`（identity/communication_style/global_rule_sections），`build_base_system(base_system=...)` 已支持外部覆盖 |
| workflow 定义 | `src/voidx/agent/domain/automation/workflow_schema.py` | `WorkflowDAG`/`WorkflowNode`/`Edge` pydantic 模型；`edges` 默认空列表，无边 DAG 天然合法 |
| workflow 硬编码 | `src/voidx/agent/application/automation/workflow/service.py:90` | `WorkflowService.__init__` 硬绑 `DEFAULT_WORKFLOW_DAG` |
| workflow 策略函数 | `src/voidx/agent/domain/automation/workflow_policy.py` | `is_workflow_terminal_condition()` 等模块级函数全部硬绑 `DEFAULT_WORKFLOW_DAG` |
| 工具面 | `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py:78` | `resolve_tool_surface(registry, context)` 已按上下文裁剪工具 |
| 工具注册 | `src/voidx/tooling/application/registry.py:39` | `ToolRegistry.register(tool_id, instance, description, parameters)`，无能力元数据 |
| skill 发现 | `src/voidx/skills/registry.py:36` | `SkillRegistry`：bundled/global/project 三层发现 + (scope, path, mtime_ns, size) 四元组签名缓存 |
| MCP 配置 | `src/voidx/config/settings_mcp.py` | per-server 配置 + `disabled` 标志，统一 gateway 暴露 |
| profile 持久化通道 | `src/voidx/agent/adapters/persistence/session_repository.py:42` | session 表 `runtime_profile` 字段；`RUNTIME_PROFILES` 硬编码四元组 |
| 子代理装配 | `src/voidx/agent/adapters/langgraph/runtime/subagent.py:368` | 已示范"按 profile 独立装配 prompt + workflow + 受限工具面" |
| HITL 审批 | `src/voidx/tooling/policy/permission/rules.py:69`、`application/authorization.py` | `classify_tool_call`（定义于 rules.py，authorization.py 消费）+ `RiskLevel` + ask 动作 + session 白名单 |
| HITL 工具封锁 | `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py:24` | `CHILD_BLOCKED_TOOLS = {agent, clarify, checkpoint}`（子代理=非 HITL 实践） |
| 自主升级通道 | `src/voidx/agent/domain/automation/loop.py:62` | `LoopDecision.outcome` 已含 `needs_user` |
| TUI 切换 | `tui/voidx_cli/app.py:226` | `_lock_submit_context_for_profile` 按 profile_id 锁定提交上下文 |
| 前端标签 | `frontend/src/services/state.ts:178` | `profileLabels` 硬编码四模式 |

## 架构：四层正交配置

```
AgentProfile（扩展 RuntimeProfile）
├── 身份层  identity / style_rules / extra_rules / persona / prompt_policy 策略
├── 行为层  WorkflowDAG —— 全量用户自定义（节点/边/门控/规则）
├── 运行层  run_mode（预设 id）+ hitl_mode: interactive | autonomous
└── 资源层  tools allow/block · skills 名单 · mcp_servers 名单
```

四模式分解验证（各自只是一个组合）：

| preset | 身份层 | 行为层 | 运行层 | 资源层 |
|---|---|---|---|---|
| coding | CodingPromptPolicy（标准段落） | 默认 DAG 全量 8 节点 | single | 全量工具 · interactive |
| chat | ChatPromptPolicy（换身份 + suppress Persona/Workflow 段） | 无 | single | 全量工具 · interactive |
| goal | GoalPromptPolicy（intake/evaluator/idle 三阶段指令） | 可选（`GoalSpec.workflow_enabled` 已是开关） | goal_eval | 按阶段过滤工具 |
| loop | LoopPromptPolicy（idle + 用户 system_prompt） | 无 | loop_fixed / loop_dynamic | 按阶段过滤工具 |

## 解析契约：从文件 profile 到运行时快照

配置文件不是运行时直接消费的对象。每次选择 profile 时，`AgentRegistry.resolve(name, workspace)` 必须完成发现、覆盖、解析、校验和快照化，返回不可变的 `ResolvedAgentProfile`：

```text
ResolvedAgentProfile
├── snapshot: AgentProfileSnapshot
│   ├── profile_id / revision / source
│   ├── content_hash / snapshot_hash
│   └── canonical_payload          # 规范化后的完整 profile 数据
├── runtime_profile                # RuntimeProfile + 已解析的固定 prompt policy
├── workflow_context               # WorkflowRuntimeContext | None
├── run_config                     # 固定 RunConfig preset
└── resource_policy                # tools / skills / MCP / HITL 的交集策略
```

解析契约：

1. `content_hash` 是规范化、补齐默认值并展开 `workflow.ref` 后 canonical payload 的 SHA-256；不使用原始 YAML 文本、mtime 或文件路径作为内容身份。`snapshot_hash` 再对 `{source_scope, profile_id, revision, content_hash}` 做 SHA-256，用于标识一次可恢复的解析快照；因此不同来源即使内容相同，也有不同 snapshot hash。
2. 同一来源、同一名称的内容发生变化时，`revision` 必须递增；revision 不变而 canonical payload 改变是硬错误。不同来源覆盖时，content hash 可以相同，但 snapshot hash 必须进入快照并保持来源可追踪。
3. session 创建或显式切换时解析一次并持久化快照；普通 turn、子代理、goal attempt、loop iteration 都只能使用该快照，不得再次按 profile id 读取当前文件。
4. goal/loop 的 attempt 在启动时固定 profile、run preset、resource policy 和 workflow context；文件后续修改只影响新 session 或显式重新启动的 attempt。
5. TUI、前端消费端和运行时装配都调用同一个 resolver，不得根据 profile id 自行拼装一个最小 `RuntimeProfile`。
6. 存量 `coding/chat/goal/loop` 值映射到对应 bundled preset。已有快照优先于当前文件；没有快照且自定义 profile 已删除或 hash 不匹配时，不得静默回退 coding，应标记 profile unavailable、记录诊断并要求用户选择可用 profile。

持久化至少保留 `runtime_profile`（旧字段）、`runtime_profile_revision`、`runtime_profile_content_hash`、`runtime_profile_hash`（snapshot hash）、`runtime_profile_source` 和 `runtime_profile_snapshot`。snapshot 必须足以在源文件删除、覆盖或进程重启后重建同一个 `ResolvedAgentProfile`；hash 只用于校验和诊断，不能替代 snapshot。

## Prompt 组合与保留段落

- bundled preset 可以选择固定的 `prompt_policy`；用户 profile 只能引用注册表中的策略 id，不能从 YAML 导入 Python 类或可执行代码。
- `identity` 渲染为 profile 专属的追加段落，不替换系统 `Base System`；`style_rules` 和 `extra_rules` 分别追加到 communication style 与 global rules。
- `suppress_sections` 是白名单，只允许 `Persona`/`Agent Role`、`Workflow Runtime`、`Current Task State`；`Base System`、`Runtime State`、`ExecutionPolicy`、`RuntimeEnvelope`、`Project Instructions` 和 `Session Time` 永远不能抑制或覆盖。
- profile prompt 只影响 LLM 指令，不改变 sandbox、approval、workspace 或工具执行授权；所有 prompt 规则都必须在解析阶段完成保留段落校验。

## 资源策略的默认方向

资源层不是“绕过权限的 allowlist”，而是对系统默认能力的进一步限制。最终能力永远按以下交集计算：

```text
final_policy = preset_baseline
             ∩ phase_policy
             ∩ profile_allow_block
             ∩ hitl_policy
             ∩ child_policy
```

其中任一层拒绝，LLM 不可见且执行端也必须拒绝。interactive 的缺省资源面保持现有行为；autonomous 的缺省资源面采用最小权限：不包含 `hitl_interaction`，不自动启用未声明的 skill 或 MCP server，execution-gated 工具只有在授权结果为 allow 时才能执行。`skills` 和 `mcp_servers` 未声明时，interactive 可继承已发现/已启用集合，autonomous 视为空集合。 

## 存储格式：独立文件（仿 skills）

三层来源，后者覆盖前者同名 profile：

```
内置 preset:  src/voidx/agent/bundled/agents/*.yaml    （四模式降级为 bundled）
全局:        ~/.voidx/agents/<name>.yaml
项目:        <workspace>/.voidx/agents/<name>.yaml
```

新建 `AgentRegistry`，结构仿 `SkillRegistry`（`src/voidx/skills/registry.py`）：

- 三层 discovery + `(path, mtime_ns, size)` 签名缓存 + `invalidate()`。
- profile 名称规范化复用 skill 命名规则：`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`，1–64 字符。
- 消费端检测新 profile **不需要文件监听器**：每次 discover 自动比对签名，变了即重建缓存。

### Profile 文件 schema

```yaml
name: my-reviewer                 # 必填，文件名须与之一致
revision: 1                       # 必填，整型，修改时递增
display_name: "代码评审员"
prompt_policy: coding             # 可选；只能引用已注册的策略 id，缺省 coding

identity: |                       # 身份层；追加，不替换系统 Base System
  你是一个严格的代码评审员……
style_rules:                      # 追加进 BaseSystemPrompt.communication_style
  - "评审输出按严重度分级：blocker / major / minor"
extra_rules:                      # 追加进 Global Rules
  - "不修改代码，只输出评审意见"
persona: review                   # 可选，∈ coordinate/explore/plan/implement/review
suppress_sections: []             # 只允许抑制非保留 prompt 段落

workflow:                         # 行为层；缺省 = 无 workflow，不隐式使用默认 DAG
  name: reviewer-workflow        # 可选缺省值由 loader 从 profile name 派生；显式值参与 hash
  nodes:
    - ref: review                 # 引用内置节点：按名继承完整定义
    - name: summarize             # 自定义节点：完整 WorkflowNode 字段
      goal: "汇总评审发现"
      description: "……"
      io: { input: {...}, output: {...} }
      persona: review
      gate: { required_before_transition: "所有 blocker 已列出" }
      workflow: [{ order: 1, action: "……" }]
      rules: ["……"]
  edges:                          # 可选，见「Workflow 引擎」
    - { source: review, target: summarize, condition: completed }
  terminal_exit: { condition: done }   # 可选

run_mode: single                  # 运行层，∈ 预设目录；缺省 single
hitl_mode: interactive            # interactive | autonomous；缺省 interactive

tools:                            # 只能收窄 preset/phase 的默认能力面
  allow: [read, search, find]     # 与 block 二选一；不得包含 turn/goal/loop
  # block: [bash]                 # 需要 block 时删除 allow 后使用
skills: [code-review]             # 省略=按 hitl_mode 默认；[]=显式不启用任何 skill
mcp_servers: []                   # autonomous 必须显式列出；[]=显式不启用任何 server
```

### Loader 与完整校验

AgentRegistry 的 YAML loader 在生成 `ResolvedAgentProfile` 前完成所有校验；任何硬错误都拒绝保存和解析，不能降级为 coding 或忽略未知字段：

1. YAML 顶层和嵌套对象使用 strict schema；未知字段、错误类型、重复 map key、空必填字符串和非法 enum 都是硬错误。
2. 文件名、`name`、profile id 使用同一规范化值；名称匹配 `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`，长度 1–64；`revision >= 1`。
3. `prompt_policy` 只能引用内置/注册策略 id；YAML 不得指定 Python import、模板执行、脚本或其他可执行内容。
4. `run_mode` 必须属于固定 preset 目录；协议、生命周期工具、阶段集合和终止策略由 preset 派生，profile 文件不能自行指定 protocol 或 RunConfig。
5. `tools.allow` 与 `tools.block` 不得同时出现；二者只能减少 preset/phase 默认能力，不得加入 catalog 中不存在的 tool，也不得控制由 control protocol 注入的生命周期工具。
6. `skills` 中的显式名称必须能解析到已发现 skill；显式 `mcp_servers` 必须能解析到已配置且未 disabled 的 server。缺失的显式资源是硬错误，不能静默扩大或替换资源面。
7. `workflow.ref` 先深拷贝并展开内置节点，再应用允许的同名字段覆盖；展开后的完整 DAG 才参与校验和 hash。自定义节点不能通过 `ref` 继承未公开的运行时对象。
8. 解析后检查 run_mode × hitl_mode × resource policy 的兼容矩阵；阶段指令引用的工具必须存在于该阶段最终能力面，否则拒绝 profile。autonomous preset 必须使用不依赖 `clarify`/同步 HITL 的阶段指令，不能靠 prompt 改写掩盖缺失能力。
9. 所有校验错误返回稳定的 path、code、message；桌面端展示这些诊断，TUI/运行时只消费已成功解析的 snapshot。

保存流程固定为：解析 YAML → 展开 ref → 完整校验 → 规范化 canonical payload → 计算 hash → 写临时文件 → flush/fsync → 同目录 atomic rename。保存失败不得改变旧文件；discover 遇到损坏的新文件时保留上一次有效 snapshot 并报告诊断，不得把半解析对象放入 registry。

### Workflow 运行时上下文（必须显式传递）

`WorkflowDAG` 是 profile snapshot 的一部分，不是全局可变服务。解析后的 profile 若声明 workflow，必须同时生成：

```text
WorkflowRuntimeContext
├── dag: WorkflowDAG
├── dag_revision: int              # 当前等于 profile revision
├── dag_hash: sha256(canonical expanded DAG)
└── source: bundled | global | project
```

运行时约束：

1. `ResolvedAgentProfile.workflow_context` 随 `TurnExecutionContext` 进入每个 turn；goal/loop attempt 和子代理继承同一个 context，除非它们显式解析了新的 profile snapshot。
2. `WorkflowService`、workflow policy、route、runtime、reconcile、workflow tools、prompt/context renderer 都必须接收该 context 或其中的 `dag`；不得从模块级 `DEFAULT_WORKFLOW_DAG` 推断当前 DAG。
3. `TaskState`/`WorkflowRunState` 至少保存 `dag_hash`。恢复 session 时 hash 不匹配必须停止自动流转并报告 profile/workflow snapshot 不可用，不能用当前默认 DAG 继续执行。
4. `DEFAULT_WORKFLOW_DAG` 只允许在 bundled coding preset 的 resolver 装配阶段作为明确输入；它不能作为 service、policy 或 helper 的隐藏默认参数。无 workflow 的 profile 就是无 workflow，不自动获得默认 DAG。
5. `workflow_policy` 的优先级、transition、gate、terminal condition 都从传入 DAG 计算；不得保留由默认 DAG 在 import 时预计算的模块级 transition map。

## Workflow 引擎：节点自包含，边正交可选

目标引擎模型（现有 schema 支持部分字段；本节明确自定义 profile 所需的完整语义）：

- **节点 = 自包含流程单元**：`WorkflowNode.workflow`（有序步骤）+ `subworkflow` 定义节点内部流程。
- **边/条件 = 正交可选的节点间编排**：有边则条件驱动自动流转后继；无边则节点按名手动 enter/done（`workflow` 工具语义不变）。
- **零边 DAG 合法**：等价于一组独立节点定义库，模型按需激活。

### DAG 校验细则

**硬错误（保存即拒绝）：**

1. 禁止自环边（source == target）。**环本身合法，不做无环检测**：retry 回路是引擎一等语义，
   内置 DAG 即含 verify↔tdd、verify↔debug、feedback→brainstorm 等环（`workflow_dag.py:19-27`）。
2. edge 的 source/target 必须指向已定义节点（含 `ref` 引用的内置节点）。
3. 同一节点的出边 condition 不得重复（advance 歧义）。
4. condition 不得占用保留字 `done`（`terminal_exit.condition`）。
5. `node.persona` ∈ 5 个内置 PersonaName。
6. 节点 name 与 profile name 符合 `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`，1–64 字符。
7. `ref` 引用的内置节点名必须存在。

**警告（保存通过但提示）：** DAG 不含 verify 类门控节点。判定标准：全部节点的
`gate.required_before_transition` 均为空（内置 verify/review 节点均声明了非空 gate，
`ref` 引用其一即视为满足）。

**引用语义：** `ref: <内置节点名>` 在 YAML loader 层展开为完整 WorkflowNode 副本
（WorkflowNode 模型本身不新增 ref 字段）；同名字段（goal/rules 等）出现在自定义
节点上时视为覆盖内置定义。

## RunConfig 预设目录（内部固定，用户按 id 选择）

goal/loop 在代码里已同构（controller/intake_controller/scheduler/service/idle 平行结构，
共用 outbox + wakeup pump + RuntimeDispatcher 基座），差异只是触发与终止策略：

| run_mode id | 触发策略 | 终止策略 | 对应现状 |
|---|---|---|---|
| `single` | 单发 turn | — | coding/chat |
| `goal_eval` | immediate 链式 | detached evaluator 对照 acceptance_condition 裁决，max_attempts 封顶 | goal |
| `loop_fixed` | interval_seconds 固定间隔 | self_commit（LoopDecision outcome 枚举） | loop FIXED |
| `loop_dynamic` | agent 在 commit 决策里自选 next_delay | self_commit | loop DYNAMIC |

新增组合（如 interval + evaluator）未来以新预设 id 加入目录，不开放自由拼装。
校验矩阵收敛到已知预设，HITL 兼容性逐预设定点解决。

### Preset 的协议与阶段契约

`run_mode` 是内部固定目录，不是可由 YAML 自由组合的字段。resolver 为每个 preset 返回固定的 protocol、phase、生命周期工具和资源基线；profile 文件只能收窄这些结果：

| `run_mode` | control protocol | phase 集合 | 生命周期工具 | profile 进入运行时的方式 |
|---|---|---|---|---|
| `single` | `turn` | `turn` | `turn` | 普通 turn 和 `single + autonomous` 子代理都使用同一个 snapshot |
| `goal_eval` | `goal` | `idle/intake/work/evaluator` | `goal` | goal idle、attempt work 和 evaluator 共享同一 snapshot；`GoalSpec` 只提供任务数据 |
| `loop_fixed` | `loop` | `idle/work` | `loop` | scheduler 每次 iteration 使用启动时固定的 snapshot |
| `loop_dynamic` | `loop` | `idle/work` | `loop` | scheduler 每次 iteration 使用启动时固定的 snapshot，delay 只由 LoopDecision 决定 |

`GOAL_PROFILE`、`LOOP_PROFILE` 等代码常量只能作为 bundled preset 的默认输入，不能在 runner 中覆盖已解析的 profile。goal/loop runner 必须接收 `ResolvedAgentProfile`，并把其中的 runtime profile、workflow context、resource policy 和 hitl mode 传入 `TurnExecutionContext`；不能只根据 `profile_id` 或 `GoalSpec.workflow_enabled` 重新装配一套策略。

## HITL 模式

工具注册时增加能力分类元数据（`ToolRegistry.register` 扩展）：

```
hitl_interaction   # clarify / checkpoint —— 需要人类在场
execution_gated    # write / bash —— 走 rules.py 风险分类 + authorization.py 审批
read_only          # read / search / find —— 自动放行
orchestration      # todo / workflow / skill —— 自动放行
external           # mcp —— 外部能力，不自动放行，见下
```

生命周期工具（turn/goal/loop）不在 ToolRegistry catalog 中（`tool_surface.py:96-97`
按 "lifecycle_catalog" 拒绝，由 control protocol 注入），不参与元数据分类，
其可用性由 run_mode 预设直接控制。

MCP 工具单独归类 `external`：interactive 模式沿用现状（gateway 暴露）；autonomous
模式下必须在 profile 的 `mcp_servers` 名单中显式声明，且一律按 execution_gated
对待（命中 ask → 自动拒绝）——否则等于给无人值守 agent 开未审批写入通道。

`hitl_mode` 语义：

- **interactive**：全量工具（现状）。
- **autonomous**：剥离 `hitl_interaction` 工具；execution_gated 命中 "ask" 时按策略自动拒绝；
  需要人类介入时走 `needs_user` 通道（`LoopDecision.outcome` 已有此枚举值）挂起并通知。

兼容约束：`run_mode=single` + `hitl_mode=autonomous` 合法（子代理场景）；
autonomous × self_commit 预设时若 agent 返回 needs_user，行为为挂起 + 通知，不阻塞等待输入。

### 可见性与执行授权必须闭合

`ProfileToolPolicy` 是同一个不可变策略对象，必须同时注入 LLM tool-surface resolver 和实际 tool execution/permission flow；只从 LLM surface 删除工具不构成安全边界。

```text
visible(tool, phase) = policy.allows(tool, phase)
execute(tool_call) = policy.check(tool_call, phase)
                    AND existing sandbox/authorization decision
```

执行规则：

1. execute 入口重新规范化工具名（包括 legacy alias）并检查 profile allow/block、preset、phase、HITL、child policy；LLM 未见的工具即使伪造 tool call 也必须拒绝。
2. `allow` 只能取交集，`block` 优先于任何默认 allow；profile policy 不能绕过 `rules.py`、sandbox、session deny 或 authorization grant。`ask`、`deny`、`blocked_ack` 的既有语义不能被 profile 改成 allow。
3. lifecycle 工具由 control protocol 注入，仍须通过 run_mode/phase 校验；不能通过 `tools.allow` 把被 preset 禁止的 `turn`/`goal`/`loop` 重新启用，也不能把 catalog 中的同名工具伪装成 lifecycle 工具。
4. autonomous 下剥离 `hitl_interaction`；execution-gated 工具的授权结果为 `ask` 时自动拒绝并记录原因，不等待同步输入。只有生命周期 controller 明确返回 `needs_user` 时，才挂起并通过事件通知用户。
5. MCP gateway 的授权对象必须包含 `server` 和 `tool`：autonomous 只有 `mcp_servers` 显式声明的 server 可见，且每次 `mcp` 调用都按 `external/execution_gated` 重新授权。不得沿用“mcp 永远允许”的快捷路径；interactive 仍经过现有 permission/sandbox 流程。
6. `ToolResult`、审计日志和 UI 事件记录最终 profile snapshot hash、phase、policy decision 和拒绝原因，便于证明一次执行使用了哪个权限快照。

## 客户端能力分层

| | TUI | 桌面端（frontend + Tauri） |
|---|---|---|
| 定位 | 纯消费端 | 配置端 + 消费端 |
| 能力 | 列出 profile、切换模式、显示不可用诊断 | agent/workflow 编辑器、校验、保存、删除和消费 |
| 配置 UI | 不做 | 做 |

- TUI 列表和切换都消费 `AgentRegistry.discover/resolve` 的结果；不得调用 `_lock_submit_context_for_profile` 后再根据 id 自行重建最小 `RuntimeProfile`。提交上下文必须携带已解析的 `ResolvedAgentProfile` 或其快照。
- TUI 检测新 profile：依赖 AgentRegistry 签名缓存，无 watcher。每次“列出 profile / 切换模式”都重新调用 `discover()`；调用方不得长期缓存 discover 结果。桌面端写入后 TUI 无需重启，下次列出即可见。
- profile 文件系统由后端拥有。前端不得提交任意路径，也不得直接写 `~/.voidx` 或 workspace；后端只允许 `scope=project|global`，bundled profile 只读。
- RPC 至少包含：
  - `list-agent-profiles`：返回 `name/display_name/revision/content_hash/source/run_mode/hitl_mode/availability/diagnostics`，不返回不必要的 prompt、路径或敏感配置；
  - `validate-agent-profile`：接收 YAML 或结构化 payload 与目标 scope/name，执行完整 loader 校验但不写盘；
  - `save-agent-profile`：要求 `expected_revision` 或 `expected_hash`，后端校验通过后按临时文件 + `flush/fsync` + 同目录 atomic rename 保存；并返回新的 snapshot 元数据；
  - `delete-agent-profile`：只允许删除 global/project 文件，存在活动 session/attempt 快照时只影响未来解析，不破坏已有 snapshot。
- 保存遇到 revision/hash 冲突必须返回 conflict，不覆盖磁盘上的新版本；保存失败不得改变旧文件。损坏文件的诊断通过 RPC 返回，运行时不把半解析对象放入 registry。
- 前端的模式列表、标签、白名单和 guidance 判断全部由 profile capability/metadata 驱动，不再使用固定的 `coding/chat/goal/loop` union；profile id 只作为 opaque string。
- 需新增上述 RPC 并用 `./python.py scripts/export_ui_protocol_schema.py` 同步协议；前端生成类型后只更新 RPC 消费代码，禁止手工编辑生成的 `frontend/src/rpc/protocol.d.ts`。

## 关联改造点（实现时按此清单核对）

1. **AgentRegistry 与快照**：新建 `AgentRegistry.discover/resolve` 和 strict YAML loader；完成三层发现、覆盖、`ref` 展开、完整校验、canonical payload、content/snapshot hash 和诊断。bundled preset、global/project profile 必须统一返回 `ResolvedAgentProfile`，不能让消费端按 id 自行重建 `RuntimeProfile`。
2. **持久化与生命周期**：扩展 `session_repository.py`、`provisional_sessions.py` 及 runtime snapshot mapper，保存 profile revision/content hash/snapshot hash/source/snapshot。session 创建或显式切换时固定 snapshot；goal/loop runner、scheduler、evaluator、子代理和普通 turn 均从 snapshot 取得 profile，不得在运行中重新 discover。
3. **RunConfig 与 runner**：`run_mode` 只从固定 preset 目录解析 protocol、phase、lifecycle tools 和 baseline policy。`GoalRuntimeRunner`、goal idle/evaluator、loop idle/scheduler 接收 `ResolvedAgentProfile`；`GOAL_PROFILE`/`LOOP_PROFILE` 仅作为 bundled preset 默认输入，不能覆盖用户已解析的 profile。
4. **Workflow context**：`WorkflowService.__init__` 改为接收 `WorkflowRuntimeContext` 或明确的 `WorkflowDAG`；`workflow_policy.py` 删除基于 `DEFAULT_WORKFLOW_DAG` 的模块级 transition/priority 缓存。core/helpers、workflow_state、checkpoint、reconcile、route、runtime、service、prompts 和 context renderer 的所有 workflow 查询都显式传入 DAG；`TaskState`/`WorkflowRunState` 持久化 `dag_hash`，恢复时 hash 不匹配必须停止自动流转。
5. **DAG loader/校验**：在 loader 层处理 `ref + override` 的原始节点结构，展开后只交给 `WorkflowNode`/`WorkflowDAG`；实现自环、未知引用、重复 condition、保留 terminal condition、Persona、命名、DAG gate 警告等全部规则，并让展开后的 DAG 参与 hash。
6. **资源与执行授权**：`ToolRegistry.register` 增加 capability metadata；新建不可变 `ProfileToolPolicy`，同时注入 `resolve_tool_surface`、tool executor 和 permission flow。LLM 不可见的工具在执行入口也必须拒绝；profile policy 只能收窄既有 sandbox/authorization，不能把 ask/deny/blocked_ack 变成 allow。
7. **MCP、skill 与生命周期工具**：skills 和 MCP catalog 按 snapshot policy 过滤；autonomous 下未显式声明的 skill/server 默认不可用。MCP 每次授权都携带 server+tool，移除 `mcp`/`mcp__*` 的无条件 allow 快捷路径；turn/goal/loop 仍由 control protocol 注入，并由 preset/phase 校验，不能通过 profile tools 重新启用。
8. **Prompt 与保留段落**：四个 bundled prompt policy 迁移为策略 id；YAML identity/rules 只能追加，`suppress_sections` 只接受白名单，系统保留的 `Base System`、`Runtime State`、`ExecutionPolicy`、`RuntimeEnvelope` 等不得抑制或覆盖。
9. **客户端与配置 RPC**：TUI 消费 resolver 返回的 snapshot；删除按 id 重建最小 profile 的逻辑。后端提供 `list-agent-profiles`、`validate-agent-profile`、`save-agent-profile`、`delete-agent-profile`；前端不能直接写配置路径，保存必须做 expected revision/hash 乐观并发校验、完整验证和 fsync+atomic rename。协议变更后运行 `./python.py scripts/export_ui_protocol_schema.py`。

## 不变量（禁止破坏）

1. `ExecutionPolicy`、`RuntimeEnvelope`（sandbox/approval/workspace）为系统保留段落，用户 identity/rules 只能追加，不得覆盖。
2. `run_mode`、control protocol、phase 和生命周期工具由固定 preset 派生；profile 文件不能自行指定 protocol 或自由拼装 RunConfig。
3. 所有 session/attempt/turn 使用固定的 `ResolvedAgentProfile` snapshot；profile 文件修改、覆盖或删除不影响已启动的 session/attempt。旧 `coding/chat/goal/loop` 值必须映射到 bundled preset；自定义 snapshot 不可恢复时必须标记 unavailable 并诊断，不能静默改用 coding。
4. workflow 查询、transition、gate、terminal condition 和 prompt rendering 必须使用当前 snapshot 的 `WorkflowRuntimeContext`；无 workflow 的 profile 不自动获得 `DEFAULT_WORKFLOW_DAG`。
5. 最终工具能力必须同时满足 preset、phase、profile allow/block、HITL 和 child policy；可见性与执行授权使用同一策略，既有 sandbox、session deny 和 authorization grant 仍是硬边界。
6. autonomous 不暴露 `hitl_interaction`；execution-gated 工具命中 ask 时自动拒绝并记录，只有 lifecycle controller 的 `needs_user` 才能挂起并通知。
7. profile 必须完整校验后原子落盘；revision/hash 冲突不得覆盖磁盘上的新版本；损坏文件不得进入可执行 registry。
8. 内置 preset 的 DAG 保留完整门控；verify 缺失只对用户自定义 DAG 产生警告，不降低内置 preset 的约束。

## 落地顺序

1. **契约与注册表**：先实现 `AgentProfileSnapshot`、`ResolvedAgentProfile`、strict loader、三层 AgentRegistry、诊断模型和 bundled preset；为旧四模式保留行为等价的解析结果。
2. **持久化快照**：扩展 session/provisional/runtime persistence，固定 profile snapshot 的创建、恢复、不可用诊断和 goal/loop attempt 传播；先验证文件修改不会影响已启动执行。
3. **RunConfig 与 Workflow**：把 profile snapshot 接入普通 turn、goal/loop runner 和子代理；显式传递 `WorkflowRuntimeContext`，解绑默认 DAG，加入 DAG loader/validator 与 hash 恢复保护。
4. **资源与 HITL**：实现 capability metadata、`ProfileToolPolicy` 的可见性/执行双重检查、skill/MCP 过滤、autonomous 的 ask→deny/needs_user 语义；优先验证伪造 tool call 和 MCP server/tool 越权。
5. **客户端**：实现后端 profile RPC、原子保存与乐观并发，更新 TUI/前端为 metadata/snapshot 驱动，最后同步 UI protocol schema。

## 验收标准

- `./test.py --backend` 全绿；新增测试至少覆盖：
  - AgentRegistry 三层发现、同名覆盖、签名缓存、strict schema、重复 key/未知字段和损坏文件诊断；
  - revision/content hash/snapshot hash/source、session 持久化，以及 profile 文件在 turn/goal/loop 启动后修改不影响既有 snapshot；
  - 四个 run preset 的 protocol/phase/lifecycle contract，goal/loop runner 使用传入 profile 而非硬编码常量；
  - `ref + override` 展开、DAG 全部硬错误规则、无 verify 警告、自定义 terminal condition、dag hash 恢复保护；
  - prompt 保留段落不可抑制，资源策略交集，以及 LLM 未见的伪造 tool call 在执行入口被拒绝；
  - autonomous 的 hitl 工具剥离、execution-gated ask→deny/needs_user、MCP server+tool 双重授权、既有 sandbox/authorization 不可绕过；
  - save/validate/delete RPC 的 scope 限制、expected revision/hash 冲突、原子落盘和旧文件保留。
- 桌面端生成或修改的 profile 在 TUI 无需重启即可被 discover；活动 session/attempt 仍使用原 snapshot，下一次启动才使用新版本。
- `./test.py --frontend` 全绿；运行 `./python.py scripts/export_ui_protocol_schema.py` 后协议 diff 与新增 RPC 一致。
- 文档示例可被 YAML loader 解析，所有 profile/schema 诊断包含稳定的 path、code、message。

## 风险

1. **兼容迁移**：旧 session 只有 profile id 时可映射 bundled preset；自定义 profile 无 snapshot 且源文件不可用时必须保持可诊断加载，但不能为了“可加载”静默改变行为。
2. **快照膨胀与恢复**：session/attempt 持久化完整 snapshot 会增加存储体积；必须定义清理策略，但在活动 session/attempt 结束前不得删除其 snapshot。
3. **workflow context 漏传**：任一旧 helper 继续读取默认 DAG 都会造成静默错流转；应以 custom DAG 的 terminal/transition 集成测试和静态搜索作为门禁。
4. **HITL/MCP 冲突**：autonomous 阶段指令不能依赖 clarify/checkpoint；MCP 必须同时通过 profile server allowlist 和现有权限系统，不能复用无条件 allow。
5. **evaluator 泛化**：`goal_eval` 仍依赖 `GoalSpec.acceptance_condition` 和 evaluator 语义；profile 可以选择该 preset，但不能声称仅靠 workflow YAML 就改变 evaluator 协议。
6. **循环与唤醒**：loop wakeup 的进程内 pump、fixed interval 的恢复和 outbox 持久化仍是独立风险，不由 profile registry 自动解决。
7. **用户 DAG 门控**：用户 DAG 缺省 verify 节点仍只是警告；桌面端必须明确展示风险，且 autonomous profile 不能因此绕过执行授权。
