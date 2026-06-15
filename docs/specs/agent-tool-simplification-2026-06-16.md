# Agent Tool 调用参数简化 — 技术设计

> **Status: In Progress**

## Context

`AgentTool`（`src/voidx/tools/agent.py`）是 voidx 指派子 agent 的唯一入口。当前 `AgentInput` 有 8 个字段，其中 6 个必填，且多个 persona 存在**隐式校验**——规则未写入字段 description，LLM 无法预知，只能碰运气。实际使用中已出现连续 3 次调用被拒的情况。

## 问题分析

### 问题 1：必填参数过多

`AgentInput` 当前字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `agent` | 否（default="voidx"） | 子 agent 标识 |
| `persona` | 是 | 运行角色 |
| `description` | 是 | 任务描述（最少 12 字符） |
| `model` | 否 | 模型覆盖 |
| `max_steps` | 是 | 步数预算 |
| `delegation_reason` | 是 | 委派原因 |
| `expected_output` | 是 | 期望输出格式 |
| `parent_evidence` | 是 | 父 agent 已有证据 |

6 个必填字段，LLM 每次调用都要凑齐，容易遗漏或填写不规范。此外 `description` 有最小长度限制（12 字符），但字段 description 中未提及。

### 问题 2：多个 persona 隐式校验

`_delegation_rejection`（agent.py:190-207）和 `_review_delegation_rejection`（agent.py:210-259）对多个 persona 做了隐式校验：

#### 2a：review persona 关键词匹配

**expected_output 校验**：必须同时包含 `verdict`、`pass`、`fail`、`needs_change` 四个关键词（子串匹配，如 `"failure"` 也能匹配 `"fail"`）。

**parent_evidence 校验**：
- 必须包含文件目标标记：`changed files` / `files changed` / `review target` / `target:` / `file:` / `files:`
- 必须包含验证证据标记：`verification` / `verified` / `tests:` / `test:` / `pytest` / `not verified` / `not run` / `未验证` / `未运行`

这些规则**没有写在任何字段的 description 里**，LLM 看不到，只能被拒后猜测原因。

#### 2b：implement persona goal_type 校验

`implement` persona 要求当前 goal_type 为 `feature`、`bugfix` 或 `refactor`（agent.py:205-206），否则被拒。但 `persona` 字段的 description 只说 "Use implement only when the user explicitly asked to modify code"，未提及 goal_type 限制。

#### 2c：parallel_independent 前置条件校验

`delegation_reason` 为 `parallel_independent` 时，要求 parallel subagents 已启用（agent.py:197-198），否则被拒。但 `delegation_reason` 的枚举值 description 中未说明此前置条件。

### 问题 3：校验失败缺少示例格式指引

`_delegation_rejection` 返回的 rejection message 描述了原因（如 "must include changed files or review target"），但没有给出**示例格式**。对于关键词匹配类校验，LLM 可能反复试错——知道要包含 "changed files"，但不确定该写成什么格式。

### 问题 4：关键词匹配脆弱

- 大小写：代码做了 `.lower()` 处理，这点没问题
- 语义等价：写 "review the files" 不包含 `changed files` 或 `file:`，会被拒——但语义上已经说明了审查目标
- 中英文：`未验证`/`未运行` 被支持，但其他中文表述不在列表中

## 根因

校验的出发点是好的——确保子 agent 有足够上下文。但实现方式是**拒绝后不给示例格式**，且规则对 LLM 不可见。这导致：

1. LLM 反复试错（浪费 token）
2. 关键词匹配脆弱（语义等价但措辞不同会被拒；子串匹配导致误匹配）
3. 规则对 LLM 不可见，等于黑盒校验
4. 隐式校验不限于 review persona，implement 和 parallel_independent 也有同类问题

## 改进方案

### 方案 A：规则前置到 description（最小改动）

把所有 persona 的校验规则写进对应字段的 Field description。

**优点**：改动最小，LLM 一次填对
**缺点**：所有 persona 都看到其他 persona 的规则，description 变长；子串匹配的脆弱性仍在

### 方案 B：review 专属结构化字段

用 discriminated union，review persona 使用 `ReviewAgentInput`，含结构化的 `files_changed: list[str]` 和 `test_commands: list[str]` 字段，替代自由文本的关键词匹配。

**优点**：类型安全，校验可靠，不需要关键词匹配
**缺点**：增加类型复杂度，需要改 AgentInput 模型

### 方案 C：合并 expected_output + parent_evidence

将 `expected_output` 和 `parent_evidence` 合并为一个 `context` 字段，校验逻辑内化到 tool 执行中。

**优点**：减少一个必填参数
**缺点**：当前两个字段本身就是自由文本 `str`，合并不会丢失结构化信息；但合并后校验逻辑更难区分 expected_output 和 parent_evidence 各自的规则

### 推荐：方案 A + 方案 B 的组合

1. **短期**：方案 A——把校验规则写进 description，立即减少试错
2. **中期**：方案 B——review persona 用结构化字段替代关键词匹配

## 待定

- 方案选择需确认
- 是否同步简化 `delegation_reason` 枚举（4 个值是否都有必要）——注意 `delegation_reason` 是 `Literal` 类型，LLM 能从 schema 看到枚举选项，比关键词校验透明得多，问题性质不同
- `max_steps` 是否可改为可选（由 runtime 根据任务复杂度自动决定）
