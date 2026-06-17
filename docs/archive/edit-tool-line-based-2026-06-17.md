# Edit Tool: Line-Based Replacement

Date: 2026-06-17

> **Status: Done**

## Goal

Replace the current string-matching edit tool with a line-number-based edit tool.
Instead of requiring an exact `old_string` match, the tool locates the replacement
region by line number range. This eliminates ambiguity from multiple matches and
removes the need for the agent to copy large blocks of existing code verbatim.

## Current State

Key file: `src/voidx/tools/file_ops.py`

Current `EditEntry` model:

```python
class EditEntry(BaseModel):
    old_string: str  # exact string to replace, must match exactly once
    new_string: str  # replacement string
```

Current `FileEditTool` behavior:

1. Reads the file content as a string.
2. For each edit entry, counts occurrences of `old_string` in the content.
3. Rejects if `old_string` is not found (0 matches) or ambiguous (>1 matches).
4. Replaces the single match with `new_string`.
5. Applies edits sequentially — later edits see earlier results.

Pain points:

- **Ambiguity**: Common patterns (e.g. `return None`, `pass`, blank lines) match
  multiple times. The agent must include surrounding context to disambiguate,
  which wastes tokens and is fragile.
- **Token cost**: The agent must reproduce the exact `old_string` in the tool
  call, including whitespace and indentation. For multi-line replacements this
  can be very large.
- **Fragility**: Any whitespace mismatch causes a failed edit. The agent must
  re-read the file and retry.
- **Sequential complexity**: When multiple edits shift line numbers, the agent
  must reason about the cumulative offset to provide correct `old_string` values
  for later edits.

## Design Summary

Replace `old_string` with line-number addressing from the `read` tool output.
For replacements, the tool identifies the replacement region with
`start_line` / `end_line` (1-based, inclusive). For insertions, the tool uses an
explicit operation and a single anchor line. This keeps the agent-facing schema
literal and avoids surprising zero-width range conventions.

Line numbers are meaningful only when they refer to the same file view the
agent saw. The `edit` tool must therefore verify that the target line range was
recently read from the same file version before applying an edit.

### Shared line model

`read` and `edit` must share one helper for converting file text to displayed
line numbers:

- An empty file has zero displayed lines.
- A non-empty file records whether it originally ended with `\n`.
- For display and line addressing, remove exactly one final `\n` if present,
  then split on `\n`.
- Writing joins addressed lines with `\n` and restores the original final
  newline only when the edit did not explicitly affect the final line.

Replacement content uses the same split rule, except it does not inherit the
target file's final-newline state. If `new_string` ends with `\n`, remove exactly
one final newline before splitting so that `"a\n"` means one inserted line, not
`"a"` plus an accidental blank line. Intentional blank lines remain possible:
`"a\n\n"` becomes two inserted lines, `"a"` and `""`. If `new_string` starts
with `\n`, the first inserted/replacement line is intentionally blank; tool
descriptions should warn agents not to start `new_string` with `\n` unless that
blank line is desired.

## New Model

```python
from typing import Literal


class EditEntry(BaseModel):
    operation: Literal["replace", "insert_before", "insert_after"] = Field(
        description=(
            "Edit operation. Use replace to replace an inclusive line range. "
            "Use insert_before or insert_after to insert relative to start_line."
        ),
    )
    start_line: int = Field(
        description=(
            "1-based line number where replacement starts, or the insertion "
            "anchor line for insert_before/insert_after."
        )
    )
    end_line: int | None = Field(
        default=None,
        description=(
            "1-based line number where replacement ends (inclusive). Required "
            "for replace; omitted for insert_before/insert_after."
        ),
    )
    new_string: str = Field(
        description="Replacement or inserted content."
    )
```

## New Behavior

### `write`

`write` remains the whole-file creation/overwrite tool. It should not become a
line-editing API, but its description and post-write state must align with the
line-based `edit` contract:

1. Small files can still be written in one `write` call with complete content.
2. For large new files, the agent should use `write` to create a small non-empty
   scaffold containing stable anchors, section headers, or placeholders.
3. After `write`, the agent must call `read` on the scaffold before calling
   `edit`, because line-level read coverage is required for all edits.
4. The agent then uses `edit` with `insert_before`, `insert_after`, or `replace`
   to fill the scaffold in chunks.
5. Writing an empty file is still allowed, but an empty file cannot be expanded
   with `edit` in this design because there is no line anchor returned by
   `read`. For a new large file, write at least one anchor line.

Example large-file creation flow:

1. `write`:

   ```text
   # module title

   # __VOIDX_SECTION_IMPORTS__
   # __VOIDX_SECTION_TYPES__
   # __VOIDX_SECTION_IMPL__
   ```

2. `read` the scaffold to get exact line numbers.
3. `edit` each anchor line or insert after it with generated chunks.
4. `read` again before later edit batches if a prior edit changed the file.

`write` must clear line-level read coverage for the target file after a
successful write, the same as `edit`, because any previous line numbers for that
path are now stale.

`write` must use the same displayed-line splitting as `read` when estimating
line count for large-file guidance, so an empty file counts as 0 lines and a
file ending in exactly one final newline does not gain an extra displayed line.

If a later `read` request is fully covered by an existing line-level read range
for the same file fingerprint, `read` returns a short "already read" summary
instead of repeating the file content. Partial overlaps still return the
requested content normally.

### `edit`

1. Read the file and split it using the same line model as `read`.
2. Preserve the file's original trailing-newline state unless an edit explicitly
   changes the affected final line content.
3. Before validation, confirm the relevant lines were recently read from the
   same file version:
   - `replace`: every line from `start_line` through `end_line` must be covered
     by a prior `read` result for this file.
   - `insert_before` / `insert_after`: the anchor `start_line` must be covered
     by a prior `read` result for this file.
4. For each edit entry:
   a. Validate the operation-specific line range.
   b. Convert `new_string` to replacement lines without inventing an extra blank
      line for a trailing newline.
   c. Replace or insert lines according to the operation.
5. When multiple edits are provided, apply them **in reverse order** (highest
   line numbers first) so that earlier edits don't shift the line numbers of
   later ones. This eliminates the need for the agent to reason about cumulative
   offsets.
6. If any edit's line range, read coverage, or batch relationship is invalid,
   reject the entire batch (atomic).
7. Write the result and produce a unified diff.

One `read` may support one subsequent batch `edit` containing multiple edit
entries, as long as every referenced replacement range or insertion anchor is
covered by that read result and the file fingerprint still matches. After that
batch succeeds, line-level read coverage is cleared and later edits must read
again.

### Read coverage

`record_mtime()` currently records file-level staleness after `read`. This design
extends that tracking with the line ranges returned by each read result. A line
range is editable only if:

- The target file path matches a prior read.
- The file fingerprint still matches the read-time fingerprint. Use at least
  `stat().st_mtime_ns` and `stat().st_size`; do not rely on float `st_mtime`
  alone.
- The requested replacement range, or insertion anchor line, is fully contained
  in at least one prior read range for that file.

This prevents the agent from editing unseen line numbers or using line numbers
from a stale partial read. After a successful `write` or `edit`, the tool updates
the stored file fingerprint and clears line-level read coverage for that file.
The agent must call `read` again before any later `edit` call against the same
file, because line numbers may have shifted.

`read.offset` and `read.limit` are optional, but when provided must be positive
1-based integers. `0` or negative values are rejected rather than silently
falling back to a default or producing surprising Python slice behavior.

### Reverse-order application example

File has 10 lines. Two edits:

- Edit A: replace lines 3–4 with 2 new lines
- Edit B: replace lines 7–8 with 1 new line

Apply B first (lines 7–8), then A (lines 3–4). Since B is after A, applying B
first doesn't affect A's line numbers. If we applied A first, lines 7–8 would
shift to 8–9 (A added one extra line), and B would need adjustment.

### Insert examples

Insert before line 3:

```json
{"operation": "insert_before", "start_line": 3, "new_string": "import os\n"}
```

Insert after the last visible line:

```json
{"operation": "insert_after", "start_line": 10, "new_string": "\nmain()\n"}
```

Insertions use an explicit operation so the agent does not need to remember
special sentinel values such as `start_line = end_line + 1`.

### Edge cases

| Case | Behavior |
|---|---|
| `operation` omitted | Error: operation is required |
| `replace` with `start_line == end_line` | Replace a single line |
| `replace` with `new_string` empty | Delete lines `start_line` through `end_line` |
| `insert_before` / `insert_after` with empty `new_string` | Error: insertion content must not be empty |
| `insert_before` / `insert_after` with `end_line` set | Error: end_line must be omitted for insertions |
| `replace` with `end_line` omitted | Error: end_line is required for replace |
| `new_string` has trailing newline | Do not create an extra blank line solely because of the trailing newline |
| Original file ends with newline and edit does not touch final line | Preserve final newline |
| Original file does not end with newline and edit does not touch final line | Preserve missing final newline |
| `start_line` or `end_line` out of range | Error: line number out of range, report total lines |
| `start_line > end_line` for replace | Error: start_line must be <= end_line |
| Replacement range not recently read | Error: line range must be read before editing |
| Insertion anchor not recently read | Error: anchor line must be read before inserting |
| Empty file | Replacement is invalid; insertion is invalid until an explicit empty-file insertion operation is added |
| Empty `edits` array | Error: at least one edit required |

### Batch validation

All edits are validated before writing. The batch is rejected if any two entries
touch the same original line or would create ambiguous ordering:

- Replacement ranges must not overlap.
- Duplicate replacement ranges are rejected.
- Multiple insertions at the same anchor are rejected.
- An insertion anchored inside a replacement range is rejected.
- Adjacent replacement ranges are allowed, because their order is unambiguous.
- An insertion immediately before or after a replacement range is allowed only
  when its anchor line is outside the replaced range.

## File Changes

### `src/voidx/tools/file_ops.py`

- Update `FileWriteTool.description` and `FileWriteInput.content` guidance:
  - Small files may be written completely.
  - Large new files should be created as a small non-empty scaffold with anchor
    lines, then filled through read + line-based edit.
  - Empty files are allowed, but cannot be expanded by line-based edit until
    rewritten with at least one anchor line.
- Clear line-level read coverage for the target file after every successful
  `write`.
- Remove `old_string` from `EditEntry`.
- Add `operation`, `start_line`, optional `end_line`, and `new_string` to
  `EditEntry`; `operation` is required so callers must explicitly choose
  replacement versus insertion.
- Update `FileReadTool.execute` to use the shared line model and record the
  exact line range returned by each read.
- Validate `FileReadInput.offset` and `FileReadInput.limit` as positive
  integers when supplied.
- Add small helper functions for line splitting/joining and edit-content
  splitting so `read` and `edit` cannot drift into subtly different behavior.
- Add a small line-read coverage helper/structure rather than scattering this
  state across tools. Suggested shape:
  - `FileFingerprint(mtime_ns: int, size: int)`
  - `ReadLineRange(start_line: int, end_line: int)`
  - helpers such as `record_read_range(ctx, path, start_line, end_line)`,
    `check_read_coverage(ctx, path, start_line, end_line)`, and
    `clear_read_coverage(ctx, path)`
- Rewrite `FileEditTool.execute`:
  - Split file into lines with the same display semantics as `read`.
  - Preserve the original trailing-newline state when unaffected.
  - Validate read coverage for each requested range or anchor.
  - Validate all edits before applying any.
  - Sort edits by `start_line` descending and apply.
  - Join lines and write.
  - Clear line-level read coverage for the file after a successful write.
- Update `FileEditTool.description` and `FileEditInput.edits` field description.
- Extend `ToolContext` or file-state helpers to track recently read line ranges
  per resolved file and file fingerprint (`mtime_ns` + size).
- Store the same file fingerprint (`mtime_ns` + size) for both file-level
  staleness and line-level read coverage; never compare float `st_mtime`.

### `tests/test_tools/test_basic.py`

- Update `test_edit_input` to use new fields.
- Update `test_edit` to use line-based parameters.
- Update `test_edit_output_contains_diff` to use line-based parameters.
- Add write-related tests:
  - `test_write_clears_read_coverage_after_success`
  - `test_write_allows_empty_file_but_edit_rejects_empty_file`
  - `test_write_large_file_guidance_mentions_scaffold_and_read`
  - `test_read_coverage_uses_mtime_ns_and_size_fingerprint`
- Replace `test_edit_rejects_multiple_matches` with new tests:
  - `test_edit_line_range_out_of_bounds`
  - `test_edit_overlapping_ranges`
  - `test_edit_reverse_order_application`
  - `test_edit_delete_lines`
  - `test_edit_single_line`
  - `test_edit_insert_before_line`
  - `test_edit_insert_after_line`
  - `test_edit_requires_read_coverage_for_replace`
  - `test_edit_requires_read_coverage_for_insert_anchor`
  - `test_edit_preserves_trailing_newline_when_unchanged`
  - `test_edit_preserves_missing_trailing_newline_when_unchanged`
  - `test_edit_trailing_newline_in_new_string_does_not_add_blank_line`
  - `test_edit_rejects_empty_file_without_supported_anchor`
  - `test_edit_rejects_duplicate_ranges`
  - `test_edit_rejects_insert_inside_replacement_range`
  - `test_read_empty_file_reports_zero_lines`
  - `test_edit_clears_read_coverage_after_success`
  - `test_single_read_allows_one_batch_edit_with_multiple_covered_ranges`
  - `test_edit_leading_newline_in_new_string_creates_intentional_blank_line`

## Migration

This is a breaking change with no backward compatibility. All callers (LLM
prompts, tool descriptions) will reference the new parameter format. The
`old_string` field is removed entirely. Callers must read the target lines before
calling `edit`; line-level read coverage is now part of the tool contract.

## Open Questions

- Whether to add a future `insert_into_empty_file` operation. The first
  implementation rejects empty-file edits to avoid inventing an anchor line that
  `read` could not have returned.
- No regex, fuzzy matching, or multi-file support in this design.
