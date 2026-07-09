---
name: capability-spec
display_name: Capability Spec
description: 面向 LLM 和长期维护的能力规格，适用于记录系统能力的新建、修改、兼容性和验收场景
doc_type: capability-spec
audience: llm
---

# {capability_name} — Capability Spec

## Capability Summary

<!-- 用稳定、可测试的语言描述系统现在应该具备什么能力。 -->

## Status

- **Type**: New / Modified / Deprecated
- **Owner**: {owner}
- **Related Change**: `{change_or_doc_path}`

## Requirements

### Requirement: {requirement_name}

The system MUST {required_behavior}.

#### Scenario: {happy_path}

- **GIVEN** {initial_state}
- **WHEN** {action}
- **THEN** {expected_result}

#### Scenario: {failure_or_edge_path}

- **GIVEN** {initial_state}
- **WHEN** {action}
- **THEN** {expected_result}

## Compatibility

<!-- 说明旧行为是否保留、是否 breaking、迁移窗口和回滚方式。 -->

- Public API compatibility: 
- Data compatibility: 
- Client / caller compatibility: 
- Rollback behavior: 

## Non-Requirements

<!-- 明确能力边界，避免实现时扩大范围。 -->

- 

## Acceptance Tests

| Scenario | Test Command / Check | Expected Result |
|----------|----------------------|-----------------|
| Happy path | `{test_command}` | |
| Failure path | `{test_command}` | |
| Regression | `{test_command}` | |

## Traceability

| Requirement | Source | Implementation | Tests |
|-------------|--------|----------------|-------|
| `{requirement_name}` | `{doc_path}` | `{code_path}` | `{test_path}` |
