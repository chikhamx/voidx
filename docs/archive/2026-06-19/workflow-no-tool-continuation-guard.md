# Spec: Workflow No-Tool Continuation Guard

> **Status: Done**

## Background

voidx 的主图采用常见 tool-calling agent 终止规则：LLM 输出 `tool_calls` 时进入 `execute_tools`，否则进入 `finalize` 并结束当前 turn。

最近出现两类可复现现象：

1. LLM 输出"Spec 已经很详细了，直接跳到实施计划。"后没有工具调用，turn 直接结束。
2. LLM 在完成文档写入后输出"文档完整且一致。现在推进 workflow："后没有调用 `workflow(...)`，turn 直接结束。

这不是工具执行失败，而是 `workflow(...)` 根本没有被调用。模型表达了"我要继续"的意图，但没有实际调用工具来推进状态。

## Goals

- 让模型在表达"我要继续"意图时，主动调用 `workflow(...)` 而不是只输出文本。
- 保持"无 tool_call 可正常结束"的主流 agent 默认语义。
- 不引入 graph router 层面的启发式分类或额外路由分支。

## Proposed Design

在全局规则（`BASE_SYSTEM.global_rules`）中添加一条 `PromptRule`，直接指导模型行为：

```
When you intend to continue work but have not called any tool this turn, call workflow(action="enter", workflow="<node>") to activate the next workflow node, or workflow(action="advance", ...) to transition the current one. Do not end a turn with only text that promises a next action.
```

这条规则放在 `src/voidx/agent/prompts.py` 的 `BASE_SYSTEM.global_rules` 列表末尾。

### 为什么选择提示词方案而非启发式 guard

| 维度 | 提示词方案 | 启发式 guard |
|------|-----------|-------------|
| 实现复杂度 | 加一行 `PromptRule` | 新增模块、修改 router、增加 state 字段、循环保护 |
| 误判风险 | 无（模型自行判断意图） | 启发式分类可能误判正常输出为"继续承诺" |
| 无限循环风险 | 无 | 需要计数器/marker 防护 |
| 可测试性 | 提示词存在即可 | 需要单元测试覆盖各种文本分类 |
| 有效性 | 依赖模型遵守规则 | 依赖启发式准确性 + 模型对 guidance 的响应 |
| 回退路径 | 效果不足时可叠加 guard | 已实现则难以简化 |

提示词方案的核心优势：**在模型决策时点直接引导行为**，而不是在模型已经做出错误决策后事后补救。

## Components

### 1. `src/voidx/agent/prompts.py`

在 `BASE_SYSTEM.global_rules` 列表末尾添加：

```python
PromptRule(
    detail=(
        'When you intend to continue work but have not called any tool this turn, '
        'call workflow(action="enter", workflow="<node>") to activate the next workflow node, '
        'or workflow(action="advance", ...) to transition the current one. '
        'Do not end a turn with only text that promises a next action.'
    ),
),
```

无其他代码改动。不新增模块、不修改 router、不增加 state 字段。

## Tests

### Prompt 存在性测试

在 `tests/test_agent/test_prompts.py`（如不存在则新建）中验证：

- `BASE_SYSTEM.global_rules` 最后一条规则包含 `workflow(action="enter"` 关键词。
- `BASE_SYSTEM.render()` 输出包含该规则文本。

### 现有测试回归

```bash
.venv/bin/python -m pytest tests/test_agent/ -v
```

## Acceptance Criteria

- `BASE_SYSTEM.global_rules` 包含指导模型在未调用工具但需要继续时调用 `workflow` 的规则。
- 规则文本出现在 `BASE_SYSTEM.render()` 输出的 `## Global Rules` 部分。
- 现有测试全部通过。

## Risks

- **模型可能不遵守规则**：提示词无法 100% 保证模型行为。但这是所有提示词引导的固有局限，且当前问题本身就是模型"忘记"调用工具，一条明确的规则比隐含期望更有效。
- **后续可叠加 guard**：如果提示词方案效果不足，仍可在 graph router 层面添加启发式 guard 作为兜底。
