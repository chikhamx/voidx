# Refactor: Simplify clarify tool

## Goal
Remove unnecessary fields from clarify tool: `context`, `blocking` from input; simplify `ClarifyOption` to `str`; remove `selected_option` from result.

## Architecture
- `ClarifyInput.options: list[str]` — label = value, no description
- `UserInteraction.options: list[str]` — same, label = value
- TUI layer (`ask_choice`, `_active_choice`, overlays) adapts to `list[str]` internally, treating each string as both label and value
- `plan_checkpoint` options become `list[str]`, label = value; internal `decision` field keeps machine-readable values via `_DECISION_MAP`
- `_infer_state_patch` drops context-based heuristic, only matches answer against intent map

### plan_checkpoint decision mapping
`plan_checkpoint` uses natural-language labels as option values, but its internal `decision` field and workflow evidence must keep machine-readable values. A thin mapping dict handles this:

```python
_DECISION_MAP = {
    "Implement directly": "approved",
    "Document first": "needs_doc",
    "Modify scope": "modified",
    "Reject": "rejected",
}
```

`response.value` is the natural-language label; `decision` is looked up from `_DECISION_MAP`. This keeps `WorkflowEvidence.condition` (e.g. `checkpoint_approved`) and `WorkflowRunState.reason` (e.g. `checkpoint:approved`) stable.

## File Structure

| File | Change |
|---|---|
| `src/voidx/tools/clarify.py` | Delete `ClarifyOption`; `options: list[str]`; remove `context`/`blocking`; remove `selected_option`/`_selected_option`; simplify `_prompt`/`_infer_state_patch` |
| `src/voidx/tools/base.py` | `UserInteraction.options: list[str]`; remove `blocking` |
| `src/voidx/agent/graph/tool_executor.py` | `_make_interact_callback` adapt to `list[str]`; `_other_choice_value` adapt |
| `src/voidx/tools/plan_checkpoint.py` | options from tuples to `list[str]`; add `_DECISION_MAP`; branch on label, map to decision |
| `src/voidx/ui/tui/choice_mixin.py` | `ask_choice` takes `list[str]`; `_active_choice: list[str] \| None` |
| `src/voidx/ui/tui/overlays.py` | render from `list[str]` instead of tuples |
| `src/voidx/ui/tui/input.py` | quick-select from `list[str]` |
| `src/voidx/ui/tui/panels.py` | `_submit_choice_selection` from `list[str]` |
| `src/voidx/ui/tui/state.py` | `_active_choice` / `ChoiceState.active` type annotation |
| `src/voidx/ui/tui/parser.py` | choice-active checks adapt to `list[str]` |
| `src/voidx/ui/tui/render_input.py` | cursor rendering when choice active adapts |
| `src/voidx/ui/tui/render_frame.py` | choice-active check adapts |
| `src/voidx/ui/tui/app.py` | choice-active checks adapt |
| `tests/test_tools/test_infer_state_patch.py` | Remove context test; adapt ClarifyInput usage |
| `tests/test_tools/test_interactive_tools_clarify.py` | Adapt to new format; update plan_decision assertions |
| `tests/test_tools/test_tool_registry.py` | Remove `ClarifyOption` schema assertion |
| `tests/test_tools/test_make_interact_callback.py` | Adapt to `list[str]` options |
| `tests/test_agent/test_tool_execution_auth.py` | Update `needs_doc` decision assertion |
| Other test files importing `ClarifyOption` (14 files) | Remove `ClarifyOption` from imports |
| Other TUI test files using `_active_choice` tuples (6 files) | Adapt to `list[str]` |

## Tasks

- [ ] 1. `src/voidx/tools/clarify.py` — Delete `ClarifyOption`; `options: list[str]`; remove `context`/`blocking` from `ClarifyInput`; remove `selected_option` from `ClarifyResult`; delete `_selected_option`; simplify `_prompt` to just return `inp.question`; simplify `_infer_state_patch` to drop context heuristic; update `execute` to pass `options` directly
- [ ] 2. `src/voidx/tools/base.py` — `UserInteraction.options: list[str]`; remove `blocking`
- [ ] 3. `src/voidx/agent/graph/tool_executor.py` — Adapt `_make_interact_callback` and `_other_choice_value` to `list[str]`
- [ ] 4. `src/voidx/tools/plan_checkpoint.py` — Change options to `list[str]`; add `_DECISION_MAP`; update `execute` to branch on label and map to decision
- [ ] 5. `src/voidx/ui/tui/choice_mixin.py` — `ask_choice` takes `list[str]`; `_active_choice: list[str] | None`
- [ ] 6. `src/voidx/ui/tui/overlays.py` — Render choices from `list[str]`
- [ ] 7. `src/voidx/ui/tui/input.py` — Quick-select from `list[str]`
- [ ] 8. `src/voidx/ui/tui/panels.py` — `_submit_choice_selection` from `list[str]`
- [ ] 9. `src/voidx/ui/tui/state.py` — Update `ChoiceState.active` type annotation
- [ ] 10. `src/voidx/ui/tui/parser.py` — Adapt choice-active checks
- [ ] 11. `src/voidx/ui/tui/render_input.py` — Adapt cursor rendering
- [ ] 12. `src/voidx/ui/tui/render_frame.py` — Adapt choice-active check
- [ ] 13. `src/voidx/ui/tui/app.py` — Adapt choice-active checks
- [ ] 14. Update all test files — Remove `ClarifyOption` imports; adapt `ClarifyInput` usage; remove context test; remove `ClarifyOption` schema assertion; update plan_decision assertions; adapt `_active_choice` tuples
- [ ] 15. Run focused tests and verify

## Test Commands
```bash
.venv/bin/python -m pytest tests/test_tools/test_infer_state_patch.py tests/test_tools/test_interactive_tools_clarify.py tests/test_tools/test_tool_registry.py tests/test_tools/test_make_interact_callback.py -v
.venv/bin/python -m pytest tests/ -v --timeout=60
```

## Risks
- TUI choice rendering loses description text — acceptable since clarify options are short labels and plan_checkpoint labels are self-explanatory
- `_infer_state_patch` loses context-based scope heuristic — **functional regression** (previously, answers to questions with "scope" in context would set `GoalType.CHORE`); answer-based matching is cleaner but cannot recover this behavior
- `plan_checkpoint` decision values in `WorkflowEvidence.condition` and `WorkflowRunState.reason` remain machine-readable via `_DECISION_MAP` — no downstream breakage
- 14 test files import `ClarifyOption` only for side-effect-free import checks — bulk removal needed but low risk
