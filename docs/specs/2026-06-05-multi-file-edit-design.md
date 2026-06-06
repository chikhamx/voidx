# Multi-File Edit / Apply Patch Design

Date: 2026-06-05

## Goal

Enable voidx to apply structured edits across multiple files in a single tool call, supporting unified diff format and search/replace blocks. This closes the core productivity gap where large-scale refactors currently require dozens of sequential single-file `edit` calls.

## Current State

Key files:

- `src/voidx/tools/file_ops.py` — `FileReadTool`, `FileWriteTool`, `FileEditTool`. Edit is single-file, single `old_string → new_string` replacement.
- `src/voidx/tools/base.py` — `ToolContext` carries `file_mtimes` for staleness guard; `resolve_safe()` for sandbox path validation.
- `src/voidx/agent/graph/tool_execution.py` — executes tool calls sequentially, one per `ToolMessage`.
- `src/voidx/permission/engine.py` — classifies `edit` and `write` as `FILE_WRITE` capability, default action `ask`.

Observed gaps:

- No way to modify more than one file per tool call. A 10-file refactor needs 10 round-trips.
- No unified diff / patch support. LLMs generate diffs naturally but voidx can't consume them.
- No atomic multi-file edit — if edit 3 of 5 fails, files 1-2 are already changed with no rollback.
- The `edit` tool's `old_string` must match exactly once, making it fragile for boilerplate changes across many files.

## External References

- **Claude Code** `apply_patch` tool: accepts unified diff, applies atomically, returns per-file results.
- **Aider** search/replace blocks: `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` format, supports multiple blocks per file and multiple files per message.
- **Cursor** multi-file edit: LLM proposes edits across files, applied as a batch with per-file accept/reject.
- **Git apply**: `git apply --check` for dry-run, `git apply` for actual application. Well-tested diff parsing.

References:

- https://docs.aider.chat/docs/usage/editing.html
- https://code.claude.com/docs/en/tools

## Design

### Approach: Unified Diff Apply Tool

Add an `apply_patch` tool that accepts a unified diff string and applies it across multiple files atomically.

**Why unified diff over search/replace blocks:**

1. LLMs already generate unified diffs naturally (especially with thinking/reasoning models).
2. `git apply` provides a battle-tested parser we can reuse for validation.
3. Unified diff is a universal format — users can paste from `git diff`, PR reviews, or generate manually.
4. Search/replace blocks are Aider-specific; unified diff is industry-standard.

### Tool Definition

```python
class ApplyPatchInput(BaseModel):
    patch: str = Field(
        description="Unified diff to apply. Can contain changes to multiple files. "
                    "Use standard unified diff format with --- a/file and +++ b/file headers."
    )
    dry_run: bool = Field(
        default=False,
        description="If true, validate the patch without applying changes. "
                    "Returns what would change without modifying files."
    )
```

### Core Algorithm

```
1. Parse unified diff → list of FilePatch(file_path, hunks)
2. For each FilePatch:
   a. resolve_safe() — sandbox check
   b. _check_staleness() — mtime guard
   c. Read current file content
   d. Apply hunks sequentially (fuzzy match with offset search)
3. If any hunk fails to apply:
   - Roll back all already-applied files (restore from pre-read content)
   - Return error with details of which hunk failed
4. If all hunks apply:
   - Write all modified files
   - Update file_mtimes for all touched files
   - Return summary with per-file diff stats
```

### Fuzzy Matching

Exact `old_string` matching is too brittle. The apply algorithm should:

1. Try exact match first.
2. If exact match fails, try with ±3 lines of offset (search nearby).
3. If offset match fails, try with whitespace normalization (ignore leading/trailing whitespace differences).
4. If all attempts fail, report the hunk as failed with context.

### Atomicity

All file changes are applied atomically:

1. Pre-read all target files before any writes.
2. Apply all hunks in memory first.
3. Only write to disk if all hunks succeed.
4. On failure, no files are modified.

### Permission

`apply_patch` is classified as `FILE_WRITE` — same permission tier as `edit` and `write`.

### Testing

| Test | Description |
|------|-------------|
| `test_apply_patch_single_file` | Apply a simple single-file diff |
| `test_apply_patch_multi_file` | Apply a diff touching 3+ files |
| `test_apply_patch_atomic_rollback` | If one hunk fails, no files are changed |
| `test_apply_patch_fuzzy_match` | Offset and whitespace-tolerant matching |
| `test_apply_patch_dry_run` | dry_run=True validates without writing |
| `test_apply_patch_staleness` | Rejects if file was modified since last read |
