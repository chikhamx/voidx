---
name: writing-plans
description: Use when turning a spec, requirements, or agreed design into an implementation plan before editing code.
triggers:
  - implementation plan
  - write a plan
  - planning
  - spec
  - requirements
  - 计划
  - 实施方案
  - 需求
---

# Writing Plans for voidx

Use this skill when the user has a spec, requirements, or agreed design and wants an implementation plan.

Core rule: plans must be executable — exact paths, concrete commands, voidx tool names.

## Scope Check

If the spec covers multiple independent subsystems, suggest splitting into separate plans. Each plan should produce working, testable software on its own.

## Plan Structure

1. **Goal:** one sentence.
2. **Architecture:** 2-3 sentences about approach.
3. **Tech stack:** key technologies and libraries.
4. **File structure:** list files to create or modify, with one-line responsibility per file. Files that change together should live together. Prefer smaller, focused files.
5. **Tasks:** ordered steps with checkboxes. Each step is one action (2-5 minutes): "write the failing test" is a step, "run it to confirm it fails" is a step, "implement minimal code" is a step.
6. **Tests:** targeted commands and expected results per task.
7. **Risks:** edge cases, compatibility, and rollback concerns.

## Task Template

```
## Task N: [Component Name]

**Files:**
- Modify: `path/to/file.py`

- [ ] **Step 1:** Write failing test for [behavior]
- [ ] **Step 2:** Run test, confirm it fails for the expected reason
- [ ] **Step 3:** Implement minimal code to pass
- [ ] **Step 4:** Run test, confirm it passes
- [ ] **Step 5:** Run broader test set
```

## Execution

After the plan is approved, follow test-driven-development for each task. Before claiming any task complete, follow verification-before-completion.

Do not force git worktrees or commits unless the user asked for them.
