# Turn Lifecycle Metadata Spec

Date: 2026-07-30

> **Status: Done** — Archived on 2026-07-30.
> loop Ctrl-C hotfix. This spec defines the long-term replacement for text-based
> turn classification.

## Goal

Make turn identity explicit and structured across the runtime and UI layers so
features such as loop activity rendering, interrupt handling, transcript display,
and future runtime profiles do not infer behavior from rendered text.

The implementation should introduce a generic turn metadata channel, not a
loop-only flag. Loop should become one consumer of the metadata, alongside coding,
chat, goal, and future profiles.

## Problem

Current loop turn detection in the TUI is coupled to user-visible text:

- `tui/voidx_cli/render_activity.py` treats a turn as loop if the current turn text
  equals `LOOP_ITERATION_USER_TEXT` or starts with `[loop]`.
- `src/voidx/ui/output/events/schema.py::TurnStarted` carries only `text`, so UI
  consumers cannot distinguish semantic turn type from display content.
- `src/voidx/ui/output/dock/app.py::BottomInputDock.start_turn()` receives only the
  rendered text, so dock state cannot expose a reliable runtime/profile signal.

This is fragile because display text is not a protocol boundary:

- a normal coding/chat message can start with `[loop]`;
- loop display wording can change independently of behavior;
- localization, formatting, or transcript presentation changes can break TUI
  interrupt behavior;
- future profiles would need more string heuristics instead of structured state.

## Current State

Relevant files and current responsibilities:

- `src/voidx/agent/runtime/contracts.py::TurnRequest` carries `user_text`,
  `display_text`, `context`, and persistence/runtime state.
- `src/voidx/agent/domain/turn_context.py::TurnExecutionContext` carries
  `runtime_profile`, `tool_policy`, and optional `loop_controller`.
- `src/voidx/agent/domain/profile.py::RuntimeProfile` defines `profile_id` and
  `protocol` (`turn`, `loop`, `goal`, etc.).
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py` derives
  `turn_display_text` and emits `TurnStarted(text=turn_display_text)`.
- `src/voidx/ui/output/events/schema.py::TurnStarted` is the UI event boundary for
  starting a turn.
- `src/voidx/ui/output/events/consumers.py::DockEventConsumer` maps
  `TurnStarted` to `BottomInputDock.start_turn(text)`.
- `src/voidx/ui/output/dock/app.py::BottomInputDock` stores current turn display
  state and `turn_in_progress`.
- `tui/voidx_cli/render_activity.py::_loop_turn_in_progress()` currently needs to
  infer loop state from dock-visible text.

## Design Summary

Introduce a generic `TurnMetadata` value object and pass it through the turn
lifecycle path:

```python
class TurnMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str = "coding"
    protocol: str = "turn"
    category: str = "coding"
```

Use `protocol` as the primary behavior discriminator for runtime protocol state.
For loop activity and interrupts, the semantic check becomes:

```python
dock.turn_in_progress and dock.current_turn_metadata.protocol == "loop"
```

`category` is intentionally separate from `protocol` for UI and product-level grouping.
For example, a future `goal` profile may still use protocol `turn` but present as
category `goal`; chat can use protocol `turn` and category `chat`.

## Data Model

### `TurnMetadata`

Create a small immutable Pydantic model near existing runtime turn contracts:

- Preferred file: `src/voidx/agent/domain/turn_metadata.py`
- Alternative: `src/voidx/agent/runtime/contracts.py` if avoiding a new module is
  preferred, but a domain module is cleaner because UI events and dock state both
  need the type.

Fields:

```python
class TurnMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str = "coding"
    protocol: str = "turn"
    category: str = "coding"
```

Field semantics:

- `profile_id`: runtime profile identity (`coding`, `chat`, `loop`, future ids).
- `protocol`: graph protocol/tool lifecycle (`turn`, `loop`, `goal`, future ids).
- `category`: UI/product category. It is explicit metadata, and defaults to the
  profile identity for built-in profiles (`coding`, `chat`, `loop`). A future
  profile may choose a different category without changing its protocol.

Event correlation IDs remain on `UiEventBase`: `TurnStarted.thread_id` is the
canonical event/thread correlation field. Do not duplicate `thread_id` or
`session_id` inside `TurnMetadata`; consumers must not have to choose between two
sources of identity. Session correlation remains available through the existing
runtime/session context and gateway state where needed.

Do not include rendered text, user prompt text, or localized labels in metadata.
Metadata must be safe to compare and stable under display changes.

### Metadata construction

Add helper construction from `TurnExecutionContext`:

```python
def turn_metadata_from_context(context: TurnExecutionContext) -> TurnMetadata:
    profile = context.runtime_profile
    profile_id = profile.profile_id
    protocol = profile.protocol
    return TurnMetadata(
        profile_id=profile_id,
        protocol=protocol,
        category=profile_id,
    )
```

`RuntimeProfile.profile_id` and `RuntimeProfile.protocol` are already validated
as the runtime source of truth. Do not silently coerce an empty value here;
invalid profile construction should fail at the profile boundary.

The loop profile must come from `src/voidx/agent/domain/loop.py::LOOP_PROFILE`
(or its `loop_profile_for_spec()` copy), whose protocol is explicitly `"loop"`.
The loop service must preserve that profile when creating the loop turn context.

This keeps the source of truth in runtime profile/context, not in UI text.

## Event Schema

Extend `TurnStarted` with metadata:

```python
class TurnStarted(UiEventBase):
    kind: Literal["turn.started"] = "turn.started"
    text: str
    metadata: TurnMetadata = Field(default_factory=TurnMetadata)
```

Event boundary rules:

- `metadata` must have a default so internal callers that only pass `text` stay simple.
- `TurnStarted.thread_id` remains the canonical event correlation field; the metadata
  object must not reintroduce a competing thread/session ID source.
- The same `TurnStarted` payload should be the source for dock, TUI, and any UI gateway
  adapter that exposes turn lifecycle events.
- Generic event serialization must forward `metadata` unchanged. The frontend may
  ignore the field for now, but its checked-in protocol schema and decoder must accept
  the additional object without dropping or rejecting it.
- Do not overload the existing event `kind` discriminator; use `metadata.protocol` /
  `metadata.category` for turn semantics.

## Runtime Flow

Update `turn_runner` to emit metadata at the same boundary as `TurnStarted`:

```python
turn_metadata = turn_metadata_from_context(turn_context)
await host._ui.events.request(
    TurnStarted(text=turn_display_text, metadata=turn_metadata)
)
```

For non-event mode, pass the same metadata directly:

```python
host._ui.dock.start_turn(turn_display_text, metadata=turn_metadata)
```

The key invariant is that event and non-event paths must produce identical dock
metadata for the same runtime turn.

## Dock State

Update `BottomInputDock` to store current metadata explicitly:

```python
self._current_turn_metadata = TurnMetadata()

@property
def current_turn_metadata(self) -> TurnMetadata:
    return self._current_turn_metadata

def start_turn(self, text: str, *, metadata: TurnMetadata | None = None) -> OutputNode:
    self._turn_in_progress = True
    self._current_turn_metadata = metadata or TurnMetadata()
    ...

def end_turn(self) -> None:
    self._turn_in_progress = False
    self._current_turn_metadata = TurnMetadata()
    self.refresh()
```

Reset paths must also restore `TurnMetadata()`:

- `BottomInputDock._reset_runtime_nodes()`
- `BottomInputDock.reset()` through `_reset_runtime_nodes()`
- `BottomInputDock.restore_tree()` through `_reset_runtime_nodes()`

Avoid storing only `current_turn_text` as the semantic signal. The text can remain
available for rendering/debugging if needed, but behavior must use metadata.

## TUI Behavior

Replace text-based loop checks in `tui/voidx_cli/render_activity.py`:

```python
def _loop_turn_in_progress(self) -> bool:
    if not getattr(dock, "turn_in_progress", False):
        return False
    metadata = getattr(dock, "current_turn_metadata", None)
    return getattr(metadata, "protocol", "turn") == "loop"
```

`_handle_interrupt()` in `tui/voidx_cli/app.py` should continue to use
`_loop_turn_in_progress()` plus `_loop_waiting_active()` as the high-level decision.
It should not inspect metadata directly unless more turn kinds need different
interrupt behavior later.

## Non-Goals

- Do not create a loop-only `is_loop_turn` field.
- Do not infer turn type from `display_text`, `user_text`, `thread_id` prefixes, or
  rendered transcript headers.
- Do not change graph protocol selection; `resolve_graph_protocol()` remains driven
  by `RuntimeProfile.protocol`.
- Do not change loop scheduling, loop persistence, or loop tool execution semantics.
- Do not add frontend-specific behavior for loop interrupts or profile semantics in this
  change. The generic gateway/protocol path must still forward `metadata` unchanged,
  and the checked-in frontend schema/decoder must accept the new optional object.
- Do not remove user-visible `[loop] ...` display text; it remains presentation only.
- Do not make slash-command presentation turns look like loop turns. The direct
  `agent_service.py` slash-command `dock.start_turn(user_input)` path uses default
  metadata unless it is later migrated to an explicit command profile.

## Implementation Shape

The change should be implemented as one coherent metadata path:

1. Add `TurnMetadata` and `turn_metadata_from_context()`.
2. Add defaulted `metadata` to `TurnStarted`.
3. Derive metadata once in `turn_runner` from `TurnExecutionContext.runtime_profile`.
4. Pass that metadata through both event and direct dock paths.
5. Store and clear `current_turn_metadata` in `BottomInputDock`.
6. Make TUI loop-turn behavior read `current_turn_metadata.protocol`.
7. Delete text-based loop checks and imports that exist only for those checks.

Keep `current_turn_text` only for presentation/debugging. It must not participate in
behavior decisions.

## Tests

Add or update focused tests before implementation:

### UI event schema / consumer

- `src/tests/test_ui/...` or existing event consumer tests:
  - `TurnStarted(text="hi")` defaults to metadata protocol `turn`.
  - `TurnStarted(text="...", metadata=TurnMetadata(protocol="loop"))` passes loop
    metadata into `BottomInputDock`.

### Dock

- `BottomInputDock.start_turn("hi")` stores default metadata.
- `BottomInputDock.start_turn("[loop] x", metadata=TurnMetadata(protocol="loop"))`
  stores loop metadata.
- `end_turn()` and `reset()` clear metadata to default.
- `_reset_runtime_nodes()` and `restore_tree()` clear metadata together with
  `turn_in_progress`; no stale loop metadata may survive a reset.

### Runtime propagation

- Runtime tests must assert profile metadata from the actual `TurnExecutionContext`,
  not only construct `TurnStarted` manually. This prevents the default metadata from
  masking a missing runtime propagation call.
- The loop service/profile construction path must prove that a loop turn context uses
  `LOOP_PROFILE` (or its spec-specific copy) with `protocol == "loop"`.
- The direct slash-command `dock.start_turn(user_input)` path remains a default/regular
  presentation turn and must not classify text beginning with `[loop]` as loop.

### TUI

- Regular coding/chat turn in progress does not trigger `/loop stop` on Ctrl-C.
- Regular user text that starts with `[loop]` does not trigger `/loop stop` unless
  metadata protocol is `loop`.
- Loop turn with metadata protocol `loop` triggers `/loop stop`, including active
  choice/approval prompt cases.
- Loop waiting status still triggers `/loop stop` independently of current turn
  metadata.

### Runtime

- Coding turn emits `TurnStarted.metadata.protocol == "turn"` and
  `profile_id == "coding"`.
- Chat turn emits `TurnStarted.metadata.profile_id == "chat"` and protocol `turn`.
- Loop runtime turn emits `TurnStarted.metadata.profile_id == "loop"` and protocol
  `loop`.
- Event and non-event paths populate identical metadata for the same
  `TurnExecutionContext`.
- Any UI gateway adapter that exposes `turn.started` forwards the same metadata shape
  instead of reconstructing turn type from text.
- The exported backend UI protocol schema and checked-in
  `frontend/src/rpc/protocol.schema.json` agree on the optional `metadata` object;
  frontend handling may ignore its values, but must not reject or strip them.

## Verification Commands

Run these after implementation:

```bash
./test.py --backend -- \
  tui/tests/test_status_activity.py \
  tui/tests/test_input_advanced.py \
  tui/tests/test_input_handling.py -q

./test.py --backend -- \
  src/tests/test_agent/graph/test_call_llm_tools.py::test_call_llm_default_profile_does_not_bind_loop_tool \
  src/tests/test_agent/graph/test_call_llm_tools.py::test_call_llm_injects_loop_only_for_loop_profile \
  src/tests/test_agent/loop/test_runtime_scheduler.py \
  src/tests/test_agent/runtime/test_runtime.py -q

./python.py -m py_compile \
  src/voidx/agent/domain/turn_metadata.py \
  src/voidx/ui/output/events/schema.py \
  src/voidx/ui/output/events/consumers.py \
  src/voidx/ui/output/dock/app.py \
  tui/voidx_cli/render_activity.py

./python.py scripts/export_ui_protocol_schema.py
./test.py --backend -- src/tests/test_ui/protocol/test_dto.py -q
```

Run a broader backend suite if the event schema or runtime contracts affect many
callers:

```bash
./test.py --backend
```

## Acceptance Criteria

- No TUI behavior depends on matching `[loop]` or `LOOP_ITERATION_USER_TEXT`.
- Ordinary coding/chat turns, including messages that literally start with `[loop]`,
  keep normal Ctrl-C behavior.
- Active loop turns still stop with Ctrl-C, including during AI approval / choice
  prompts.
- Loop waiting countdown/status still stops with Ctrl-C even when no turn is active.
- `TurnStarted(text=...)` gets default metadata without extra caller ceremony.
- Metadata source of truth is `TurnExecutionContext.runtime_profile`, not UI display.
- Event and non-event UI paths expose the same dock metadata.
- Public UI event adapters, where present, forward metadata from `TurnStarted` rather
  than deriving it from rendered text.

## Rollout Notes

Implement this as a replacement for the current text-based hotfix, not as an
additional heuristic. Once metadata is wired through and tests are green, delete the
text matching branch to avoid split-brain behavior.

This spec intentionally keeps metadata generic. Future profiles should extend or
reuse `TurnMetadata` rather than adding UI-specific string matching or one-off flags.
