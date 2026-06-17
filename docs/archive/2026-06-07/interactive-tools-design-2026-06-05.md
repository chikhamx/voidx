# Interactive Tools Design: clarify & plan_checkpoint

> **Status: Done** — 归档自 `docs/specs/2026-06-05-interactive-tools-design.md`，实现已于 2026-06-05 完成。

Date: 2026-06-05

## Goal

Add two structured interactive tools — `clarify` and `plan_checkpoint` — that let the LLM explicitly request user input during task execution. These tools replace ad-hoc natural-language questioning with typed, state-updating interactions that integrate with the existing intent/skill/runtime state pipeline.

## Current State

Key files:

- `src/voidx/tools/on_intent.py` — existing `OnIntentTool` with `IntentResolver` callback pattern. Already defines `OnIntentStatePatch` for runtime state updates.
- `src/voidx/agent/task_state.py` — `TaskState`, `TaskRun`, `IntentResolution`, `PendingApproval`, `resolve_turn_intent()`. Manages intent transitions, approval flow, and pending implementation approval as structured state.
- `src/voidx/agent/runtime_context.py` — `RuntimeContextBuilder` assembles the system prompt from task state, intent, skills, etc.
- `src/voidx/agent/graph/tool_execution.py` — `GraphToolExecutionMixin._execute_tools()` runs tools in parallel, returns `ToolMessage`s. No mechanism for mid-execution user interaction.
- `src/voidx/agent/graph/permissions.py` — `_ask_tool_permission()` uses `self._app.ask_choice()` to get user input for permission decisions. This is the only existing pattern for tool→user interaction.
- `src/voidx/ui/tui/app.py` — `ask_choice()` and `ask_text()` provide async user-input primitives with timeout support.
- `src/voidx/tools/base.py` — `BaseTool`, `ToolContext`, `ToolResult`. No support for interactive (blocking) tools.

Observed gaps:

1. **No structured clarification** — When the LLM is unsure about intent, scope, or requirements, it can only ask questions in natural language. The user's answer is unstructured text that the LLM must re-interpret, often incorrectly.
2. **No structured plan approval** — The plan→implement transition can preserve `PendingApproval`, but the approval itself still relies on keyword detection (`_APPROVAL_ONLY_HINTS`) when no explicit tool is used. The LLM outputs a plan, the user says "好", but there's no structured checkpoint record of what was approved or modified.
3. **No interactive tool support** — `BaseTool.execute()` is fire-and-forget. Tools cannot pause execution to ask the user a question and resume. The only existing pattern is in `permissions.py`, which is tightly coupled to the permission flow.
4. **State updates are implicit** — When the LLM refines intent through conversation, the runtime state (`TaskState`, `TaskRun`) is only updated at turn boundaries via `resolve_turn_intent()`. Mid-turn refinements from tool results don't flow back.

## External References

- **Claude Code** `ask_followup_question` tool: LLM calls a tool to ask the user a question with suggested answers. Blocks until the user responds.
- **Cursor** agent: uses a "clarification" step before implementation, presenting options to the user.
- **Aider** `/ask` command: user can ask questions mid-session, but the agent doesn't proactively ask structured questions.

References:

- https://docs.anthropic.com/en/docs/build-with-claude/tool-use

## Design

### 1. Interactive Tool Infrastructure

Both `clarify` and `plan_checkpoint` need to pause tool execution, get user input, and resume. Rather than duplicating the permission-style `ask_choice` pattern, we introduce a lightweight callback on `ToolContext`.

#### 1.1 ToolContext Extension

```python
# In src/voidx/tools/base.py

class UserInteraction(BaseModel):
    """A request for user input from a tool."""
    prompt: str
    options: list[tuple[str, str, str]] = Field(default_factory=list)
        # (label, value, description) — empty means free-text input
    blocking: bool = True
    timeout: float | None = None

class UserResponse(BaseModel):
    """The user's response to an interaction."""
    answer: str
    cancelled: bool = False
    state_patch: dict = Field(default_factory=dict)
```

The `ToolContext` gets an `interact` callback:

```python
class ToolContext:
    # ... existing fields ...
    interact: Callable[[UserInteraction], Awaitable[UserResponse]]
```

This callback is injected by the graph at tool execution time, routing through the existing `ask_choice`/`ask_text` UI primitives.

#### 1.2 ToolResult Extension

`ToolResult` gains an optional `state_patch` field:

```python
class ToolResult:
    output: str
    error: str | None = None
    metadata: dict = Field(default_factory=dict)
    state_patch: ToolStatePatch | None = None
```

When a tool returns a `state_patch`, the graph applies it to the current runtime state before the next LLM call.

### 2. clarify Tool

#### 2.1 Purpose

Let the LLM ask a structured question with optional suggested answers. The user's response updates the runtime state (intent, confidence, etc.).

#### 2.2 Tool Definition

```python
class ClarifyInput(BaseModel):
    question: str = Field(description="The specific question to ask the user.")
    options: list[ClarifyOption] = Field(
        default_factory=list,
        description="Suggested answers. Leave empty for open-ended questions.",
    )
    blocking: bool = Field(
        default=True,
        description="Whether the agent should wait for the answer.",
    )
    context: str = Field(
        default="",
        description="Why this question matters or what decision depends on the answer.",
    )

class ClarifyOption(BaseModel):
    label: str = Field(description="Short display label.")
    value: str = Field(description="Machine-readable answer value.")
    description: str = Field(default="", description="One-line explanation of this option.")
```

#### 2.3 Behavior

1. Tool calls `ctx.interact(UserInteraction(...))` with the question and options.
2. User responds (or cancels / times out).
3. Tool returns `ToolResult` with:
   - `output`: the user's answer text
   - `metadata.clarify_answer`: the selected value
   - `metadata.clarify_cancelled`: bool
   - `state_patch`: `ToolStatePatch` updating intent/confidence if the answer resolves ambiguity

#### 2.4 State Patch Rules

| User Response | State Patch |
|---------------|-------------|
| Selected "implement" option | `task_intent=IMPLEMENT, intent_source="clarify"` |
| Selected "design" option | `task_intent=DESIGN, intent_source="clarify"` |
| Free-text answer | `intent_source="clarify"` (intent unchanged unless keyword match) |
| Cancelled / timeout | No state patch, tool returns blocked result |

### 3. plan_checkpoint Tool

#### 3.1 Purpose

Present a concrete implementation plan for user approval before changing files, running write-capable commands, or delegating implementation. The user can approve, modify scope, or reject.

#### 3.2 Tool Definition

```python
class PlanCheckpointInput(BaseModel):
    plan_summary: str = Field(description="Concise implementation plan summary.")
    steps: list[PlanStep] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    alternatives: list[PlanAlternative] = Field(default_factory=list)
    estimated_steps: int = Field(default=0)

class PlanStep(BaseModel):
    description: str
    files: list[str] = Field(default_factory=list)
    tool: str = Field(default="")

class PlanAlternative(BaseModel):
    name: str
    description: str
    trade_off: str = Field(default="")
```

#### 3.3 Behavior

1. Tool calls `ctx.interact(UserInteraction(...))` with the plan details.
2. User can: approve, modify scope, or reject.
3. Tool returns `ToolResult` with:
   - `output`: approval status + any modifications
   - `metadata.checkpoint_status`: "approved" | "rejected" | "modified"
   - `metadata.modified_scope`: user's scope modification (if any)
   - `state_patch`: `ToolStatePatch` clearing `pending_approval` or updating scope

#### 3.4 State Patch Rules

| User Response | State Patch |
|---------------|-------------|
| Approved | `pending_approval=None, task_intent=IMPLEMENT, intent_resolution_reason="plan_checkpoint: approved"` |
| Rejected | `task_intent=DESIGN, intent_resolution_reason="plan_checkpoint: rejected"` |
| Modified scope | `pending_approval.scope=modified_scope, intent_resolution_reason="plan_checkpoint: modified"` |
| Cancelled | No state patch, tool returns blocked result |

### 4. Graph Integration

#### 4.1 Tool Registration

Both tools are registered in the orchestrator agent's tool list:

```python
# In src/voidx/agent/agents.py
ORCHESTRATOR_TOOLS = [
    # ... existing tools ...
    "clarify",
    "plan_checkpoint",
]
```

#### 4.2 Interaction Callback Wiring

The `interact` callback is wired in `_execute_tools()`:

```python
# In src/voidx/agent/graph/tool_execution.py

async def _execute_tools(self, tool_calls, messages):
    async def interact(interaction: UserInteraction) -> UserResponse:
        if interaction.options:
            choices = [(opt.label, opt.value, opt.description) for opt in interaction.options]
            answer = await self._app.ask_choice(interaction.prompt, choices)
            return UserResponse(answer=answer, cancelled=False)
        else:
            answer = await self._app.ask_text(interaction.prompt)
            return UserResponse(answer=answer, cancelled=False)

    ctx = ToolContext(interact=interact, ...)
    # ... execute tools with ctx ...
```

#### 4.3 State Patch Application

After each tool execution, if the result has a `state_patch`, apply it:

```python
if result.state_patch:
    patch = result.state_patch
    if patch.task_intent is not None:
        state["task_intent"] = patch.task_intent.value
    if patch.intent_source:
        state["intent_source"] = patch.intent_source
    # ... apply other fields ...
```

### 5. Permission Model

| Tool | Default Permission | Rationale |
|------|-------------------|-----------|
| `clarify` | Allow | Read-only interaction, no side effects |
| `plan_checkpoint` | Allow | Read-only interaction, no side effects |

Both tools are informational — they don't modify files or run commands. The state patches they produce are runtime-internal and don't need user permission.

### 6. Timeout Handling

Both tools support a default timeout (configurable):

- `clarify`: 120 seconds default
- `plan_checkpoint`: 300 seconds default (plans need more review time)

On timeout:
1. Return `UserResponse(cancelled=True)`
2. Tool returns `ToolResult(output="User did not respond in time", error=None)`
3. No state patch applied
4. LLM decides next action (usually re-ask or proceed with best guess)

### 7. Testing

| Test | Description |
|------|-------------|
| `test_clarify_with_options` | clarify with suggested answers returns selected value |
| `test_clarify_free_text` | clarify without options returns free-text answer |
| `test_clarify_cancelled` | clarify on cancel returns blocked result |
| `test_clarify_state_patch` | clarify updates intent when user selects implement |
| `test_plan_checkpoint_approved` | plan_checkpoint on approve clears pending_approval |
| `test_plan_checkpoint_rejected` | plan_checkpoint on reject sets intent to DESIGN |
| `test_plan_checkpoint_modified` | plan_checkpoint on modify updates scope |
| `test_interact_callback_wired` | ToolContext.interact is wired in _execute_tools |
| `test_state_patch_applied` | state_patch from tool result is applied to graph state |
