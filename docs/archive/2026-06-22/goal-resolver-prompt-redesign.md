> **Status: Done**

# Goal Resolver 提示词重设计

## Context

当前 goal resolver 的提示词存在以下问题：

1. **静态规则混入动态消息** — Available Workflows、Return Fields、ResolverGoal Schema 每轮不变，却放在 HumanMessage 里，无法利用 prompt cache，且每轮浪费 token。
2. **对话历史无角色区分** — Recent Conversation Content 用 `### Content N` 格式，不区分 human/assistant，LLM 难以判断对话走向。
3. **Schema 用 Markdown 列表** — 不如 JSON template 精确，与 structured output 的 JSON schema 对齐度低。
4. **用户输入标题不准确** — "Current User Content" 不如 "Current User Question" 语义明确。

## Goals and Non-Goals

### Goals

- 将静态规则（schema、字段描述、workflow 枚举）移入 System prompt，支持 prompt cache
- Human message 只包含动态数据（状态、对话历史、用户输入）
- 对话历史区分 human/assistant 角色
- Schema 改用 JSON template 格式
- 用户输入标题改为 "Current User Question"

### Non-Goals

- 不改变 `_normalize_resolution` 等后处理逻辑
- 不改变 `ResolverGoal` Pydantic 模型
- 不改变 `_coerce_resolution` 的解析逻辑
- 不改变日志格式

## Architecture

改动集中在 `goal_resolver.py` 的三个函数，不影响外部接口：

```
_resolver_system_prompt()     ← 重写：加入 schema + field rules + workflows
_resolver_request_markdown()  ← 精简：只含 Context / Recent Conversation / Current User Question
_recent_exchanges_content()   ← 改格式：区分 Human/Assistant 角色
```

## 新版提示词

### System Prompt

> 注：实际代码中用 Python 多行字符串拼接，此处用缩进表示代码块边界。

```
You are a goal resolver. Classify the user's current turn into intent, goal, workflow, and kind_hint.

## Output Schema

Return a JSON object matching this template:

{
  "intent": "coding" or "general",
  "goal": null or "<short summary of the user's request in their language, 1-2 sentences>",
  "workflow": null or "<one of the workflows listed below>",
  "kind_hint": null or "<semantic hint: review | debug | feature | inspect | refactor | test | docs>"
}

## Field Rules

- **intent**: "coding" for codebase/workspace work; "general" for non-code conversation.
- **goal**: Short user-language summary when a workflow should start; null otherwise. Must be set exactly when workflow is set, and null exactly when workflow is null.
- **workflow**: The workflow to start, or null. Must be set exactly when goal is set.
- **kind_hint**: Optional semantic hint. Advisory only; never overrides workflow selection.

## Available Workflows

- brainstorm: Confirm requirements and design, get user approval
- debug: Locate root cause and confirm fix direction
- design: Produce a structured document that passes the reader test
- feedback: Verify and implement valid review feedback
- plan: Produce an executable implementation plan, get user approval
- review: Initiate structured code review request and collect verdict
- tdd: Complete implementation via TDD cycle, all tests green
- verify: Prove changes reach expected state with reproducible evidence
```

### Human Message

```markdown
# Context

- intent: coding
- goal: 梳理 resolve_goal 的逻辑
- active workflows: design

# Recent Conversation

## Turn 1

**Human**: 梳理一下resolve_goal的逻辑

**Assistant**: resolve_goal 的核心是 LLM 分类 + normalize 修正……

## Turn 2

**Human**: 梳理一下他的请求提示词

# Current User Question

梳理一下他的请求提示词
```

## 旧版 vs 新版对比

| 维度 | 旧版 | 新版 |
|---|---|---|
| Schema 位置 | Human message 末尾 | System prompt 开头 |
| Schema 格式 | Markdown 列表 | JSON template |
| Available Workflows | Human message | System prompt（Field Rules 下） |
| Return Fields | Human message | System prompt（Field Rules） |
| 对话历史格式 | `### Content N` 无角色区分 | `**Human**: / **Assistant**:` 明确角色 |
| 用户输入标题 | Current User Content | Current User Question |
| Human message 内容 | 静态+动态混合 | 纯动态数据 |
| Prompt cache 友好 | ❌ 每轮全量变化 | ✅ System prompt 可缓存 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|---|---|---|
| Recent Conversation 用 Markdown 角色标注 | JSON 数组 `[{human, assistant}]` | 长文本在 JSON 内需转义，LLM 对 Markdown 对话格式训练数据更多，解析准确率更高 |
| Schema 用 JSON template | 保持 Markdown 列表 | JSON template 与 structured output 天然对齐，比 Markdown 列表更精确无歧义 |
| Available Workflows 放 System prompt | 放 Human message | 每轮不变，放 System prompt 可利用 prompt cache，减少每轮 token 开销 |
| Field Rules 独立 section | 合并进 Schema 注释 | 分离后职责清晰：Schema 定义结构，Field Rules 定义语义约束 |

## 代码改动清单

### `src/voidx/agent/goal_resolver.py`

1. **`_resolver_system_prompt()`** — 重写，包含 Output Schema (JSON template) + Field Rules + Available Workflows
2. **`_resolver_request_markdown()`** — 精简为 Context / Recent Conversation / Current User Question 三个 section
3. **`_recent_exchanges_content()`** — 改为 `**Human**: / **Assistant**:` 格式，每轮用 `## Turn N` 标题

### `tests/test_agent/test_goal_resolver.py`

需更新的断言：
- L74: `"Resolve this turn into intent, goal, workflow, and kind_hint."` → 改为匹配新 system prompt 开头
- L76: `"## Recent Conversation Content"` → `"# Recent Conversation"`
- L79: `"## Current User Content"` → `"# Current User Question"`
- L81: `"## Return Fields"` → 从 human message 断言移到 system message 断言
- L82-87: 末尾内容断言 → 改为匹配新 human message 末尾

### `tests/test_agent/test_goal_resolver_advanced.py`

需更新的断言：
- L99: `"GoalResolution JSON schema" not in messages[0].content` → 保持（新 system prompt 不含此字符串）
- L314: `"## Current State"` → `"# Context"`
- L340: `"## Current State"` → `"# Context"`
- L366: `"## Return Fields" in request` → 移到 system message 断言
- L401: `"## ResolverGoal Schema" in content` → 移到 system message 断言

## Open Questions

- [ ] （已解决）JSON template 中的联合类型语法：已选用 `or` 替代 `|`，因为 `|` 在部分 LLM provider 中可能被误解为管道符。
