# Soft-Limit Read Coverage for File Edits

Date: 2026-08-07

> **Status: Approved design; awaiting implementation**

## Goal

Change file-edit tools so unread coverage is a **soft limit**, not a hard block:

- Prefer matching and applying the edit when the target can be resolved uniquely.
- Only fail with a “read first, then write” hint when matching fails (or when external staleness is detected).
- Apply this to **all write paths**: `replace`, `write` insert, and `write` full overwrite.

## Current State

Relevant code:

- `src/voidx/tooling/application/file_state.py`
  - `check_read_coverage()` — returns a blocking error when the edit range is not covered by prior `read` ranges
  - `check_staleness()` — returns a blocking error when the file fingerprint changed since last tracked read/write
  - `covered_read_range()` / `record_read_range()` / `remap_read_coverage_from_file_diff()` — coverage tracking and post-edit remap
- `src/voidx/tooling/domain/file_tracking.py`
  - `EDIT_COVERAGE_GAP_TOLERANCE = 3` — small unread gaps still count as covered
- `src/voidx/tooling/builtin/file/replace.py`
  - `_execute_text_replace()`: resolves anchors first, then hard-fails on `check_read_coverage` even after a successful match
  - `_apply_resolved_edits()`: second coverage gate when `coverage_checked=False`
- `src/voidx/tooling/builtin/file/write.py`
  - `_execute_write_insert()`: hard-fails on `check_read_coverage` for insert/overlap lines
  - `_execute_write_full()`: hard-fails if the existing file was never recorded in `ctx.file_tracking.mtimes`
- Tests expecting hard failures:
  - `src/tests/test_tooling/file/test_edit_coverage.py`
  - `src/tests/test_tooling/file/test_coverage_fingerprint.py`
  - `src/tests/test_tooling/test_replace_failure_logging.py`
  - related insert coverage cases in `src/tests/test_tooling/file/test_edit_line_insert.py`

Current hard-fail messages:

```text
Lines {start}-{end} in {path} must be read before editing.
Retry after reading lines {start}-{end}.

File must be read before full overwrite: {path}. Please read the file first.
```

Observed pain:

- Anchor match can already be unique and correct, but the tool still rejects the edit because some lines were never read (or only partially covered).
- Models then re-read and re-issue the same edit, wasting turns.
- Same-file descending execution and drift fallback already mitigate line-number drift; coverage hard-fail undoes that benefit after a successful match.

## Non-goals

This change does not:

- remove `check_staleness` hard failures for externally modified files;
- change permission / path access checks;
- change same-file descending line-order execution or result-order restoration;
- change anchor resolution, drift fallback, or overlap/collapse behavior;
- redesign coverage tracking storage, fingerprint format, or remap logic after successful edits;
- auto-read files on the model’s behalf;
- weaken uniqueness requirements for anchors (ambiguous match still fails).

## Design

### Principle

**Match-first, coverage-second.**

1. Resolve the edit target (anchors / lineno / full file).
2. If resolution succeeds uniquely, apply the write.
3. If resolution fails, return the existing match/locate error.
4. When resolution fails **and** the file (or required range) was never read, append a short read-first hint.
5. Keep `check_staleness` as a hard pre-write guard whenever the file has been tracked before.

Coverage remains useful for:

- post-success remap of already-read ranges;
- optional soft hints on match failure;
- future analytics / logging.

Coverage is **not** a gate that blocks a successfully resolved edit.

### Behavior by path

| Path | Success condition | Soft-limit failure | Still hard-fail |
|---|---|---|---|
| `replace` | Anchor(s) resolve uniquely (with existing drift fallback) | Match miss / ambiguous; if never read or range unread, add read-first hint | Permission, missing file (unless auto-create path), staleness, IO |
| `write` insert | `lineno` is valid for insert and overlap can be resolved | Invalid lineno / unresolvable overlap; add read-first hint when useful | Permission, missing file, staleness, IO |
| `write` full | Existing file is readable (or file is being created) | N/A for “unread”; only real read/write errors | Permission, staleness (if previously tracked), IO |
| `write` append | Unchanged: append does not require prior coverage | — | Permission, missing file, IO |

### Detailed rules

#### 1. `replace`

File: `src/voidx/tooling/builtin/file/replace.py`

Current order in `_execute_text_replace()`:

1. resolve path + staleness
2. read file text
3. match anchors (with drift fallback)
4. **hard `check_read_coverage` on matched range** ← remove as hard gate
5. apply edit + remap coverage

Target order:

1. resolve path + staleness (unchanged)
2. read file text
3. match anchors (unchanged)
4. on match success → apply edit (no coverage block)
5. on match failure → return match error; if no useful coverage for the requested range, append:

```text
Hint: read the target lines first, then retry the edit.
```

Also remove / bypass the secondary hard coverage loop in `_apply_resolved_edits()` for paths that already resolved content by match or explicit insert conventions. Prefer deleting the hard gate rather than leaving a dead `coverage_checked` flag if no callers still need it.

Logging:

- Do **not** log successful soft-limit writes as `replace_failed`.
- Match failures continue to use existing failure logging.
- Optional metadata flag is allowed but not required: e.g. `coverage_soft_bypassed=true` when an edit succeeds without prior coverage. Prefer minimal change; only add if useful for tests/debug.

#### 2. `write` insert

File: `src/voidx/tooling/builtin/file/write.py`

Current: after resolving insert lineno/overlap, hard-fails via `check_read_coverage`.

Target:

- If insert position is valid for the current file contents, apply insert.
- Do not require prior read coverage for the insert line or overlapped neighbors.
- Keep overlap detection behavior unchanged; overlap still uses actual file lines at apply time.
- If lineno is out of range or otherwise invalid, return the existing validation error. When the file was never read, a short read-first hint may be appended.

#### 3. `write` full overwrite

File: `src/voidx/tooling/builtin/file/write.py` (`_execute_write_full`)

Current hard gate:

```python
if str(path.resolve()) not in ctx.file_tracking.mtimes:
    return ToolResult(... "File must be read before full overwrite" ...)
```

Target:

- Remove this unread hard gate.
- If the file exists:
  - still run `check_staleness` when the path is already tracked in `mtimes` (external change remains hard-fail);
  - if never tracked, allow overwrite after a normal filesystem read for diff/snapshot purposes;
  - keep `save_file_version` / structured diff / post-edit format / coverage remap behavior.
- Creating a new file remains allowed without prior read.

#### 4. Staleness remains hard

`check_staleness()` stays a hard failure on all existing-file edit paths that already consult it.

Rationale: external modification means the model’s anchors/line numbers may be wrong even if a string still happens to match somewhere. Re-read is the correct recovery.

#### 5. Coverage tracking after success

Unchanged:

- successful edits still snapshot old ranges and call `remap_read_coverage_from_file_diff` where they do today;
- reads still record ranges via `record_read_range`;
- `EDIT_COVERAGE_GAP_TOLERANCE` remains for any remaining coverage queries / soft hints.

Optional cleanup (not required for the first patch):

- keep `check_read_coverage()` for soft-hint generation or tests;
- or narrow it to a helper that returns structured coverage status instead of a blocking message.

### Error / hint copy

Prefer short, actionable text.

Match failure with unread context (example):

```text
{existing match error}
Hint: read lines {start}-{end} in {path}, then retry.
```

If no specific line range is known:

```text
{existing match error}
Hint: read the file first, then retry the edit.
```

Do **not** keep using the old hard-fail-only phrasing as the primary error when a match already succeeded. That path should succeed silently (aside from normal diff output).

### Invariants after the change

1. A uniquely matched `replace` is applied even if the matched lines were never read.
2. A valid `write` insert is applied even if the insert neighborhood was never read.
3. A `write` full overwrite of an existing untracked file is applied without a prior `read` tool call.
4. Ambiguous or missing anchors still fail without modifying the file.
5. Externally modified tracked files still hard-fail via staleness until re-read.
6. Same-file descending execution order and UI/tool-message original-call order restoration remain unchanged.
7. Successful edits still update fingerprint/mtime tracking and remap coverage when applicable.

## Implementation Plan

Ordered, small steps:

1. **`replace` soft-limit**
   - Remove hard `check_read_coverage` after successful match in `_execute_text_replace`.
   - Remove or neutralize hard coverage loop in `_apply_resolved_edits`.
   - On match failure, optionally append read-first hint when coverage is missing.

2. **`write` insert soft-limit**
   - Remove hard `check_read_coverage` block in `_execute_write_insert`.
   - Keep lineno/overlap validation.

3. **`write` full soft-limit**
   - Remove “must be read before full overwrite” gate for untracked existing files.
   - Preserve staleness check for tracked files.

4. **Tests**
   - Update hard-fail expectations to soft-limit expectations.
   - Add/adjust cases listed below.

5. **Docs / comments only if needed**
   - No user-facing tool schema change is required unless descriptions currently promise hard coverage failures. Update tool descriptions only if they explicitly document the old hard rule.

## Tests

Primary files:

- `src/tests/test_tooling/file/test_edit_coverage.py`
- `src/tests/test_tooling/file/test_coverage_fingerprint.py`
- `src/tests/test_tooling/file/test_edit_line_insert.py`
- `src/tests/test_tooling/file/test_write_file.py` (or nearest full-overwrite tests)
- `src/tests/test_tooling/test_replace_failure_logging.py`

Required expectation changes:

| Case | Old | New |
|---|---|---|
| `replace` unique anchor on unread lines | error `must be read` | success; file updated |
| `replace` span across large unread gap with unique bounds | error `must be read` | success if anchors resolve |
| `replace` match miss on never-read file | match error only | match error + optional read hint |
| `write` insert without prior read | coverage error | success when lineno valid |
| `write` full overwrite without prior read | `File must be read before full overwrite` | success |
| tracked file externally modified | staleness error | still staleness error |
| ambiguous anchors | no write | still no write |

Suggested focused commands:

```bash
./test.py --backend -- src/tests/test_tooling/file/test_edit_coverage.py -q
./test.py --backend -- src/tests/test_tooling/file/test_coverage_fingerprint.py -q
./test.py --backend -- src/tests/test_tooling/file/test_edit_line_insert.py -q
./test.py --backend -- src/tests/test_tooling/file/test_write_file.py -q
./test.py --backend -- src/tests/test_tooling/test_replace_failure_logging.py -q
```

Broader verification after green focused runs:

```bash
./test.py --backend -- src/tests/test_tooling/file -q
```

## Risks

1. **Blind edits** — models may change code without reading nearby context. Mitigation: anchors still require unique match; ambiguous edits fail; staleness still forces re-read after external changes.
2. **Wrong-line unique anchors** — a repeated-looking substring that is actually unique elsewhere could still match. This is already true today after a full-file read; soft-limit does not create a new matching algorithm.
3. **Test churn** — several tests encode the old hard gate and must be rewritten carefully so they still protect staleness and ambiguous-match behavior.
4. **Hint noise** — only attach read-first hints on failure paths, never on success.

## Rollback

Revert the `replace.py` / `write.py` gate removals and restore the previous coverage tests. No schema migration or on-disk format change is involved.

## Acceptance Criteria

- [ ] Successful unique `replace` no longer fails solely due to unread coverage.
- [ ] Valid `write` insert no longer fails solely due to unread coverage.
- [ ] Existing-file `write` full overwrite no longer requires a prior `read` tool call when the file is untracked.
- [ ] `check_staleness` hard-fail behavior remains for tracked externally modified files.
- [ ] Ambiguous/missing anchor behavior remains fail-closed with no file change.
- [ ] Focused backend tests above are green.
- [ ] No intentional changes to same-file execution ordering or post-success coverage remap.

## Open Questions

None for the first implementation. Deferred optional follow-ups:

- coerce string `line_no` values for same-file sort stability;
- expose real execution order in logs/UI (display order already restored to call order).
