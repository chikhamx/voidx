# Workspace Model Profile Scope Design

Date: 2026-07-05

> **Status: Done**

## Goal

Make model selection scope explicit and predictable across workspaces.

The desired behavior:

- `current_profile` in `.voidx/settings.json` is workspace-level by default.
- When a workspace has no local `current_profile`, voidx initializes it by copying the global `~/.voidx/settings.json` `current_profile`.
- `/model switch` supports choosing whether the switch updates the local workspace default or the global default.
- TUI and desktop model switching default to local scope.
- Model/provider credentials remain user-level and shared across workspaces.

## Current State

Relevant files:

- `src/voidx/config/settings.py`
  - `current_profile` is currently listed in `GLOBAL_KEYS`.
  - `_effective_data()` merges global settings into workspace settings.
  - `_write_target()` writes global keys to `~/.voidx/settings.json` when the workspace file does not already contain that key.
  - `save_profile()` always calls `_set_setting("current_profile", profile.name)`.
- `src/voidx/agent/slash/model.py`
  - `/model switch` updates the in-memory graph model immediately.
  - It persists the selected profile through `settings.save_profile(profile)`.
  - The direct switch path also updates the current session model when a session exists.
- `frontend/src/main.ts`
  - The desktop dropdown sends `/model switch provider/model`.
  - It does not specify scope.
- `src/voidx/memory/model_profiles.py`
  - Profile credentials are stored in the user-level SQLite database under `~/.voidx/store/voidx.db`.

Observed problem:

- A model switch inside one workspace can update the global default when that workspace does not already have local `current_profile`.
- Other workspaces that inherit global settings can then silently change default model.
- This is surprising because model switching feels like a workspace/session action.

## Definitions

### Profile

A profile is the configured provider/model credential record:

```text
provider/model -> api_key, base_url, protocol
```

Profiles remain user-level. They are shared across all workspaces and stored in the existing model profile database.

### Local Current Profile

The workspace-local default model:

```text
<workspace>/.voidx/settings.json -> current_profile
```

This controls the default model for new sessions in that workspace.

### Global Current Profile

The user-level fallback default model:

```text
~/.voidx/settings.json -> current_profile
```

This is used only to seed a workspace that has no local model selection yet, or when the user explicitly chooses a global switch.

## Design

### 1. Make `current_profile` Workspace-Level

`current_profile` should no longer behave like a normal inherited `GLOBAL_KEYS` setting during writes.

New behavior:

1. `Settings.create(workspace)` loads workspace and global settings as it does today.
2. If the workspace settings file does not contain `current_profile`, and the global settings file does contain one:
   - write that global value into `<workspace>/.voidx/settings.json`;
   - use the copied value as the workspace's local default.
3. After initialization, model switches in that workspace update only the workspace file unless global scope is explicitly requested.

This gives each workspace a stable default after first use while preserving a useful global seed for new workspaces.

### 2. Keep Credentials Global

Model profile rows stay in `~/.voidx/store/voidx.db`.

This avoids duplicating API keys into project folders and keeps provider setup reusable across workspaces.

Workspace settings store only the selected profile name, not secrets.

### 3. Add Explicit Model Switch Scope

Add a switch scope enum:

```python
ModelSwitchScope = Literal["local", "global"]
```

Accepted command forms:

```text
/model switch provider/model
/model switch provider/model --local
/model switch provider/model --global
/model switch --local provider/model
/model switch --global provider/model
```

Default scope:

```text
local
```

Scope meanings:

- `local`
  - updates the current graph model immediately;
  - writes `<workspace>/.voidx/settings.json.current_profile`;
  - updates the current session model when a session exists;
  - does not mutate `~/.voidx/settings.json.current_profile`.
- `global`
  - updates the current graph model immediately;
  - writes `~/.voidx/settings.json.current_profile`;
  - also writes the current workspace local `current_profile` (see Open Questions for a future opt-out flag);
  - updates the current session model when a session exists.

Writing both global and local for `--global` keeps the current workspace aligned with the model the user just selected, while also changing the seed for future workspaces.

### 4. Add Settings APIs for Scope

Introduce explicit methods instead of overloading `save_profile()`:

```python
async def save_profile(profile: Profile, *, scope: ModelProfileScope = "local") -> Path:
    ...

def set_current_profile(name: str, *, scope: ModelProfileScope = "local") -> Path:
    ...

def ensure_workspace_current_profile() -> Path | None:
    ...
```

Recommended internal shape:

- `save_profile(profile, scope="local")`
  - saves/updates the user-level profile row;
  - sets current profile according to scope.
- `set_current_profile(..., scope="local")`
  - `local`: write `self._data["current_profile"]` and save workspace settings.
  - `global`: write `self._global_data["current_profile"]` and save global settings.
- `ensure_workspace_current_profile()`
  - if `self._global_path == self._path`, no copy is needed.
  - if workspace lacks `current_profile` and global has it, write the global value into workspace settings.
- `delete_profile(name)`
  - deletes the user-level profile row as today;
  - if the deleted profile was the active `current_profile`, the fallback write (`next_profile.name` or pop) uses **local scope** by default, consistent with `save_profile`;
  - this prevents a delete inside one workspace from mutating the global seed.

`Settings.create()` should call `ensure_workspace_current_profile()` after migration.

Plain `Settings(...)` constructors must remain side-effect-free (read-only load), because they are used by desktop gateway snapshots (`gateway/session.py`), read-only lookups, and tests that assert on the on-disk state. Do **not** call `ensure_workspace_current_profile()` in `__init__`.

Instead:

- `Settings.create()` calls `ensure_workspace_current_profile()` after migration (this is the persistent-init path used by `main.py`).
- Desktop gateway write paths (`gateway/session.py` lines that currently do `Settings(self._workspace or ".")` before a write) should either:
  - call `await settings.ensure_workspace_current_profile()` explicitly before the write, or
  - switch to `await Settings.create(workspace)` when an async context is available.
- Read-only snapshot paths (e.g. `gateway/session.py:874` `return Settings(...)`) keep using plain `Settings(...)` with no write.

The helper must avoid writing when the workspace already has `current_profile`, and must invalidate `self._effective_cache` after a write so subsequent reads see the new value.

### 5. Preserve Read Semantics Carefully

After this change, `current_profile` should be treated specially:

- read path:
  - workspace local value wins;
  - if missing, global value may seed local and then be read from local;
  - if neither exists, fall back to the most recently saved configured profile as today.
- write path:
  - local by default;
  - global only when explicitly requested.

Implementation options:

1. Remove `current_profile` from `GLOBAL_KEYS` and handle it with dedicated methods.
2. Keep it in `GLOBAL_KEYS` for read compatibility but override write behavior in `_write_target()`.

Preferred: option 1. Dedicated handling is clearer and prevents future accidental global writes.

### 6. Desktop Model Dropdown Behavior

Desktop model dropdown should remain a local switch by default.

Change the submitted command from:

```text
/model switch provider/model
```

to:

```text
/model switch provider/model --local
```

This is explicit for logs and future compatibility, even though local is the default.

Future UI enhancement:

- Add an advanced model action that can switch globally.
- Do not add extra UI for global scope in this change unless needed.

### 7. TUI Behavior

TUI `/model switch` defaults to local.

Examples:

```text
/model switch deepseek/deepseek-v4-flash
```

updates only the active workspace default.

```text
/model switch deepseek/deepseek-v4-flash --global
```

updates the user global seed and the current workspace default.

Interactive model selection should show a short confirmation after switch:

```text
deepseek/deepseek-v4-flash ✓ switched (local)
```

or:

```text
deepseek/deepseek-v4-flash ✓ switched (global + local)
```

### 8. Startup and New Workspace Behavior

When opening or creating a new workspace:

1. Resolve workspace path.
2. Initialize settings.
3. If `<workspace>/.voidx/settings.json.current_profile` is missing:
   - copy `~/.voidx/settings.json.current_profile` into the workspace file;
   - do not copy API keys or full profile data.
4. Build config from the workspace-local current profile.

This makes the first open deterministic:

- new workspace starts with the global default;
- after first open, it owns its default locally;
- later global default changes do not silently change existing workspaces.

## Migration

No broad migration command is required.

Lazy migration is enough:

- Existing workspaces with local `current_profile` keep it.
- Existing workspaces without local `current_profile` receive a copied value from global the next time they are opened.
- Global `current_profile` remains as the seed for future workspaces.
- Profile credentials remain untouched.

Safety rule:

- Never delete or rewrite existing model profiles during this migration.
- Never copy API keys into workspace `.voidx/settings.json`.

## Testing

### Settings Tests

Add coverage in `tests/test_config/test_config_advanced.py` (existing `current_profile` / `save_profile` tests already live there). If the file grows too large, split into `tests/test_config/test_config_profile_scope.py`.

- `test_current_profile_is_copied_from_global_for_new_workspace`
  - global has `current_profile`;
  - workspace has no settings;
  - `Settings.create(workspace)` writes workspace `.voidx/settings.json.current_profile`;
  - global file remains unchanged.
- `test_local_model_switch_does_not_update_global_current_profile`
  - workspace and global start with different current profiles;
  - local switch updates only workspace current profile.
- `test_global_model_switch_updates_global_and_current_workspace`
  - global switch writes global current profile;
  - current workspace local current profile also matches selected profile.
- `test_current_profile_not_written_to_global_by_default`
  - workspace has no local current profile before initialization;
  - default local switch does not mutate global.
- `test_delete_profile_fallback_writes_local_only`
  - workspace and global start with different `current_profile`, both pointing at profiles that exist;
  - delete the active workspace profile;
  - fallback `current_profile` write lands in workspace settings only;
  - global `current_profile` remains unchanged.
- `test_plain_settings_constructor_does_not_write_disk`
  - global has `current_profile`, workspace has none;
  - `Settings(workspace)` (plain constructor, not `create()`) leaves the workspace settings file absent / unchanged;
  - only `Settings.create(workspace)` performs the copy.

### Slash Command Tests

Add coverage in `tests/test_agent/test_slash_model.py` (basic switch dispatch) and `tests/test_agent/test_slash_model_advanced.py` (scope flag parsing):

- `/model switch provider/model` uses local scope.
- `/model switch provider/model --local` uses local scope.
- `/model switch provider/model --global` uses global scope.
- Interactive TUI switch calls save with local scope.

### Desktop Frontend Tests

Add/update frontend coverage:

- model dropdown submits `/model switch provider/model --local`.
- UI state still updates provider/model after successful switch.

### Regression Test

Keep the existing test isolation fix:

- tests must isolate `voidx.memory.store.DATA_DIR`;
- test-created profiles must not leak into real `~/.voidx/store/voidx.db`.

## Non-Goals

- Do not make API keys workspace-local.
- Do not add a full global/local settings UI in this change.
- Do not change how sessions store historical `model_provider` / `model_name`.
- Do not delete existing profile rows during migration.
- Do not make global default changes retroactively update every workspace.

## Open Questions

1. Should `--global` update only global, or global plus current workspace?
   - Recommended: global plus current workspace, because the user just switched the current session too.
2. Should there be a command to reset a workspace to follow global again?
   - Future command candidate: `/model reset --local`, which deletes workspace `current_profile`.
3. Should desktop settings show whether the active default is local or global-seeded?
   - Future UI candidate: show `Local default` / `Global seed` in model settings.
4. Should `--global` support an opt-out flag so that only the global seed is updated without touching the current workspace local `current_profile`?
   - Future command candidate: `/model switch provider/model --global --no-local`.
   - Out of scope for this change; current `--global` always writes both global and current workspace local.

