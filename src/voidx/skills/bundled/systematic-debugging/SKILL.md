---
name: systematic-debugging
description: Use when debugging bugs, failed tests, build failures, tracebacks, crashes, or unexpected behavior.
triggers:
  - bug
  - failed
  - failure
  - traceback
  - error
  - crash
  - broken
  - not working
  - unexpected
  - test failure
  - build failure
  - 报错
  - 失败
  - 异常
  - 崩溃
  - 排查
  - 不对
  - 结果不对
---

# Systematic Debugging for voidx

Use this skill before proposing or applying fixes for bugs, failed tests, build failures, crashes, or unexpected behavior.

Core rule: find the root cause before changing code.

## Gate

Root cause investigation must complete before proposing any fix. If you catch yourself about to suggest a fix without evidence, stop and gather more data.

## Four Phases

### Phase 1: Root Cause Investigation

1. Read the full error, traceback, logs, or failing assertion. Do not skip warnings or partial output.
2. Reproduce the issue with the smallest reliable command or steps. If not reproducible, gather more data instead of guessing.
3. Check recent changes with read-only tools: `grep`, `read`, safe `bash`. Look at git diff, recent commits, config changes.
4. For multi-component systems (CI → build → deploy, API → service → database): add diagnostic logging at each component boundary first, run once to locate where it breaks, then investigate that component.

### Phase 2: Hypothesis

5. Form one concrete hypothesis from evidence. Not "maybe it's broken" — a specific claim about what is wrong and where.

### Phase 3: Fix

6. Verify the hypothesis with a targeted command, diagnostic, or code read.
7. Only then make the smallest fix that addresses the root cause. For non-trivial fixes, follow test-driven-development: write a failing test that reproduces the bug, then implement the fix.

### Phase 4: Verify

8. Run the reproduction command again and report the evidence.
9. Run the relevant broader test set.

## Anti-Patterns

- "This bug is too simple to need investigation" — simple bugs have root causes too.
- "I'll just try this fix" — trying fixes is not debugging, it's guessing.
- Skipping phases under time pressure — rushing guarantees rework.

For flaky or non-reproducible failures, gather more evidence instead of guessing.
