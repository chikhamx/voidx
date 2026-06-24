# voidx plan TUI Checkpoint Node Design

Date: 2026-06-24

> **Status: Pending**

## Goal

Render `checkpoint` tool interactions as first-class TUI tree nodes named
`voidx plan`, while preserving the existing keyboard choice overlay. The user's
selection should be recorded visibly as the response to that plan checkpoint,
instead of disappearing into the bottom prompt or a hidden tool result.

This design is TUI-first. It does not require the web or desktop frontend to
render checkpoint interactions in V1, although the event shape should remain
compatible with future gateway support.

## Current State

Relevant files:

- `src/voidx/tools/plan_checkpoint.py` defines the `checkpoint` tool, builds the
  plan prompt, calls `ctx.interact()`, and returns a structured `ToolResult`
  with `metadata.plan_decision` and a runtime `state_patch`.
- `src/voidx/agent/graph/tool_executor/helpers.py` wires `UserInteraction` to
  `app.ask_choice()` / `app.ask_text()`. It appends an `Other...` option for
  tuple-choice interactions.
- `src/voidx/ui/tui/choice_mixin.py` and `src/voidx/ui/tui/overlays.py` render
  the active choice prompt in the bottom overlay and return only the selected
  value.
- `src/voidx/ui/output/display_policy.py` hides `checkpoint` by default.
- `src/voidx/ui/output/dock/nodes_permission.py` already has a dedicated dock
  mixin for prompt-like UI nodes, which is the closest existing pattern.
- `src/voidx/ui/output/events/schema.py` has typed UI events and should remain
  the structured boundary for dock rendering.

Observed gaps:

- A checkpoint prompt is visible only while the bottom choice overlay is active.
  After selection, the TUI transcript has no durable record of the presented plan
  or the user's decision.
- Making `checkpoint` visible through the generic tool display policy would show
  a normal tool call/result, but it would not express "the agent asked the user
  to approve this plan" as a first-class interaction.
- The checkpoint result JSON is useful for the model and runtime state, but it is
  not an appropriate TUI artifact.

## Design Summary

Add a dedicated checkpoint interaction node to the TUI dock tree.

When `PlanCheckpointTool` asks for approval:

1. The runtime emits a `CheckpointPromptShown` UI event before opening the
   existing choice overlay.
2. The TUI dock renders a `voidx plan` node containing the plan summary, steps,
   affected files, risks, and choices.
3. The user selects an option through the current bottom choice overlay.
4. The runtime emits `CheckpointDecisionSubmitted` with the selected value,
   display label, and optional custom-text response.
5. The TUI updates the same node to a settled status — approved, needs doc,
   modified, or rejected — and appends a visible response line. (Cancellation
   and timeout are mapped to `rejected` by the tool layer; there is no separate
   `cancelled` state in V1.)
6. The `checkpoint` tool remains hidden in the generic tool display policy, so
   the model-facing JSON result does not duplicate the human-facing node.

The important split is:

- the dock node is the durable visual record;
- the existing overlay remains the input mechanism;
- the tool result remains the model/runtime contract.

## TUI Rendering

Initial node:

```text
● voidx plan
  Plan: Implement checkpoint UI node

  Steps:
  1. Add checkpoint prompt events
  2. Render a dock node while the choice overlay is active
  3. Record the user's selected decision

  Affected files:
  src/voidx/tools/plan_checkpoint.py
  src/voidx/ui/output/events/schema.py

  Risks:
  - Avoid duplicating hidden checkpoint tool JSON
```

After an approval:

```text
● voidx plan approved
  User: Implement directly
```

After a modified scope:

```text
● voidx plan modified
  User: only implement S1 and S3, do not change permission events
```

After rejection (also covers user cancellation and timeout — the tool layer
unifies all three into `decision="rejected"`):

```text
● voidx plan rejected
  User: Reject
```

If the user cancels or times out before selecting an option, the node still uses
the rejected status but must not pretend that the user explicitly clicked
`Reject`:

```text
● voidx plan rejected
  User: no response; treated as rejected
```

Rendering rules:

- Header prefix: `voidx plan`, not `Checkpoint`.
- Running node color should match existing prompt/status language, likely yellow
  while awaiting input.
- Settled successful decisions use dim/green-ish styling consistent with normal
  completed tool/status nodes.
- Rejected decisions should be visually distinct (e.g. dim red) but not rendered
  as a process crash.
- The plan body should be expanded while awaiting input.
- After resolution, the node may collapse to the header plus response summary if
  the body is long; the initial V1 can keep it expanded for simplicity.
- There is no `voidx plan cancelled` state in V1. The tool layer never produces
  a `cancelled` decision — cancellation and timeout are mapped to `rejected`
  (see Runtime Flow → Rejected / cancelled unification).

## Event Model

Add typed UI events:

```python
class CheckpointChoicePayload(BaseModel):
    label: str
    value: str
    description: str = ""


class CheckpointPlanPayload(BaseModel):
    plan_summary: str
    steps: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class CheckpointPromptShown(UiEventBase):
    kind: Literal["checkpoint_prompt.shown"] = "checkpoint_prompt.shown"
    checkpoint_id: str
    plan: CheckpointPlanPayload
    choices: list[CheckpointChoicePayload] = Field(default_factory=list)


class CheckpointDecisionSubmitted(UiEventBase):
    kind: Literal["checkpoint_decision.submitted"] = "checkpoint_decision.submitted"
    checkpoint_id: str
    decision: str
    label: str = ""
    response: str = ""
    was_custom_input: bool = False
```

### `checkpoint_id` stability

`PlanCheckpointTool.execute()` does not receive the tool call id — its signature
is `async def execute(self, args: dict, ctx: ToolContext)`, and `ToolContext`
does not carry `tool_call_id`. The tool call id exists only at the executor
dispatch layer (`executor.py`, `tc.get("id")`) and is never passed into the
tool.

Therefore `checkpoint_id` must be self-generated by `PlanCheckpointTool` using
`uuid4().hex` at the start of `execute()`, held in a local variable, and reused
for both `CheckpointPromptShown` and `CheckpointDecisionSubmitted` emissions.
Do not use `id(request)` — the `UserInteraction` object is constructed inside
`execute()`, so identity-based ids are fragile, non-serializable, and would not
survive across the two emit calls if the object is rebuilt.

Adding `tool_call_id` to `ToolContext` is a viable future improvement (the
executor already has the value), but is out of scope for V1 to avoid churning
the context model.

### `was_custom_input` semantics

The `was_custom_input` flag indicates that the user's final response came from
free-text input rather than a structured choice selection. This is **not**
equivalent to "the user picked `Other...`". The interaction callback
(`helpers.py:_make_interact_callback`) sets `UserResponse.free_text=True` in
two distinct cases:

1. The user selected `Other...` from the choice overlay, then typed a custom
   answer.
2. The user selected `Modify scope` (a structured choice), which triggers a
   **second** `ctx.interact()` call asking for scope text — that second response
   also has `free_text=True`.

Rendering consumers must not treat `was_custom_input=True` as "Other path was
used". The `response` field always carries the final display text regardless.

### Event emission path

V1 uses the **module-level `ui_events` singleton** with `emit_direct(...)`,
consistent with how `capture.py`, `console/app.py`, and `tui/app.py` emit all
other UI events (40+ existing call sites). `PlanCheckpointTool` imports
`ui_events` lazily inside `execute()` to avoid import cycles:

```python
from voidx.ui.output.events import ui_events
from voidx.ui.output.events.schema import CheckpointPromptShown, CheckpointDecisionSubmitted
```

The `ToolContext.ui_events: ToolInteractionEvents | None` abstraction proposed
earlier is **deferred to future work**. It would establish a new event-emission
path that conflicts with the established global-singleton pattern, and
`ToolContext` is a Pydantic model that requires `exclude=True` for non-serializable
fields (like the existing `interact` callback). The direct singleton approach is
narrower and matches existing conventions.

Do not reuse `PermissionPromptShown` for checkpoint. Permission prompts describe
authorization for tool execution; checkpoint prompts describe user approval of an
agent plan. They are both prompt-like, but their payloads and visual semantics
are different.

## Runtime Flow

V1 emits checkpoint events from `PlanCheckpointTool.execute()` itself, not from
the generic tool display policy and not from the shared interaction callback.
That keeps the behavior scoped to checkpoint and avoids redesigning every
`ctx.interact()` caller in the same change.

For checkpoint specifically:

1. `PlanCheckpointTool.execute()` builds the structured `PlanCheckpointInput`
   and generates `checkpoint_id = uuid4().hex`.
2. Before calling `ctx.interact()`, it emits `CheckpointPromptShown` via
   `ui_events.emit_direct(...)` (guard with `ui_events.is_running` if available).
3. It calls `ctx.interact(UserInteraction(...))` exactly as today.
4. After `ctx.interact()` returns, it emits `CheckpointDecisionSubmitted`.
5. It continues producing the existing `PlanCheckpointResult`, metadata, and
   runtime `state_patch`.

### Modified-scope two-interaction flow

When the user selects `Modify scope`, `execute()` makes a **second**
`ctx.interact()` call (asking "Describe the modified scope:"). The full sequence
is:

1. Generate `checkpoint_id`.
2. Emit `CheckpointPromptShown`.
3. First `ctx.interact()` → user selects "Modify scope".
4. Second `ctx.interact()` → user enters scope text (or cancels).
5. Emit `CheckpointDecisionSubmitted` with `decision="modified"`,
   `response=<entered text>`, `was_custom_input=True`.

During step 4, the dock node remains **unsettled**. The node header stays
`● voidx plan` (not `voidx plan modified` yet) — the decision is not final until
the scope text is collected. `CheckpointDecisionSubmitted` is emitted only once,
after the second interaction completes. There is no intermediate event for the
"Modify scope selected, awaiting text" state in V1; the node simply stays
unsettled with its existing prompt body visible.

If the second interaction is cancelled, `execute()` falls back to using an empty
`modified_scope`, which later resolves to the original `inp.plan_summary`, and
`CheckpointDecisionSubmitted` is emitted with `decision="modified"` and
`response=""` with `was_custom_input=False`. The dock renderer should fall back
to the selected label for the visible response when `response` is empty:

```text
● voidx plan modified
  User: Modify scope
```

### Rejected / cancelled unification

`execute()` treats both user rejection and interaction cancellation as
`decision="rejected"` (`if response.cancelled or decision == "rejected"`).
The tool layer never produces a `cancelled` decision. Therefore:

- `CheckpointDecisionSubmitted.decision` will be `"rejected"` for both cases.
- The `cancelled` field has been removed from the event schema (see Event Model).
- The TUI renders both as `● voidx plan rejected`. There is no separate
  `voidx plan cancelled` state in V1 unless the tool's decision semantics are
  changed, which is a non-goal.
- The emitted `response` should still distinguish display text when possible:
  `Reject` for an explicit rejection, and `no response; treated as rejected` for
  cancellation or timeout.

### Emission mechanism

V1 emits events directly from `PlanCheckpointTool.execute()` using the
module-level `ui_events` singleton (see Event Model → Event emission path). The
`ToolContext.ui_events` helper abstraction is deferred to future work.

## Dock Integration

Add `DockCheckpointNodeMixin`, modeled after `DockPermissionNodeMixin`.

Responsibilities:

- `show_checkpoint(checkpoint_id, plan, choices, parent=None) -> OutputNode`
- `resolve_checkpoint(checkpoint_id, decision, label, response, was_custom_input=False) -> None`
- Track active checkpoint nodes by `checkpoint_id`.
- Mark the node unsettled while awaiting input and settled after a decision.
- **Do not implement a `clear_checkpoint` method.** Unlike
  `DockPermissionNodeMixin.clear_permission()` which removes the permission node
  after the prompt is answered, checkpoint nodes must be **retained** on
  resolution — the whole point is to preserve the plan and the user response in
  the transcript. `resolve_checkpoint` only updates node state and appends the
  response child; it never removes the node.

### Mixin wiring

`DockCheckpointNodeMixin` must be added to the `DockNodeMixin` base class list
in `src/voidx/ui/output/dock/nodes.py` (currently `DockNodeMixin` inherits from
`DockStartupNodeMixin`, `DockStatusNodeMixin`, `DockPermissionNodeMixin`). This
makes `show_checkpoint` / `resolve_checkpoint` available on `BottomInputDock`
automatically, since `BottomInputDock` inherits `DockNodeMixin`.

`BottomInputDock.__init__` also needs a checkpoint node registry, analogous to
`_status_nodes`, because `resolve_checkpoint` runs after the original show event:

```python
self._checkpoint_nodes: dict[str, OutputNode] = {}
```

### Event consumer wiring

The event consumer in `src/voidx/ui/output/events/consumers.py` handles UI events
via a `match` statement (see the `PermissionPromptShown` case at ~line 325). Add
two new cases:

```python
case CheckpointPromptShown() as e:
    choices = [c.model_dump() for c in e.choices]
    return self._dock.show_checkpoint(
        e.checkpoint_id,
        e.plan.model_dump(),
        choices,
        parent=self._agent_parent(e.agent_id),
    )
case CheckpointDecisionSubmitted() as e:
    return self._dock.resolve_checkpoint(
        e.checkpoint_id,
        e.decision,
        e.label,
        e.response,
        was_custom_input=e.was_custom_input,
    )
```

`OutputNode.node_type` should include `"checkpoint"` or a more generic
`"interaction"` type. Prefer `"checkpoint"` for V1 because rendering and
snapshot consumers can reason about the specific payload without a larger
interaction taxonomy.

Adding `"checkpoint"` is not limited to `OutputNode`. It must be reflected in
all typed tree/snapshot boundaries that validate node types:

- `src/voidx/ui/output/tree.py` — `OutputNode.node_type` literal.
- `src/voidx/ui/transcript.py` — `NodeType` literal and `_NODE_TYPES` restore
  allowlist.
- `src/voidx/ui/protocol/transcript.py` — inherits `NodeType`; snapshot
  validation will reject unknown node types if `NodeType` is not updated.
- `frontend/src/protocol.schema.json` — regenerated via
  `scripts/export_ui_protocol_schema.py` after Python schema changes.

Suggested node payload:

```python
{
    "interaction": "checkpoint",
    "checkpoint_id": checkpoint_id,
    "plan": plan_dict,
    "choices": choice_dicts,
    "decision": decision,
    "response": response,
}
```

## User Response Semantics

The selected option should be shown as a user response line, not as a raw tool
result. In TUI terms this can be a child node with `node_type="message"` or
`node_type="tool_result"` depending on what renders most cleanly.

Preferred V1: append a child message-like node:

```text
User: Implement directly
```

For `Other...` / free-text cases, display the entered text:

```text
User: only update the TUI path
```

The model still receives the existing checkpoint tool JSON. This design only
changes the human-visible TUI record.

## Non-goals

- Do not change checkpoint decision semantics or workflow activation behavior.
- Do not make `checkpoint` visible through `DEFAULT_DISPLAY_RULES`.
- Do not render checkpoint result JSON in the TUI.
- Do not redesign all choice prompts in this phase.
- Do not require web frontend rendering in V1.

## Testing

Focused tests should cover:

- `CheckpointPromptShown` creates a visible `voidx plan` node in the dock tree.
- The node includes plan summary, ordered steps, affected files, and risks.
- `CheckpointDecisionSubmitted` updates the same node and appends the user's
  selected label or custom-text response.
- Rejected decisions (including cancellation and timeout, which the tool maps to
  `rejected`) settle the node without rendering a tool error.
- Modified-scope flow: the node stays unsettled during the second `ctx.interact()`
  (scope text entry), and `CheckpointDecisionSubmitted` is emitted only once
  after the second interaction completes.
- `checkpoint_id` is identical between `CheckpointPromptShown` and
  `CheckpointDecisionSubmitted` for the same interaction.
- Hidden `checkpoint` tool display behavior remains unchanged in
  `ToolDisplayPolicy`.
- Existing `ask_choice` overlay tests continue to pass; the overlay remains the
  input mechanism.
- Gateway protocol schema exports the new events: add `CheckpointPromptShown`
  and `CheckpointDecisionSubmitted` to the `UiEvent` union in
  `src/voidx/ui/output/events/schema.py` (line ~252), then run
  `scripts/export_ui_protocol_schema.py` to verify they appear in the exported
  schema.
- Transcript and gateway snapshot serialization accept `node_type="checkpoint"`
  without degrading restored nodes to `"message"` or failing Pydantic
  validation.

Recommended focused commands:

```bash
# Dock node rendering for new events
.venv/bin/python -m pytest tests/test_ui/gateway/test_ui_events_dock.py -v
# Display policy unchanged for checkpoint
.venv/bin/python -m pytest tests/test_ui/test_display_policy.py -v
# Existing checkpoint tool behavior (approval, rejection, modified, free-text)
.venv/bin/python -m pytest tests/test_tools/test_interactive_tools_clarify.py -v
# Interaction callback wiring unchanged
.venv/bin/python -m pytest tests/test_tools/test_make_interact_callback.py -v
# Schema export includes new events
.venv/bin/python scripts/export_ui_protocol_schema.py
```

## Open Questions

- Should the resolved `voidx plan` node stay expanded, or collapse automatically
  when the plan body is long?
- Should `clarify` later reuse the same event family under a generic
  `interaction_prompt.*` model, or should it get its own purpose-built node?
- Should the tool layer eventually distinguish cancellation from rejection
  (producing a `cancelled` decision), or is unifying them into `rejected`
  acceptable long-term?
