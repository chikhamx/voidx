# Model-Level Context Window Design

> **Status: Done**

## Problem

`get_context_limit()` in `src/voidx/llm/provider.py` maps **provider → context window size**.
Same provider often has models with very different context windows:

| Provider | Model A | Model B | Gap |
|---|---|---|---|
| anthropic | claude-opus-4-8: 1M | claude-haiku-4-5: 200K | 5× |
| openai | gpt-5.4-mini: 1.1M | o3: 200K | 5.5× |
| zhipu | glm-5.1: 200K | glm-5: 128K | 1.6× |
| kimi | kimi-k2.6: 262K | kimi-k2: 128K | 2× |

Current code returns 200K for all anthropic models, 1.05M for all openai models, etc.
This causes:
- **Over-allocation**: small-window models get compaction triggered too late → API errors.
- **Under-allocation**: large-window models get compaction too early → wasted context.

Additionally, users who add custom models (via `/model new`) have no way to specify
context window size — they always get the provider default or 128K fallback.

## Design

### Two-layer lookup: static defaults + user override

```
get_context_limit(provider, model)
  │
  ├─ 1. User override: ModelProfileRow.context_window (if > 0)
  │
  ├─ 2. Static table: _MODEL_CONTEXT_LIMITS["provider/model"]
  │
  └─ 3. Fallback: 128_000
```

- Known models get correct values from the static table — zero config.
- Custom / unknown models can be overridden per-profile via `/model context`.
- No provider-level fallback — always resolves to a specific model or 128K.

### Layer 1: Static table (`provider.py`)

Replace the current provider-level dict with a `provider/model`-keyed dict:

```python
_MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # anthropic
    "anthropic/claude-opus-4-8": 1_000_000,
    "anthropic/claude-opus-4-7": 1_000_000,
    "anthropic/claude-sonnet-4-6": 1_000_000,
    "anthropic/claude-haiku-4-5": 200_000,
    # openai
    "openai/gpt-5.5": 1_050_000,
    "openai/gpt-5.4-mini": 1_100_000,
    "openai/gpt-5.4-nano": 400_000,
    "openai/o3": 200_000,
    "openai/o4-mini": 200_000,
    # deepseek
    "deepseek/deepseek-v4-pro": 1_000_000,
    "deepseek/deepseek-v4-flash": 1_000_000,
    # mimo
    "mimo/mimo-v2.5-pro": 1_100_000,
    "mimo/mimo-v2.5": 1_100_000,
    "mimo/mimo-v2.5-tts": 1_100_000,
    "mimo-token-plan/mimo-v2.5-pro": 1_100_000,
    "mimo-token-plan/mimo-v2.5": 1_100_000,
    "mimo-token-plan/mimo-v2.5-tts": 1_100_000,
    # qwen
    "qwen/qwen3.7-max": 1_000_000,
    "qwen/qwen3-max": 1_000_000,
    "qwen/qwen3.6-plus": 1_000_000,
    "qwen/qwen-plus": 1_000_000,
    "qwen/qwen-turbo": 1_000_000,
    # zhipu
    "zhipu/glm-5.1": 200_000,
    "zhipu/glm-5": 128_000,
    "zhipu/glm-4.7": 200_000,
    "zhipu/glm-4.7-flash": 128_000,
    # kimi
    "kimi/kimi-k2.6": 262_144,
    "kimi/kimi-k2.5": 262_144,
    "kimi/kimi-k2": 128_000,
    # doubao
    "doubao/doubao-seed-1.6-thinking": 256_000,
    "doubao/doubao-seed-1.6": 256_000,
    "doubao/doubao-seed-1.6-flash": 256_000,
    # typex
    "typex/zai-org/GLM-5-FP8": 128_000,
}

_DEFAULT_CONTEXT_LIMIT = 128_000
```

### Layer 2: User override (`ModelProfileRow`)

Add an optional `context_window` field to `ModelProfileRow` and the `model_profiles` table.
When set (> 0), it takes priority over the static table.

**Schema change** (SQLite migration):

```sql
ALTER TABLE model_profiles ADD COLUMN context_window INTEGER DEFAULT 0;
```

`0` means "use default" (not set). Positive values are explicit overrides.

**ModelProfileRow** (`memory/model_profiles.py`):

```python
class ModelProfileRow(BaseModel):
    name: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None
    context_window: int = 0  # 0 = use default
```

**Profile** (`config/models.py`):

```python
class Profile(BaseModel):
    name: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None
    context_window: int = 0  # 0 = use default
```

### `/model context` command

New sub-command following the same pattern as `/model reasoning`.
Provides preset tiers via arrow-key selection — no manual number input.

**Usage:**
- `/model context` — show current context window, prompt to change
- `/model context 200k` — set directly (shorthand)

**Preset tiers:**

| Label | Value |
|---|---|
| 128K (default) | 128,000 |
| 200K | 200,000 |
| 256K | 256,000 |
| 400K | 400,000 |
| 500K | 500,000 |
| 1M | 1,000,000 |

**Flow:**

```
/model context
  Current: auto (anthropic/claude-opus-4-8 → 1,000,000)

  Select context window:
  ❯ 128K (default)
    200K
    256K
    400K
    500K
    1M
    auto (use model default)
```

When user selects a tier, the value is saved to the current profile's `context_window`
field. Selecting "auto" resets it to `0` (use static table default).

**Shorthand parsing:** `/model context 200k` → 200,000. Accepts `k` and `m` suffixes,
case-insensitive. Invalid values show the tier selector instead.

**Implementation** (`agent/slash/model.py`):

```python
_CONTEXT_TIERS = [
    ("128K (default)", 128_000),
    ("200K", 200_000),
    ("256K", 256_000),
    ("400K", 400_000),
    ("500K", 500_000),
    ("1M", 1_000_000),
    ("auto (use model default)", 0),
]

async def _model_context(self, arg: str = "") -> None:
    # Parse shorthand if provided
    if arg:
        value = _parse_context_arg(arg)
        if value is None:
            ui.error(f"Invalid context size: '{arg}'. Use /model context to select.")
            return
    else:
        # Show current + tier selector
        current = self._get_current_context_window()
        ui.print(f"Current: {current}")
        labels = [t[0] for t in _CONTEXT_TIERS]
        idx = await _select_from_list(self._g._app, "Context window", labels)
        if idx is None:
            return
        value = _CONTEXT_TIERS[idx][1]

    # Save to profile
    await self._save_context_window(value)
    self._sync_context_limit()
    label = "auto" if value == 0 else _format_context(value)
    ui.print(f"Context window: [cyan]{label}[/cyan] [green]✓[/green]")

def _parse_context_arg(arg: str) -> int | None:
    """Parse shorthand like '200k', '1m'. Returns None on failure."""
    s = arg.strip().lower().replace(",", "")
    try:
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("k"):
            return int(float(s[:-1]) * 1_000)
        return int(s)
    except (ValueError, OverflowError):
        return None
```

**Dispatch** (`agent/slash/handler.py`):

Add to `_dispatch_model`:

```python
elif args == "context" or args.startswith("context "):
    target = args.removeprefix("context").strip()
    await self._model_context(target)
```

**Help text update:**

```
Usage: /model list|new|reasoning|context|test|del|switch|<name>
```

### New `get_context_limit` signature

```python
def get_context_limit(provider: str, model: str = "", *, context_window_override: int = 0) -> int:
    """Return context-window limit for provider/model.

    Priority: context_window_override > static table > 128K default.
    """
    if context_window_override > 0:
        return context_window_override
    if model:
        key = f"{provider}/{model}"
        return _MODEL_CONTEXT_LIMITS.get(key, _DEFAULT_CONTEXT_LIMIT)
    return _DEFAULT_CONTEXT_LIMIT
```

- `protocol` parameter removed (no longer needed).
- `context_window_override` keyword-only: passed from profile when available.
- When `model` is empty (e.g. openrouter dynamic models), returns 128K.

### Call sites

| File | Current | New |
|---|---|---|
| `agent/graph/wiring.py:63` | `get_context_limit(config.model.provider)` | `get_context_limit(config.model.provider, config.model.model, context_window_override=profile.context_window)` |
| `agent/graph/run_loop.py:134` | `get_context_limit(self.config.model.provider)` | `get_context_limit(self.config.model.provider, self.config.model.model, context_window_override=profile.context_window)` |
| `agent/slash/model.py:412` | `get_context_limit(self._g.config.model.provider)` | `get_context_limit(self._g.config.model.provider, self._g.config.model.model, context_window_override=profile.context_window)` |

Note: call sites need access to the current profile's `context_window`. This is available
from `settings.resolve_profile()` or from the graph's stored profile reference.

### `/model new` flow — no change

The `/model new` flow does NOT prompt for context window. Users set it separately
via `/model context` after the model is configured. This keeps the new-model flow
simple and focused.

### What does NOT change

- `CompactionService` interface — only the `context_limit` value passed in changes.
- `ModelConfig.max_tokens` — output token limit, unrelated.
- `catalog.py` — OpenRouter dynamic fetch still uses `context_length` for filtering only.
- `_PROVIDER_PROTOCOLS` dict — still needed for protocol resolution, unrelated.

### OpenRouter handling

OpenRouter models are fetched dynamically and are not in the static table.
When `model` is empty or not found, `get_context_limit` returns 128K.
Users can override via `/model context`.

Future enhancement: persist `context_length` from OpenRouter's `/models` response
into the profile automatically.

### Data sources

All context window values verified against official docs and OpenRouter model pages
as of 2026-06-07. Values should be updated when providers release new models or
change context windows.

## Implementation steps

1. **`provider.py`** — Replace provider-level dict with `_MODEL_CONTEXT_LIMITS`,
   update `get_context_limit` signature and logic.
2. **`config/models.py`** — Add `context_window: int = 0` to `Profile`.
3. **`memory/model_profiles.py`** — Add `context_window: int = 0` to `ModelProfileRow`,
   update SQL queries and `_row_to_profile`.
4. **`memory/store.py`** — Add migration: `ALTER TABLE model_profiles ADD COLUMN context_window INTEGER DEFAULT 0`.
5. **`config/settings.py`** — Update `save_profile` and `_get_profile` to include `context_window`.
6. **`agent/slash/model.py`** — Add `_model_context` method with tier selector,
   update `_sync_context_limit`, update help text.
7. **`agent/slash/handler.py`** — Add `context` dispatch in `_dispatch_model`.
8. **`agent/graph/wiring.py`** — Update `build_compaction_service` call.
9. **`agent/graph/run_loop.py`** — Update `get_context_limit` call.
10. **Tests** — Add unit tests for `get_context_limit` (exact match, override, fallback),
    `_parse_context_arg`, and `_model_context` flow.
11. **Update design doc status** — Move to `docs/archive/` when complete.
