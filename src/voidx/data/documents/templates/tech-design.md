---
name: tech-design
display_name: Technical Design Doc
description: 面向人类评审的技术设计文档，前半部分讲清方案，后半部分提供 LLM 可执行约束
doc_type: tech-design
audience: human+llm
---

# {feature_name} — 技术设计文档

## TL;DR

<!-- 3-5 句话说明：要解决的问题、推荐方案、主要影响、关键风险。 -->

## Context

<!-- 当前行为、问题来源、为什么现在要做。引用真实模块、接口或数据流。 -->

## Goals / Non-Goals

### Goals

- 

### Non-Goals

- 

## Proposed Design

<!-- 模块边界、数据流、关键接口。用图、表或步骤表达，避免纯散文。 -->

### Request / Data Flow

1. 
2. 
3. 

### API / Function Contract

| Name | Input | Output | Error Behavior |
|------|-------|--------|----------------|
| | | | |

## Data Model / Migration

<!-- Schema、关系、迁移策略；不涉及则写 N/A。 -->

```text
{entity_name}
├── field1: type (constraints)
├── field2: type (constraints)
└── field3: type (constraints)
```

## Decisions

| Decision | Alternatives | Rationale |
|----------|--------------|-----------|
| | | |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| | | |

## Implementation Notes for LLM

<!-- 这部分服务落地质量，可以比正文更细、更机械化。 -->

### Files / Entry Points

| Path | Expected Change | Notes |
|------|-----------------|-------|
| `{path}` | | |

### Existing Behavior

- 

### Target Behavior

- 

### Invariants

<!-- 实现过程中绝不能破坏的行为、兼容性、数据语义。 -->

- 

### Edge Cases / Failure Paths

| Case | Expected Behavior | Test Coverage |
|------|-------------------|---------------|
| | | |

### Forbidden Changes

<!-- 明确禁止 LLM 顺手重构、改接口、改语义等。 -->

- 

## Test Plan

| Scenario | Command / Check | Expected Result |
|----------|-----------------|-----------------|
| Unit | `{test_command}` | |
| Integration / Regression | | |
| Manual / Smoke | | |

## Open Questions

- [ ] 
