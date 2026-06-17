# Tool Rename — 技术设计文档

> **Date**: 2026-06-16
> **Status**: Done

## Context

部分工具 ID 命名偏长（`load_doc_template`、`load_skills`、`plan_checkpoint`、`compact_context`），在 prompt 和 tool schema 中占用额外 token，且 `load_` 前缀对 LLM 选择工具没有实质帮助。缩短 ID 可减少 prompt 开销、提升可读性。

## Goals and Non-Goals

### Goals

- 将四个工具 ID 缩短为更简洁的名称
- 更新所有代码引用（tool 定义、registry、permission、prompt、UI display、workflow nodes）
- 保持行为和 schema 不变

### Non-Goals

- 不改 `task_status`、`advance_workflow` 等其他工具
- 不改工具的参数 schema 或执行逻辑
- 不改文件名（文件名保持语义清晰，与 class 名对应）

## Rename Mapping

| 原 ID | 新 ID | 理由 |
|-------|-------|------|
| `load_doc_template` | `document` | 加载文档模板，`document` 一词足够 |
| `load_skills` | `skill` | 加载技能，`skill` 一词足够 |
| `plan_checkpoint` | `checkpoint` | 提交计划检查点，`checkpoint` 一词足够 |
| `compact_context` | `compact` | 压缩上下文，`compact` 一词足够 |

## Affected Files

按引用类型分组，每个文件列出需改的字符串。

### Tool 定义（`id` 字段）

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/load_doc_template.py` | `id = "load_doc_template"` → `id = "document"` |
| `src/voidx/tools/load_skills.py` | `id = "load_skills"` → `id = "skill"` |
| `src/voidx/tools/plan_checkpoint.py` | `id = "plan_checkpoint"` → `id = "checkpoint"` |
| `src/voidx/tools/compact_context.py` | `id = "compact_context"` → `id = "compact"` |

### Registry

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/registry.py` | import 和 register 调用不变（用 `.id` 动态取值），无需改 |

### Permission rules

| 文件 | 改动 |
|------|------|
| `src/voidx/permission/rules.py` | `Rule(permission="plan_checkpoint", ...)` → `"checkpoint"` |
| | `Rule(permission="compact_context", ...)` → `"compact"` |
| | `Rule(permission="load_skills", ...)` → `"skill"` |
| | `"CompactContext": "compact_context"` → `"compact"` |
| | frozenset 中 `"load_skills"`, `"compact_context"` → `"skill"`, `"compact"` |

### Prompt / Agent config

| 文件 | 改动 |
|------|------|
| `src/voidx/agent/agents.py` | tool 列表中四处旧名 → 新名；注释中 `load_skills` → `skill` |
| `src/voidx/agent/graph/core.py` | compaction prompt 文本中 3 处 `compact_context` → `compact` |
| `src/voidx/agent/graph/runtime_guards.py` | `LOW_VALUE_REPETITIVE_TOOLS` frozenset 中 `plan_checkpoint` → `checkpoint` |
| `src/voidx/agent/graph/tool_executor.py` | comment 中 `compact_context` → `compact`；set 中 `plan_checkpoint`, `compact_context` → `checkpoint`, `compact` |

### Workflow nodes

| 文件 | 改动 |
|------|------|
| `src/voidx/workflow/nodes.py` | `plan_checkpoint` → `checkpoint`；`load_doc_template` → `document`；step description 中文本引用 |

### UI display

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/output/display_policy.py` | dict key 和 `tool_name` 四处旧名 → 新名；注释中旧名 → 新名 |
| `src/voidx/ui/output/dock/nodes.py` | `"plan_checkpoint": "Checkpoint"` → `"checkpoint": "Checkpoint"`；elif 分支 |
| `src/voidx/ui/output/console/app.py` | `"plan_checkpoint": "checking"` → `"checkpoint": "checking"` |

### Tool 内部字符串

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/load_skills.py` | `title="load_skills failed"` → `title="skill failed"`；truncation 消息中 `load_skills` → `skill` |
| `src/voidx/tools/compact_context.py` | `title="context compacted"` 不变（语义正确） |

## 不改的部分

- `docs/archive/` 下的旧文档：已归档，不追溯修改
- `src/voidx.egg-info/SOURCES.txt`：构建产物，自动生成
- 测试文件：需同步更新工具名引用（见下方）

## 测试影响

需 grep 测试目录中所有旧名引用并同步替换，预计涉及：
- `tests/` 下引用 `plan_checkpoint`、`compact_context`、`load_skills`、`load_doc_template` 的测试用例

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 不改文件名 | 改文件名与 ID 一致 | 文件名保持语义清晰（`load_doc_template.py` 比 `document.py` 更易定位），class 名同理 |
| 不改 class 名 | `CompactContextTool` → `CompactTool` | class 名是内部实现细节，不影响 prompt token，改动收益低 |

## Open Questions

- 无
