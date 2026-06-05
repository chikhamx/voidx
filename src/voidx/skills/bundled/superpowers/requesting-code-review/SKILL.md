---
name: requesting-code-review
description: Use after substantial implementation work, complex bug fixes, or before merging to request a focused review.
triggers:
  - request review
  - ask for review
  - before merge
  - pre-merge
  - review this change
  - 复核一下
  - 合并前
---

# Requesting Code Review for voidx

Use this skill after substantial implementation work, complex bug fixes, or before merge.

Core rule: review early, review often.

## Gate

Do not merge to main or mark substantial work complete without requesting review.

## When to Request

**Mandatory:**
- After completing a major feature.
- Before merge to main.

**Valuable:**
- When stuck — a fresh perspective helps.
- After fixing a complex bug.
- Before refactoring — establish a baseline.

## How to Request

In voidx, request review with `agent(review)` when available.

Review brief must include:
1. What changed.
2. Requirements or plan being checked.
3. Files changed or relevant diff range.
4. Verification already run.
5. Specific risks to inspect.

## Acting on Feedback

When review feedback arrives, follow receiving-code-review.

- Fix correctness, security, and broken behavior before proceeding.
- Fix important issues before moving to the next task.
- Note minor issues for later.
- Push back if the review is wrong and explain the evidence.

## Anti-Patterns

- Skipping review because "it's simple."
- Ignoring critical issues and proceeding.
- Arguing with valid technical feedback.
