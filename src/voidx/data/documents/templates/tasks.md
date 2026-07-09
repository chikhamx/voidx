---
name: tasks
display_name: Implementation Tasks
description: 面向 LLM 执行的任务清单，适用于把 implementation spec 拆成小步实现、测试和验收动作
doc_type: tasks
audience: llm
---

# {feature_name} — Implementation Tasks

## Goal

<!-- 一句话说明本任务清单完成后用户可见的结果。 -->

## Preconditions

- [ ] Requirement / design source has been read: `{path}`
- [ ] Existing implementation has been inspected: `{path}`
- [ ] Target test command is known: `{test_command}`

## Task List

### 1. {task_name}

- **Files**: `{path}`
- **Change**: 
- **Test First**: `{test_command}`
- **Expected RED**: 
- **Implementation**: 
- **Expected GREEN**: 
- **Done When**: 

### 2. {task_name}

- **Files**: `{path}`
- **Change**: 
- **Test First**: `{test_command}`
- **Expected RED**: 
- **Implementation**: 
- **Expected GREEN**: 
- **Done When**: 

## Cross-Cutting Checks

- [ ] Public behavior matches the approved spec.
- [ ] Existing compatibility and data semantics are preserved.
- [ ] Error paths and boundary cases are covered.
- [ ] No unrelated refactor or opportunistic cleanup is included.
- [ ] Documentation or changelog is updated if user-facing behavior changed.

## Verification

| Command | Purpose | Expected Result |
|---------|---------|-----------------|
| `{focused_test_command}` | focused coverage | pass |
| `{regression_test_command}` | regression coverage | pass |
| `{lint_or_typecheck_command}` | static checks | pass |

## Completion Notes

<!-- Fill after implementation: changed files, tests run, known limitations. -->

- Changed files:
- Tests run:
- Known limitations:
