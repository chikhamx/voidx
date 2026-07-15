# Insert Boundary Overlap Implementation Plan

> **Status: Done** — Archived on 2026-07-16.

> **Execution override:** Implement inline in the current dirty worktree by explicit user instruction. Preserve the dirty-tree baseline, use `./test.py`, and do not create worktrees, branches, commits, or unrelated edits. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Share replace's bounded line-overlap compatibility with `write(op="insert")` while enforcing read coverage for every consumed existing line.

**Architecture:** Add a pure resolver in `overlap.py` that reports head and tail line counts. Keep replace and insert responsible for operation-specific boundaries, coverage, persistence, diff output, and metadata; insert explicitly enables overlap so append stays literal.

**Tech Stack:** Python 3, dataclasses, pytest, voidx `ToolRegistry`, `./test.py`.

---

## File Structure

- Create `src/voidx/tools/file/overlap.py`: pure overlap resolution and result type.
- Create `src/tests/test_tools/file/test_overlap.py`: resolver unit tests.
- Modify `src/voidx/tools/file/replace.py`: use the resolver, validate expanded coverage, report metadata, and handle no-op results.
- Modify `src/voidx/tools/file/write.py`: enable overlap only for `op="insert"`, preserve append/full-write behavior, and condition the EOF append hint.
- Modify `src/tests/test_tools/file/test_edit_dedup.py`: replace integration and expanded-coverage regression tests.
- Modify `src/tests/test_tools/file/test_edit_line_insert.py`: insert overlap, coverage, metadata, no-op, and append exclusion tests.

### Task 1: Pure Line Overlap Resolver

**Files:**
- Create: `src/voidx/tools/file/overlap.py`
- Create: `src/tests/test_tools/file/test_overlap.py`

- [ ] **Step 1: Write failing resolver tests**

Cover no match, one-to-three line head and tail matches, simultaneous matches, head priority, empty-line rejection, exact whitespace matching, limit enforcement, and file boundaries.

Representative expectation:

```python
def test_resolve_overlap_matches_two_tail_lines():
    assert resolve_overlap(
        ["before"],
        ["new", "@decorator", "def existing():"],
        ["@decorator", "def existing():", "body"],
    ) == LineOverlap(head=0, tail=2)
```

- [ ] **Step 2: Run resolver tests and verify import failure**

Run:

```bash
./test.py --backend -- src/tests/test_tools/file/test_overlap.py -v
```

Expected: FAIL because `voidx.tools.file.overlap` does not exist.

- [ ] **Step 3: Implement the pure resolver**

Implement immutable `LineOverlap(head, tail)` and `resolve_overlap(before_lines, new_lines, after_lines, limit=3)`. Search largest-to-smallest, reject candidates containing `""`, prioritize head, and cap the tail budget by `len(new_lines) - head`.

- [ ] **Step 4: Run resolver tests**

Run the Task 1 command. Expected: PASS.

### Task 2: Replace Integration And Expanded Coverage

**Files:**
- Modify: `src/voidx/tools/file/replace.py:328-468`
- Modify: `src/tests/test_tools/file/test_edit_dedup.py`

- [ ] **Step 1: Write failing replace integration tests**

Add tests proving:

- the observed decorator/signature suffix overlap resolves;
- adjacent overlap outside read coverage fails without mutation;
- reading the effective range permits the edit;
- metadata includes `overlap.head` and `overlap.tail`;
- an identical final result returns `No changes`, `operations=0`, and does not write.

- [ ] **Step 2: Run the new replace tests and verify failure**

Run:

```bash
./test.py --backend -- src/tests/test_tools/file/test_edit_dedup.py -k 'coverage or metadata or decorator or no_changes' -v
```

Expected: new assertions fail because replace does not expose overlap metadata, validate expanded coverage, or short-circuit no-op writes.

- [ ] **Step 3: Refactor replace to use the resolver**

Before mutation, derive:

```python
before = list(display.lines[:start_line - 1])
after = list(display.lines[end_line:])
overlap = resolve_overlap(before, new_lines, after)
actual_start_line = start_line - overlap.head
actual_end_line = end_line + overlap.tail
```

Validate coverage against `actual_start_line..actual_end_line`, then compose:

```python
kept_before = before[:-overlap.head] if overlap.head else before
lines = [*kept_before, *new_lines, *after[overlap.tail:]]
```

Preserve the existing single-line deletion compatibility before overlap resolution. Add stable overlap metadata and a concise output hint. If joined content equals the original, return a no-op result before saving or writing.

- [ ] **Step 4: Run replace integration tests**

Run the Task 2 command, then:

```bash
./test.py --backend -- src/tests/test_tools/file/test_edit_dedup.py src/tests/test_tools/file/test_edit_trailing_newline.py -v
```

Expected: PASS.

### Task 3: Enable Overlap For Write Insert Only

**Files:**
- Modify: `src/voidx/tools/file/write.py:66-227`
- Modify: `src/voidx/tools/file/replace.py:471-562` if the shared edit application needs an explicit overlap policy/result parameter.
- Modify: `src/tests/test_tools/file/test_edit_line_insert.py`

- [ ] **Step 1: Write failing insert tests**

Add tests for:

- the observed two-line decorator/signature tail overlap;
- one-to-three line head and tail overlap;
- simultaneous head and tail overlap;
- literal insertion when no overlap exists;
- empty-line and over-limit behavior;
- missing head and tail coverage failing without mutation;
- beginning-of-file and insert-at-EOF behavior;
- fully overlapping insertion returning `No changes` without writing;
- structured overlap metadata and output hint;
- append retaining literal duplicate content;
- full write retaining overwrite behavior;
- EOF append hint appearing only when overlap is zero.

- [ ] **Step 2: Run insert tests and verify failure**

Run:

```bash
./test.py --backend -- src/tests/test_tools/file/test_edit_line_insert.py -v
```

Expected: new overlap tests fail because insert still performs a literal splice.

- [ ] **Step 3: Implement insert overlap integration**

Enable overlap explicitly from `_execute_write_insert`; do not infer it for every `ResolvedEdit(operation="insert")`. Compute overlap from the original insert boundary, require current target coverage plus all consumed head/tail lines, and pass resolved counts into the application path. Keep `_execute_write_append` and `_execute_write_full` unchanged.

Return overlap metadata for insert. Omit the EOF append hint when either overlap count is non-zero. Reuse the same no-op helper/result shape as replace rather than duplicating output construction.

- [ ] **Step 4: Run insert and write regression tests**

Run:

```bash
./test.py --backend -- src/tests/test_tools/file/test_edit_line_insert.py src/tests/test_tools/file/test_write_file.py src/tests/test_tools/file/test_read_write.py -v
```

Expected: PASS.

### Task 4: Focused And Broad Verification

**Files:**
- Verify: `src/voidx/tools/file/overlap.py`
- Verify: `src/voidx/tools/file/replace.py`
- Verify: `src/voidx/tools/file/write.py`
- Archive after verification: `docs/specs/insert-boundary-overlap-2026-07-15.md`
- Archive after verification: `docs/specs/insert-boundary-overlap-plan-2026-07-15.md`

- [ ] **Step 1: Run the complete file-tool test directory**

```bash
./test.py --backend -- src/tests/test_tools/file -v
```

Expected: PASS.

- [ ] **Step 2: Run the backend suite**

```bash
./test.py --backend
```

Expected: PASS.

- [ ] **Step 3: Inspect the final diff and status**

Confirm only the planned source, tests, and documentation changed. Preserve all pre-existing user changes.

- [ ] **Step 4: Archive completed specs**

Only after source files exist and all verification passes:

```bash
./scripts/archive.py docs/specs/insert-boundary-overlap-2026-07-15.md
./scripts/archive.py docs/specs/insert-boundary-overlap-plan-2026-07-15.md
```

Expected: both documents move under `docs/archive/` according to the repository archive script.
