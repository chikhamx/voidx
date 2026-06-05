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
3. If offset search fails, try whitespace-normalized match (strip trailing spaces, normalize indentation).
4. If all fail, report the hunk as failed with context.

This matches `git apply --3way` philosophy: be lenient on whitespace, strict on content.

### Atomicity

- Read all target files before writing any.
- Store original content in memory.
- Apply hunks in memory first (no disk writes until all succeed).
- Only write to disk after all hunks apply successfully.
- On failure, no files are modified — the operation is all-or-nothing.

### Result Format

```python
class FilePatchResult(BaseModel):
    file_path: str
    hunks_applied: int
    hunks_total: int
    additions: int  # lines added
    deletions: int  # lines removed

class ApplyPatchResult(BaseModel):
    files_changed: int
    total_additions: int
    total_deletions: int
    file_results: list[FilePatchResult]
    rolled_back: bool  # true if any hunk failed and changes were reverted
```

### Permission Integration

- `apply_patch` is classified as `FILE_WRITE` capability.
- Default action: `ask` (same as `edit` and `write`).
- Session allow via `/allow apply_patch`.
- Sandbox check applies to every file path in the patch.

### Interaction with Existing Tools

- `edit` and `write` remain available for single-file, simple edits.
- `apply_patch` is the preferred tool for multi-file changes.
- The LLM prompt should guide: "Use `apply_patch` for changes spanning 2+ files. Use `edit` for single-file targeted replacements."
- `apply_patch` reuses `resolve_safe()` and `_check_staleness()` from `file_ops.py`.

### Diff Parsing

Implement a lightweight unified diff parser in `src/voidx/tools/diff_parser.py`:

```python
@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # lines with +/ /- prefix

@dataclass
class FilePatch:
    old_path: str   # from --- a/path
    new_path: str   # from +++ b/path
    hunks: list[Hunk]
```

Do NOT shell out to `git apply` — we need in-process control for:
- Atomic rollback
- Fuzzy matching customization
- Staleness guard integration
- Sandbox path validation before any disk write

### File Creation and Deletion

Unified diff supports new file creation (`--- /dev/null`) and file deletion (`+++ /dev/null`):

- **New file**: `--- /dev/null` → create the file with the `+` lines content. Parent directories created automatically.
- **Delete file**: `+++ /dev/null` → delete the file after confirming all `-` lines match current content.
- Both subject to sandbox and permission checks.

### Token Budget

Patches can be large. Guard rails:

- Max patch size: 50,000 characters (configurable).
- Max files per patch: 20.
- Max hunks per file: 50.
- Return error immediately if limits exceeded, suggesting splitting the patch.

## Scope

In scope:

- `apply_patch` tool with unified diff parsing.
- Multi-file atomic apply with rollback.
- Fuzzy matching with offset and whitespace normalization.
- Dry-run mode.
- File creation and deletion via diff.
- Permission and sandbox integration.
- Diff parser module.

Out of scope:

- Interactive per-hunk accept/reject (separate feature, requires UI changes).
- Three-way merge with conflict markers (future enhancement).
- Binary file patching (text files only).
- Rename detection (can be expressed as delete + create in the diff).

## File Changes

| File | Change |
|------|--------|
| `src/voidx/tools/apply_patch.py` | New — `ApplyPatchTool`, `ApplyPatchInput`, apply logic |
| `src/voidx/tools/diff_parser.py` | New — unified diff parser (`FilePatch`, `Hunk`) |
| `src/voidx/tools/registry.py` | Register `ApplyPatchTool` |
| `src/voidx/permission/engine.py` | Add `apply_patch` to `BASIC_RULES` (action=ask) |
| `src/voidx/agent/agents.py` | Update prompts to mention `apply_patch` for multi-file edits |
| `tests/test_tools/test_apply_patch.py` | New — parser tests, apply tests, rollback tests, fuzzy match tests |

## Risks

| Risk | Mitigation |
|------|-----------|
| Fuzzy matching applies wrong hunk | Strict content match first, fuzzy only as fallback with offset limit |
| Large patches exceed context window | Size limits + suggest splitting |
| Rollback fails if disk write partially completes | Write to temp files first, rename atomically |
| Diff parser doesn't handle all git diff quirks | Start with common subset, extend based on test failures |
| LLM generates malformed diffs | Parser returns clear error with line number of parse failure |
