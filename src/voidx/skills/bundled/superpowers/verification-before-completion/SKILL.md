---
name: verification-before-completion
description: Use before claiming work is complete, fixed, passing, ready, or safe to merge.
triggers:
  - done
  - complete
  - fixed
  - passing
  - ready
  - verify
  - verified
  - 完成
  - 修好了
  - 通过
  - 验证
---

# Verification Before Completion for voidx

Use this skill before saying work is complete, fixed, passing, ready, or safe to merge.

Core rule: evidence before completion claims.

Checklist:
1. Identify the command or check that proves the claim.
2. Run the full relevant command in this turn.
3. Read the exit code and failure count.
4. If the check fails, report the actual failure and next step.
5. If the check passes, report the command and result.

Do not rely on earlier runs, assumptions, or subagent summaries without verifying the actual state.
