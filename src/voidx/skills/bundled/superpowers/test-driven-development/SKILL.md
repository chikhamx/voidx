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

Workflow:
1. Identify the smallest behavior to prove.
2. Add or update a focused test.
3. Run the targeted test and confirm it fails for the expected reason.
4. Implement the smallest code change that makes the test pass.
5. Run the targeted test again.
6. Refactor only after the test is green.
7. Run the relevant broader test set before reporting completion.

Allowed exceptions: pure documentation, prompt-only edits, generated assets, or configuration-only changes. If you skip TDD for one of these, say why briefly.
