# Interactive Tools Design: clarify & plan_checkpoint

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
    """The user's response to an interaction request."""
    value: str
    cancelled: bool = False

class ToolContext(BaseModel):
    workspace: str
    session_id: str = "default"
    agent: str = "build"
    interaction_mode: str = "auto"
    task_intent: str = "chat"
    pending_approval: dict | None = None
    goal: str = ""
    goal_turn_count: int = 0
    file_mtimes: dict[str, float] = Field(default_factory=dict)
    mcp_manager: Any | None = None
    lsp_manager: Any | None = None
    sandbox_mode: str = "workspace-write"
    sandbox_extra_paths: list[str] = Field(default_factory=list)
    # NEW: callback for interactive tools
    interact: Any | None = Field(default=None, exclude=True)
        # Type: Callable[[UserInteraction], Awaitable[UserResponse]] | None

    model_config = {"arbitrary_types_allowed": True}
```

The `interact` callback is injected by `GraphToolExecutionMixin` when constructing `ToolContext`, using the same `self._app.ask_choice()` / `self._app.ask_text()` primitives that permissions already use.

#### 1.2 GraphToolExecutionMixin Wiring

In `_execute_tools()`, when building the `ToolContext`:

```python
ctx = ToolContext(
    workspace=...,
    file_mtimes=...,
    # ... existing fields ...
    interact=self._make_interact_callback(),
)
```

```python
def _make_interact_callback(self):
    async def interact(request: UserInteraction) -> UserResponse:
        if not self._app:
            # Headless mode: return empty response, tool should handle gracefully
            return UserResponse(value="", cancelled=True)

        if request.options:
            choices = [
                (label, value, desc)
                for label, value, desc in request.options
            ]
            result = await self._app.ask_choice(
                request.prompt,
                choices,
                timeout=request.timeout,
            )
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result)
        else:
            result = await self._app.ask_text(
                request.prompt,
                timeout=request.timeout,
            )
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result)

    return interact
```

**Important**: `clarify`, `plan_checkpoint`, and `on_intent` are **barrier tools**, not merely interactive tools. If any barrier tool appears in a batch, it must run before all other tool calls from that same LLM response. All non-barrier calls in that batch are returned as deferred `ToolMessage`s and must be re-issued after the runtime state is updated.

This matters because `plan_checkpoint + edit` in one assistant message must not execute `edit` before the user approves the plan, and `on_intent + write` must not execute `write` before the refined intent updates available tools.

```python
# In _execute_tools, after authorization:
barrier_calls = [tc for tc in approved if self._is_barrier_tool(tc)]

if barrier_calls:
    deferred_calls = [tc for tc in approved if tc not in barrier_calls]
    # Run barrier calls sequentially. These calls may update runtime state.
    executed = []
    for tc in barrier_calls:
        executed.append(await execute_one(tc))
    return {
        "messages": [item.message for item in executed] + _deferred_messages(deferred_calls),
        **_state_update_from_executed_tools(executed),
    }

# No barrier: preserve existing parallel execution.
executed = await asyncio.gather(*[execute_one(tc) for tc in approved])
return {"messages": [item.message for item in executed], **_state_update_from_executed_tools(executed)}
```

#### 1.3 State Patch Protocol

`on_intent`, `clarify`, and `plan_checkpoint` can update runtime state. We reuse the `OnIntentStatePatch` pattern from `on_intent.py`, but generalize it so every stateful tool can return the same patch shape:

```python
# In src/voidx/agent/task_state.py — add to existing

class ToolStatePatch(BaseModel):
    """Structured state updates that a tool can request after execution."""
    task_intent: TaskIntent | None = None
    intent_resolution_reason: str | None = None
    goal: str | None = None
    goal_phase: str | None = None
    goal_status: str | None = None
    pending_approval: PendingApproval | None = None
    available_tool_ids: list[str] | None = None
    intent_confidence: float | None = None
    intent_source: str | None = None
    intent_refined: bool | None = None
    skill_runs: list[SkillRunState] = Field(default_factory=list)
```

The `ToolResult.metadata` already carries arbitrary data. Stateful tools include a `"state_patch"` key in metadata. `_state_update_from_executed_tools()` applies these patches to the LangGraph state after execution. Persistence remains centralized in the normal turn-finalization path, where `TaskState`, `TaskRun`, and `MessageRuntimeSnapshot` are saved.

```python
# In _execute_tools, after all tools complete:
state_update = _state_update_from_executed_tools(executed)
return {"messages": [item.message for item in executed], **state_update}
```

### 2. `clarify` Tool

#### 2.1 Purpose

Let the LLM ask the user a structured question when intent, scope, or requirements are ambiguous. This is the interactive counterpart to `on_intent` — when `on_intent` returns low confidence, `clarify` resolves the ambiguity.

#### 2.2 Input/Output Models

```python
# In src/voidx/tools/clarify.py

class ClarifyInput(BaseModel):
    question: str = Field(
        description="The specific question to ask the user. Be precise — one question at a time."
    )
    options: list[ClarifyOption] = Field(
        default_factory=list,
        description="Suggested answers. Provide 2-5 options when the question has known alternatives. "
                    "Leave empty for open-ended questions.",
    )
    context: str = Field(
        default="",
        description="Why this question matters — what decision depends on the answer.",
    )
    blocking: bool = Field(
        default=True,
        description="If true, the agent will wait for the user's response before continuing. "
                    "If false, the user can skip the question.",
    )

class ClarifyOption(BaseModel):
    label: str = Field(description="Short display label, e.g. 'Refactor'")
    value: str = Field(description="Machine-readable value, e.g. 'refactor'")
    description: str = Field(default="", description="One-line explanation of this option")

class ClarifyResult(BaseModel):
    question: str
    answer: str
    selected_option: str | None = None
    cancelled: bool = False
    state_patch: ToolStatePatch | None = None
```

#### 2.3 Tool Implementation

```python
class ClarifyTool(BaseTool):
    id = "clarify"
    description = (
        "Ask the user a clarifying question with optional structured choices. "
        "Use when the task intent, scope, or requirements are ambiguous and you "
        "need explicit user input before proceeding. Prefer this over guessing — "
        "one structured question is better than five assumptions. "
        "The user's answer updates runtime state (intent, goal, scope) automatically."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ClarifyInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = ClarifyInput.model_validate(args)

        if ctx.interact is None:
            return ToolResult(
                output="Clarification not available in this runtime. "
                       "Proceed with your best judgment and note the ambiguity.",
                metadata={"clarify_cancelled": True},
            )

        options = [
            (opt.label, opt.value, opt.description)
            for opt in inp.options
        ] if inp.options else []

        interaction = UserInteraction(
            prompt=inp.question,
            options=options,
            blocking=inp.blocking,
        )

        response = await ctx.interact(interaction)

        if response.cancelled:
            return ToolResult(
                title="clarify: skipped",
                output=f"User skipped: {inp.question}",
                metadata={"clarify_cancelled": True},
            )

        # Build state patch based on the answer
        patch = self._infer_state_patch(inp, response)
        result = ClarifyResult(
            question=inp.question,
            answer=response.value,
            selected_option=response.value if inp.options else None,
            cancelled=False,
            state_patch=patch,
        )

        return ToolResult(
            title=f"clarify: {response.value}",
            output=_format_clarify_result(result),
            metadata={
                "clarify_answer": response.value,
                "state_patch": patch.model_dump(mode="json") if patch else None,
            },
        )

    def _infer_state_patch(self, inp: ClarifyInput, response: UserResponse) -> ToolStatePatch | None:
        """Infer runtime state updates from the clarification answer.

        This is intentionally conservative — we only update state when
        the answer maps clearly to an intent or scope change.
        """
        answer = response.value.strip().lower()

        # Map option values to intents if they match known intent names
        intent_map = {
            "inspect": TaskIntent.INSPECT,
            "design": TaskIntent.DESIGN,
            "implement": TaskIntent.IMPLEMENT,
            "review": TaskIntent.REVIEW,
            "debug": TaskIntent.DEBUG,
            "chat": TaskIntent.CHAT,
        }

        if answer in intent_map:
            return ToolStatePatch(
                task_intent=intent_map[answer],
                intent_resolution_reason=f"clarify: user selected '{answer}'",
            )

        # If the question was about scope, update goal
        if inp.context and "scope" in inp.context.lower():
            return ToolStatePatch(
                goal=answer,
                intent_resolution_reason="clarify: scope refined",
            )

        return None
```

#### 2.4 When LLM Should Use `clarify`

The tool description guides the LLM, but we also add a hint in the runtime context when `on_intent` returns low confidence:

```python
# In RuntimeContextBuilder._current_task_state(), add:
if self.intent_confidence is not None and self.intent_confidence < 0.6:
    lines.append("- Suggestion: use the clarify tool to resolve intent ambiguity before proceeding.")
```

#### 2.5 Interaction with `on_intent`

The two tools form a pipeline:

```
on_intent (confidence < 0.6)
    → LLM sees "Suggestion: use clarify"
    → LLM calls clarify with structured question
    → User answers
    → clarify returns answer + state_patch
    → State patch updates task_intent, goal, etc.
    → LLM proceeds with clarified intent
```

`clarify` can also be used independently — the LLM can call it at any point when it realizes it needs more information, not just after `on_intent`.

### 3. `plan_checkpoint` Tool

#### 3.1 Purpose

Replace the informal "here's my plan, let me know if you want me to proceed" pattern with a structured approval gate. The LLM presents a plan with affected files, risks, and alternatives; the user explicitly approves, modifies, or rejects it. Upon approval, the runtime transitions to `implement`, clears `pending_approval`, and records the approved scope as the current goal/scope.

#### 3.2 Input/Output Models

```python
# In src/voidx/tools/plan_checkpoint.py

class PlanCheckpointInput(BaseModel):
    plan_summary: str = Field(
        description="Concise summary of the implementation plan (2-5 sentences)."
    )
    steps: list[PlanStep] = Field(
        default_factory=list,
        description="Ordered list of implementation steps.",
    )
    affected_files: list[str] = Field(
        default_factory=list,
        description="Files that will be created, modified, or deleted.",
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Potential risks or trade-offs the user should be aware of.",
    )
    alternatives: list[PlanAlternative] = Field(
        default_factory=list,
        description="Alternative approaches considered but not chosen, with reasons.",
    )
    estimated_steps: int = Field(
        default=0,
        description="Rough estimate of tool-call steps needed.",
    )

class PlanStep(BaseModel):
    description: str = Field(description="What this step does")
    files: list[str] = Field(default_factory=list, description="Files touched in this step")
    tool: str = Field(default="", description="Primary tool used, e.g. 'edit', 'write', 'bash'")

class PlanAlternative(BaseModel):
    name: str = Field(description="Short name for this alternative")
    description: str = Field(description="What this approach would do differently")
    trade_off: str = Field(default="", description="Why it was not chosen")

class PlanCheckpointResult(BaseModel):
    plan_summary: str
    decision: str  # "approved", "modified", "rejected"
    user_feedback: str = ""
    modified_scope: str = ""
    state_patch: ToolStatePatch | None = None
```

#### 3.3 Tool Implementation

```python
class PlanCheckpointTool(BaseTool):
    id = "plan_checkpoint"
    description = (
        "Present an implementation plan for user approval before making changes. "
        "Use this when the task involves modifying files, running commands, or "
        "any action that changes the workspace. The user can approve, modify the "
        "scope, or reject the plan. Upon approval, the runtime transitions from "
        "design to implement phase and clears pending approval. "
        "Do NOT skip this for non-trivial changes — it ensures the user and agent "
        "agree on what will be done before irreversible actions are taken."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(PlanCheckpointInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = PlanCheckpointInput.model_validate(args)

        if ctx.interact is None:
            return self._interaction_unavailable(inp)

        # Build the approval prompt
        prompt = self._build_prompt(inp)
        choices = [
            ("Approve", "approved", "Proceed with this plan"),
            ("Modify scope", "modified", "Approve with changes to the scope"),
            ("Reject", "rejected", "Do not proceed"),
        ]

        interaction = UserInteraction(
            prompt=prompt,
            options=choices,
            blocking=True,
        )

        response = await ctx.interact(interaction)

        if response.cancelled or response.value == "rejected":
            return self._rejected(inp)

        if response.value == "modified":
            # Ask for the modified scope
            scope_interaction = UserInteraction(
                prompt="Describe the modified scope:",
                options=[],
                blocking=True,
            )
            scope_response = await ctx.interact(scope_interaction)
            modified_scope = scope_response.value if not scope_response.cancelled else ""
            return self._modified(inp, modified_scope)

        return self._approved(inp)

    def _build_prompt(self, inp: PlanCheckpointInput) -> str:
        parts = [f"📋 Plan: {inp.plan_summary}"]
        if inp.steps:
            parts.append("\nSteps:")
            for i, step in enumerate(inp.steps, 1):
                file_info = f" ({', '.join(step.files)})" if step.files else ""
                parts.append(f"  {i}. {step.description}{file_info}")
        if inp.affected_files:
            parts.append(f"\nAffected files: {', '.join(inp.affected_files)}")
        if inp.risks:
            parts.append("\nRisks:")
            for risk in inp.risks:
                parts.append(f"  ⚠ {risk}")
        if inp.alternatives:
            parts.append("\nAlternatives considered:")
            for alt in inp.alternatives:
                parts.append(f"  • {alt.name}: {alt.description}")
                if alt.trade_off:
                    parts.append(f"    Not chosen because: {alt.trade_off}")
        return "\n".join(parts)

    def _approved(self, inp: PlanCheckpointInput) -> ToolResult:
        scope = inp.plan_summary
        patch = ToolStatePatch(
            task_intent=TaskIntent.IMPLEMENT,
            intent_resolution_reason="plan_checkpoint: user approved",
            goal=scope,
            goal_phase="implement",
            pending_approval=None,
        )
        result = PlanCheckpointResult(
            plan_summary=inp.plan_summary,
            decision="approved",
            state_patch=patch,
        )
        return ToolResult(
            title="plan: approved ✓",
            output=_format_checkpoint_result(result),
            metadata={
                "plan_decision": "approved",
                "state_patch": patch.model_dump(mode="json"),
            },
        )

    def _modified(self, inp: PlanCheckpointInput, modified_scope: str) -> ToolResult:
        patch = ToolStatePatch(
            task_intent=TaskIntent.IMPLEMENT,
            intent_resolution_reason="plan_checkpoint: user approved with modifications",
            goal=modified_scope,
            goal_phase="implement",
            pending_approval=None,
        )
        result = PlanCheckpointResult(
            plan_summary=inp.plan_summary,
            decision="modified",
            user_feedback=modified_scope,
            modified_scope=modified_scope,
            state_patch=patch,
        )
        return ToolResult(
            title="plan: modified ✓",
            output=_format_checkpoint_result(result),
            metadata={
                "plan_decision": "modified",
                "state_patch": patch.model_dump(mode="json"),
            },
        )

    def _rejected(self, inp: PlanCheckpointInput) -> ToolResult:
        patch = ToolStatePatch(
            task_intent=TaskIntent.DESIGN,
            intent_resolution_reason="plan_checkpoint: user rejected",
            goal_phase="design",
            pending_approval=None,
        )
        result = PlanCheckpointResult(
            plan_summary=inp.plan_summary,
            decision="rejected",
            state_patch=patch,
        )
        return ToolResult(
            title="plan: rejected ✗",
            output=_format_checkpoint_result(result),
            metadata={
                "plan_decision": "rejected",
                "state_patch": patch.model_dump(mode="json"),
            },
        )

    def _interaction_unavailable(self, inp: PlanCheckpointInput) -> ToolResult:
        """Headless mode without an interaction channel cannot approve plans."""
        return ToolResult(
            title="plan: approval unavailable",
            output=(
                "Plan approval is not available in this runtime. "
                f"Do not implement without explicit user approval: {inp.plan_summary}"
            ),
            metadata={"plan_decision": "interaction_unavailable", "blocked": True},
        )
```

#### 3.4 Interaction with Existing Approval Flow

Currently, `resolve_turn_intent()` creates `PendingApproval` when a turn resolves to `DESIGN`, and approval-only user replies can confirm that pending approval as a fallback. `plan_checkpoint` replaces this implicit flow with an explicit one:

| Scenario | Before | After |
|----------|--------|-------|
| LLM outputs plan, user says "好" | Keyword match → `IMPLEMENT` | `plan_checkpoint` → structured approval → `IMPLEMENT` |
| LLM outputs plan, user says "别动 X 文件" | LLM must re-read and adjust | `plan_checkpoint` with "modified" → current goal/scope excludes X |
| LLM outputs plan, user says "不行" | Falls to `AMBIGUOUS` | `plan_checkpoint` with "rejected" → stays in `DESIGN` |

The existing `_APPROVAL_ONLY_HINTS` mechanism remains as a **fallback** for when the LLM doesn't use `plan_checkpoint` (e.g., in simple cases where the LLM just proceeds and the user approves via natural language).

#### 3.5 When LLM Should Use `plan_checkpoint`

Add to the runtime context when the task is in design phase:

```python
# In RuntimeContextBuilder._current_task_state(), add:
if self.task_intent == TaskIntent.DESIGN and self.pending_approval:
    lines.append("- Suggestion: use plan_checkpoint to get explicit approval before implementing.")
```

### 4. State Patch Application

`on_intent`, `clarify`, and `plan_checkpoint` produce `ToolStatePatch` via `ToolResult.metadata["state_patch"]`. We need a single place to apply these patches to the graph state.

#### 4.1 In `_execute_tools`

After all executed tool results are collected, scan for state patches. Barrier tool batches apply their patches immediately and defer non-barrier calls to the next LLM step.

```python
patches_to_apply: list[dict] = []
for item in executed:
    patch = getattr(item.result, "metadata", {}).get("state_patch")
    if patch:
        patches_to_apply.append(patch)

# Apply the merged patch to graph state
if patches_to_apply:
    merged = self._merge_state_patches(patches_to_apply)
    state_updates = self._state_patch_to_updates(merged)
    # These will be merged into the graph state on the next iteration
    return {"messages": [item.message for item in executed], **state_updates}
```

#### 4.2 Merge Logic

Later patches override earlier ones. Importantly, `None` is meaningful for nullable fields such as `pending_approval`: it means "clear this state", not "ignore this key". The merge must therefore use the explicit fields sent by the Pydantic model, not `value is not None`.

```python
def _merge_state_patches(self, patches: list[ToolStatePatch]) -> dict:
    merged = {}
    for patch in patches:
        data = patch.model_dump(mode="json")
        for key in patch.model_fields_set:
            merged[key] = data.get(key)
    return merged

def _state_patch_to_updates(self, merged: dict) -> dict:
    updates = {}
    if "task_intent" in merged:
        updates["task_intent"] = merged["task_intent"]
    if "intent_resolution_reason" in merged:
        updates["intent_resolution_reason"] = merged["intent_resolution_reason"]
    if "goal" in merged:
        updates["goal"] = merged["goal"]
    if "goal_phase" in merged:
        updates["goal_phase"] = merged["goal_phase"]
    if "goal_status" in merged:
        updates["goal_status"] = merged["goal_status"]
    if "pending_approval" in merged:
        updates["pending_approval"] = merged["pending_approval"]
    if "available_tool_ids" in merged:
        updates["available_tool_ids"] = merged["available_tool_ids"]
    if "skill_runs" in merged:
        updates["skill_runs"] = merged["skill_runs"]
    if "intent_confidence" in merged:
        updates["intent_confidence"] = merged["intent_confidence"]
    if "intent_source" in merged:
        updates["intent_source"] = merged["intent_source"]
    if "intent_refined" in merged:
        updates["intent_refined"] = merged["intent_refined"]
    return updates
```

### 5. Tool Registration

Both tools are registered in `ToolRegistry._register_builtins()`:

```python
# In src/voidx/tools/registry.py

from voidx.tools.clarify import ClarifyTool
from voidx.tools.plan_checkpoint import PlanCheckpointTool

# In _register_builtins:
for cls in [
    FileReadTool, FileWriteTool, FileEditTool,
    RepoMapTool,
    GlobTool, GrepTool, BashTool,
    LspDiagnosticsTool, LspSymbolsTool,
    LspDefinitionTool, LspReferencesTool, LspFormatTool,
    LoadDocTemplateTool,
    ClarifyTool,          # NEW
    PlanCheckpointTool,   # NEW
]:
    instance = cls()
    self.register(instance.id, instance, instance.description, instance.parameters_schema())
```

### 6. Agent Tool Allowlists

User-interactive tools are available to the **orchestrator** only. Child agents do not have a direct user interaction channel and should not block the parent run waiting for input.

The `plan` role is still part of the workflow: the orchestrator can delegate to `agent(plan)` to produce `plan_summary`, steps, affected files, risks, and alternatives. The orchestrator then calls `plan_checkpoint` with that structured plan. If a child plan agent needs clarification, it returns the ambiguity in its result and the orchestrator calls `clarify`.

Future extension: if `plan` can run as the top-level active agent rather than a child agent, it may receive `plan_checkpoint`. That requires top-level/child-agent tool allowlists to be distinguishable.

```python
# In src/voidx/agent/agents.py — update agent definitions

ORCHESTRATOR_TOOLS = [
    "read", "write", "edit", "glob", "grep", "bash",
    "repomap", "lsp_diagnostics", "lsp_symbols", "lsp_definition",
    "lsp_references", "lsp_format", "todo", "task_status",
    "webfetch", "websearch", "agent", "on_intent",
    "clarify", "plan_checkpoint",  # NEW — orchestrator only
]

PLAN_TOOLS = [
    "read", "glob", "grep", "repomap",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references",
    "webfetch", "websearch",
]

EXPLORE_TOOLS = [
    "read", "glob", "grep", "repomap",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references",
    "webfetch", "websearch",
]

IMPLEMENT_TOOLS = [
    "read", "write", "edit", "glob", "grep", "bash",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition",
    "lsp_references", "lsp_format", "todo",
]

REVIEW_TOOLS = [
    "read", "glob", "grep", "bash",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition", "lsp_references",
]
```

### 7. Headless / Web UI Considerations

- **Headless mode** (`--web --web-headless`): `ctx.interact` is `None`. `clarify` returns a "not available" message. `plan_checkpoint` returns `interaction_unavailable` with `blocked=True` and does not transition to implementation. The LLM should ask for explicit user approval through a normal user turn before changing files.
- **Web UI** (`--web`): The web UI already has a WebSocket-based event system. We add two new event types:
  - `ClarificationRequested` — sent to the frontend with question + options
  - `PlanApprovalRequested` — sent to the frontend with plan details + approve/modify/reject choices
  The frontend renders these as interactive cards. User responses are sent back via WebSocket.

### 8. File Structure

```
src/voidx/tools/
├── clarify.py              # NEW — ClarifyTool
├── plan_checkpoint.py      # NEW — PlanCheckpointTool
├── on_intent.py            # EXISTING — no changes needed
├── base.py                 # MODIFIED — add UserInteraction, UserResponse, interact on ToolContext
├── registry.py             # MODIFIED — register new tools
└── ...

src/voidx/agent/
├── task_state.py           # MODIFIED — add ToolStatePatch
├── graph/
│   ├── tool_execution.py   # MODIFIED — inject interact callback, enforce barrier tools, apply state patches
│   └── ...
├── runtime_context.py      # MODIFIED — add clarify/plan_checkpoint hints in task state rendering
├── agents.py               # MODIFIED — update tool allowlists
└── ...
```

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Interactive tools block the execution loop | Treat `clarify`, `plan_checkpoint`, and `on_intent` as barrier tools; defer same-batch non-barrier calls. Add timeout (default 120s) to prevent indefinite blocking. |
| LLM over-uses `clarify` for simple questions | Tool description emphasizes "one structured question is better than five assumptions" — use for genuine ambiguity, not laziness. Runtime can rate-limit (max 3 clarify calls per turn). |
| `plan_checkpoint` adds latency for simple changes | Tool description says "Do NOT skip this for non-trivial changes" — implies trivial changes can skip it. The existing keyword-based approval flow remains as a fast path. |
| State patches from multiple tools conflict | Merge logic: later patches override earlier ones. `plan_checkpoint` is authoritative for approval state. |
| Headless mode can't interact | Graceful degradation: `clarify` returns "not available"; `plan_checkpoint` blocks implementation until explicit approval arrives through a later user turn. |
| Web UI doesn't support interactive tools yet | Phase 1: TUI only. Phase 2: add WebSocket events for web UI. |

## Open Questions

1. ~~**Should `clarify` be allowed in child agents?**~~ — **Decided: No.** `clarify` is orchestrator-only. Child agents have limited context and no direct user interaction channel. If a child agent needs clarification, it should return its ambiguity in the result and let the orchestrator call `clarify` on its behalf.
2. ~~**Should `plan_checkpoint` auto-approve in `auto` mode?**~~ — **Decided: No.** `plan_checkpoint` always asks the user when an interaction channel exists. If no interaction channel exists, it blocks instead of auto-approving. This ensures the user always has explicit control over what gets implemented.
3. ~~**Rate limiting**~~ — **Decided: No limit.** `clarify` calls per turn are not capped. The LLM is trusted to use `clarify` judiciously — over-use will be self-correcting because each call adds latency and consumes the step budget.
4. ~~**State patch persistence**~~ — **Decided: Persist through existing runtime snapshots.** Tool patches update graph state mid-turn. At turn finalization, `TaskState`, `TaskRun`, and `MessageRuntimeSnapshot` persist the resulting structured state through the existing memory layer.

## Implementation Order

1. `ToolContext` extension (`interact` callback, `UserInteraction`, `UserResponse`)
2. `ToolStatePatch` model in `task_state.py`
3. `ClarifyTool` implementation
4. `PlanCheckpointTool` implementation
5. `GraphToolExecutionMixin` changes (inject callback, enforce barrier tools, apply state patches)
6. `RuntimeContextBuilder` hints (clarify suggestion, plan_checkpoint suggestion)
7. Agent tool allowlist updates (orchestrator owns interactive tools; plan child agent returns plan artifacts)
8. Tool registration
9. Tests
