# Multi-File Edit / Apply Patch Design

> **Status: Done**

Date: 2026-06-05

## Goal

Enable voidx to apply structured edits across multiple files in one tool call. The first implementation adds an `apply_patch` tool that accepts standard text unified diffs, validates all hunks in memory, and writes only after every target file can be updated.

This closes the productivity gap where multi-file refactors currently require many sequential single-file `edit` calls.

## Current State

Key files:

- `src/voidx/tools/file_ops.py` — `FileReadTool`, `FileWriteTool`, `FileEditTool`.
- `FileEditTool` is single-file but already supports multiple `old_string -> new_string` replacements in one call.
- `src/voidx/tools/base.py` — `ToolContext.file_mtimes` supports staleness guards; `resolve_safe()` validates sandbox-safe paths.
- `src/voidx/tools/registry.py` — built-in tools are registered explicitly.
- `src/voidx/agent/graph/tool_execution.py` — executes tool calls and returns one `ToolMessage` per call.
- `src/voidx/permission/rules.py` — `write` and `edit` classify as `FILE_WRITE`.
- `src/voidx/permission/evaluate.py` — already classifies `apply_patch` as `FILE_WRITE` alongside `edit` and `write`.

Observed gaps:

- No registered tool modifies more than one file per tool call.
- No unified diff / patch support. LLMs and users naturally produce diffs, but voidx cannot consume them.
- Current `edit` requires exact unique string matches, which is fragile for broad refactors.
- There is no batch validation across files before writes.

## External References

- Aider search/replace blocks: `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE`.
- Git unified diff format.
- Git apply dry-run semantics.

References:

- https://docs.aider.chat/docs/usage/editing.html

## Design

### Approach: Custom Unified Diff Apply Tool

Add an `apply_patch` tool that accepts a unified diff string and applies it across multiple files.

The first implementation uses a small custom parser and in-memory hunk application instead of shelling out to `git apply`.

Why not `git apply` for MVP:

1. We need sandbox validation through `resolve_safe()` before writes.
2. We need `ToolContext.file_mtimes` staleness checks per touched file.
3. We need structured per-file metadata and a combined `ToolResult.diff`.
4. The design includes limited fuzzy matching; `git apply` would require a different behavior contract.

### MVP Scope

Supported:

- Modify existing text files.
- Create new text files using `/dev/null -> b/path`.
- Delete text files using `a/path -> /dev/null`.
- Multiple hunks per file.
- Multiple files per patch.
- `dry_run=True` validation without writes.

Rejected:

- Rename patches where old and new paths differ and neither side is `/dev/null`.
- Binary patches.
- File mode changes.
- Symlink patches.
- Quoted paths with spaces. This can be added later if needed.

## Tool Definition

```python
class ApplyPatchInput(BaseModel):
    patch: str = Field(
        description="Unified diff to apply. Can contain changes to multiple files."
    )
    dry_run: bool = Field(
        default=False,
        description="Validate the patch without writing files.",
    )
```

## Core Algorithm

```
1. Parse unified diff into FilePatch entries.
2. Validate each FilePatch:
   a. reject unsupported rename/binary/mode-only patch
   b. resolve_safe() target path
   c. _check_staleness() for existing files
   d. read original content or use empty content for creates
3. Apply all hunks in memory:
   a. exact match at expected line
   b. exact match within +/-3 lines
   c. whitespace-normalized match within +/-3 lines
4. If any hunk fails:
   - return error
   - do not write any file
5. If every hunk succeeds:
   - dry_run=True: return summary and combined diff, no writes
   - dry_run=False: write all files
   - if a write fails mid-batch, restore already-written files from pre-read snapshots
6. Update file_mtimes for files that still exist.
```

## Fuzzy Matching

The hunk matcher is intentionally conservative:

1. Try exact match at the hunk's expected line.
2. Try exact match within `+/-3` lines.
3. Try leading/trailing-whitespace-normalized matching within `+/-3` lines.
4. If multiple fuzzy matches are possible, reject the hunk instead of guessing.

This is less powerful than Aider-style search/replace, but safer for an automated tool.

## Atomicity

The tool provides pre-write atomic validation and best-effort rollback:

- All target contents are computed in memory before any write.
- If validation or hunk application fails, no files are modified.
- If writing fails after some files were written, the tool attempts to restore written files from original snapshots.
- Restore behavior is content-based:
  - modified files are written back to their original content
  - created files are removed
  - deleted files are recreated with their original content

This is not a filesystem transaction. A process crash or disk failure can still leave partial writes, but normal tool errors should not.

## Permission

`apply_patch` must be classified as `FILE_WRITE`, same as `edit` and `write`.

Implementation points:

- Register `ApplyPatchTool` in `ToolRegistry`.
- Add `apply_patch` to `permission.rules.capability_for_tool()`.
- Keep `permission.evaluate.disabled_tools()` mapping `apply_patch` to edit-class rules.

## Session Change Tracking

Rollback and end-of-turn change summaries depend on `SessionChangeTracker` capturing file snapshots before a tool writes. `apply_patch` does not have a single `file_path` argument, so `capture_tool_call()` must parse the unified diff and snapshot every target file before execution.

Implementation points:

- Extract file paths from `args["patch"]` by scanning `--- a/path` and `+++ b/path` header lines (lightweight regex, no full hunk parsing needed).
- Capture each parsed file path with `capture_file()`.
- Ignore `/dev/null`; create/delete operations snapshot the real side of the diff.
- Let `record_diff()` consume the combined `ToolResult.diff` after successful execution.

## Result Shape

Successful result:

- `title`: `Applied patch to N files` or `Patch validated for N files`
- `output`: concise per-file summary
- `diff`: combined unified diff of the actual before/after contents
- `metadata`:
  - `dry_run`
  - `changed_files`
  - `files`: list of `{file, status, added, removed}`

Failure result:

- `metadata.error = True`
- Include `file` and `hunk` when available.
- Do not write files for validation/hunk failures.
- Validation and hunk failures do not include `partial_results`; no files have been written.
- If a write fails after some writes, return `metadata.error = True` and a message that rollback was attempted. The tool does not report the patch as partially applied.

## Testing

| Test | Description |
|------|-------------|
| `test_apply_patch_registered` | Tool registry exposes `apply_patch` |
| `test_apply_patch_permission_file_write` | Permission capability is `FILE_WRITE` |
| `test_apply_patch_single_file` | Apply a simple single-file diff |
| `test_apply_patch_multi_file` | Apply a diff touching 3+ files |
| `test_apply_patch_create_file` | Create a new text file |
| `test_apply_patch_delete_file` | Delete a text file |
| `test_apply_patch_atomic_validation` | If one hunk fails, no files are changed |
| `test_apply_patch_fuzzy_match` | Offset and whitespace-tolerant matching |
| `test_apply_patch_dry_run` | `dry_run=True` validates and returns diff without writing |
| `test_apply_patch_staleness` | Rejects if file was modified since last read |
| `test_apply_patch_blocks_path_traversal` | Rejects paths outside sandbox |
| `test_apply_patch_rejects_rename` | Rejects unsupported rename patches |
| `test_apply_patch_returns_combined_diff` | `ToolResult.diff` contains all touched files |
| `test_session_change_tracker_captures_apply_patch_targets` | Rollback and summaries capture all apply_patch targets |
