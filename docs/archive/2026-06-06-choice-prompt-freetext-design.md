# Choice Prompt Free-Text Input Design

> **Status: Done**

## Problem

When `clarify` or `plan_checkpoint` present options to the user, the TUI only shows a selectable list — there is no way to type a custom answer. The user is forced to pick from predefined choices even when none of them fit.

### Root Cause

`_make_interact_callback()` in `tool_execution.py:314-322` routes `UserInteraction` with options exclusively to `ask_choice()`:

```python
async def interact(request: UserInteraction) -> UserResponse:
    timeout = request.timeout
    if request.options:
        result = await app.ask_choice(request.prompt, request.options, timeout=timeout)
    else:
        result = await app.ask_text(request.prompt, timeout=timeout)
    if result is None:
        return UserResponse(value="", cancelled=True)
    return UserResponse(value=result)
```

When `request.options` is non-empty, the user gets a choice overlay with arrow-key selection and Enter to confirm. No text input is possible.

### Affected Interactions

This change applies only to tool `UserInteraction` requests routed through
`ToolContext.interact` and `_make_interact_callback()`.

| Tool | Options | Current Behavior | Problem |
|------|---------|------------------|---------|
| `clarify` | User-defined (often 2-4) | Only selection | User can't type a custom answer |
| `plan_checkpoint` | Approve / Modify scope / Reject | Only selection | "Modify scope" triggers a second `ask_text`, but the user must know to pick that option first |

Permission prompts are not affected: they call `_app.ask_choice()` directly in
`permissions.py` and must stay binary/explicit. Slash command pickers such as
`/lang`, `/tone`, and model selection already have their own manual-entry flow.

### User Expectation

Users expect to be able to type their own answer at any choice prompt, similar to how `/lang` and `/tone` already offer an "Other (enter manually)" option.

## Design

### Approach: Append "Other" Option with ask_text Fallback

Add an "Other (type your answer)" option at the end of every tool choice list.
When selected, switch to `ask_text()` for free-form input.

This mirrors the existing `_pick_or_reset()` pattern in `SlashProfileMixin` and requires minimal code changes.

### Change

`UserResponse` needs a source flag so tools can distinguish a selected option
from free text, even if the typed text happens to equal an option value:

```python
class UserResponse(BaseModel):
    value: str
    cancelled: bool = False
    free_text: bool = False
```

`_make_interact_callback()` appends a collision-safe Other option:

```python
_OTHER_VALUE_PREFIX = "__voidx_choice_prompt_other__"


def _other_value(options: list[tuple[str, str, str]]) -> str:
    used = {value for _, value, _ in options}
    value = _OTHER_VALUE_PREFIX
    index = 1
    while value in used:
        value = f"{_OTHER_VALUE_PREFIX}_{index}"
        index += 1
    return value

def _make_interact_callback(app):
    if app is None:
        return None

    async def interact(request: UserInteraction) -> UserResponse:
        timeout = request.timeout
        if request.options:
            other_value = _other_value(request.options)
            options = [*request.options, ("Other (type your answer)", other_value, "")]
            result = await app.ask_choice(request.prompt, options, timeout=timeout)
            if result == other_value:
                result = await app.ask_text(request.prompt, timeout=timeout)
                if result is None:
                    return UserResponse(value="", cancelled=True)
                return UserResponse(value=result, free_text=True)
        else:
            result = await app.ask_text(request.prompt, timeout=timeout)
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result, free_text=True)
        if result is None:
            return UserResponse(value="", cancelled=True)
        return UserResponse(value=result)

    return interact
```

### Why This Works

- `ask_choice` takes `list[tuple[str, str, str]]` — the "Other" entry is just another `(label, value, description)` tuple. No API changes needed.
- The choice overlay renderer (`_render_choice_overlay`) already handles arbitrary-length lists. The "Other" option renders like any other choice.
- When the user selects "Other", `ask_text()` is called with the same prompt. The text prompt UI already supports multiline input and Enter to submit.
- The sentinel is generated from a reserved prefix and checked against existing option values before use, so a real option cannot accidentally trigger the free-text fallback.
- `UserResponse.free_text` preserves input provenance for tools that must treat typed text differently from selected values.

### Interaction Flow

**Before:**
```
? Do you want to proceed?
  ❯ Yes, proceed
    No, cancel
```
User presses Enter → `value="yes"` → done.

**After:**
```
? Do you want to proceed?
  ❯ Yes, proceed
    No, cancel
    Other (type your answer)
```
User selects "Other" → text prompt appears → user types custom answer → `value="custom text"` → done.

### plan_checkpoint Interaction

`plan_checkpoint` already has a two-step flow for "Modify scope": first the choice, then a text prompt. With this change:

- "Approve" → proceeds as before
- "Modify scope" → triggers `ask_text` as before (via explicit second `ctx.interact` call)
- "Reject" → proceeds as before
- "Other (type your answer)" → triggers `ask_text` with the plan prompt, then `plan_checkpoint` treats the response as `decision="modified"` with `modified_scope=<typed text>`

`plan_checkpoint` must never treat free-text as plain `approved`. A typed
response is approval with modifications, because the user is supplying a scope
or constraint that should be preserved in `user_feedback`, `modified_scope`,
and the `state_patch.goal`.

The "Other" option and "Modify scope" serve different purposes: "Modify scope"
is an explicit structured branch, while "Other" is a shortcut for entering the
modified scope immediately. Both produce `decision="modified"`.

### clarify Interaction

For `clarify`, free text should be recorded as the answer but not as a selected
option:

- selected predefined option → `answer=<value>`, `selected_option=<value>`
- Other/free text → `answer=<typed text>`, `selected_option=None`

### Headless / Non-Interactive Path

When `app` is `None`, `_make_interact_callback` returns `None` and the tool returns a "not available" result. No change needed.

### Web UI / Desktop UI

The protocol does not need a new request type. The backend may send a choice
request and then a text request in sequence when "Other" is selected. The web
gateway and desktop shell must be covered by tests that prove consecutive
`choice -> text` requests are handled through the existing `UiChoiceRequest`,
`UiTextRequest`, and `UiResponse` flow.

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/tools/base.py` | Add `UserResponse.free_text` |
| `src/voidx/agent/graph/tool_execution.py` | Append collision-safe "Other" option in `_make_interact_callback()`, add `ask_text` fallback |
| `src/voidx/tools/clarify.py` | Do not mark free-text answers as selected options |
| `src/voidx/tools/plan_checkpoint.py` | Treat free-text answers as `decision="modified"` |
| `tests/` | Cover Other option, sentinel collision, clarify free text, plan_checkpoint free text, and gateway choice->text flow |

## Test Plan

- `_make_interact_callback()` appends `Other (type your answer)` to tool choice prompts.
- Selecting Other calls `ask_text()` and returns `UserResponse(free_text=True)`.
- Existing option values that collide with the reserved sentinel still select normally.
- `clarify` selected option keeps `selected_option`; clarify free text leaves `selected_option=None`.
- `plan_checkpoint` free text produces `plan_decision="modified"`, not `approved`.
- Gateway/protocol can service consecutive `UiChoiceRequest` then `UiTextRequest`.

## Out of Scope

- Allowing the agent to control whether "Other" appears via a `UserInteraction` field — over-engineering for now
- Changing the `plan_checkpoint` "Modify scope" flow — it already works well
