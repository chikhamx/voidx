# Refactor: Simplify clarify tool

## Goal
Remove unnecessary fields from clarify tool: `context`, `blocking` from input; simplify `ClarifyOption` to `str`; remove `selected_option` from result.

## Status

**Phase 1 complete** (751 tests passing): clarify core + TUI simplified to `list[str]`.
**Phase 2 in progress**: restore `ask_choice` dual format to fix 6 broken callers.

## Architecture

### Two-path design for `UserInteraction.options`

`UserInteraction.options` accepts two formats, and `_make_interact_callback` routes them differently:

| Format | Source | Routing | TUI method |
|---|---|---|---|
| `list[str]` | `clarify` tool | `ask_text` (free-text with optional quick-select) | `ask_text` |
| `list[tuple[str, str, str]]` | `plan_checkpoint`, slash commands, permissions | `ask_choice` (structured choice) | `ask_choice` |

Union type: `list[str | tuple[str, str, str]]`

### Why two paths?

- **clarify** asks open-ended questions; options are suggestions. The user can type anything. `ask_text` is the right UX — a text input with optional quick-select chips.
- **ask_choice** callers (permissions, slash commands, compaction) need structured `(label, value, description)` tuples where the returned value is a machine-readable key, not the displayed label.

### Routing logic in `_make_interact_callback`

```python
async def interact(request: UserInteraction) -> UserResponse:
    if request.options:
        if _is_str_options(request.options):
            # clarify path: ask_text with quick-select
            result = await app.ask_text(request.prompt, suggestions=request.options, timeout=timeout)
            ...
        else:
            # choice path: ask_choice with (label, value, desc) tuples
            result = await app.ask_choice(request.prompt, request.options, timeout=timeout)
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

`plan_checkpoint` uses natural-language labels as option values, but its internal `decision` field and workflow evidence must keep machine-readable values:

```python
_DECISION_MAP = {
    "Implement directly": "approved",
    "Document first": "needs_doc",
    "Modify scope": "modified",
    "Reject": "rejected",
}
```

`response.value` is the natural-language label; `decision` is looked up from `_DECISION_MAP`. This keeps `WorkflowEvidence.condition` (e.g. `checkpoint_approved`) and `WorkflowRunState.reason` (e.g. `checkpoint:approved`) stable.

### TUI layer

- `ask_choice` signature: `list[str | tuple[str, str, str]]`, internally normalizes to `list[tuple[str, str, str]]` (str → `(str, str, "")`)
- `_active_choice: list[tuple[str, str, str]] | None` — restored to tuple format
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
| `src/voidx/tools/base.py` | `UserInteraction.options: list[str \| tuple[str, str, str]]`; remove `blocking` | 🔄 Phase 2 |
| `src/voidx/agent/graph/tool_executor.py` | `_make_interact_callback` dual-path routing; `_other_choice_value` adapt | 🔄 Phase 2 |
| `src/voidx/tools/plan_checkpoint.py` | options to `list[str]`; add `_DECISION_MAP`; branch on label, map to decision | ✅ Done |
| `src/voidx/ui/tui/choice_mixin.py` | `ask_choice` takes `list[str \| tuple[str, str, str]]`; normalize internally; `_active_choice: list[tuple[str, str, str]] \| None` | 🔄 Phase 2 |
| `src/voidx/ui/tui/overlays.py` | render from `(label, value, desc)` tuples | 🔄 Phase 2 |
| `src/voidx/ui/tui/input.py` | quick-select from `(label, value, desc)` tuples | 🔄 Phase 2 |
| `src/voidx/ui/tui/panels.py` | `_submit_choice_selection` returns `value` from tuple | 🔄 Phase 2 |
| `src/voidx/ui/tui/state.py` | `ChoiceState.active: list[tuple[str, str, str]] \| None` | 🔄 Phase 2 |
| `src/voidx/ui/tui/parser.py` | choice-active checks adapt to tuple format | 🔄 Phase 2 |
| `src/voidx/ui/tui/render_input.py` | cursor rendering when choice active adapts | 🔄 Phase 2 |
| `src/voidx/ui/tui/render_frame.py` | choice-active check adapts | 🔄 Phase 2 |
| `src/voidx/ui/tui/app.py` | choice-active checks adapt | 🔄 Phase 2 |
| `src/voidx/agent/graph/compaction_coordinator.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/graph/permissions.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/handler.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/session.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/runtime.py` | No change needed (already passes tuples) | ✅ No change |
| `src/voidx/agent/slash/code_ide.py` | No change needed (already passes tuples) | ✅ No change |
| Test files (14+ files) | Remove `ClarifyOption` imports; adapt to new format | ✅ Done (Phase 1); 🔄 Phase 2 TUI tests |

## Tasks

### Phase 1 — clarify core simplification ✅

- [x] 1. `src/voidx/tools/clarify.py` — Delete `ClarifyOption`; `options: list[str]`; remove `context`/`blocking` from `ClarifyInput`; remove `selected_option` from `ClarifyResult`; delete `_selected_option`; simplify `_prompt`; simplify `_infer_state_patch`; update `execute`
- [x] 2. `src/voidx/tools/base.py` — `UserInteraction.options: list[str]`; remove `blocking`
- [x] 3. `src/voidx/agent/graph/tool_executor.py` — Adapt `_make_interact_callback` and `_other_choice_value` to `list[str]`
- [x] 4. `src/voidx/tools/plan_checkpoint.py` — Change options to `list[str]`; add `_DECISION_MAP`; update `execute`
- [x] 5. TUI layer — `ask_choice`, overlays, input, panels, state all adapted to `list[str]`
- [x] 6. All test files — Remove `ClarifyOption` imports; adapt `ClarifyInput` usage; remove context test; update assertions

### Phase 2 — restore dual format for ask_choice callers

- [ ] 7. `src/voidx/tools/base.py` — Change `UserInteraction.options` to `list[str | tuple[str, str, str]]`
- [ ] 8. `src/voidx/agent/graph/tool_executor.py` — `_make_interact_callback` dual-path: str-only → `ask_text`, tuple → `ask_choice`; `_other_choice_value` only for tuple path
- [ ] 9. `src/voidx/ui/tui/choice_mixin.py` — `ask_choice` accepts `list[str | tuple[str, str, str]]`; normalize str→`(str, str, "")`; `_active_choice: list[tuple[str, str, str]] | None`
- [ ] 10. `src/voidx/ui/tui/overlays.py` — Render `(label, value, desc)` from tuples; label as main text, desc as dim subtitle
- [ ] 11. `src/voidx/ui/tui/input.py` — Quick-select returns `value` from tuple
- [ ] 12. `src/voidx/ui/tui/panels.py` — `_submit_choice_selection` returns `value` (tuple[1])
- [ ] 13. `src/voidx/ui/tui/state.py` — `ChoiceState.active: list[tuple[str, str, str]] | None`
- [ ] 14. `src/voidx/ui/tui/parser.py` — Adapt choice-active checks to tuple format
- [ ] 15. `src/voidx/ui/tui/render_input.py` — Adapt cursor rendering to tuple format
- [ ] 16. `src/voidx/ui/tui/render_frame.py` — Adapt choice-active check to tuple format
- [ ] 17. `src/voidx/ui/tui/app.py` — Adapt choice-active checks to tuple format
- [ ] 18. Update TUI test files — Adapt `_active_choice` assertions back to tuple format
- [ ] 19. Run focused tests and verify

## Test Commands
```bash
# Phase 2 focused tests
.venv/bin/python -m pytest tests/test_tools/test_infer_state_patch.py tests/test_tools/test_interactive_tools_clarify.py tests/test_tools/test_tool_registry.py tests/test_tools/test_make_interact_callback.py -v

# TUI tests
.venv/bin/python -m pytest tests/test_ui/test_tui/ -v

# Full suite
.venv/bin/python -m pytest tests/ -v --timeout=60
```

## Risks
- `UserInteraction.options` Union type may produce `$defs` in Pydantic JSON schema — need to verify LLM tool schema compatibility. If it causes issues, consider a discriminator field or separate `choice_options` field.
- clarify `list[str]` options must route to `ask_text` in `_make_interact_callback`, not `ask_choice` — otherwise the UX degrades (forced selection instead of free text with suggestions).
- `_infer_state_patch` loses context-based scope heuristic — **intentional removal**. Previously, answers to questions with "scope" in context would set `GoalType.CHORE`. This heuristic was fragile (depended on the word "scope" appearing in the question text) and has been replaced by answer-based matching only. If scope-aware goal typing is needed in the future, it should be implemented as an explicit field in `ClarifyInput` rather than a text heuristic.
- `plan_checkpoint` decision values in `WorkflowEvidence.condition` and `WorkflowRunState.reason` remain machine-readable via `_DECISION_MAP` — no downstream breakage.
