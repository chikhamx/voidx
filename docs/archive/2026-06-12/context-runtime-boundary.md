# Context Runtime Boundary — 技术设计文档

## Context

voidx 的 LLM context 当前由多层信息拼接而成：stable system prompt、workflow/skill
HumanMessage、per-turn task overlay、历史消息、todo/convergence 临时消息，以及 context frame
快照。随着 persona、goal、workflow runtime 扩展，多个字段开始重复表达同一件事：

- `Runtime State` 和 `Current Task State` 都包含 mode、agent、agent id。
- `Session Date` 和 `Current DateTime` 同时存在。
- `TaskState.current_goal`、`TaskRun.goal`、`AgentState.goal`、`WorkflowRunState.scope`
  都在表达 goal 或 scope。
- `TaskPhase` 和 `Goal.type` 表达同一类阶段/目标信息。
- workflow selection 在 `workflow_context_for()` 中被计算两次。

本设计定义 context 的职责边界和 source of truth。实现时不做兼容逻辑，不迁移旧数据。

相关设计：

- [role-rename.md](role-rename.md)：persona/prompt 命名与 hidden runtime persona。
- [goal-workflow-runtime.md](goal-workflow-runtime.md)：Goal、Intent、workflow 入口重构。

## Goals

- 明确哪些字段是 source of truth，哪些只是 LLM 展示、transport、cache 或 snapshot。
- 移除 Runtime State / Current Task State 的重复字段。
- 只保留 session 固定时间，不再每轮注入 current datetime。
- 只保留一个最新结构化 goal，位置为 `TaskState.current_goal`。
- 删除 `TaskRun.goal`、`AgentState.goal`、`goal_phase`、`goal_status`、`goal_turn_count`。
- 删除 `TaskPhase`，使用 `Goal.type` 和 workflow run status 表达进度。
- workflow selection 每轮只计算一次。
- 将 workflow context 与 skill context 命名拆开，不再通过 `skill_context_content`
  承载 workflow body。

## Non-Goals

- 不兼容旧 session runtime state、message runtime snapshots 或 context frames。
- 不保留旧字段 alias。
- 不改变消息层 `role` 字段。
- 不改变 conversation history 的语义消息持久化方式。

## Source of Truth

| Concept | Source of Truth | Not Source of Truth |
| --- | --- | --- |
| Session fixed time | Session runtime state / session metadata | per-turn `Current DateTime` |
| Current intent | `TaskState.current_intent` | `AgentState.task_intent` except as current-turn cache |
| Current goal | `TaskState.current_goal: Goal | None` | `TaskRun.goal`, `AgentState.goal`, tool string fields |
| Pending approval | `TaskState.pending_approval` | duplicated `approved_scope` booleans |
| Workflow run state | `TaskState.workflow_runs` | separate `TaskRun.workflow_runs` |
| Persona | current execution context from graph/subagent/runtime trigger | top-level `agent` user-facing identity |
| Active workflow gates | `WorkflowRunState` + `WorkflowDAG` | rendered context text |
| Todo state | `TaskState.todo_state` | replayed todo context message |

`TaskState` becomes the single persisted runtime state object for task-level data:

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

`TaskRun` is removed. `goal` interaction mode can continue to exist as an interaction mode,
but it reads and writes `TaskState.current_goal` instead of owning a second goal object.

## Context Layers

### Stable System Prefix

Stable system prefix should contain long-lived rules and facts that benefit from cache reuse:

```text
VOIDX_RUNTIME_CONTEXT

## Base System
...

## Persona
...

## Mode
...

## Tool Contract
...

## Workspace Facts
...

## Project Facts
...

## Session Time
...

## Long Summary
...
```

Changes:

- Rename `Role Prompt` to `Persona`.
- Rename `Mode Prompt` to `Mode`.
- Remove `Workflow DAG` from the stable prefix. Workflow definitions are injected in the
  workflow context message.
- Replace `Session Date` with `Session Time`.
- `Session Time` is created once for the session and restored unchanged on resume. It does not
  update per turn.
- Remove `Current DateTime` task section.

### Runtime Context Messages

Workflow context and skill context are separate optional HumanMessages:

```python
class RuntimeContext(BaseModel):
    sections: list[ContextSection]
    task_sections: list[ContextSection]
    workflow_context_content: str = ""
    skill_context_content: str = ""
```

Compile order:

1. Stable `SystemMessage`.
2. `HumanMessage(workflow_context_content)` when non-empty.
3. `HumanMessage(skill_context_content)` when non-empty.
4. Semantic history with latest user overlay.

The marker-based stripping logic remains, but names are no longer misleading:

- `VOIDX_WORKFLOW_CONTEXT` -> Workflow Context.
- skill marker -> Skill Context.

### Per-Turn Task Overlay

The latest user message gets only volatile state needed for this turn:

```text
VOIDX_RUNTIME_CONTEXT

## Runtime State
- Persona: voidx
- Intent: coding
- Goal type: debug
- Goal target: tests/test_auth.py
- Goal expected result: failing login test is diagnosed
- User requested write: false
- Pending approval: none
- Active workflow nodes: debug
- Workflow gates: debug denies write/edit/apply_patch/lsp_format until root cause identified
- Runtime-visible tools: read, grep, ...
- Latest user request: ...

## User Message
...
```

Rules:

- Do not include `Mode`, `Agent`, or `Agent ID`; the graph has one primary identity,
  and persona is the useful LLM-facing concept.
- Do include `Persona`.
- Do include `Intent` and structured `Goal` fields when present.
- Do include active workflow nodes, gates, exits, and visible tools.
- Do include pending approval.
- Do not duplicate model/provider/workspace/sandbox fields if they are already in stable
  Workspace Facts / Tool Contract / permission system.

## Field Removals

Remove these fields from runtime models, graph state, tool context, and persistence:

- `TaskPhase`
- `TaskRun`
- `TaskRunStatus`
- `TaskRun.goal`
- `TaskRun.phase`
- `TaskRun.status`
- `TaskRun.turn_count`
- `TaskRun.workflow_runs`
- `AgentState.goal`
- `AgentState.goal_phase`
- `AgentState.goal_status`
- `AgentState.goal_turn_count`
- `ToolContext.goal`
- `ToolContext.goal_turn_count`
- `MessageRuntimeSnapshot.goal`
- `MessageRuntimeSnapshot.goal_phase`
- `MessageRuntimeSnapshot.goal_status`
- `MessageRuntimeSnapshot.goal_turn_count`

Replace with:

- `TaskState.current_goal: Goal | None`
- `TaskState.workflow_runs: dict[str, WorkflowRunState]`
- optional read-only `ToolContext.goal_type` and `ToolContext.goal_target` derived from
  `TaskState.current_goal` at execution time
- `MessageRuntimeSnapshot.goal_json` only if per-message debugging requires a snapshot;
  otherwise omit goal snapshot entirely and rely on session runtime state.

## Persistence

Schema is intentionally breaking:

```sql
CREATE TABLE session_runtime_state (
    session_id TEXT PRIMARY KEY,
    interaction_mode TEXT NOT NULL DEFAULT 'auto',
    current_intent TEXT NOT NULL DEFAULT 'coding',
    previous_intent TEXT,
    current_goal_json TEXT,
    pending_approval_json TEXT NOT NULL DEFAULT '',
    workflow_runs_json TEXT NOT NULL DEFAULT '{}',
    recent_user_texts_json TEXT NOT NULL DEFAULT '[]',
    todo_state_json TEXT NOT NULL DEFAULT '',
    compaction_summary TEXT NOT NULL DEFAULT '',
    session_time TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Drop `session_task_runs`.

`message_runtime_snapshots` should keep only fields useful for debugging a specific turn:

```sql
CREATE TABLE message_runtime_snapshots (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    interaction_mode TEXT NOT NULL DEFAULT 'auto',
    task_intent TEXT NOT NULL DEFAULT 'coding',
    current_goal_json TEXT,
    pending_approval_json TEXT NOT NULL DEFAULT '',
    workflow_runs_json TEXT NOT NULL DEFAULT '{}',
    available_tool_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
```

No migrations are provided. Old local DBs must be cleared.

## Workflow Selection

`InstructionService.workflow_context_for()` must call selection once:

```python
matches = service.select(
    user_text,
    persona=persona,
    task_intent=task_state.current_intent,
    goal_type=goal_type,
    interaction_mode=interaction_mode,
    runtime_trigger=runtime_trigger,
    exclude_names=exclude_names,
)
runs = service.runs_from_matches(
    matches,
    scope=goal_target,
    turn_count=turn_count,
)
content = service.context(active_names=active_names_from(matches, existing_runs))
```

`WorkflowService.select_runs()` should either be removed or accept precomputed matches.
It must not call `select()` again internally.

## RuntimeContextBuilder API

New constructor shape:

```python
class RuntimeContextBuilder:
    def __init__(
        *,
        config: Config,
        workspace: str,
        base_system_prompt: str,
        persona_prompt: str = "",
        mode_prompt: str = "",
        tool_contract: str = "",
        persona: str,
        interaction_mode: str | InteractionMode,
        project_instructions: Iterable[str] = (),
        workflow_context_content: str = "",
        skill_context_content: str = "",
        task_state: TaskState,
        session_time: str,
        summary: str | None = None,
        current_user_text: str = "",
    ) -> None:
        ...
```

Remove:

- `agent`
- `agent_id`
- `task_intent`
- `intent_resolution_reason`
- `goal`
- `goal_phase`
- `goal_status`
- `goal_turn_count`
- `intent_confidence`
- `intent_source`
- `intent_refined`
- `available_tool_ids` as a top-level builder argument; these can live inside a small
  turn-only `ToolVisibility` object if still needed.

## AgentState

`AgentState` should be graph transport, not task source of truth:

```python
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    workspace: str
    persona: str
    interaction_mode: str
    task_state: NotRequired[TaskState | dict[str, Any]]
    available_tool_ids: NotRequired[list[str]]
    todo_state: NotRequired[TodoRunState | dict[str, Any] | None]
    user_message_id: NotRequired[int]
    tool_results: dict[str, str]
    step_count: int
    max_steps: int
    should_continue: bool
    convergence_forced: NotRequired[bool]
```

`persona` replaces `agent`. The primary turn always uses `persona="voidx"`;
subagents use `explore`/`plan`/`implement`/`review`; runtime calls use `compaction`/`title`.
Workflow runs are read from `task_state.workflow_runs`; they are not carried as a second
top-level graph field.

## ToolContext

`ToolContext` should receive a read-only snapshot derived from `TaskState`, not duplicated
mutable goal fields:

```python
class ToolContext(BaseModel):
    workspace: str
    session_id: str = "default"
    persona: str = "voidx"
    interaction_mode: str = "auto"
    task_intent: str = "coding"
    goal_type: str = ""
    goal_target: str = ""
    pending_approval: dict | None = None
    active_workflow_names: list[str] = Field(default_factory=list)
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)
    ...
```

Tools that need to mutate task state return `ToolStatePatch` with structured fields. They do
not mutate `ToolContext`.

## Open Implementation Notes

- Context frame snapshots should record `persona`, `session_time`, and hashes for stable
  prefix/workflow context separately.
- Compaction must strip runtime overlays and workflow/skill context HumanMessages before
  summarizing semantic conversation history.
- Todo context remains a temporary HumanMessage and must not be persisted as semantic history.
- The model should not see internal numeric `agent_id`; UI can keep ids for event routing.

## Testing

- Runtime context contains one `Session Time` and no `Current DateTime`.
- Runtime context does not include `Agent ID`.
- Runtime context includes `Persona`, `Intent`, and structured goal fields.
- `Workflow DAG` is absent from stable prefix when workflow context is present.
- Workflow selection is called once per context build.
- `TaskState.current_goal` is the only persisted current goal.
- `TaskRun` and `TaskPhase` imports are gone.
- `ToolContext` defaults are `persona="voidx"` and `task_intent="coding"`.
- Old DB schemas are not accepted silently.

## Decisions

| Decision | Alternative | Reason |
| --- | --- | --- |
| `TaskState` owns latest goal | Keep `TaskRun` and `AgentState.goal` | Avoids multiple goal truths |
| Delete phase | Keep `TaskPhase` | `Goal.type` plus workflow run status expresses progress more clearly |
| Fixed session time only | Per-turn datetime | Stable context should not churn and relative date handling can use session time |
| Split workflow and skill context fields | Keep `skill_context_content` for both | Avoids misleading names and marker-dependent mental model |
| One workflow selection call | Select once for context and again for runs | Prevents mismatched active summaries and run state |
| No compatibility logic | Migrate or alias old fields | User explicitly wants a breaking cleanup |
