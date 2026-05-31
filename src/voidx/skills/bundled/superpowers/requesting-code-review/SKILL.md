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

In voidx, request review with `agent(review)` when available.

Review brief should include:
1. What changed.
2. Requirements or plan being checked.
3. Files changed or relevant diff range.
4. Verification already run.
5. Specific risks to inspect.

Act on review findings by severity. Fix correctness, security, and broken behavior before proceeding. Push back if the review is wrong and explain the evidence.
