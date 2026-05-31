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

Workflow:
1. Read the full feedback.
2. Restate the concrete requested changes if needed.
3. Check the relevant code and tests.
4. Decide whether each item is correct for this codebase.
5. Push back with technical reasons when feedback is wrong or unnecessary.
6. If feedback is valid, implement one coherent item at a time.
7. Verify with targeted tests or commands before reporting.

Do not give performative agreement. Technical correctness and the user's intent decide what changes.
