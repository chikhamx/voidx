---
name: test-driven-development
description: Use before implementing features, bug fixes, refactors, or behavior changes.
triggers:
  - implement
  - feature
  - bugfix
  - refactor
  - behavior change
  - add support
  - fix bug
  - 实现
  - 修复
  - 重构
  - 功能
---

# Test-Driven Development for voidx

Use this skill before writing production code for a feature, bug fix, refactor, or behavior change.

Core rule: write a test that fails for the intended reason before writing the implementation.

## Red-Green-Refactor

### Red — Write Failing Test

1. Identify the smallest behavior to prove.
2. Add or update a focused test. One behavior, clear name, real code (no mocks unless unavoidable).

Good: `test('retries failed operations 3 times', ...)` — clear name, tests real behavior.
Bad: `test('retry works', ...)` — vague name, tests mock not code.

### Verify Red

3. Run the targeted test and confirm it fails for the expected reason. If it fails for the wrong reason, fix the test.

### Green — Make It Pass

4. Implement the smallest code change that makes the test pass.
5. Run the targeted test again. If it fails, adjust the implementation — do not change the test to fit the code.

### Refactor

6. Refactor only after the test is green. Run the test after each refactor step.

## Completion

7. Run the relevant broader test set before reporting completion.

## Gate

If you wrote implementation code before a failing test, delete the implementation and start from the test. Do not keep it as "reference."

## Transition

After completing implementation and verification, follow requesting-code-review for substantial changes.

## Allowed Exceptions

Pure documentation, prompt-only edits, generated assets, or configuration-only changes. If you skip TDD for one of these, say why briefly.
