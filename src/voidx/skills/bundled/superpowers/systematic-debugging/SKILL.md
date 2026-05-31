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
  - test failure
  - build failure
  - 报错
  - 失败
  - 异常
  - 崩溃
  - 排查
---

# Systematic Debugging for voidx

Use this skill before proposing or applying fixes for bugs, failed tests, build failures, crashes, or unexpected behavior.

Core rule: find the root cause before changing code.

Workflow:
1. Read the full error, traceback, logs, or failing assertion.
2. Reproduce the issue with the smallest reliable command or steps.
3. Check recent changes with read-only tools such as `grep`, `read`, and safe `bash`.
4. Form one concrete hypothesis from evidence.
5. Verify the hypothesis with a targeted command, diagnostic, or code read.
6. Only then make the smallest fix.
7. Run the reproduction command again and report the evidence.

For flaky or non-reproducible failures, gather more evidence instead of guessing.
