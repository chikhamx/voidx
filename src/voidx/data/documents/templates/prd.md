---
name: prd
display_name: Product Requirements Doc
description: 面向人类阅读的产品需求文档，适用于功能规划、范围对齐和验收定义
doc_type: prd
audience: human
---

# {product_name} — 产品需求文档

## TL;DR

<!-- 3-5 句话说明：为谁解决什么问题、做什么、不做什么、如何判断成功。 -->

## Why

> 这是一个给 {target_user} 用的 {product_type}，帮他们 {core_problem}。
> 核心差异：{differentiator}。

## Goals / Success Metrics

| Goal | Metric | Target |
|------|--------|--------|
| | | |

## Target Users

- 核心用户画像：{user_profile}
- 核心痛点：{pain_point}
- 选择理由：{why_choose_this}

## Scope / Non-Goals

### In Scope

- 

### Non-Goals

- 

## User Journey

<!-- 用流程图或结构化文字描述主路径 + 异常分支。 -->

1. 
2. 
3. 

## Feature List

<!-- 树状结构，标注优先级：🔴 MVP / 🟡 Later / ⚪ Future。 -->

```text
{product_name}
├── 🔴 模块A（MVP）
│   ├── 功能1
│   └── 功能2
├── 🟡 模块B（Later）
│   └── 功能3
└── ⚪ 模块C（Future）
    └── 功能4
```

## Feature Details

### {feature_name}

**User Value**: {why_user_needs_it}

**Trigger**: {when_user_enters}

**Expected Behavior**:

- 

**States**:

| State | Trigger | UI / Copy | User Action |
|-------|---------|-----------|-------------|
| Default | | | |
| Loading | | | |
| Success | | | |
| Failure | | | |
| Empty | | | |
| No Permission | | | |

**Acceptance Criteria**:

- [ ] 

## Data / Content Requirements

| Field | Type | Required | Validation | Notes |
|-------|------|----------|------------|-------|
| | | | | |

## User-Facing Copy

| Scenario | Copy | Notes |
|----------|------|-------|
| Page title | | |
| Empty state | | |
| Success message | | |
| Error message | | 说明原因 + 下一步 |
| Dangerous action confirm | | 说明后果 |

## Non-Functional Requirements

- **Performance**: 
- **Permissions**: 
- **Compatibility**: 
- **Security / Privacy**: 
- **Data Retention**: 

## Open Questions

- [ ] 
