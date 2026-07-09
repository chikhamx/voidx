---
name: implementation-spec
display_name: Implementation Spec
description: 面向 LLM 执行的实现规格，适用于把已批准的需求或设计转成可落地的工程约束
doc_type: implementation-spec
audience: llm
---

# {feature_name} — Implementation Spec

## Objective

<!-- 一句话说明这次实现必须达成的结果。 -->

## Source of Truth

| Source | Path / Link | Notes |
|--------|-------------|-------|
| Requirement | `{path}` | |
| Design | `{path}` | |
| Existing Code | `{path}` | |

## Current Behavior

<!-- 描述代码现在怎么工作，必须基于已读到的真实文件、函数、配置或接口。 -->

- 

## Target Behavior

<!-- 描述实现完成后必须出现的行为。不要写泛泛目标，要写可验证结果。 -->

- 

## Files to Change

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `{path}` | create / modify / delete | | |

## Invariants

<!-- 实现过程中绝不能破坏的兼容性、数据语义、权限、错误行为或性能约束。 -->

- 

## Implementation Requirements

### Functional Requirements

- [ ] 

### Error Handling

- [ ] 

### Data / Migration Requirements

- [ ] N/A

### API / Compatibility Requirements

- [ ] N/A

## Edge Cases

| Case | Required Behavior | Verification |
|------|-------------------|--------------|
| Empty / missing input | | |
| Invalid input | | |
| Permission denied | | |
| Duplicate / idempotent action | | |
| External dependency failure | | |

## Forbidden Changes

<!-- 限制 LLM 发散：不要顺手重构、改公共接口、扩大范围或替换既有架构。 -->

- Do not modify unrelated files.
- Do not change public API behavior unless listed above.
- Do not replace existing patterns when a local extension is sufficient.
- Do not add new dependencies unless explicitly required.

## Tests

| Test Level | Command | Expected Result |
|------------|---------|-----------------|
| Focused | `{test_command}` | |
| Regression | `{test_command}` | |
| Manual / Smoke | | |

## Definition of Done

- [ ] All functional requirements are implemented.
- [ ] Existing invariants still hold.
- [ ] Edge cases above are covered by tests or documented manual checks.
- [ ] Verification commands pass with captured output.
- [ ] No unrelated files were changed.
