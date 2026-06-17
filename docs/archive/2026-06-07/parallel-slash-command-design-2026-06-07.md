# /parallel Slash Command Design

> **Status: Done**

Date: 2026-06-07

## Problem

Parallel subagent execution is controlled by `ParallelSubagentsConfig` in
settings, defaulting to `enabled: false`. The only way to enable it today is
manually editing `.voidx/settings.json`. Users need a convenient runtime toggle,
similar to `/debug on|off`, but this feature does not need to affect the
currently running graph immediately.

## Current State

| File | Role |
|------|------|
| `src/voidx/config/models.py:121-125` | `ParallelSubagentsConfig(enabled=False, max_concurrent=4)` |
| `src/voidx/config/settings_agent.py:22-33` | `get_parallel_subagents()` / `set_parallel_subagents()` on Settings |
| `src/voidx/agent/graph/tool_execution.py:334-342` | `_parallel_subagent_limit()` reads `config.parallel_subagents.enabled` per tool execution |
| `src/voidx/agent/graph/core.py:415` | `role_prompt_for_llm()` reads `config.parallel_subagents.enabled` per turn |
| `src/voidx/agent/graph/wiring.py:41-48` | `AgentTool` description is built from config once at registration time |
| `src/voidx/agent/slash/handler.py:446-459` | `/debug` toggle pattern — updates in-memory state + prints result |

The executor and role prompt read `config.parallel_subagents.enabled`, but the
`agent` tool description is registered once. To avoid partial live state where
execution, prompt, and tool description disagree, `/parallel` should persist the
setting only. The saved value is applied when the graph reloads configuration:
after `/clear` or after restarting voidx.

## Design

### Add `/parallel` command

Follow the `/debug` toggle pattern:

- `/parallel` — toggle on/off
- `/parallel on` — enable
- `/parallel off` — disable
- `/parallel status` — show current state and max_concurrent

When toggled on or off:

1. Read the saved `ParallelSubagentsConfig` from settings.
2. Save a copy with `enabled` changed via `settings.set_parallel_subagents()`.
3. Do not mutate `graph.config`, do not rebuild the tool registry, and do not
   affect any currently running or already-built graph state.
4. Print the saved state and tell the user to run `/clear` or restart to apply.

`/parallel status` should show both the active in-memory state and the saved
state when they differ. This avoids confusion after a user toggles the setting
but has not reloaded the graph yet.

`max_concurrent` is not exposed as a command argument in V1. Users who need to
change it can still edit settings directly. The command focuses on the common
toggle use case.

### Changes

#### 1. `handler.py` — add `_parallel` method

```python
def _parallel(self, arg: str) -> None:
    value = arg.strip().lower()
    settings = self._host_settings()
    if settings is None:
        ui.error("No settings available.")
        return

    active = self._g.config.parallel_subagents
    saved = settings.get_parallel_subagents()

    if value in ("on", "true", "1", "yes"):
        new_enabled = True
    elif value in ("off", "false", "0", "no"):
        new_enabled = False
    elif value == "status":
        self._print_parallel_status(active, saved)
        return
    elif value:
        ui.error("Usage: /parallel [on|off|status]")
        return
    else:
        new_enabled = not saved.enabled

    saved = saved.model_copy(update={"enabled": new_enabled})
    settings.set_parallel_subagents(saved)

    state = "on" if new_enabled else "off"
    ui.print(
        f"[dim]Saved parallel subagents {state} "
        f"(max_concurrent={saved.max_concurrent}). "
        "Run /clear or restart to apply.[/dim]"
    )
```

#### 2. `handler.py` — register in dispatch table

Add to the `handlers` dict:

```python
"/parallel": lambda: self._parallel(args),
```

#### 3. `commands.py` — add command palette entries

```python
("/parallel", "Toggle parallel subagent execution"),
("/parallel off", "Disable parallel subagent execution"),
("/parallel on", "Enable parallel subagent execution"),
("/parallel status", "Show parallel subagent config"),
```

#### 4. `/clear` and `/resume` — apply saved parallel setting

`/clear` already resets the current session and runtime state. Extend the graph
clear path to reload the saved `parallel_subagents` config and re-register the
`agent` tool definition. This ensures the next turn sees a consistent state:

- `config.parallel_subagents`
- dynamic role prompt text
- `agent` tool description
- executor semaphore behavior

Do not reload unrelated settings in this change. A full settings reload would
touch model, permission, MCP, and provider state and is outside this command's
scope.

`/resume` should call the same parallel-subagent reload helper after restoring
session runtime state. The user-facing `/parallel` prompt remains focused on
`/clear` or restart because those are the explicit ways to apply the setting
without changing sessions.

### What does NOT change

- **`max_concurrent`** — not adjustable via command in V1.
- **`_parallel_subagent_limit()`** — no logic change, still reads from config.
- **`role_prompt_for_llm()`** — no logic change, still reads from config.
- **Settings file schema** — no new fields, reuses existing `ParallelSubagentsConfig`.
- **Active graph state during `/parallel`** — no live mutation of config or tool
  definitions until `/clear` or restart.

### Testing

| Test | Description |
|------|-------------|
| `test_parallel_toggle_on_persists_without_live_config_update` | `/parallel on` saves enabled=True but leaves current graph config unchanged |
| `test_parallel_toggle_off_persists_without_live_config_update` | `/parallel off` saves enabled=False but leaves current graph config unchanged |
| `test_parallel_toggle_no_arg_uses_saved_state` | `/parallel` with no arg toggles the saved setting |
| `test_parallel_status_shows_active_and_saved_state` | `/parallel status` prints active and saved state when they differ |
| `test_parallel_invalid_arg` | `/parallel foo` prints usage error |
| `test_parallel_command_is_in_palette` | command palette includes `/parallel` entries |
| `test_clear_applies_saved_parallel_subagents_config` | `/clear` loads saved parallel setting and refreshes `agent` tool description |
| `test_resume_applies_saved_parallel_subagents_config` | `/resume` loads saved parallel setting and refreshes `agent` tool description |

### Acceptance Criteria

- `/parallel on` saves concurrent child-agent execution as enabled.
- `/parallel off` saves concurrent child-agent execution as disabled.
- `/parallel` with no argument toggles the saved state.
- The active graph is unchanged until `/clear` or restart.
- `/clear` applies the saved parallel setting before the next turn.
- `/resume` also applies the saved parallel setting for the resumed session.
- `/parallel status` shows the active state and saved state.
