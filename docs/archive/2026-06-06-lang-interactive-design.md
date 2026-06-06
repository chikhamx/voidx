# `/lang` & `/tone` Interactive Selection Design

> **Status: Done**

## Problem

`/lang` and `/tone` currently accept only free-text input. Users must know the exact values beforehand — there is no interactive selection, no discoverability of supported options, and `auto`/`default` are exposed as values when they really mean "unset".

### `/lang` Current Behavior

| Input | Result |
|-------|--------|
| `/lang` | Prints current language (e.g. `Language: auto`) |
| `/lang zh-CN` | Sets language directly |
| `/lang auto` | Resets to auto-detect |

### `/lang` Issues

1. **No interactive selection** — unlike `/model new` which offers arrow-key pickers, `/lang` requires knowing the exact code
2. **`auto` is not a language** — it means "no preference", but it appears as a language option in the command palette
3. **Supported languages are hidden** — `_LANGUAGE_LABELS` defines 7 languages (zh-CN, zh, zh-TW, en, en-US, ja, ko) but users can't discover them
4. **Free-text is error-prone** — typing `chinese` or `中文` doesn't work; only BCP 47 codes like `zh-CN` are recognized

### `/tone` Current Behavior

| Input | Result |
|-------|--------|
| `/tone` | Prints current tone (e.g. `Tone: default`) |
| `/tone direct` | Sets tone directly |
| `/tone default` | Resets to default |

### `/tone` Issues

1. **No interactive selection** — same problem as `/lang`
2. **No predefined tone list** — tone is completely free-text; users don't know what's expected
3. **`default` is not a tone** — it means "no preference", but appears as a tone value
4. **Vague prompt injection** — the system prompt says `Keep the response tone {tone}`, but users have no guidance on what values produce good results

## `/lang` Design

### New Behavior

| Input | Result |
|-------|--------|
| `/lang` | Opens interactive picker with supported languages |
| `/lang zh-CN` | Sets language directly (shortcut, unchanged) |

### Interactive Picker

When the user types `/lang` with no arguments, show an arrow-key selectable list:

```
 Language
 ─────────────────────────────
 ❯ Chinese (Simplified) [zh-CN]
   Chinese (Traditional) [zh-TW]
   Chinese [zh]
   English [en]
   English [en-US]
   Japanese [ja]
   Korean [ko]
   Other (enter manually)
   Reset (auto-detect)
```

- **Supported languages** — populated from `_LANGUAGE_LABELS`, displayed as `Name [code]`
- **Other (enter manually)** — falls through to a text prompt for custom BCP 47 codes
- **Reset (auto-detect)** — clears the language preference (equivalent to current `/lang auto`)

### Command Palette Changes

Remove `/lang auto` from the command list — it's now a picker option, not a standalone command:

```python
# Before
("/lang", "Set response language preference"),
("/lang auto", "Auto-detect response language"),

# After
("/lang", "Set response language preference"),
```

### Fallback Without TUI

When `app` is not available (headless / non-interactive), fall back to a text prompt:

```
Language: [current: auto]
Available: zh-CN, zh-TW, zh, en, en-US, ja, ko
Enter language code (or 'auto' to reset):
```

## `/tone` Design

### New Behavior

| Input | Result |
|-------|--------|
| `/tone` | Opens interactive picker with predefined tones |
| `/tone direct` | Sets tone directly (shortcut, unchanged) |

### Predefined Tone Options

Since tone currently has no predefined list, we need to define one. These are the tones that produce meaningful differences in LLM output:

| Value | Display | Prompt Instruction |
|-------|---------|--------------------|
| `concise` | Concise - short and to the point | Prefer short answers. Remove filler and avoid restating obvious context. |
| `friendly` | Friendly - warm and approachable | Keep phrasing warm and approachable while staying concrete. |
| `formal` | Formal - professional and structured | Use polished, structured phrasing and avoid casual wording. |
| `direct` | Direct - straightforward, no fluff | Be direct and practical. Lead with the answer or action. |
| `technical` | Technical - precise, uses domain terminology | Use precise domain terminology. Prefer concrete specs and implementation details over broad summaries. |
| `casual` | Casual - relaxed and conversational | Use relaxed conversational phrasing without losing technical accuracy. |

### Interactive Picker

When the user types `/tone` with no arguments:

```
 Tone
 ─────────────────────────────
 ❯ Concise - short and to the point
   Friendly - warm and approachable
   Formal - professional and structured
   Direct - straightforward, no fluff
   Technical - precise, uses domain terminology
   Casual - relaxed and conversational
   Other (enter manually)
   Reset (default)
```

- **Predefined tones** — curated list with descriptions
- **Other (enter manually)** — falls through to a text prompt for custom tone values
- **Reset (default)** — clears the tone preference

### Tone Label Registry

Define a `_TONE_LABELS` dict (mirroring `_LANGUAGE_LABELS`) in `runtime_context.py`. Include the prompt instruction so predefined tones do more than interpolate the raw tone value:

```python
_TONE_LABELS: dict[str, tuple[str, str, str]] = {
    "concise": (
        "Concise",
        "short and to the point",
        "Prefer short answers. Remove filler and avoid restating obvious context.",
    ),
    "friendly": (
        "Friendly",
        "warm and approachable",
        "Keep phrasing warm and approachable while staying concrete.",
    ),
    "formal": (
        "Formal",
        "professional and structured",
        "Use polished, structured phrasing and avoid casual wording.",
    ),
    "direct": (
        "Direct",
        "straightforward, no fluff",
        "Be direct and practical. Lead with the answer or action.",
    ),
    "technical": (
        "Technical",
        "precise, uses domain terminology",
        "Use precise domain terminology. Prefer concrete specs and implementation details over broad summaries.",
    ),
    "casual": (
        "Casual",
        "relaxed and conversational",
        "Use relaxed conversational phrasing without losing technical accuracy.",
    ),
}
```

Each entry maps `(value, (display_name, description, instruction))`. The picker shows `display_name - description` using an ASCII separator. Runtime context uses the predefined `instruction` when the tone value is known; custom tones fall back to the existing generic instruction.

### Command Palette Changes

Remove `/tone default` from the command list:

```python
# Before
("/tone", "Set response tone preference"),
("/tone default", "Use default response tone"),

# After
("/tone", "Set response tone preference"),
```

### Fallback Without TUI

When `app` is not available:

```
Tone: [current: default]
Available: concise, friendly, formal, direct, technical, casual
Enter tone (or 'default' to reset):
```

## Implementation Plan

### Step 1: Add `_TONE_LABELS` to `runtime_context.py`

File: `src/voidx/agent/runtime_context.py`

Add the tone label registry alongside `_LANGUAGE_LABELS`.

### Step 2: Refactor slash prompt handling

File: `src/voidx/agent/slash/runtime.py`, `src/voidx/agent/slash/handler.py`, `src/voidx/agent/slash/model.py`

`_prompt()` currently lives on `SlashModelMixin`, but profile, MCP, model, and future slash mixins all need the same text prompt behavior. Move the implementation to shared slash infrastructure:

```python
# runtime.py
async def prompt_text(app, text: str, default: str = "", secret: bool = False) -> str | None:
    if app is not None and hasattr(app, "ask_text"):
        return await app.ask_text(text, default=default, secret=secret)
    ...


# handler.py
async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
    return await prompt_text(self._host_app(), text, default=default, secret=secret)
```

Remove the `_prompt()` implementation from `SlashModelMixin` after the handler wrapper exists. Existing calls in model and MCP mixins keep using `self._prompt(...)`.

`_host_app()` and `_host_settings()` already live on `SlashHandler`; `SlashProfileMixin` should keep using those handler adapter methods rather than reaching into `self._g._app` or `self._g._settings` directly.

### Step 3: Refactor `SlashProfileMixin` — shared interactive pattern

File: `src/voidx/agent/slash/profile.py`

Both `/lang` and `/tone` share the same interactive pattern (picker -> other -> reset). Extract a shared helper. Do not add synthetic sentinel values to the values list; use the selected index to identify the appended actions.

```python
async def _pick_or_reset(
    self,
    title: str,
    option_items: list[str],
    values: list[str],
    prompt_label: str,
    other_label: str,
    reset_label: str,
) -> str | None:
    """Show picker with items, handle Other/Reset. Return selected value or None."""
    items = [*option_items, other_label, reset_label]
    app = self._host_app()
    idx = await _select_from_list(app, title, items)
    if idx is None:
        ui.print("[dim]Cancelled.[/dim]")
        return None
    if idx == len(values):
        result = await self._prompt(prompt_label)
        if result is None or not result.strip():
            ui.print("[dim]Cancelled.[/dim]")
            return None
        return result.strip()
    if idx == len(values) + 1:
        return ""
    return values[idx]
```

Then `_lang` and `_tone` both delegate to it:

```python
async def _lang(self, args: str) -> None:
    value = args.strip()
    if not value:
        await self._lang_interactive()
        return
    self._apply_language(value)

async def _lang_interactive(self) -> None:
    from voidx.agent.runtime_context import _LANGUAGE_LABELS
    items, codes = [], []
    for code, (name, tag) in _LANGUAGE_LABELS.items():
        items.append(f"{name} [{tag}]")
        codes.append(code)
    if self._host_app() is None:
        await self._lang_headless(codes)
        return
    selected = await self._pick_or_reset(
        "Language",
        items,
        codes,
        "Language code (e.g. fr, de, pt-BR; auto to reset)",
        "Other (enter manually)",
        "Reset (auto-detect)",
    )
    if selected is not None:
        self._apply_language(selected)

async def _tone(self, args: str) -> None:
    value = args.strip()
    if not value:
        await self._tone_interactive()
        return
    self._apply_tone(value)

async def _tone_interactive(self) -> None:
    from voidx.agent.runtime_context import _TONE_LABELS
    items, values = [], []
    for val, (name, desc, _instruction) in _TONE_LABELS.items():
        items.append(f"{name} - {desc}")
        values.append(val)
    if self._host_app() is None:
        await self._tone_headless(values)
        return
    selected = await self._pick_or_reset(
        "Tone",
        items,
        values,
        "Tone (e.g. patient, enthusiastic; default to reset)",
        "Other (enter manually)",
        "Reset (default)",
    )
    if selected is not None:
        self._apply_tone(selected)

def _apply_language(self, value: str) -> None:
    settings = self._host_settings()
    if settings is not None:
        settings.set_user_language(value)
        profile = settings.get_user_profile()
    else:
        profile = self._current_user_profile()
        profile.language = _normalize_language(value)
    self._set_current_user_profile(profile)
    label = profile.language or "auto-detect"
    ui.print(f"Language: [cyan]{label}[/cyan] [green]✓[/green]")

def _apply_tone(self, value: str) -> None:
    settings = self._host_settings()
    if settings is not None:
        settings.set_user_tone(value)
        profile = settings.get_user_profile()
    else:
        profile = self._current_user_profile()
        profile.tone = _normalize_tone(value)
    self._set_current_user_profile(profile)
    label = profile.tone or "default"
    ui.print(f"Tone: [cyan]{label}[/cyan] [green]✓[/green]")
```

Reset semantics:

- `_pick_or_reset()` returns `""` for reset.
- `_apply_language("")` calls `Settings.set_user_language("")`; settings normalization clears `userProfile.language` rather than ignoring the update.
- `_apply_tone("")` calls `Settings.set_user_tone("")`; settings normalization clears `userProfile.tone` rather than ignoring the update.

### Step 4: Update command palette

File: `src/voidx/ui/commands.py`

Remove `/lang auto` and `/tone default` entries.

### Step 5: Add headless fallback

`_select_from_list()` returns `None` when `app` is not available, so headless mode must branch before calling `_pick_or_reset()`.

```python
async def _lang_headless(self, codes: list[str]) -> None:
    ui.print(f"Language: [cyan]{self._current_language_label()}[/cyan]")
    ui.print(f"[dim]Available: {', '.join(codes)}[/dim]")
    value = await self._prompt("Language code (or 'auto' to reset)", default="")
    if value is None:
        ui.print("[dim]Cancelled.[/dim]")
        return
    self._apply_language(value)


async def _tone_headless(self, values: list[str]) -> None:
    ui.print(f"Tone: [cyan]{self._current_tone_label()}[/cyan]")
    ui.print(f"[dim]Available: {', '.join(values)}[/dim]")
    value = await self._prompt("Tone (or 'default' to reset)", default="")
    if value is None:
        ui.print("[dim]Cancelled.[/dim]")
        return
    self._apply_tone(value)
```

### Step 6: Tests

- Test `/lang` with no args opens picker
- Test `/tone` with no args opens picker
- Test picker selection sets value correctly for both
- Test "Other" option prompts for manual input
- Test "Reset" option clears value
- Test `/lang zh-CN` and `/tone direct` shortcuts still work
- Test headless fallback prints available options

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/agent/runtime_context.py` | Add `_TONE_LABELS` dict and use specific instructions for known tones |
| `src/voidx/agent/slash/runtime.py` | Add shared text prompt helper |
| `src/voidx/agent/slash/handler.py` | Add `_prompt()` wrapper using `_host_app()` |
| `src/voidx/agent/slash/model.py` | Remove mixin-local `_prompt()` implementation |
| `src/voidx/agent/slash/profile.py` | Refactor `_lang`/`_tone` with interactive pickers, shared `_pick_or_reset` helper |
| `src/voidx/ui/commands.py` | Remove `/lang auto` and `/tone default` entries |
| `tests/` | New tests for interactive lang and tone selection |

## Out of Scope

- Renaming `_LANGUAGE_LABELS` / `_TONE_LABELS` to public names (cosmetic, separate PR)
- Adding more languages to `_LANGUAGE_LABELS` (content change, not UX)
- Adding more tones to `_TONE_LABELS` (can be done incrementally)
- Persisting language/tone across sessions (already works via settings)
