# Goal 与 Workflow Runtime 重构 — 技术设计文档

## Context

当前 `TaskIntent` 同时表达大类、动作和 workflow 入口：

- `chat`
- `inspect`
- `design`
- `review`
- `implement`
- `debug`
- `ambiguous`

这些值既被本地关键词分类器使用，也被 workflow activation、permission gating、
task state、`on_intent` tool schema 和 UI runtime snapshot 使用。结果是 intent
承担了太多职责：有时它表示是否需要代码上下文，有时表示用户目标，有时又表示工作流入口。

本设计把它拆成两层：

- `TaskIntent`：粗粒度 runtime 大类，只判断是否属于编码工作。
- `Goal`：voidx 主 agent 在每个 turn 开始时通过一次结构化 LLM 调用判断的具体目标。

Persona/prompt rename 见 [role-rename.md](role-rename.md)。Context source-of-truth
和注入边界见 [context-runtime-boundary.md](context-runtime-boundary.md)。

## Goals

- 将 `TaskIntent` 收敛为 `coding` / `general`。
- 新增结构化 `Goal`，用 `GoalType` 表达具体目标类型。
- `GoalType` 覆盖 `bugfix`、`debug`、`refactor`、`feature`、`chore`、`inspect`、
  `design`、`doc`、`review`。
- 每个 top-level user turn 开始时，voidx 主 agent 先主动调用一次 LLM，
  返回结构化 goal 数据。
- Goal 结构化数据只进入 runtime state，不混入 assistant 自然语言输出。
- Workflow activation 改为根据 `intent + goal.type` 编排。
- 不使用 HTML 注释、隐藏 markdown、用户不可见文本等方式传递控制协议。
- `on_intent` 不再作为首轮 goal 判定主路径。

## Non-Goals

- 不在本 spec 中处理 persona 命名、prompt 常量重命名或 `orchestrator` -> `voidx`。
- 不保留旧 `TaskIntent` enum 值的兼容行为。
- 不迁移旧 runtime state 或 message snapshots；旧本地数据库不兼容。
- 不让 goal resolver 替代权限系统。是否能写文件仍由 plan mode、workflow gates、
  tool allowlist、permission engine 和用户授权共同决定。

## Data Model

### TaskIntent

```python
class TaskIntent(str, Enum):
    CODING = "coding"
    GENERAL = "general"
```

`coding` 表示请求需要代码库、开发工作、技术设计、调试、文档或 review 上下文。
`general` 表示普通问答、闲聊或非代码任务。

### GoalType

```python
class GoalType(str, Enum):
    BUGFIX = "bugfix"       # 修复已知 bug 或错误行为
    DEBUG = "debug"         # 排查失败、异常、traceback、测试失败或未知根因
    REFACTOR = "refactor"   # 结构调整、重命名、抽象整理
    FEATURE = "feature"     # 新功能或行为扩展
    CHORE = "chore"         # 配置、依赖、构建、清理、发布辅助
    INSPECT = "inspect"     # 查看、理解、分析，不默认修改
    DESIGN = "design"       # 方案、架构、规划、权衡
    DOC = "doc"             # 文档、README、spec、注释、release notes
    REVIEW = "review"       # 代码审查或处理 review feedback
```

### Goal

```python
class Goal(BaseModel):
    type: GoalType
    target: str = ""
    expected_result: str = ""
    user_requested_write: bool = False
    needs_confirmation: bool = False
```

`Goal.type` 选择 workflow 入口；`user_requested_write` 和 `needs_confirmation`
帮助保留现有安全语义。比如“看看这个 bug”应是 `debug` 或 `inspect`，但
`user_requested_write=false`；“修复这个 bug”才允许后续进入实现路径。

### GoalResolution

```python
class GoalResolution(BaseModel):
    intent: TaskIntent
    goal: Goal | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
```

当 `intent=general` 时，`goal` 通常为 `None`。

## Goal Resolution Flow

Goal 判定是 voidx 主 agent 每轮第一时间做的 runtime 工作。

```
User turn starts
  |
  v
Build minimal resolver context
  - latest user message
  - interaction mode
  - pending approval / existing task state
  - recent user text window
  - workspace path and session date
  |
  v
Call model once with structured output schema GoalResolution
  |
  +-- valid structured result
  |     -> write TaskState.current_intent/current_goal
  |     -> seed AgentState.task_intent/goal
  |     -> select workflow by intent + goal.type
  |
  +-- invalid / timeout / no model
        -> local fallback: infer coding/general only
        -> current_goal = None
        -> no goal-triggered workflow activation
        -> continue normal voidx turn
```

This call is not an assistant message. It is an internal structured LLM call owned by the
main voidx turn runner. It may save a debug context frame with `frame_kind="goal"` and
`agent_persona="voidx"`, but it must not be persisted to the conversation transcript as
user-visible content.

## Structured LLM Contract

The resolver prompt should be small and rules-first:

```text
You are voidx resolving the current user's goal before normal work begins.
Return only structured data matching GoalResolution.

Rules:
- Use intent=general for non-code, non-workspace conversation.
- Use intent=coding for codebase inspection, design, docs, review, debugging, or edits.
- Do not infer write permission from analysis words like "look at", "看看", "分析", or "建议".
- Set user_requested_write=true only when the user explicitly asks to change, fix, implement, edit, write, apply, or continue an approved implementation.
- Set needs_confirmation=true when the user appears to approve work but no pending approval exists, or when write intent is ambiguous.
```

Providers with native structured output should use the provider feature. Otherwise, use
JSON mode plus strict Pydantic validation. Invalid output is treated as resolver failure.

## Workflow Activation

`workflow_activations()` changes from intent-based classification to goal-aware selection:

```python
def workflow_activations(
    user_text: str,
    *,
    agent: str = "",
    task_intent: str | None = None,
    goal_type: str | None = None,
    interaction_mode: str | None = None,
    runtime_trigger: str | None = None,
) -> list[WorkflowActivation]:
    ...
```

Primary mapping:

| Intent | Goal Type | Entry Nodes |
| --- | --- | --- |
| `general` | none | none |
| `coding` | `inspect` | none; use read/explore persona as needed |
| `coding` | `design` | `brainstorm`, optionally `plan` |
| `coding` | `doc` | `design-doc` |
| `coding` | `feature` | `brainstorm`, then `plan`, `tdd`, `verify` through DAG transitions |
| `coding` | `refactor` | `brainstorm` or `plan`, then `tdd`, `verify` |
| `coding` | `chore` | `tdd`, `verify` when it changes files; none for read-only chores |
| `coding` | `debug` | `debug` |
| `coding` | `bugfix` | `debug`, then `tdd`, `verify` |
| `coding` | `review` | `review` or `review-feedback` depending on request text/current state |

Runtime-only mapping:

| Runtime Trigger | Entry Nodes |
| --- | --- |
| `compaction` | `compaction` |
| `title` | `title` |

`DEFAULT_WORKFLOW_DAG.intent_map` should become a goal map:

```python
class GoalEntry(BaseModel):
    goal_type: str
    nodes: list[str]
    reason: str
```

`WorkflowDAG.entry_nodes()` should accept goal type, not old intent values.

## Runtime State Changes

### TaskState

```python
class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CODING
    previous_intent: TaskIntent | None = None
    current_goal: Goal | None = None
    pending_approval: PendingApproval | None = None
    workflow_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)
    recent_user_texts: list[str] = Field(default_factory=list)
    todo_state: TodoRunState | None = None
```

### AgentState

LangGraph state should not own a second goal. It may carry `TaskState` as current-turn
transport, but `TaskState.current_goal` remains the only latest goal source of truth:

```python
class AgentState(TypedDict):
    task_state: NotRequired[TaskState | dict[str, Any]]
```

### Persistence

Persist goal as part of `TaskState`, not as a separate task-run goal:

- `session_runtime_state.current_goal_json`
- `message_runtime_snapshots.current_goal_json` only if per-message debug snapshots need it

Drop `session_task_runs`. No migration is provided. Old rows with string goals or old
`TaskIntent` values are unsupported.

## on_intent Tool

`on_intent` is no longer the primary goal classification path.

Allowed transition strategy:

- Keep the tool temporarily if existing graph paths still call it for mid-turn correction.
- Update its schema to accept `intent: coding|general` and optional `goal`.
- Make its `state_patch` write the same `Goal` JSON shape as the resolver.
- Remove prompt instructions that tell voidx to call `on_intent` as the first action.

Long term, once first-step resolver and workflow routing are stable, `on_intent` can be removed
or narrowed to an explicit debugging override.

## Permission and Safety

Goal resolution does not grant write permission.

Write-capable work requires all of the following:

- `TaskIntent.CODING`
- `Goal.user_requested_write == true` or a confirmed pending approval
- not in plan mode
- no active workflow gate denying write tools
- agent/tool allowlist permits the write tool
- permission engine approves the concrete operation

For `debug` and `bugfix`, root-cause workflow gates still apply. Even when the user asks to
"fix", the debug workflow can deny write tools until root cause is identified.

## Error Handling

| Failure | Behavior |
| --- | --- |
| Resolver model call times out | Fall back to local coding/general classification, `goal=None` |
| Structured output validation fails | Treat as resolver failure; do not parse natural language |
| Unknown `GoalType` | Drop goal, continue as `coding` with no goal-triggered workflow |
| `intent=general` with non-null goal | Normalize to `goal=None` |
| `intent=coding` with null goal | Continue normal turn; workflow activation relies on plan mode/runtime trigger only |
| Old persisted intent/goal values | Unsupported; local DB must be cleared |

## Implementation Order

1. Add `GoalType`, `Goal`, and `GoalResolution` models.
2. Simplify `TaskIntent` and local `infer_task_intent()` to `coding/general`.
3. Add a goal resolver component in the turn runner before normal graph invocation.
4. Store resolver output in `TaskState.current_goal`.
5. Change workflow activation and DAG entry map from old intent values to `goal_type`.
6. Add runtime trigger support for `compaction` and `title` workflow nodes.
7. Update context/runtime boundaries per [context-runtime-boundary.md](context-runtime-boundary.md).
8. Update or deprecate `on_intent`.
9. Update tests.

## Files Expected to Change

- `src/voidx/runtime/intent.py`
- `src/voidx/runtime/task_state.py`
- `src/voidx/runtime/intent_classifier.py`
- `src/voidx/agent/graph/turn_runner.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/tool_executor.py`
- `src/voidx/agent/runtime_context.py`
- `src/voidx/agent/state.py`
- `src/voidx/agent/intent_refinement.py`
- `src/voidx/tools/on_intent.py`
- `src/voidx/workflow/schema.py`
- `src/voidx/workflow/dag.py`
- `src/voidx/workflow/policy.py`
- `src/voidx/workflow/service.py`
- `src/voidx/workflow/nodes.py`
- `src/voidx/memory/runtime_state.py`
- `src/voidx/memory/store.py`
- Tests covering intent, goal resolution, workflow activation, persistence, and safety gates

## Testing

- `infer_task_intent()` returns only `coding` or `general`.
- Goal resolver validates structured output and rejects invalid JSON.
- Goal resolver output is not persisted as an assistant message.
- `look at` / `看看` requests set `user_requested_write=false`.
- `fix` / `implement` / `修改` requests set `user_requested_write=true`.
- `GoalType.DEBUG` activates `debug`; `GoalType.BUGFIX` activates `debug` before write workflows.
- `GoalType.REVIEW` activates review or review-feedback correctly.
- Plan mode still denies write tools even when goal says write was requested.
- Active workflow gates still deny write tools after goal resolution.
- Runtime triggers select `compaction` and `title` workflow nodes without user text classification.

## Decisions

| Decision | Alternative | Reason |
| --- | --- | --- |
| Goal resolver is a first-step structured LLM call | Ask main assistant to emit hidden text | Keeps control data out of user-visible output and transcript |
| No HTML comments | HTML annotations in assistant content | Streaming, transcript, persistence, and resume paths would all need filtering |
| `TaskIntent` only `coding/general` | Keep old mixed enum | Separates coarse runtime category from concrete user goal |
| Add `debug` and `review` GoalType | Map them into bugfix/chore | Existing workflows need first-class debug/review routing |
| Workflow maps from `goal.type` | Continue mapping from old intent | Goal type is the durable workflow entry signal |
| No DB migration | Backfill old state | This is an internal breaking runtime protocol change |
