---
name: agent-runtime-unification
display_name: Unified Agent Runtime for Chat, Coding, Goal, and Loop
description: 将 chat、coding、goal、loop 统一到可复用且具备独立 thread 边界的 Agent Runtime
_doc_type: tech-design
audience: human+llm
---

# Unified Agent Runtime — 技术设计文档

## TL;DR

当前 `VoidXGraph` 同时承担 graph host、session host、runtime resource container 和 turn controller，导致 `/loop` 只能通过 `run_synthetic_turn()` 伪装成主 session 的用户输入，也使 goal、chat、coding 难以共享同一套明确的状态边界。

本设计将执行能力、行为语义和运行状态拆成三个核心抽象：

- **`AgentRuntime`**：负责一次 agent thread 的执行、工具调用、上下文编译、压缩和生命周期。
- **`RuntimeProfile`**：描述本次运行的 prompt、goal、workflow、工具策略和继续策略。
- **`AgentThread`**：隔离消息、task/workflow state、todo、权限、runtime guards 和 compaction 状态。

`chat`、`coding`、`goal`、`loop` 不再是四套 agent 实现，而是 profile 和 lifecycle 的不同组合。Loop 是 scheduler 驱动的 thread，goal 是目标语义，workflow 是可选编排，coding 只是带有 coding 默认策略的 profile。

本阶段是设计，不修改运行时代码；后续实现必须保持现有 chat/coding 行为兼容，并逐步将 loop、goal 迁移到统一 runtime。

## Context

### 当前实现边界

当前主要运行路径位于：

- `src/voidx/agent/infrastructure/langgraph/execution.py`：`VoidXGraph` 初始化模型、工具、权限、MCP/LSP、compaction、UI 和 `TurnRunner`。
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`：加载 session 消息、构造用户消息、编译上下文、运行一轮 graph、保存结果和 runtime state。
- `src/voidx/agent/infrastructure/langgraph/runtime/topology.py`：LangGraph 的 prepare → call LLM → execute tools → finalize 拓扑。
- `src/voidx/agent/infrastructure/langgraph/runtime/thread_context.py`：使用 `ThreadExecutionState` 和 `ContextVar` 隔离不同 thread 的 session/task/workflow/compaction 状态。
- `src/voidx/agent/runtime_context.py`：通过 `RuntimeContextBuilder` 和 `ContextCompiler` 组装 system、task、workflow、goal、todo 和环境上下文。
- `src/voidx/agent/infrastructure/langgraph/runtime/wiring.py`：构造 tool registry、权限服务、compaction service 和外部 manager。
- `src/voidx/workflow/`：workflow DAG、run state、evidence 和 transition。
- `src/voidx/agent/loop/manager.py`：当前 loop scheduler；每次触发后调用宿主的 `run_synthetic_turn()`。

现有 `ThreadExecutionState` 已经能隔离：

- `TaskState`；
- `WorkflowRunState`；
- `ContextCompilerCache`；
- `compaction_summary` 和 `pending_summary`；
- `RuntimeGuardState`。

但它目前主要是 host 字段的临时镜像，执行器仍大量通过 `host._xxx` 取得资源，loop 也仍属于主 session 的执行路径。

### 问题

1. runtime 能力和 coding/主 session 状态耦合在 `VoidXGraph`。
2. `InteractionMode` 同时表达交互方式、写入策略和 goal/plan 语义。
3. `TaskState` 同时承载 intent、goal、workflow、todo 和近期 exchange。
4. loop 没有独立 transcript、context frame、workflow state 或 compaction 生命周期。
5. tool、MCP、skill 的目录可以复用，但权限、选择范围和调用状态没有明确 runtime scope。
6. 上下文编译依赖主 agent 的 persona 和 session state，无法自然承载用户自定义 loop prompt。

## Goals / Non-Goals

### Goals

- 建立可被 chat、coding、goal、loop 共同使用的 Agent Runtime。
- 明确 `RuntimeProfile`、`AgentThread`、`RuntimeResources` 和生命周期边界。
- 复用现有 LangGraph、tool、MCP、skill、workflow、context 和 compaction 基建，不复制第二套 agent。
- 让 loop 拥有独立 thread 和独立上下文，但可以继承经过筛选的 workspace、资源配置和权限策略。
- 允许用户通过 prompt、goal、workflow 和 constraints 定义 loop/goal 行为，而不是强制 coding persona。
- 保持现有 chat/coding 路径行为兼容，采用渐进迁移。
- 为暂停、恢复、取消、完成、阻塞和需要用户输入提供统一生命周期模型。

### Non-Goals

- 本设计不规定 loop 必须是 coding、monitoring 或 review agent。
- 本设计不立即重写 LangGraph topology。
- 本设计不定义新的前端协议细节。
- 本设计不要求所有 profile 都自动执行 workflow。
- 本设计不允许 loop 默认读写主 session 的 transcript、TaskState 或临时权限。
- 本设计不在当前阶段实现多 agent 编排或跨 loop 依赖。

## Design Principles

1. **执行能力与行为语义分离**：Runtime 决定如何执行，Profile 决定这次执行是什么。
2. **状态属于 thread**：所有可变 task、workflow、todo、context、permission 和 compaction 状态必须有明确 owner。
3. **资源可共享，视图必须隔离**：模型 provider、tool definition、MCP catalog、skill catalog 可以共享；ToolContext、权限 grant、allowlist 和调用记录必须按 runtime/thread 隔离。
4. **目标、workflow、loop 是正交能力**：goal 表达目标，workflow 表达编排，loop 表达自动触发；三者不能继续隐含绑定。
5. **用户 prompt 是行为来源**：系统提供 runtime envelope 和生命周期协议，不把通用 loop 强行改造成 coding agent。
6. **结构化状态优先于文本解析**：完成、阻塞、继续和需要用户等状态必须通过模型或 tool 协议表达，不能解析最终自然语言。

## Target Architecture

```text
AgentRuntimeFactory
  ├── RuntimeResources       # 可共享能力和 runtime-scoped views
  ├── RuntimeProfile         # prompt / goal / workflow / policies
  └── AgentThread            # transcript / state / persistence boundary
          │
          ▼
AgentRuntime
  ├── ContextCompiler
  ├── TurnExecutor
  ├── ToolExecutor
  ├── CompactionCoordinator
  └── LifecycleController
          │
          ▼
LangGraph topology
  prepare → call_llm → execute_tools → finalize
```

### AgentRuntime

```python
class AgentRuntime:
    runtime_id: str
    profile: RuntimeProfile
    resources: RuntimeResources
    thread: AgentThread

    async def run_turn(self, input_frame: InputFrame) -> TurnResult: ...
    async def compact(self, *, force: bool = False) -> CompactionResult | None: ...
    async def cancel(self) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
```

`AgentRuntime` 不判断自己是 chat、coding 还是 loop；它只执行 profile 和 thread 提供的运行契约。`TurnExecutor` 可以继续复用当前 `TurnRunner`，但最终应通过显式 runtime/context/resources 取依赖，而不是依赖 `VoidXGraph` 隐式字段。

### RuntimeProfile

```python
class RuntimeProfile(BaseModel):
    profile_id: str
    revision: int
    name: str
    system_prompt: str = ""
    goal: GoalSpec | None = None
    workflow: WorkflowSpec | None = None
    constraints: list[str] = Field(default_factory=list)
    persona: str | None = None
    interaction_policy: InteractionPolicy
    execution_policy: ExecutionPolicy
    tool_policy: ToolPolicy
    skill_ids: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)
    continuation_policy: ContinuationPolicy
```

约束：

- profile 只保存不可变/版本化运行规格：prompt、`GoalSpec`、`WorkflowSpec` 和 policy；不保存运行进度。`(profile_id, revision)` 唯一，更新产生新 revision，不原地修改。
- `GoalSpec/WorkflowSpec` 各自具有稳定 `spec_id` 和内容 `revision`（或等价 content hash）；run state 和 turn attempt 固定引用精确 revision。
- `goal`、`workflow` 和 `constraints` 可为空；`persona` 是可选 prompt 策略，不是 runtime 类型。
- `tool_policy` 只描述允许范围；实际 grants 由 runtime/thread 权限视图决定。
- `continuation_policy` 定义是否自动继续、最大轮数、最大生命周期和无进展/失败策略。
- `InteractionMode` 只存在于旧 adapter 输入；runtime 创建时一次性翻译为两个 policy，之后不再读取它。

### AgentThread

```python
class AgentThread:
    id: str
    parent_id: str | None
    session_id: str | None
    workspace: str
    state: AgentThreadState
    store: ThreadStore
```

`AgentThreadState` 只保存可变运行态，而不是 host 字段的镜像：

```python
class AgentThreadState:
    thread_id: str
    transcript: TranscriptState
    goal: GoalRunState | None
    workflow: WorkflowRunCollection
    todo: TodoRunState | None
    context: ContextState
    compaction: CompactionState
    permissions: PermissionState
    runtime_guards: RuntimeGuardState
    lifecycle: LifecycleState
```

`GoalSpec/WorkflowSpec` 的唯一 owner 是 profile；对应 run state 只保存 status、位置和 evidence，并通过 spec id/version 引用 profile。todo 的唯一 owner 是 thread state。

现有 `TaskState` 不进入新 API 或新 thread 表。迁移期由 `LegacyTaskStateProjection` 从 profile + thread state 单向生成，只供尚未切换的 context/tool adapter 读取，禁止反向写回；Phase 4 后删除。

主 chat/coding 通常是 session-backed thread；loop 是带 `parent_id` 的 child thread。loop 可以引用父 session 的 workspace 和稳定配置，但不能隐式读写父 thread 的可变状态。

### RuntimeResources

```python
class RuntimeResources:
    model: Any
    tools: ToolCatalog
    permissions: PermissionService
    mcp: McpManager
    lsp: LspManager
    skills: SkillService
    compaction: CompactionService
    usage: UsageStats
    ui: RuntimeUIPort
```

资源分为两层：

```text
Shared resources:
- model/provider setup
- static tool definitions
- MCP process/catalog manager
- skill catalog
- LSP connection manager
- compaction algorithms and token estimation

Runtime-scoped views:
- filtered ToolRegistry
- ToolContext
- permission grants and approval state
- MCP allowlist
- selected skills
- usage accounting scope
- UI event scope
```

### 状态所有权

统一 runtime 不以“复制 host 字段”作为隔离手段。可变状态的 owner 固定如下：

| 状态 | Owner | 是否共享 |
|---|---|---|
| model/provider、静态 tool schema、MCP/skill catalog | process resources | 是 |
| transcript、goal/workflow run state、todo、compaction、lifecycle | `AgentThreadState` | 否 |
| goal/workflow spec、interaction/execution/tool/continuation policy | `RuntimeProfile` | profile 可复用，内容不可变 |
| tool instances、`ToolContext`、task tracker、调用记录 | `AgentRuntime` | 否 |
| allow/deny、临时 grants、pending approvals、execution leases | `PermissionView` | 否 |
| file mtimes/read coverage、workflow repeat guard、dangerous-call guard | turn/thread guard state | 否 |
| current messages、UI output node、pending guidance | `TurnExecution` | 否 |
| token/usage counters | runtime scope，聚合到 process | 明细不共享 |
| MCP/LSP 连接进程 | process manager | 是，但调用 view 不共享 |
| workspace mutation lock | workspace lock manager | 可共享，按 workspace key 仲裁 |

Phase 3 facade 在这些状态完成迁移前只能串行执行，不宣称支持多 runtime 并发。不得通过继续扩充 `ThreadExecutionState` 对 host 做双向镜像来完成最终实现。

### Tool 构造边界

`ToolCatalog` 只保存不可变 definition 和 runtime tool factory，不保存有状态 tool instance：

```python
class ToolCatalog:
    def definitions(self) -> tuple[ToolDefinition, ...]: ...
    def bind(self, scope: RuntimeToolScope) -> RuntimeToolRegistry: ...

class RuntimeToolScope:
    thread_id: str
    tracker: TaskTracker
    permissions: PermissionView
    mcp: McpView
    skills: SkillView
    lifecycle: LifecyclePort | None
```

每个 runtime 调用 `bind()` 创建 registry 和有状态 tool instance。`ToolRegistry.filtered_copy()` 只可在现有串行路径中临时使用，不能作为 thread 隔离机制；迁移完成后删除该用途。无状态实现可以由 factory 复用，但对调用方仍表现为 runtime-bound registry。

### ThreadStore 与持久化决策

`AgentThread` 是独立持久化实体，不复用父 session 的 `session_runtime_state` 行。存储模型必须在迁移 loop 前落地：

```text
agent_threads
  id / parent_thread_id / session_id / workspace
  profile_id / profile_revision / profile_json / resource_scope_json
  created_at / updated_at

agent_thread_state
  thread_id (PK)
  goal_run_json / workflow_runs_json / todo_json
  context_json / compaction_json / permission_json
  runtime_guards_json / lifecycle_json / state_version

agent_thread_messages
  id / thread_id / role / payload / status / created_at

agent_thread_frames
  id / thread_id / prefix_hash / frame_hash / metadata / created_at

runtime_turn_attempts
  id / thread_id / source_outbox_id / input_frame_json / base_state_version
  profile_id / profile_revision / status / side_effect_started
  lease_owner / fencing_token / lease_expires_at / updated_at

runtime_outbox
  id (= wakeup_id) / thread_id / source_attempt_id / kind / payload_json
  expected_state_version / available_at / claimed_by / claimed_until / delivered_at
```

约束：

- 主 chat/coding 迁移期可以通过 `SessionThreadStoreAdapter` 读取现有 session 表；child thread 一律写入 thread 表。
- `profile_json` 和 `resource_scope_json` 保存可恢复快照；secret 只保存引用，不复制凭据。attempt 固定保存 profile revision；恢复发现 snapshot/revision 或 spec 引用不匹配时进入 `blocked`，不能自动升级到新 profile。
- 一轮成功提交以同一事务写入消息、thread state、lifecycle decision，并向 `runtime_outbox` 写入下一调度意图；工具产生的外部副作用不宣称可事务回滚。
- lifecycle decision 保存在 `agent_thread_state.lifecycle_json`；调度意图只保存在 outbox。scheduler 以 claim/ack 消费 outbox，ack 可重试且不改变 thread state。
- 使用 `state_version` 做 optimistic concurrency control；同一 thread 同时只允许一个 active turn。
- `runtime_turn_attempts.source_outbox_id` 唯一且非空；`runtime_outbox.source_attempt_id` 对同一 `(attempt_id, kind)` 唯一。一个 wakeup 最多创建一个 attempt，一个 committed attempt 最多创建一个同类后续意图。
- parent 删除时默认暂停 child 并等待显式清理策略；不得依赖数据库静默级联删除正在运行的 loop。
- 恢复时先取得 scheduler lease，再读取 profile、resource scope 和 state；缺失或不兼容的资源使 thread 进入 `blocked`，不能扩大权限继续运行。

`ThreadStore` 提供 `load()`、`begin_attempt()`、`mark_side_effect_started()`、`commit_decision()`、`recover_attempt()` 和 lease/outbox claim/ack API；物理表名可在实施时调整，但上述 identity、事务和恢复语义不是 Open Question。

`ToolRegistry.filtered_copy()` 可以保留用于旧串行入口的选择过滤；它不能继续作为 runtime 隔离基础。`schedule_wakeup` 不能继续作为唯一 loop 能力入口，后续应通过 runtime-scoped lifecycle capability 暴露调度意图。

## Context Model

上下文由四层组成：

```text
RuntimeEnvelope
  workspace / platform / execution policy
ProfileContext
  system prompt / goal / workflow / constraints / selected resources
ThreadContext
  summary / workflow state / todo / evidence / context frames
TurnContext
  input / trigger / iteration / lifecycle / pending guidance
```

当前 `RuntimeContextBuilder` 和 `ContextCompiler` 应演化为：

```python
context = ContextCompiler.compile(
    envelope=runtime.envelope,
    profile=runtime.profile,
    thread=thread.state,
    turn=turn.context,
)
```

通用 loop 的上下文示例：

```text
[LOOP RUNTIME]
loop_id: deploy-monitor
iteration: 4
trigger: scheduled
status: waiting

GOAL
<user-defined goal>

WORKFLOW
<user-defined workflow>

PREVIOUS LOOP STATE
<summary, evidence, actions, blockers>

CURRENT INPUT
<user-defined loop prompt>

RUNTIME RULES
- use only the selected tools, MCP servers, and skills;
- do not assume a coding persona;
- preserve evidence and state for the next iteration;
- report a structured lifecycle decision before the turn ends.
```

该 envelope 是 runtime context，不应作为主 session 的普通用户消息写入。

## Modes and Composition

模式是 profile、thread 和 lifecycle 的组合，而非独立 agent 类别：

| Mode | Profile defaults | Thread | Lifecycle |
|---|---|---|---|
| Chat | conversational prompt, optional tools | parent/session thread | user-driven, no automatic continuation |
| Coding | coding prompt, workspace tools, optional workflow | session-backed thread | user-driven, task-oriented |
| Goal | goal + optional user workflow | independent task thread | manual or automatic advancement |
| Loop | user prompt + optional goal/workflow | child thread | scheduler-driven, pause/resume/stop |

允许组合：

```text
Chat + Workflow
Coding + Goal
Goal + Loop
Loop + MCP + Skills
Chat + Tools
Autonomous + custom prompt
```

语义边界：

- **Goal** 是目标/完成条件，不等于自动循环。
- **Loop** 是触发和持续执行机制，不等于某种 persona。
- **Workflow** 是状态和转移编排，不等于 plan mode。
- **Coding** 是默认 profile，不是 runtime 内核。
- **Chat** 是用户驱动 lifecycle，不代表不能使用 tools 或 workflow。

## Workflow and Policy Separation
### PermissionView

共享的 `PermissionService` 只承载持久策略、sandbox 配置和跨 runtime 的路径锁；每个 runtime 必须绑定独立 view：

```python
class PermissionView:
    scope_id: str                 # runtime_id
    thread_id: str
    inherited_policy_revision: int

    async def check(self, request: PermissionRequest) -> PermissionDecision: ...
    async def approve(self, approval_id: str, grant: GrantScope) -> None: ...
    async def revoke(self, grant_id: str) -> None: ...
    async def acquire_execution_lease(self) -> ExecutionLease: ...
```

规则：

- persistent grants 可按当前策略继承；session/runtime grants、allow/deny 和 pending approvals 默认不继承。
- approval id、grant 和 execution lease 都带 `scope_id`，跨 scope 操作必须拒绝。
- UI approval event 必须包含 runtime/thread id，并路由回创建它的 view。
- 权限收紧立即提高 policy revision；已有 lease 按当前安全规则失效或完成，不能悄然获得扩大后的权限。
- thread 只持久化可恢复的 grant 引用和 policy revision，不持久化 pending approval 或 active lease；重启后未决请求转为 `needs_user`。


`InteractionMode` 仅保留在旧入口，由 adapter 一次性映射为 profile 的两个 policy；新 runtime 内部不存储或判断该枚举：

```python
class InteractionPolicy(BaseModel):
    allow_user_input: bool = True
    interruptible: bool = True

class ExecutionPolicy(BaseModel):
    allow_workspace_writes: bool
    require_user_approval: bool
```

Goal、workflow 和 todo 的运行态分别由 thread state 独立持有，不再聚合回 `TaskState`。自动继续、最大轮数/生命周期、退避和失败预算只由 `ContinuationPolicy` 决定；interaction/execution policy 不重复表达这些字段。

## Loop Lifecycle Protocol

Loop 每轮应产生结构化生命周期结果，而不是让 scheduler 解析自然语言：

```python
class RuntimeDecision(BaseModel):
    outcome: Literal[
        "continue", "completed", "blocked", "needs_user", "failed"
    ]
    summary: str
    progress: Literal["none", "partial", "meaningful"] = "none"
    next_delay_seconds: float | None = None  # model suggestion, not authority
    reason: str = ""
```

推荐通过 runtime-scoped `loop_update` tool 产生决定：

- `completed`：结束 runtime；
- `needs_user`：暂停并通知父 session；
- `blocked`：停止无意义重试；
- `failed`：按 continuation policy 处理退避或终止；
- `continue`：由 scheduler 继续；
- 未调用时使用 profile 的安全默认策略。

`next_delay_seconds` 只是模型建议。`LifecycleController` 必须按 `ContinuationPolicy` 的 minimum/maximum delay、退避、剩余轮数和生命周期预算校验并裁剪；最终 `available_at` 只由 controller 计算。非法建议转为 policy 默认值，模型不能延长预算或绕过终态。

旧 `schedule_wakeup` 在迁移期保持兼容，但最终应降为调度能力，而不是完整 loop 语义。
### Lifecycle 状态机

```text
created → ready → running → waiting → running
                    │  │       ├→ paused → ready
                    │  ├→ needs_user → ready
                    │  ├→ retry_wait → running
                    │  ├→ blocked → ready       # 人工修复后
                    │  ├→ failed                # 终态
                    │  └→ completed             # 终态
                    └→ cancelling → cancelled   # 终态
```

`completed`、`failed`、`cancelled` 才是终态。`RuntimeDecision.outcome="failed"` 先由 continuation policy 分类：可重试错误提交为 `retry_wait`，预算耗尽或不可重试错误才提交 `failed`；`blocked` 是不自动调度的可恢复状态。

- 所有 transition 由 `LifecycleController` 校验并以 expected `state_version` 提交；重复提交相同 decision 幂等。
- `pause` 只阻止新 turn；已有 turn 完成后提交为 `paused`。`cancel` 进入 `cancelling`，取消模型/工具执行，等待清理后提交 `cancelled`。
- 外部 cancel 优先于同轮 `continue/completed`；已经原子提交的终态不被晚到的 cancel 覆盖。
- `loop_update` 只记录本轮 decision，不直接取消当前 asyncio task；scheduler 在 turn commit 后执行 stop/wakeup。
- scheduler wakeup 是 durable outbox item，带 `(thread_id, expected_state_version, wakeup_id)`；已 ack 的 id 直接丢弃。version 不匹配时不能静默删除，必须检查关联 attempt 后 ack、重建或转入恢复状态。
- `RuntimeDispatcher` claim wakeup 后取得 thread lease；每次取得 lease 都分配单调递增的 `fencing_token`。它以同一事务、按 `source_outbox_id` 幂等创建 `runtime_turn_attempts(status="prepared")`、将 lifecycle 置为 `running`、提高 state version。原 wakeup随后可 ack，因为恢复由 durable attempt 驱动。
- dispatcher 加载 attempt 固定的 profile revision 创建 runtime。执行期间 heartbeat 续租；`mark_side_effect_started`、每次外部 tool 调用前的 lease check、`commit_decision` 都必须以 `(attempt_id, lease_owner, fencing_token, expected_state_version)` 条件更新/验证。续租或 fencing 校验失败后旧 worker 不得调用工具或提交。
- 调用任何可能有外部副作用的 tool 前，先持久化 `side_effect_started=true`。成功时 `commit_decision(...)` 原子写 messages/state/lifecycle/outbox，并将 attempt 标记 `committed`。
- `RuntimeRecoveryWorker` 只能用更高 fencing token 接管 lease 过期的 attempt：`prepared + side_effect_started=false` 可安全重跑同一 attempt；`side_effect_started=true` 不能自动重放，转为 `needs_user` 并附 attempt/evidence；`committed` 的未投递 outbox 幂等重投。
- cancel 与 attempt 使用同一 thread lease/version/fencing；`cancelling` 恢复时只做清理和状态收敛，不重新执行 turn。
- `needs_user` 创建父 thread 控制事件并暂停；用户输入作为显式 `InputFrame` 恢复，不写入 loop transcript 之外的普通父消息。
- `blocked → ready` 必须由显式人工操作并重新校验 profile/resource/permission；`retry_wait` 只用于 continuation policy 允许的错误，并采用有上限退避。

恢复决策以 outbox/attempt 关联为准：

| Outbox | Attempt | 处理 |
|---|---|---|
| unclaimed/claim expired | 不存在 | 重新 claim，以唯一 `source_outbox_id` 创建 attempt |
| claimed/acked | `prepared`, no side effect | recovery 取得更高 fencing token，重跑同一 attempt |
| claimed/acked | `prepared`, side effect started | 转 `needs_user`，禁止自动重放 |
| 任意 | `committed` | ack source；幂等投递该 attempt 的未投递后续 outbox |
| delivered | 不存在 | 数据损坏，thread 转 `blocked` 并告警 |
| version mismatch | 任意 | 不静默删除；按以上 attempt 状态收敛后 ack 或 blocked |

## Tool, MCP, and Skill Boundaries

### Tools

复用现有 tool implementation 和 schema；每个 runtime 创建自己的 registry view、`ToolContext`、permission grants、workspace lock 和调用状态。loop 默认不继承主 session 的临时授权。

### MCP

默认继承稳定配置但使用 runtime 自己的 allowlist 和调用记录。用户可在 `RuntimeProfile.mcp_server_ids` 中指定 loop 专属 MCP。主 session 临时启用的 MCP 不应自动泄漏给 loop。

### Skills

复用 skill registry；profile 保存 skill id 或版本快照。每个 runtime 独立渲染 skill context，避免依赖主 session 当前激活的 skill。

### Workflow

复用 `src/voidx/workflow/` 的 DAG、`WorkflowRunState`、evidence 和 transition；workflow runs 存放在 thread state，不放在共享 host 上。

## Compaction and Persistence

压缩算法、token estimation、summary prompt 可以共享；以下内容必须按 thread 隔离：

- live messages 和 transcript cache；
- context compiler cache；
- compaction summary、pending summary、tail anchor；
- loop context frames；
- workflow/goal evidence；
- runtime usage scope。

Loop 的长期摘要至少保留：

1. 用户定义的 prompt、goal 和 workflow；
2. 最近观察和证据；
3. 已执行动作；
4. blocker 和未解决问题；
5. 当前 lifecycle；
6. 下一次触发原因或时间。

父 session 只记录 loop 的生命周期事件和简短摘要，不记录 loop 的全部工具输出。loop 线程恢复必须显式恢复其 profile、资源 allowlist 和 thread state。

## Migration Plan

迁移遵循 **expand → switch → contract**，adapter 必须标注删除阶段；不新增长期双写、双状态模型或第二套执行链路。

### Phase 1: Resolve names and define protocols

- 将现有 `src/voidx/agent/domain/state.py` 中的状态模型 `AgentRuntime` 重命名为 `SessionRuntimeState`，新执行对象独占 `AgentRuntime` 名称。
- 定义 `RuntimeProfile`、`AgentThread`、`TurnExecution`、`LifecycleState`、`RuntimeResources` 和 view 接口。
- 建立 mutable-state ownership 测试/清单；此阶段不改变 chat/coding 行为。

### Phase 2: Add ThreadStore and runtime-bound views

- 增加 thread schema、`ThreadStore`、scheduler lease 和版本化事务。
- 实现 `ToolCatalog.bind()`、`PermissionView`、MCP/skill/UI/usage scoped views。
- `SessionThreadStoreAdapter` 只读写现有 session 表并投影为 thread 接口，不做新旧表双写；child thread 从第一天只使用新 store。该 adapter 在 Phase 6 完成 session 数据迁移后删除。

### Phase 3: Extract turn execution

- 将 `TurnRunner`、tool executor、compaction coordinator 的 turn 可变状态迁入显式 `TurnExecution`/`AgentThreadState`。
- 新增 `AgentRuntime.run_turn()`，内部暂时调用现有 LangGraph topology。
- `VoidXGraph` 仅作为旧入口 adapter，禁止新增业务字段；完成状态所有权测试前保持串行。

### Phase 4: Switch chat/coding and prove isolation

- chat/coding 入口切换到 `AgentRuntime`；现有 session 继续由 `SessionThreadStoreAdapter` 支撑。
- 完成两个 runtime 的工具、权限、UI、usage、compaction 并发隔离测试后，才开放并发。
- 将 context compiler 和 tool executor 改为直接读取 profile/thread state；随后删除 `LegacyTaskStateProjection`、host 字段镜像、`run_once()` 的隐式 host 依赖和已替代 wiring，不保留双执行路径。

### Phase 5: Migrate goal and loop

- goal 使用 goal profile 和独立 task thread；loop 使用 child thread。
- scheduler 只持久化 wakeup 和 lifecycle command，不执行 agent turn；`RuntimeDispatcher` 消费 wakeup，取得 thread lease 后由 factory 恢复 runtime 并调用 `run_turn()`。
- loop 从切换之日起具备独立 transcript、context frame、恢复、pause/resume/cancel；不再调用 `run_synthetic_turn()`。
- `schedule_wakeup` 适配到 structured lifecycle decision，完成现有调用迁移后删除其直接操作 `LoopManager` 的路径。

### Phase 6: Contract legacy surface

- 删除 loop host adapter、`ThreadExecutionState` host 镜像和 runtime 隔离用途的 `filtered_copy()`。
- 将 UI/status/guide 全部切到 runtime/thread id；删除仅服务旧 synthetic turn 的协议和测试。
- 以一次离线/启动迁移将现有 session runtime state、messages 和 frames 导入 thread store；验证计数与 hash 后切换读取路径并删除 `SessionThreadStoreAdapter`。旧表只保留一个发布周期的只读回滚窗口，后续 schema migration 删除。
- 普通 chat/coding 不实例化 scheduler 或 loop-specific state。

每个 phase 必须同时提交代码、迁移/回滚说明和删除项；禁止以“后续清理”为由保留两个 source of truth。


## Implementation Layout

以下路径是实施默认布局，若现有模块职责在实施前发生变化，必须在对应 spec 中显式更新，不能另起平行抽象：

```text
src/voidx/agent/domain/profile.py                 # profile/spec 与 policy
src/voidx/agent/domain/thread.py                  # thread/run state 与 lifecycle
src/voidx/agent/domain/turn.py                    # InputFrame/TurnExecution/TurnResult
src/voidx/agent/runtime/runtime.py                # AgentRuntime facade
src/voidx/agent/runtime/factory.py                # shared resources → scoped views
src/voidx/agent/runtime/lifecycle.py              # transition/policy validation
src/voidx/agent/runtime/dispatcher.py             # wakeup → fenced attempt → runtime.run_turn
src/voidx/agent/runtime/recovery.py               # expired attempt recovery
src/voidx/agent/runtime/tools.py                  # ToolCatalog.bind
src/voidx/permission/view.py                      # PermissionView
src/voidx/memory/thread_store.py                  # ThreadStore repository
src/voidx/memory/store.py                         # SQLite schema migration
src/voidx/agent/loop/scheduler.py                 # wakeup/lease only
```

固定 focused tests：

```text
src/tests/test_agent/runtime/test_thread_store.py
src/tests/test_agent/runtime/test_isolation.py
src/tests/test_agent/runtime/test_permission_view.py
src/tests/test_agent/runtime/test_lifecycle.py
src/tests/test_agent/runtime/test_dispatcher.py
src/tests/test_agent/runtime/test_session_migration.py
src/tests/test_agent/loop/test_runtime_scheduler.py
```

## Compatibility and Safety

必须保持：

- 现有 chat/coding 用户输入和工具调用行为；
- 当前 LangGraph 拓扑和 tool result 处理语义；
- 当前 session transcript 的兼容读取；
- 当前权限模式和 workspace sandbox；
- 无 loop 场景的资源和性能开销近似不变。

禁止：

1. 为 loop、goal 再复制一套 tool/MCP/skill/compaction 实现。
2. 让 loop 直接读写父 thread 的 `TaskState`、workflow runs 或 transcript。
3. 用 `InteractionMode` 隐式表达 goal、workflow、写权限和自动继续。
4. 让 scheduler 解析模型自然语言判断完成状态。
5. 默认继承主 session 的临时权限、临时 MCP 或临时 skill 状态。
6. 把用户自定义 loop prompt 自动包装成 coding persona。
7. 为保持旧入口而继续扩大 `VoidXGraph` 的职责。

## Acceptance Criteria

### Runtime isolation

- 同一父 session 下两个 child thread 并发执行 tool、todo 和 workflow，TaskState、tracker、guards、messages、UI node、usage 与 compaction 均不交叉。
- loop transcript/context frame 不进入父消息历史；取消任一方不误取消另一方。
- 同一 thread 的第二个 active turn 因 lease/version 冲突被拒绝。

### Resource and permission scoping

- 每个 runtime 获得独立 tool instances、`ToolContext`、tracker 和 `PermissionView`；`filtered_copy()` 不作为隔离证明。
- 临时 allow/deny、grant、pending approval、execution lease 不跨 runtime；approval event 能准确路由。
- MCP/skill allowlist 收窄后不能通过共享 manager 绕过；普通 chat/coding 不创建 loop scheduler/state。

### Persistence and lifecycle

- child thread 重启后恢复 profile、resource scope、transcript、state、summary 与下一调度意图。
- 故障注入覆盖 prepared 前、prepared 后/run 前、side effect 后/commit 前、commit 后/ack 前四个崩溃窗口；分别证明安全重试、转 `needs_user`、outbox 幂等重投且不丢调度。
- 执行中 lease 过期并由新 worker 接管时，旧 worker 因 fencing token 失效不能再调用 tool 或提交。
- pause、cancel、complete、`loop_update` 并发时符合状态机优先级；取消期间不会丢失已提交摘要，也不会重放未知外部副作用。
- 权限或资源引用失效后恢复为 `blocked/needs_user`，不得扩大权限运行。

### Context and compatibility

- profile 可只提供用户 prompt，不要求 persona；loop 每轮获得自己的 goal、workflow、iteration、summary 和 lifecycle context。
- compaction 只处理当前 thread，并保留 profile 与未解决状态。
- 现有 chat/coding focused tests 通过；loop 测试迁移到 runtime-backed 路径，不再断言 synthetic turn。
- 最终代码中不存在 `LegacyTaskStateProjection`、host 状态镜像、loop 对 `run_synthetic_turn()` 的调用，以及两套 runtime source of truth。

### Verification commands

```bash
./test.py --backend -- src/tests/test_agent/runtime/test_thread_store.py -v
./test.py --backend -- src/tests/test_agent/runtime/test_isolation.py -v
./test.py --backend -- src/tests/test_agent/runtime/test_permission_view.py -v
./test.py --backend -- src/tests/test_agent/runtime/test_lifecycle.py -v
./test.py --backend -- src/tests/test_agent/runtime/test_dispatcher.py -v
./test.py --backend -- src/tests/test_agent/runtime/test_session_migration.py -v
./test.py --backend -- src/tests/test_agent/loop/test_runtime_scheduler.py -v
./test.py --backend -- src/tests/test_agent -v
./test.py --backend
```

## Remaining Implementation Choices

以下只允许影响局部实现，不得改变本文已确定的 ownership、隔离和事务语义：

1. thread 表是否与现有 message store 共用 repository 层；child thread 必须有独立 identity 和状态行。
2. MCP/LSP manager 的具体连接复用策略；allowlist、permission 和调用 scope 必须 runtime-bound。
3. `loop_update` 的最终 tool 名称及是否向非 loop 的 auto-continuation profile 暴露。
4. 用户 workflow 使用现有 schema 引用或自然语言编译；持久化后必须形成确定的 `WorkflowSpec` 快照。
5. UI 在现有 event types 上增加 scope，或引入 runtime event envelope；所有事件必须携带 runtime/thread identity。

## Decision Summary

最终统一模型：

```text
AgentRuntime = 如何执行
RuntimeProfile = 执行什么语义
AgentThread = 状态和上下文边界

Chat / Coding / Goal / Loop = profile + thread + lifecycle 的组合
```

这允许后续新增通用 autonomous、scheduled goal 或 custom prompt runtime，而不再在 `VoidXGraph` 和 `LoopManager` 中堆叠模式特例。
