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
  - looks good
  - should work
  - 完成
  - 修好了
  - 通过
  - 验证
  - 好了
  - 没问题了
---

# Verification Before Completion for voidx

Use this skill before saying work is complete, fixed, passing, ready, or safe to merge.

Core rule: evidence before completion claims.

## Gate

Before claiming any status:

1. Identify the command or check that proves the claim.
2. Run the full relevant command in this turn.
3. Read the exit code and failure count.
4. If the check fails, report the actual failure and next step.
5. If the check passes, report the command and result.

Only after step 5 may you make the claim.

## Common Failure Modes

| Claim | Requires | Not Sufficient |
|-------|----------|----------------|
| Tests pass | Test command output: 0 failures | Previous run, "should pass" |
| Build succeeds | Build command: exit 0 | Linter passing, logs look good |
| Bug fixed | Original symptom no longer reproduces | Code changed, assumed fixed |
| Agent completed | VCS diff shows changes | Agent reports "success" |
| Requirements met | Line-by-line checklist | Tests passing alone |

## Regression Tests

For bug fixes with regression tests, verify the red-green cycle:
1. Write the test. Run it. It must fail.
2. Apply the fix. Run it. It must pass.
3. Revert the fix. Run it. It must fail again.
4. Restore the fix. Run it. It must pass.

## Red Flags

- Using "should", "probably", "seems to" instead of reporting evidence.
- Expressing satisfaction before running verification.
- Trusting subagent success reports without independent verification.
- Relying on earlier runs or partial checks.

## Transition

If verification passes after substantial work, follow requesting-code-review. If verification fails, return to the relevant skill: test-driven-development for implementation issues, systematic-debugging for bugs.

Do not rely on earlier runs, assumptions, or subagent summaries without verifying the actual state.
