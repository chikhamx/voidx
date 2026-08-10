# Soft-Limit Read Coverage for File Edits

Date: 2026-08-07
Last revised: 2026-08-10

> **Status: Approved design; awaiting implementation**

## Goal

Change file-edit tools so unread coverage is normally a **soft limit**, not a hard block:

- Allow a `replace` when non-empty anchors resolve the target uniquely, even if the matched range was not previously read.
- Allow a valid `write` insert and an existing-file full overwrite without prior read coverage.
- Preserve prior coverage as a hard requirement for single-line `replace` with `anchor=""`, because that operation trusts only `line_no` and does not verify file content.
- On unresolved edit targets, append a required read-first hint when current read coverage is missing.
- Preserve staleness, permission, path-access, validation, and IO failures.

The affected paths are `replace`, `write` insert, and `write` full overwrite. `write` append remains behaviorally unchanged.

## Current State

Relevant code:

- `src/voidx/tooling/application/file_state.py`
  - `check_read_coverage()` returns a blocking error when an edit range is not covered by prior `read` ranges.
  - `check_staleness()` returns a blocking error when the file fingerprint changed since the last tracked read or write.
  - `covered_read_range()`, `record_read_range()`, and `remap_read_coverage_from_file_diff()` track and remap coverage.
- `src/voidx/tooling/domain/file_tracking.py`
  - `EDIT_COVERAGE_GAP_TOLERANCE = 3` lets small unread gaps count as covered.
- `src/voidx/tooling/builtin/file/replace.py`
  - `_execute_text_replace()` resolves anchors and then hard-fails on `check_read_coverage`, even after a successful content match.
  - `_apply_resolved_edits()` has a second coverage gate when `coverage_checked=False`.
- `src/voidx/tooling/builtin/file/replace_resolve.py`
  - `_find_single_line_segment()` trusts `line_no` when the single bound has `anchor=""`; this is line-number resolution, not anchor matching.
- `src/voidx/tooling/builtin/file/write.py`
  - `_execute_write_insert()` hard-fails on `check_read_coverage` for insert and overlap lines.
  - `_execute_write_full()` hard-fails if an existing file is absent from `ctx.file_state.mtimes`.

Tests that directly encode the old hard-coverage behavior include:

- `src/tests/test_tooling/file/test_edit_coverage.py`
- `src/tests/test_tooling/file/test_coverage_fingerprint.py`
- `src/tests/test_tooling/file/test_edit_line_insert.py`
- `src/tests/test_tooling/file/test_edit_errors.py`
- `src/tests/test_tooling/file/test_edit_dedup.py`
- `src/tests/test_tooling/file/test_read.py`
- `src/tests/test_tooling/file/test_read_write.py`
- `src/tests/test_tooling/file/test_write_file.py`
- `src/tests/test_tooling/file/test_edit_anchors.py`
- `src/tests/test_tooling/test_replace_failure_logging.py`

Current hard-fail messages include:

```text
Lines {start}-{end} of {path} must be read before editing. Please read that range first.
Retry after reading lines {start}-{end}.

File must be read before full overwrite: {path}. Please read the file first.
```

Observed pain:

- Non-empty anchors can already identify the correct target uniquely, but coverage still rejects the edit.
- Models then repeat the same read and edit, wasting turns.
- Same-file descending execution and drift fallback mitigate line-number drift, but the coverage hard gate can still reject an otherwise successful content match.

## Definitions

- **Content-verified replace:** every required bound has a non-empty anchor and the existing resolver returns one target range. A single-bound replace with a non-empty anchor qualifies.
- **Line-number-only replace:** a single-bound replace with `anchor=""`. The resolver trusts `line_no`, so this does not qualify as a content-verified match.
- **Current coverage:** a `read_coverage` entry whose fingerprint matches the current file.
- **Missing coverage for a range:** `check_read_coverage()` returns an error for that range.

## Non-goals

This change does not:

- remove `check_staleness` failures for externally modified tracked files;
- change permission or path-access checks;
- change same-file descending execution or result-order restoration;
- change non-empty anchor resolution, drift fallback, or ambiguity rules;
- change insert overlap or adjacent-block collapse behavior;
- redesign coverage storage, fingerprint format, snapshot storage, or post-success remapping;
- auto-read files on the model's behalf;
- allow an uncovered line-number-only `replace`;
- change `write` append behavior.

## Design

### Principle

**Resolve first; use coverage according to the strength of the resolution.**

1. Run existing path authorization and staleness checks in the same places they run today.
2. Resolve the target against current file contents.
3. If non-empty anchors resolve a `replace` uniquely, apply it without a coverage gate.
4. If a line-number-only `replace` resolves, retain the coverage gate for that exact line.
5. If resolution fails, preserve the existing match or validation error and append the required read-first hint when current coverage is missing.
6. A valid insert and an untracked existing-file full overwrite do not require prior coverage.

Coverage remains useful for:

- the line-number-only `replace` safety gate;
- required hints on failed resolution;
- post-success remapping of already-read ranges;
- future analytics or logging.

### Behavior by path

| Path | Success condition | Read-first behavior | Still hard-fail |
|---|---|---|---|
| `replace`, non-empty anchor(s) | Existing resolver returns one target, including drift fallback | No coverage gate after successful match; failed resolution gets a hint when requested range lacks current coverage | Ambiguous/missing anchor, permission, missing file unless auto-create applies, staleness, IO |
| `replace`, single empty anchor | `line_no` is valid and that exact line has read coverage | Uncovered target retains the existing coverage error and retry instruction | Out-of-range line, permission, missing file unless auto-create applies, staleness, IO |
| `write` insert | `lineno` is valid and overlap resolves under existing rules | No coverage gate; invalid `lineno` gets a file-read hint when the file has no current coverage | Invalid `lineno`, permission, missing file, staleness, IO |
| `write` full | Existing file can be read, or a new file can be created | Existing untracked file may be overwritten without prior read | Permission, staleness when tracked, read/write/snapshot IO |
| `write` append | Unchanged | No prior coverage required | Permission, missing file, staleness under the existing apply path, IO |

## Detailed Rules

### 1. `replace`

Files:

- `src/voidx/tooling/builtin/file/replace.py`
- `src/voidx/tooling/builtin/file/replace_resolve.py` only as reference; matching behavior must not change

Target flow in `_execute_text_replace()`:

1. Resolve path and enforce existing staleness behavior.
2. Read the current file text.
3. Run existing anchor and drift-fallback resolution.
4. If resolution fails:
   - return the existing resolver error;
   - if `check_read_coverage(ctx, path, start_no, end_no)` reports missing coverage, append the required hint from [Error and Hint Copy](#error-and-hint-copy);
   - log the final combined failure once through the existing `replace_failed` path.
5. If resolution succeeds with non-empty anchor(s), apply the edit without calling coverage as a hard gate.
6. If a single `anchor=""` resolves by line number, call `check_read_coverage()` for that exact resolved line and retain the current hard failure when it is uncovered.
7. Preserve overlap, collapse, snapshot, formatting, diff generation, tracking, and coverage-remap behavior.

`_apply_resolved_edits()` is currently called by the `write` insert path. Remove its hard coverage loop and delete `coverage_checked` if no caller needs it after this change. Do not leave a dead bypass flag.

Logging requirements:

- A successful soft-limit write must not emit `replace_failed`.
- Resolver failures continue to emit one failure event containing the final error and any appended hint.
- An uncovered empty-anchor replace remains a failure and continues to be logged.
- Do not add new metadata unless it is needed for diagnosis; `coverage_soft_bypassed` is not required.

### 2. `write` insert

File: `src/voidx/tooling/builtin/file/write.py`

Target behavior:

- Keep current authorization, file read, line-number validation, overlap resolution, staleness, snapshot, formatting, and diff behavior.
- Remove the hard `check_read_coverage` block from `_execute_write_insert()`.
- A valid insert succeeds without coverage for the insertion line or any neighbors consumed by overlap.
- If `lineno > total_lines + 1`, preserve the existing validation error.
- If that invalid-line failure occurs with no current coverage entry for the file, append:

```text
Hint: read the file first, then retry the edit.
```

- Do not append the hint when the file has current coverage; the existing out-of-range error is sufficient.

### 3. `write` full overwrite

File: `src/voidx/tooling/builtin/file/write.py`, function `_execute_write_full()`

Target behavior:

- Remove the “must be read before full overwrite” gate for an existing untracked file.
- If the file exists and is tracked in `ctx.file_state.mtimes`, run `check_staleness()` before reading or writing and retain its hard failure.
- If the file exists but is untracked:
  - read its current contents through `_safe_read_text()`;
  - save the old version through `save_file_version()` before writing;
  - use an empty `old_ranges` snapshot when no current coverage exists;
  - preserve structured diff, post-edit formatting, and coverage remapping.
- Creating a new file remains allowed without prior read.
- Snapshot failure or read/write failure must abort without reporting success.

### 4. Staleness remains hard

`check_staleness()` behavior is unchanged. Add tool-level regression coverage for valid `replace`, insert, and full-overwrite requests after a tracked file is externally modified. Each operation must fail without modifying the external contents.

`write` append remains unchanged and continues to use the staleness check in `_apply_single_write_edit()`.

### 5. Coverage tracking after success

Preserve current behavior:

- snapshot `old_ranges` before mutation where the path currently does so;
- call `remap_read_coverage_from_file_diff()` after a successful final file state is available;
- retain `record_read_range()` behavior for reads;
- retain `EDIT_COVERAGE_GAP_TOLERANCE` for the empty-anchor gate and failed-resolution hints;
- update fingerprint/mtime tracking after successful writes;
- clear tracking when the final file state is unavailable, as current code does.

A soft-limit success does not imply that the whole file was read. Tests must continue to ensure unrelated unseen lines are not incorrectly marked as read.

## Error and Hint Copy

Hints are required, not optional.

For failed `replace` resolution when the requested range lacks current coverage:

```text
{existing match error}
Hint: read lines {start}-{end} in {path}, then retry.
```

If no meaningful range is available, including invalid insert position on a never-read file:

```text
{existing validation error}
Hint: read the file first, then retry the edit.
```

Do not append a read-first hint when current coverage already covers the requested range. Do not replace the primary resolver or validation error with a coverage error.

Exception: an uncovered single-line `replace` with `anchor=""` intentionally retains the existing hard-coverage message because no content anchor verified the target.

## Invariants After the Change

1. A uniquely content-matched `replace` is applied even when the matched lines were never read.
2. A single empty-anchor `replace` still requires coverage for its exact target line.
3. A valid insert is applied even when its insertion or overlap neighborhood was never read.
4. An existing untracked file can be fully overwritten without a prior `read` tool call.
5. Ambiguous or missing anchors fail without modifying the file.
6. Failed resolution with missing current coverage includes the required read-first hint.
7. Externally modified tracked files still fail through staleness for valid `replace`, insert, full-overwrite, and existing append paths.
8. Existing-file full overwrite saves the old file version before mutation, including when the file was untracked.
9. Same-file execution order and UI/tool-message result order remain unchanged.
10. Successful edits update fingerprint/mtime tracking and preserve current coverage-remap semantics.

## Implementation Plan

Follow TDD for each behavior change.

1. **Add RED tests for `replace` semantics.**
   - Update unread non-empty-anchor and large-gap cases to expect success.
   - Update unread overlap-tail coverage cases to expect success.
   - Add missing/ambiguous-anchor failures with required hints and no mutation.
   - Add uncovered and covered empty-anchor cases.
   - Run the focused replace test command and confirm failures are for the new expectations.
2. **Implement `replace` soft coverage.**
   - Remove the post-match coverage gate for non-empty anchors.
   - Retain exact-line coverage for a single empty anchor.
   - Add conditional failure hints and preserve one failure log event.
   - Remove the secondary hard coverage loop and dead `coverage_checked` plumbing.
   - Re-run the focused replace tests until green.
3. **Add RED tests for insert semantics.**
   - Change unread valid insert and unread overlap-neighbor cases to expect success.
   - Add invalid-line hint/no-hint cases based on current coverage.
   - Add a tool-level insert staleness regression.
4. **Implement insert soft coverage.**
   - Remove the insert coverage block.
   - Add the conditional invalid-line hint.
   - Preserve overlap and staleness behavior.
   - Re-run focused insert tests until green.
5. **Add RED tests for full overwrite.**
   - Add existing untracked overwrite success.
   - Assert old-version snapshot creation before overwrite.
   - Add tracked externally modified failure with no mutation.
   - Assert post-success fingerprint/mtime and coverage state.
6. **Implement full-overwrite soft coverage.**
   - Remove the untracked-file gate.
   - Preserve tracked-file staleness.
   - Read and snapshot every existing file before write.
   - Re-run focused full-write tests until green.
7. **Update remaining old expectations and logging tests.**
   - Ensure successful soft-limit operations do not log `replace_failed`.
   - Preserve ambiguous/missing-anchor and empty-anchor failure logging.
8. **Run focused and broader verification.**
   - Require a non-zero collected/passed test count from each proving command.

## Tests

### Files to update or extend

- `src/tests/test_tooling/file/test_edit_coverage.py`
- `src/tests/test_tooling/file/test_coverage_fingerprint.py`
- `src/tests/test_tooling/file/test_edit_line_insert.py`
- `src/tests/test_tooling/file/test_edit_errors.py`
- `src/tests/test_tooling/file/test_edit_dedup.py`
- `src/tests/test_tooling/file/test_edit_anchors.py`
- `src/tests/test_tooling/file/test_read.py`
- `src/tests/test_tooling/file/test_read_write.py`
- `src/tests/test_tooling/file/test_write_file.py`
- `src/tests/test_tooling/test_replace_failure_logging.py`

### Required behavior matrix

| Case | Expected result |
|---|---|
| Non-empty unique anchor on unread lines | Success; file updated; no failure log |
| Non-empty unique bounds spanning a large unread gap | Success |
| Non-empty unique anchor whose replacement overlaps unread neighboring lines | Success under existing overlap rules |
| Missing anchor on unread range | Existing match error plus required line-read hint; no mutation |
| Ambiguous anchor on unread range | Existing ambiguity error plus required line-read hint; no mutation |
| Missing/ambiguous anchor on a covered range | Existing match error without an added read-first hint; no mutation |
| Empty anchor on an unread line | Existing coverage failure; no mutation |
| Empty anchor on a covered line | Success |
| Valid insert without prior read | Success |
| Valid insert consuming unread overlap neighbors | Success under existing overlap rules |
| Invalid insert line on a never-read file | Existing validation error plus file-read hint; no mutation |
| Invalid insert line on a currently covered file | Existing validation error without added hint; no mutation |
| Existing untracked full overwrite | Success; old version saved; file updated |
| New-file full write | Success |
| Tracked file externally modified before replace | Staleness error; external contents unchanged |
| Tracked file externally modified before insert | Staleness error; external contents unchanged |
| Tracked file externally modified before full overwrite | Staleness error; external contents unchanged |
| Successful edit tracking | Current fingerprint/mtime recorded; unrelated unseen lines not falsely covered |

### Focused verification

Do not add `-q`: the project test wrapper's compact parser can otherwise report a successful command with `passed: 0`, which is insufficient verification evidence.

```bash
./test.py --backend -v -- \
  src/tests/test_tooling/file/test_edit_coverage.py \
  src/tests/test_tooling/file/test_coverage_fingerprint.py \
  src/tests/test_tooling/file/test_edit_line_insert.py \
  src/tests/test_tooling/file/test_edit_errors.py \
  src/tests/test_tooling/file/test_edit_dedup.py \
  src/tests/test_tooling/file/test_edit_anchors.py \
  src/tests/test_tooling/file/test_read.py \
  src/tests/test_tooling/file/test_read_write.py \
  src/tests/test_tooling/file/test_write_file.py \
  src/tests/test_tooling/test_replace_failure_logging.py
```

Broader verification after focused tests are green:

```bash
./test.py --backend -v -- src/tests/test_tooling/file src/tests/test_tooling/test_replace_failure_logging.py
```

Both commands must show a non-zero collected test count and zero failures.

## Risks

1. **Line-number-only blind replacement:** removing every coverage check would allow `anchor=""` to edit unread content by line number alone. Mitigation: retain exact-line coverage for this case.
2. **Wrong-line unique anchor:** a weak but globally unique substring can still match unintended content. This is inherent in the existing resolver; callers should use specific anchors.
3. **Blind insertion or overwrite:** the model may write without nearby context. Mitigation: the tool still validates against current disk contents, snapshots overwritten files, and rejects tracked stale files.
4. **Snapshot regression:** moving the full-overwrite gate could accidentally skip `save_file_version()` for untracked files. Mitigation: require an explicit snapshot assertion.
5. **Hint noise:** unconditional hints would distract after a fully informed match failure. Mitigation: append only when the requested range or file lacks current coverage.
6. **Test churn:** old hard-gate expectations are distributed across several files. Mitigation: use the complete file list and behavior matrix above.

## Forbidden Changes

- Do not change anchor candidate ranking, drift mapping, span tolerance, overlap, or collapse algorithms.
- Do not change authorization or external-path permission behavior.
- Do not change same-file execution sorting or result-order restoration.
- Do not remove or weaken fingerprint-based staleness checks.
- Do not change persisted tracking or snapshot schemas.
- Do not treat empty-anchor line-number resolution as a content-verified match.

## Rollback

Restore the coverage gates in `replace.py` and `write.py`, restore the previous hard-failure tests, and remove the new hint behavior. No schema or persisted-data migration is involved.

## Acceptance Criteria

- [ ] Unread coverage does not block a uniquely content-matched `replace`.
- [ ] A single empty-anchor `replace` still requires exact-line coverage.
- [ ] A valid insert does not require prior coverage, including overlap neighbors.
- [ ] Existing untracked files can be fully overwritten without a prior `read` call.
- [ ] Existing-file full overwrite saves the old version before mutation.
- [ ] Failed resolution with missing coverage includes the required read-first hint.
- [ ] Ambiguous and missing anchors remain fail-closed with no file change.
- [ ] Tool-level staleness tests pass for replace, insert, and full overwrite.
- [ ] Successful writes update tracking and do not falsely mark unrelated unseen lines as read.
- [ ] Successful soft-limit replaces do not emit `replace_failed`.
- [ ] Focused and broader backend commands collect at least one test and pass with zero failures.
- [ ] No forbidden behavior changes are introduced.

## Open Questions

None. The empty-anchor safety rule and read-first hint behavior are decided for the first implementation.
