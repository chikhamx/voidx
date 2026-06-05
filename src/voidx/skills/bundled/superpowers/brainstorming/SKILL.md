---
name: brainstorming
description: Use before creating features, building components, or modifying behavior. Explores intent, requirements, and design before implementation.
triggers:
  - create feature
  - build component
  - add functionality
  - new feature
  - design
  - brainstorm
  - refactor
  - restructure
  - 新功能
  - 实现新功能
  - 设计
  - 头脑风暴
  - 需求澄清
  - 重构
  - 重组
---

# Brainstorming for voidx

Use this skill before any creative or implementation work — creating features, building components, adding functionality, or modifying behavior.

Core rule: present a design and get user approval before writing any code.

## Gate

Do not write code, invoke implementation skills, or take implementation action until the design is presented and approved. This applies regardless of perceived simplicity.

## Anti-Pattern

"This is too simple to need a design" is where unexamined assumptions cause the most wasted work. The design can be short, but it must be presented and approved.

## Workflow

1. **Explore context** — check files, docs, recent commits to understand the current state.
2. **Ask clarifying questions** — one at a time. Understand purpose, constraints, and success criteria. If the request is ambiguous, ask before assuming. One clarifying question is better than five assumptions.
3. **Propose 2-3 approaches** — with trade-offs and your recommendation.
4. **Present design** — scaled to complexity. Get user approval. If the scope covers multiple independent subsystems, suggest splitting into separate designs.
5. **Write design doc** — save to `docs/specs/YYYY-MM-DD-<topic>-design.md`.
6. **Transition** — invoke writing-design-docs skill to write the technical design doc, then writing-plans to create the implementation plan.

## Decision Rules

- If the user explicitly says "just implement it", skip to writing-plans but still confirm the goal in one sentence first.
- If the user's request is already a detailed spec with clear requirements, confirm understanding and go directly to writing-plans.
- For small, well-scoped changes (renaming, adding a config field, fixing a typo), confirm the goal in one sentence and go directly to test-driven-development.
- For large refactors (restructuring modules, changing architecture patterns), treat as a design task and go through the full workflow.
- Do not invoke any implementation skill, write code, or take implementation action until the design is approved.
