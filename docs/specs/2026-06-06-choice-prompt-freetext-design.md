# Choice Prompt Free-Text Input Design

> **Status: Draft**

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

### Affected Tools

| Tool | Options | Current Behavior | Problem |
|------|---------|-----------------|---------|
| `clarify` | User-defined (often 2-4) | Only selection | User can't type a custom answer |
| `plan_checkpoint` | Approve / Modify scope / Reject | Only selection | "Modify scope" triggers a second `ask_text`, but the user must know to pick that option first |
| Permission prompts | Allow / Deny | Only selection | Less critical — these are binary decisions |
| `/lang`, `/tone` pickers | Predefined + Other + Reset | Already has "Other" | Already solved via `_pick_or_reset()` |

### User Expectation

Users expect to be able to type their own answer at any choice prompt, similar to how `/lang` and `/tone` already offer an "Other (enter manually)" option.

## Design

### Approach: Append "Other" Option with ask_text Fallback

Add an "Other (type your answer)" option at the end of every choice list. When selected, switch to `ask_text()` for free-form input.

This mirrors the existing `_pick_or_reset()` pattern in `SlashProfileMixin` and requires minimal code changes.

### Change

Only `_make_interact_callback()` in `tool_execution.py` needs modification:

```python
_OTHER_VALUE = "__other__"

def _make_interact_callback(app):
    if app is None:
        return None

    async def interact(request: UserInteraction) -> UserResponse:
        timeout = request.timeout
        if request.options:
            options = [*request.options, ("Other (type your answer)", _OTHER_VALUE, "")]
            result = await app.ask_choice(request.prompt, options, timeout=timeout)
            if result == _OTHER_VALUE:
                result = await app.ask_text(request.prompt, timeout=timeout)
        else:
            result = await app.ask_text(request.prompt, timeout=timeout)
        if result is None:
            return UserResponse(value="", cancelled=True)
        return UserResponse(value=result)

    return interact
```

### Why This Works

- `ask_choice` takes `list[tuple[str, str, str]]` — the "Other" entry is just another `(label, value, description)` tuple. No API changes needed.
- The choice overlay renderer (`_render_choice_overlay`) already handles arbitrary-length lists. The "Other" option renders like any other choice.
- When the user selects "Other", `ask_text()` is called with the same prompt. The text prompt UI already supports multiline input and Enter to submit.
- The sentinel value `"__other__"` is unlikely to collide with real option values. If it ever does, the worst case is that selecting that real option accidentally triggers the text prompt — a minor UX glitch, not a crash.

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
- "Other (type your answer)" → triggers `ask_text` with the plan prompt, giving the user a free-form way to respond

The "Other" option and "Modify scope" serve different purposes: "Modify scope" is a structured follow-up that records the decision as "modified", while "Other" lets the user type anything. Both are useful.

### Headless / Non-Interactive Path

When `app` is `None`, `_make_interact_callback` returns `None` and the tool returns a "not available" result. No change needed.

### Web UI / Desktop UI

The web gateway and desktop shell also call `ask_choice` / `ask_text` through the same `app` interface. The "Other" option appears in their choice overlays too, and the `ask_text` fallback works the same way. No additional changes needed.

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/agent/graph/tool_execution.py` | Append "Other" option in `_make_interact_callback()`, add `ask_text` fallback |
| `tests/` | Test that choice prompts include "Other" and that selecting it triggers text input |

## Out of Scope

- Making "Other" option conditional (e.g. only show for `clarify` but not permission prompts) — adds complexity for marginal benefit
- Allowing the agent to control whether "Other" appears via a `UserInteraction` field — over-engineering for now
- Changing the `plan_checkpoint` "Modify scope" flow — it already works well
