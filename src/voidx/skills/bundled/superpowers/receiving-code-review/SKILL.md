---
name: receiving-code-review
description: Use when receiving review feedback, requested optimizations, or reviewer comments before implementing them.
triggers:
  - review feedback
  - code review feedback
  - reviewer says
  - feedback says
  - review comment
  - 优化点
  - 审查意见
  - 评审意见
---

# Receiving Code Review for voidx

Use this skill when the user or another reviewer gives feedback to implement.

Core rule: verify feedback against the codebase before changing code.

## Gate

Do not implement any feedback item before verifying it against the codebase.

## Workflow

1. Read the full feedback.
2. Restate the concrete requested changes if needed.
3. Check the relevant code and tests.
4. Decide whether each item is correct for this codebase.
5. Push back with technical reasons when feedback is wrong or unnecessary.
6. If feedback is valid, implement one coherent item at a time.
7. Verify with targeted tests or commands before reporting.

For implementation, follow test-driven-development. Before claiming feedback is addressed, follow verification-before-completion.

## Source-Specific Rules

### From the user (your human partner)
- Trusted intent — implement after understanding.
- Still ask if scope is unclear.
- No performative agreement. Skip to action or technical acknowledgment.

### From external reviewers
Before implementing, check:
1. Technically correct for this codebase?
2. Breaks existing functionality?
3. Does the reviewer understand the full context?

If the suggestion seems wrong, push back with technical reasoning. If it conflicts with the user's prior decisions, stop and discuss with the user first.

## Handling Unclear Feedback

If any item is unclear, stop and ask for clarification before implementing anything. Items may be related — partial understanding leads to wrong implementation.

## YAGNI Check

If a reviewer suggests "implementing properly" or adding completeness, check whether the code is actually used. If unused, question whether the addition is needed.

Do not give performative agreement. Technical correctness and the user's intent decide what changes.
