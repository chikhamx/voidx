# Insert Boundary Overlap

> **Status: Done** — Archived on 2026-07-16.

## Context

`replace` tolerates a small amount of unchanged surrounding content in `new_string`: when the replacement prefix or suffix exactly matches adjacent file lines, it consumes up to three matching non-empty lines. `write(op="insert")` currently performs a literal splice and does not use that compatibility behavior.

This difference allows an insertion payload that includes the insertion point's existing header lines to duplicate those lines. The observed case inserted two tests before a decorated test function while also ending `new_string` with the existing decorator and function signature.

## Goals

- Share one boundary-overlap algorithm between `replace` and `write(op="insert")`.
- Enable overlap handling by default for both sides of every insert operation.
- Preserve the current matching limits: exact, contiguous, non-empty lines, with at most three lines consumed per side.
- Require read coverage for every existing line in the effective edit range.
- Make overlap behavior observable through structured metadata and a concise result hint.
- Treat an overlap-resolved insertion that leaves the file unchanged as a no-op.

## Non-goals

- Do not add overlap handling to `write(op="append")`.
- Do not add overlap handling to `write(op="write")`.
- Do not change `manage`, which does not write file content.
- Do not perform global duplicate detection, fuzzy matching, whitespace normalization, or searches across empty lines.
- Do not add a caller-controlled flag; insert overlap handling is always enabled.
- Do not change the maximum overlap from three lines.

## Architecture

Add `src/voidx/tools/file/overlap.py` as the single owner of line-boundary overlap resolution.

```python
@dataclass(frozen=True)
class LineOverlap:
    head: int
    tail: int


def resolve_overlap(
    before_lines: Sequence[str],
    new_lines: Sequence[str],
    after_lines: Sequence[str],
    *,
    limit: int = 3,
) -> LineOverlap:
    ...
```

The resolver is pure. It does not depend on `ToolContext`, file paths, read coverage, diff generation, or persistence.

`replace` and `write.insert` retain responsibility for:

- dividing original content into `before_lines` and `after_lines`;
- validating read coverage for the effective edit range;
- composing final lines using the returned counts;
- preserving trailing-newline semantics;
- saving file versions, writing content, remapping coverage, and rendering diffs.

Because `write.insert` and `write.append` currently share the lower-level splice path, overlap resolution must be enabled explicitly by the insert execution path. The shared splice path must not infer overlap behavior from `ResolvedEdit(operation="insert")`, which would also change append semantics.

## Overlap Semantics

Resolution preserves the existing replace behavior:

1. Find the largest head match from `limit` down to one.
2. A head candidate matches only when the first N replacement lines exactly equal the last N lines before the edit boundary.
3. Reject any candidate containing an empty replacement line.
4. After resolving head overlap, find the largest tail match from the remaining replacement-line budget down to one.
5. A tail candidate matches only when the last N replacement lines exactly equal the first N lines after the edit boundary.
6. Reject any candidate containing an empty replacement line.
7. Enforce `head + tail <= len(new_lines)`.

Head matching retains priority over tail matching when both sides compete for the same replacement lines. Matching is case-sensitive and includes indentation and all other whitespace.

## Replace Flow

For a resolved inclusive range `start_line..end_line`:

```text
before = original lines before start_line
after  = original lines after end_line
overlap = resolve_overlap(before, new_lines, after)
effective range = start_line - overlap.head .. end_line + overlap.tail
result = before without consumed head + new_lines + after without consumed tail
```

Read coverage must include the effective range, not only the explicitly resolved range. Missing coverage fails before version saving or file mutation.

Existing `start_line` and `end_line` result metadata continue to report the effective range.

## Insert Flow

For `write(op="insert", lineno=L)`, where `L` is the existing 1-based insert-before position:

```text
position = L - 1
before = original[:position]
after  = original[position:]
overlap = resolve_overlap(before, new_lines, after)
result = before without consumed head + new_lines + after without consumed tail
```

Consumed existing lines are:

- head: `L - head .. L - 1`;
- tail: `L .. L + tail - 1`.

The existing requirement to read the target line remains in force when `L` addresses an existing line. Coverage expands to include every consumed adjacent line.

At beginning of file, head overlap is necessarily zero. At `lineno=total_lines+1`, tail overlap is necessarily zero; if head overlap is found, the consumed final lines require coverage. This applies because the requested operation is still `op="insert"`; `op="append"` remains unchanged.

The existing EOF hint recommending `op="append"` is retained only when insert resolves with zero head and tail overlap. When overlap is consumed, the hint is omitted because append would not be behaviorally equivalent.

If required coverage is missing, the insert fails and reports the exact range to read. It must not fall back to literal insertion.

## Results And No-op Behavior

Both tools include stable structured metadata:

```json
{
  "overlap": {
    "head": 0,
    "tail": 2
  }
}
```

When either count is non-zero, the result output includes one concise overlap hint. The diff remains the source of truth for the resulting content.

After coverage validation and final-content composition, compare the result with the original content. If they are equal:

- return `No changes`;
- set `operations` to `0`;
- preserve overlap metadata;
- do not save a file version;
- do not write the file;
- do not remap read coverage.

## Error Handling

- Missing coverage returns the existing coverage error style plus the effective range to read.
- Invalid insert line numbers retain current validation and error behavior.
- Empty `new_string` retains the current insert no-op behavior without overlap resolution.
- Resolver inputs with no valid overlap return `LineOverlap(head=0, tail=0)`.
- Overlap resolution never changes anchor resolution errors or stale-file checks.

## Testing

### Pure resolver tests

- Head matches of one, two, and three lines.
- Tail matches of one, two, and three lines.
- Simultaneous head and tail matches.
- Head priority when both sides compete for a limited number of new lines.
- No match, case mismatch, indentation mismatch, and whitespace mismatch.
- Empty lines are not consumed.
- Matches stop at the three-line limit.
- Beginning- and end-of-file boundaries.

### Replace integration tests

- Existing head, tail, and combined dedup behavior remains unchanged.
- The observed decorator and function-signature overlap resolves correctly for reasonable bounds.
- Missing coverage for a consumed adjacent line fails without mutation.
- Sufficient coverage permits the expanded edit.
- Effective range and overlap metadata are correct.
- A content-identical result returns `No changes`.

### Insert integration tests

- The observed two-line suffix overlap does not duplicate the following decorated function.
- One-, two-, and three-line head and tail overlaps.
- Simultaneous head and tail overlaps.
- No overlap preserves literal insert behavior.
- Empty lines and matches beyond three lines are not consumed.
- Missing head or tail coverage fails without mutation and reports the required range.
- Beginning-of-file and insert-at-EOF behavior.
- A fully overlapping insertion returns `No changes` without writing.
- Overlap metadata and output hint are present when applicable.
- Trailing-newline behavior remains unchanged.
- `write(op="append")` remains literal and does not deduplicate.
- `write(op="write")` remains a full overwrite.

## Decisions

- Overlap handling is default behavior for `write.insert`; no opt-in field is added.
- Only `write.insert` adopts the shared behavior.
- Both head and tail overlap are supported.
- Exact, contiguous, non-empty matching and the three-line limit remain unchanged.
- Missing read coverage is an error, including for lines added to the effective range by overlap resolution.
- The shared resolver is extracted instead of duplicating the algorithm or routing insert through replace.
