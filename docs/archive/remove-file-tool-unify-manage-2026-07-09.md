# Remove Legacy `file` Tool — Unify on `manage`

Date: 2026-07-09

> **Status: Done** — awaiting approval before implementation.

## Goal

Remove the deprecated `file` tool entirely, unify all file lifecycle operations
(create / delete / move) on the `manage` tool, and optimize TUI display to show
`Manage("create", "path")` style headers instead of the generic `Managing(...)`.

## Current State

Two tools coexist in `src/voidx/tools/file/manage.py`:

| Tool | id | Schema | Role |
|---|---|---|---|
| `ManageTool` (L96) | `"manage"` | `ManageInput`: `op` + `paths` (str\|list) + `moves` (list[MoveSpec]) + `overwrite` | New tool, supports batch |
| `FileTool` (L117) | `"file"` | `FileInput`: `file_path` + `op` + `dest_path` + `overwrite` | Deprecated wrapper |

`FileTool.execute` translates the old schema into `ManageInput` and delegates to
`ManageTool`, then stamps `deprecated_tool` / `replacement_tool` / `remove_after`
metadata. The `remove_after` value is `"one minor release or 30 days"` but no
enforcement mechanism exists.

### Known Issues

1. **`repair_tool_name`** (`permission/rules.py:97-113`) maps external aliases
   (`Write`, `write_file`, `writefile`) to `"file"` (old), not `"manage"`.
2. **UI display layer** hardcodes `"file"` in 6 files; `manage` calls are
   silently broken in `formatting.py:81` and `session.py:228` because they read
   `args.get("file_path")` which does not exist in `ManageInput`.
3. **`display_policy.py:130`** has no `"manage"` entry.
4. **TUI gerund** shows `Managing(...)` for `file` — no `manage` entry at all.

## Design Summary

- Delete `FileTool` and `FileInput` from `manage.py`.
- Remove `FileTool` from registry registration.
- Update `repair_tool_name` to map aliases to `"manage"` instead of `"file"`.
- Remove `"file"` from all hardcoded tool-name sets in permission, UI, and
  file-lock code paths; add `"manage"` where needed.
- Migrate `next_step_hint` from `FileTool.execute` to `ManageTool.execute`:
  when `op="create"` succeeds without `overwrite`, stamp the same hint
  (`"Use the write tool to append content to {path}..."`). This preserves
  the UX guidance that `FileTool` provided.
- Optimize TUI display: `manage` shows as `Manage("create", "path")` in dock,
  `Manage(op="create", paths="path")` in console, consistent with existing
  `Read("path")` / `Write("path")` style.

### Alias Handling

External aliases (`Write`, `write_file`, `writefile`, etc.) currently map to
`"file"`. After removal they map to `"manage"`. However, alias callers pass
`file_path` / `dest_path` arguments, not `paths` / `moves`.

**Decision**: `repair_tool_name` only repairs the tool *name*, not the argument
schema. The LLM generates tool calls based on the schema it receives from the
registry. Since `FileTool` is removed from the registry, the LLM will never see
the `file` schema and will always call `manage` with the correct `paths` /
`moves` arguments. External aliases are only relevant for human-typed or legacy
serialized calls — these are edge cases and will fail with a clear validation
error from `ManageInput` if arguments don't match.

No argument-translation shim is needed. This is a clean break.

## Affected Files and Changes

### Core Tool Definition (3 files)

| File | Change |
|---|---|
| `src/voidx/tools/file/manage.py` | Delete `FileInput` (L72-93) and `FileTool` (L117-147); migrate `next_step_hint` logic into `ManageTool.execute` (after create succeeds without overwrite, stamp hint with first path from `paths`) |
| `src/voidx/tools/file/__init__.py` | Remove `FileTool`, `FileInput` from import and `__all__` |
| `src/voidx/tools/registry.py:45` | Remove `FileTool` from `_register_builtins` list |

### Permission Layer (2 files, 4 changes)

| File:Line | Current | After |
|---|---|---|
| `permission/rules.py:99,104,106` | `Write`/`write_file`/`writefile` → `"file"` | → `"manage"` |
| `permission/rules.py:391` | `{"file", "manage", ...}` | `{"manage", ...}` |
| `permission/rules.py:436-437` | `_FILE_PATTERN_TOOLS` contains `"file"` | Remove `"file"` |
| `permission/engine.py:133,162` | `{"file", "manage", ...}` | `{"manage", ...}` |

### File Lock (1 file)

| File:Line | Change |
|---|---|
| `agent/graph/tool_executor/helpers.py:83-89` | Delete `elif name == "file"` branch; `manage` branch (L90-107) already covers all cases |

### UI Display — Console (2 files)

| File:Line | Current | After |
|---|---|---|
| `ui/output/console/app.py:43` | `"file": "managing"` | `"manage": "manage"` (renders as `Manage(...)`) |
| `ui/output/console/formatting.py:81` | `{read, file, write, replace}` reads `file_path` | Remove `"file"`; add `manage` branch: extract `op` + first path from `paths` (str/list) or `moves[0].src` |

`_fmt_args_short` for `manage` returns a short string like `create src.py` or
`move old.py → new.py`, consistent with how `read` returns just the path.

### UI Display — Dock (1 file)

| File:Line | Current | After |
|---|---|---|
| `ui/output/dock/nodes.py:375` | `"file": "File"` | `"manage": "Manage"` |
| `ui/output/dock/nodes.py:405` | `{read, file, write, replace, lsp}` reads `file_path` | Remove `"file"`; add `manage` branch in `_tool_display_value` |

Dock `_tool_header` already renders `Name("value")` format — once
`_tool_display_name` returns `"Manage"` and `_tool_display_value` extracts the
right path, it will render as `Manage("create src.py")`.

### UI Display — Events (1 file)

| File:Line | Current | After |
|---|---|---|
| `ui/output/events/consumers.py:588` | `"file": "Reading"` | `"manage": "Managing"` |
| `ui/output/events/consumers.py:612` | `{read, file, write, replace, edit, lsp}` | Remove `"file"`; add `manage` branch in `_subagent_tool_detail` |

### UI Display — Display Policy (1 file)

| File:Line | Current | After |
|---|---|---|
| `ui/output/display_policy.py:130` | `"file": ToolDisplayRule(SHOW)` | `"manage": ToolDisplayRule(SHOW)` |

### Session File Capture (1 file)

| File:Line | Current | After |
|---|---|---|
| `ui/session.py:228` | `{file, write, replace}` + `args.get("file_path")` | Add `"manage"` to set; replace manual `file_path` extraction with `file_paths_for_tool(tool_name, args)` call |

### Capture (1 file, no code change)

`ui/output/capture.py` uses `_TOOL_GERUND` and `_fmt_args` — both fixed by
console changes above. Verify only.

### Tests (~14 files)

| Test File | Change |
|---|---|
| `test_file_tools_redesign.py` | Delete `TestLegacyFileWrapper` class; update registry test to only assert `manage` |
| `test_interactive_tools.py` | `FileTool().execute({file_path, op})` → `ManageTool().execute({op, paths})` |
| `test_interactive_tools_write.py` | Same as above |
| `file/test_write_file.py` | `execute_tool("file", {file_path, op})` → `execute_tool("manage", {op, paths})`; `next_step_hint` tests stay valid (hint migrated to `ManageTool`); `test_file_create_overwrite_has_no_next_step_hint` stays valid (overwrite=True → no hint) |
| `file/test_read_write.py` | `from ...manage import FileTool` → `ManageTool`; `FileTool.description` → `ManageTool.description` |
| `file/test_read.py` | Same as above |
| `test_tool_schemas.py` | Delete `FileInput` tests |
| `test_tool_registry.py` | Remove `FileInput` import |
| `test_tool_error_handling.py` | `FileTool().execute({file_path:123})` → `ManageTool().execute({op:"create", paths:123})` |
| `test_file_rwlock.py` | `"name":"file"` → `"name":"manage"`, args `{op, paths}` / `{op, moves}` |
| `test_workflow_transactions_barrier.py` | `"name":"file"` → `"name":"manage"`, args `{op, paths}` |
| `test_agent/test_permission.py` | 5 assertions using `{"name": "file", args: {file_path}}` → `{"name": "manage", args: {op:"create", paths}}` (L280, L314, L353, L359, L374) |
| `test_agent/test_message_trimming_rules.py` | `test_file_tool_not_summarized`: `"name": "file"` → `"name": "manage"`, args `{op:"create", paths:"f.py"}`; test still passes (manage not tracked by trim logic, same as file) |

## TUI Display Examples

After changes, `manage` calls render as:

```
# Console (running)
  ⠋ Manage(op="create", paths="src/app.py")

# Console (done)
  ● Manage [dim]create src.py[/dim]

# Dock
  ● Manage("create src.py")

# Events (subagent)
  Managing: create src.py
```

For move operations:

```
# Console (running)
  ⠋ Manage(op="move", moves=[{"src": "old.py", "dest": "new.py"}])

# Console (done)
  ● Manage [dim]move old.py → new.py[/dim]

# Dock
  ● Manage("move old.py → new.py")
```

## Risks

1. **Clean break, no shim**: LLM-generated calls always use `manage` schema
   (correct args). Legacy serialized calls with `file_path` will fail with a
   clear `ManageInput` validation error — acceptable for a clean removal.
2. **UI parameter adaptation**: `formatting.py` and `session.py` must handle
   `paths` (str/list) and `moves` (list of dicts), not just `file_path`. The
   `file_paths_for_tool` function in `permission/rules.py` already implements
   this logic and can be reused.
3. **Test volume**: ~14 test files need parameter structure conversion. All
   mechanical: `{file_path, op}` → `{op, paths}` and
   `{file_path, op, dest_path}` → `{op, moves: [{src, dest}]}`.
4. **`next_step_hint` migration**: `FileTool.execute` stamps a hint after
   `op="create"` succeeds without overwrite. This logic must move to
   `ManageTool.execute` (using `paths[0]` as the path) or the hint is lost.
   Two tests in `test_write_file.py` assert this behavior.
5. **`message_trimming.py` not affected**: The trim logic
   (`message_trimming.py:372`) extracts `file_path` from args and only
   tracks `read`/`write`/`replace`. `manage` is not tracked — same as `file`
   before removal. No code change needed, but `test_file_tool_not_summarized`
   must update its tool name and args.

## Verification

```bash
# Backend tests — tool tests
./test.py --backend -- src/tests/test_tools/ -v

# Backend tests — agent + permission
./test.py --backend -- src/tests/test_agent/test_file_rwlock.py src/tests/test_agent/graph/test_workflow_transactions_barrier.py src/tests/test_agent/test_permission.py src/tests/test_agent/test_message_trimming_rules.py -v

# Full backend suite
./test.py --backend -v
```
