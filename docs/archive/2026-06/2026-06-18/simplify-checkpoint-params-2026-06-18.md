> **Status: Done** — 代码和测试已实现并通过（`src/voidx/tools/plan_checkpoint.py`、`tests/test_tools/test_tool_registry.py`、`tests/test_tools/test_interactive_tools_clarify.py`）。

# 精简 PlanCheckpoint 工具参数 — 技术设计文档

## Context

`checkpoint` 工具当前的 `PlanCheckpointInput` 包含 6 个字段和 2 个嵌套模型（`PlanStep`、`PlanAlternative`），存在以下问题：

- **冗余字段**：`PlanStep.tool`（LLM 无需提前声明工具）、`estimated_steps`（估算不准，对用户无参考价值）、`PlanAlternative`（用户只能批准、要求文档、修改范围或拒绝，不能结构化选择某个备选方案）
- **结构冗余**：`PlanStep.files` 与 `affected_files` 语义重叠
- **描述歧义**：`affected_files` 的 "may change"、`risks` 的 "for the user"、`PlanAlternative.trade_off` 的 "was not chosen" 等描述不够精准

## Goals and Non-Goals

### Goals

- 精简 `PlanCheckpointInput` 为 4 个字段 + 0 个嵌套模型
- 优化剩余字段的描述，消除歧义
- 更新 `_build_prompt` 和相关测试

### Non-Goals

- 不改动 `PlanCheckpointResult` 输出模型（`modified_scope`/`user_feedback` 重叠问题后续清理）
- 不改动 `checkpoint` 工具的交互流程和选项
- 不改动工作流路由逻辑

## Data Model

### Before

```
PlanCheckpointInput
├── plan_summary: str
├── steps: list[PlanStep]
├── affected_files: list[str]
├── risks: list[str]
├── alternatives: list[PlanAlternative]
└── estimated_steps: int

PlanStep
├── description: str
├── files: list[str]
└── tool: str

PlanAlternative
├── name: str
├── description: str
└── trade_off: str
```

### After

```
PlanCheckpointInput
├── plan_summary: str       # "Concise implementation plan summary."
├── steps: list[str]        # "Ordered implementation steps."
├── affected_files: list[str] # "All files that may be created or modified across all steps."
└── risks: list[str]        # "Risks, edge cases, or trade-offs to consider."
```

### 变更明细

| 变更 | 原字段 | 处理 |
|------|--------|------|
| 删除 `PlanStep` 模型 | `steps: list[PlanStep]` | `steps` 简化为 `list[str]`，每项为步骤描述文本 |
| 删除 `PlanAlternative` 模型 | `alternatives: list[PlanAlternative]` | 整体移除，备选信息可写在 `plan_summary` 或 `risks` 中 |
| 删除 `tool` | `PlanStep.tool` | 随 `PlanStep` 一起移除 |
| 删除 `estimated_steps` | `estimated_steps: int` | 整体移除 |
| `files` 提至外层 | `PlanStep.files` → `affected_files` | 保留 `affected_files`，描述优化 |
| 优化 `affected_files` 描述 | "Files that may change." | → "All files that may be created or modified across all steps." |
| 优化 `risks` 描述 | "Risks or trade-offs for the user." | → "Risks, edge cases, or trade-offs to consider." |

## API Contract

### `PlanCheckpointInput` (tool input schema)

- **字段**: 见上方 After 模型
- **Breaking change**: 是 — `steps` 从 `list[object]` 变为 `list[str]`，`alternatives` 和 `estimated_steps` 移除

### 兼容策略

- 不保留旧版 `steps: list[PlanStep]` 的运行时兼容转换。旧调用如果继续传入 object step，应由 `PlanCheckpointInput` 校验失败，以便尽早暴露调用方仍在使用旧 schema。
- `alternatives` 和 `estimated_steps` 从公开 schema 中移除；若旧调用方继续传入这些字段，工具不承诺使用或展示这些信息。
- 实现时应避免添加隐式迁移逻辑，保持 LLM 可见 schema、Pydantic 输入模型和 `_build_prompt` 渲染路径一致。

### `_build_prompt` 变更

- 移除 `inp.steps` 的 `step.files` 渲染（文件信息统一在 `affected_files` 展示）
- 移除 `inp.alternatives` 渲染块
- 移除 `inp.estimated_steps` 渲染行
- `steps` 渲染简化为编号列表

Before:
```
1. {step.description} ({step.files})
```

After:
```
1. {step_text}
```

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `steps` 简化为 `list[str]` | 保留 `PlanStep` 但只留 `description` | 步骤只需描述文本，无需结构化；文件信息统一在外层 |
| 保留 `affected_files` | 删除，从 steps 推导 | 全局文件列表对用户审批有直接参考价值，独立展示更清晰 |
| 删除 `PlanAlternative` | 保留但简化 | 用户不能结构化选择某个备选方案；信息可写在 summary/risks |
| 删除 `estimated_steps` | 保留但优化描述 | LLM 估算不准，对用户决策无实际帮助 |

## Implementation Tasks

1. **修改 `src/voidx/tools/plan_checkpoint.py`**
   - 删除 `PlanStep`、`PlanAlternative` 类
   - 精简 `PlanCheckpointInput`：`steps: list[str]`，删除 `alternatives`、`estimated_steps`
   - 优化 `affected_files`、`risks` 描述
   - 更新 `_build_prompt`：简化 steps 渲染，移除 alternatives/estimated_steps 块

2. **修改 `tests/test_tools/test_tool_registry.py`**
   - 移除对 `PlanStep`、`PlanAlternative` 的 `$defs` 断言（第 131-132 行）
   - 更新为验证新 schema 结构：
     - `steps.items.type == "string"`
     - schema 中不再包含 `alternatives`、`estimated_steps`
     - checkpoint schema 不再因为 `PlanStep`/`PlanAlternative` 产生 `$defs`

3. **补充 `_build_prompt` 渲染测试**
   - 覆盖 `steps: list[str]` 渲染为编号列表
   - 覆盖 `affected_files` 和 `risks` 仍正常展示
   - 断言 prompt 不再渲染 `Alternatives:` 和 `Estimated steps:`

4. **验证**
   - 运行 `tests/test_tools/test_tool_registry.py`
   - 运行 `tests/test_tools/test_interactive_tools_clarify.py`（含 checkpoint 参数化测试）
   - 运行全量测试确认无遗漏
