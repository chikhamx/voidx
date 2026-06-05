---
name: writing-design-docs
description: Use when writing technical design docs, PRDs, RFCs, API docs, READMEs, or changelogs. Covers both design-phase and post-implementation documentation.
triggers:
  - design doc
  - technical design
  - architecture doc
  - RFC
  - API doc
  - API 文档
  - README
  - changelog
  - release notes
  - write docs
  - document this
  - PRD
  - product requirements
  - 需求文档
  - 产品需求
  - 技术设计
  - 架构文档
  - 接口文档
  - 写文档
  - 变更日志
---

# Writing Design Docs for voidx

Use this skill when writing documentation — design-phase docs before implementation, or post-implementation docs after code is done.

Core rule: write for the reader who has zero context. If they can't use the doc without asking you questions, the doc isn't done.

## Gate

Do not skip the reader test. Every document must pass a fresh-read check before being considered complete.

## Two Scenarios

### Scenario 1: Design Phase (after brainstorming, before writing-plans)

Write technical design docs, PRDs, and RFCs that turn an approved product design into an implementable specification.

### Scenario 2: Post-Implementation (after verification, before requesting-code-review)

Write API docs, READMEs, and changelogs that make the completed work usable by others.

## Document Types

| Type | doc_type | When to use |
|------|----------|-------------|
| Product Requirements Doc | `prd` | A product feature needs a structured spec before implementation |
| Technical Design Doc | `tech-design` | A feature needs architecture decisions before implementation |
| RFC / Decision Doc | `rfc` | A significant technical decision needs team alignment |
| API Documentation | `api-doc` | Implementation is done and others need to integrate |
| README / Usage Guide | `readme` | Implementation is done and others need to use or contribute |
| Changelog | *(no template)* | Shipping a version or merging to main |

Changelog structure: group by Added / Changed / Fixed / Removed. One line per change, written for humans. Include migration notes for breaking changes.

## Workflow

1. **Identify the document type** — which type fits? If none fit, define the structure based on the reader's needs.
2. **Gather context** — read the relevant code, specs, and existing docs. Do not write from memory alone.
3. **Load the template** — call `load_doc_template` with the appropriate `doc_type` to get the document skeleton. Fill in each section: replace `{placeholder}` fields with actual content, replace `<!-- guidance comments -->` with the corresponding section content. If information is insufficient for a field, mark it `[TBD]`. Never fabricate content.
4. **Write the first draft** — be concrete: use real paths, real command names, real field names. No placeholders except `[TBD]`.
5. **Reader test** — re-read the document as if you have zero prior context. For every statement that raises a question the doc doesn't answer, add the answer or fix the statement.
6. **Verify accuracy** — check that code examples run, paths exist, API shapes match the actual implementation.

## PRD-Specific Rules

When writing a Product Requirements Doc, apply these additional rules:

- **Proactively fill blind spots** — users without PM training won't think to specify interaction details (loading feedback, empty states, failure recovery), state tables, edge cases, or data specs. Fill these in; don't wait to be asked.
- **Dual audience** — the doc serves both developers (need technical specs) and end users (need good product copy). Keep user-facing copy and technical field descriptions separate.
- **Merge, don't overwrite** — when updating an existing PRD with new requirements, integrate new content into the existing document. Mark changes with `【Updated】`. Never rewrite the whole doc.

## Principles

- Write for the reader, not the writer. The reader doesn't know what you know.
- Start with the most useful information. Don't bury the point.
- Show, don't tell. Code examples and commands beat descriptions.
- Link, don't duplicate. Reference other docs instead of copying content.
- Outdated docs are worse than no docs. If you can't keep it current, don't write it.

## Transition

After design-phase docs are complete, follow writing-plans to create the implementation plan. After post-implementation docs are complete, follow requesting-code-review.
