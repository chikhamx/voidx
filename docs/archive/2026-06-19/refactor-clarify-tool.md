# Refactor: Simplify clarify tool

> **Status: Done**

## Goal
Remove unnecessary fields from clarify tool: `context`, `blocking` from input; simplify `ClarifyOption` to `str`; remove `selected_option` from result.

## Architecture

### Two-path design for `UserInteraction.options`

`UserInteraction.options` accepts two formats, and `_make_interact_callback` routes them differently:

| Format | Source | Routing | TUI method |
|---|---|---|---|
| `list[str]` | `clarify` tool | `ask_text` (free-text with suggestions in prompt) | `ask_text` |
| `list[tuple[str, str, str]]` | `plan_checkpoint`, slash commands, permissions | `ask_choice` (structured choice) | `ask_choice` |

Union type: `list[str | tuple[str, str, str]]`

### Why two paths?

- **clarify** asks open-ended questions; options are suggestions. The user can type anything. `ask_text` is the right UX — a text input with suggestions appended to the prompt as `(opt1 / opt2 / ...)`.
- **ask_choice** callers (permissions, slash commands, compaction) need structured `(label, value, description)` tuples where the returned value is a machine-readable key, not the displayed label.

### Routing logic in `_make_interact_callback`

```python
async def interact(request: UserInteraction) -> UserResponse:
    if request.options and _is_tuple_options(request.options):
        # choice path: ask_choice with (label, value, desc) tuples
        other_value = _other_choice_value(request.options)
        choices = [*request.options, ("Other…", other_value, "Type a custom answer")]
        result = await app.ask_choice(request.prompt, choices, timeout=timeout)
        if result == other_value:
            result = await app.ask_text(request.prompt, timeout=timeout)
            ...
    elif request.options:
        # clarify path: ask_text with suggestions appended to prompt
        suggestions = " / ".join(str(o) for o in request.options)
        prompt = f"{request.prompt} ({suggestions})"
        result = await app.ask_text(prompt, timeout=timeout)
        ...
    else:
        result = await app.ask_text(request.prompt, timeout=timeout)
        ...
```

### clarify tool (simplified)

- `ClarifyInput.options: list[str]` — label = value, no description
- Removed: `context`, `blocking` from `ClarifyInput`; `selected_option` from `ClarifyResult`; `ClarifyOption` class; `_selected_option()`; `_prompt()`
- `_infer_state_patch` drops context-based heuristic, only matches answer against intent map

### plan_checkpoint decision mapping

`plan_checkpoint` uses `_DECISION_MAP` to decouple `response.value` (the tuple's value field) from the internal `decision` used in workflow evidence:

```python
_DECISION_MAP: dict[str, str] = {
    "approved": "approved",
    "needs_doc": "needs_doc",
    "modified": "modified",
    "rejected": "rejected",
}
```

`decision = _DECISION_MAP.get(response.value, "modified")` — explicit mapping with safe fallback. This keeps `WorkflowEvidence.condition` (e.g. `checkpoint_approved`) and `WorkflowRunState.reason` (e.g. `checkpoint:approved`) stable even if tuple values change.

### TUI layer

- `ask_choice` signature: `list[str | tuple[str, str, str]]`, internally normalizes to `list[tuple[str, str, str]]` (str → `(str, str, "")`)
- `_active_choice: list[tuple[str, str, str]] | None`
- Overlays render `(label, value, desc)` — label displayed, desc as dim subtitle
- `_submit_choice_selection` returns `value` (second element), not label
- `ChoiceState.active: list[tuple[str, str, str]] | None`

### UI protocol layer

- `UiChoiceRequest.choices: list[tuple[str, str, str]]` — unchanged
- `PermissionPromptShown.choices: list[tuple[str, str, str]]` — unchanged

## File Structure

| File | Change | Status |
|---|---|---|
| `src/voidx/tools/clarify.py` | Delete `ClarifyOption`; `options: list[str]`; remove `context`/`blocking`; remove `selected_option`/`_selected_option`; simplify `_prompt`/`_infer_state_patch` | ✅ Done |
| `src/voidx/tools/base.py` | `UserInteraction.options: list[str \| tuple[str, str, str]]`; remove `blocking` | ✅ Done |
| `src/voidx/agent/graph/tool_executor.py` | `_make_interact_callback` dual-path routing; `_other_choice_value` adapt; str-options suggestions fallback | ✅ Done |
| `src/voidx/tools/plan_checkpoint.py` | `_CHECKPOINT_OPTIONS` as tuples; add `_DECISION_MAP`; `decision = _DECISION_MAP.get(response.value, "modified")` | ✅ Done |
| `src/voidx/ui/tui/choice_mixin.py` | `ask_choice` takes `list[str \| tuple[str, str, str]]`; normalize internally; `_active_choice: list[tuple[str, str, str]] \| None` | ✅ Done |
| `src/voidx/ui/tui/overlays.py` | render from `(label, value, desc)` tuples | ✅ Done |
| `src/voidx/ui/tui/input.py` | quick-select from `(label, value, desc)` tuples | ✅ Done |
| `src/voidx/ui/tui/panels.py` | `_submit_choice_selection` returns `value` from tuple | ✅ Done |
| `src/voidx/ui/tui/state.py` | `ChoiceState.active: list[tuple[str, str, str]] \| None` | ✅ Done |
| `src/voidx/ui/tui/parser.py` | choice-active checks adapt to tuple format | ✅ Done |
| `src/voidx/ui/tui/render_input.py` | cursor rendering when choice active adapts | ✅ Done |
| `src/voidx/ui/tui/render_frame.py` | choice-active check adapts | ✅ Done |
| `src/voidx/ui/tui/app.py` | choice-active checks adapt | ✅ Done |
| `src/voidx/agent/graph/compaction_coordinator.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/graph/permissions.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/handler.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/session.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/runtime.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/code_ide.py` | No change needed (already passes tuples) | ✅ No change |
| Test files (14+ files) | Remove `ClarifyOption` imports; adapt to new format | ✅ Done |

## Tasks

### Phase 1 — clarify core simplification ✅

- [x] 1. `src/voidx/tools/clarify.py` — Delete `ClarifyOption`; `options: list[str]`; remove `context`/`blocking` from `ClarifyInput`; remove `selected_option` from `ClarifyResult`; delete `_selected_option`; simplify `_prompt`; simplify `_infer_state_patch`; update `execute`
- [x] 2. `src/voidx/tools/base.py` — `UserInteraction.options: list[str]`; remove `blocking`
- [x] 3. `src/voidx/agent/graph/tool_executor.py` — Adapt `_make_interact_callback` and `_other_choice_value` to `list[str]`
- [x] 4. `src/voidx/tools/plan_checkpoint.py` — Change options to `list[str]`; add `_DECISION_MAP`; update `execute`
- [x] 5. TUI layer — `ask_choice`, overlays, input, panels, state all adapted to `list[str]`
- [x] 6. All test files — Remove `ClarifyOption` imports; adapt `ClarifyInput` usage; remove context test; update assertions

### Phase 2 — restore dual format for ask_choice callers ✅

- [x] 7. `src/voidx/tools/base.py` — Change `UserInteraction.options` to `list[str | tuple[str, str, str]]`
- [x] 8. `src/voidx/agent/graph/tool_executor.py` — `_make_interact_callback` dual-path: str-only → `ask_text` with suggestions, tuple → `ask_choice`; `_other_choice_value` only for tuple path
- [x] 9. `src/voidx/ui/tui/choice_mixin.py` — `ask_choice` accepts `list[str | tuple[str, str, str]]`; normalize str→`(str, str, "")`; `_active_choice: list[tuple[str, str, str]] | None`
- [x] 10. `src/voidx/ui/tui/overlays.py` — Render `(label, value, desc)` from tuples; label as main text, desc as dim subtitle
- [x] 11. `src/voidx/ui/tui/input.py` — Quick-select returns `value` from tuple
- [x] 12. `src/voidx/ui/tui/panels.py` — `_submit_choice_selection` returns `value` (tuple[1])
- [x] 13. `src/voidx/ui/tui/state.py` — `ChoiceState.active: list[tuple[str, str, str]] | None`
- [x] 14. `src/voidx/ui/tui/parser.py` — Adapt choice-active checks to tuple format
- [x] 15. `src/voidx/ui/tui/render_input.py` — Adapt cursor rendering to tuple format
- [x] 16. `src/voidx/ui/tui/render_frame.py` — Adapt choice-active check to tuple format
- [x] 17. `src/voidx/ui/tui/app.py` — Adapt choice-active checks to tuple format
- [x] 18. Update TUI test files — Adapt `_active_choice` assertions back to tuple format
- [x] 19. Run focused tests and verify (585 passed)

## Risks
- `UserInteraction.options` Union type may produce `$defs` in Pydantic JSON schema — need to verify LLM tool schema compatibility. If it causes issues, consider a discriminator field or separate `choice_options` field.
- clarify `list[str]` options currently append suggestions to prompt text as a fallback. A proper `ask_text` suggestions parameter with quick-select chips UI would be a future improvement.
- `_infer_state_patch` loses context-based scope heuristic — **intentional removal**. Previously, answers to questions with "scope" in context would set `GoalType.CHORE`. This heuristic was fragile (depended on the word "scope" appearing in the question text) and has been replaced by answer-based matching only. If scope-aware goal typing is needed in the future, it should be implemented as an explicit field in `ClarifyInput` rather than a text heuristic.
- `plan_checkpoint` decision values in `WorkflowEvidence.condition` and `WorkflowRunState.reason` remain machine-readable via `_DECISION_MAP` — no downstream breakage.
