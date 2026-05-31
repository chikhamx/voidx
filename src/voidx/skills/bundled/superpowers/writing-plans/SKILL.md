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

Plan structure:
1. Goal: one sentence.
2. Context: current code paths and constraints.
3. Files: exact files to create or modify.
4. Tasks: small ordered steps with checkboxes.
5. Tests: targeted commands and expected results.
6. Risks: edge cases, compatibility, and rollback concerns.

Keep plans executable by voidx: use exact paths, concrete commands, and voidx tool names. Do not force git worktrees or commits unless the user asked for them.
