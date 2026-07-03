# Goal Resolver DeepSeek Structured Output 兼容修复 — 技术设计文档

> **Status: Done** — 已实现并合入。最终实现与本文档大体一致，差异见文末「实现差异」。

## Context

`resolve_goal_for_turn()` 使用 LangChain 的 `with_structured_output(ResolverGoal)` 提取结构化意图/目标/工作流。默认 `method="json_schema"`，会向 API 发送 `response_format: {type: "json_schema", ...}`。

DeepSeek 及所有走 deepseek 协议的国内 provider（qwen、zhipu、doubao、mimo、kimi、typex、minimax）**不支持** `json_schema` response_format。API 返回 `UnprocessableEntityError` → LangChain 重新抛出 → `resolve_goal_for_turn` 的 `except Exception` 捕获 → fallback 到 `intent="general", goal=null, plan=null`。

**结果**：这 9 个 provider 上的 goal resolver 永远不工作，所有 turn 都被当成 general 意图处理。用户看到的症状是"每次都解析不出"。

## Goals and Non-Goals

### Goals

- DeepSeek 协议 provider 的 goal resolver 能正常返回 structured output
- OpenAI / Anthropic / Gemini / OpenRouter 保持原有行为不变
- 不超过一次 LLM 调用

### Non-Goals

- 不改变 `ResolverGoal` schema 本身
- 不改变 fallback 逻辑（structured_output_error 等失败路径保留）
- 不改变 logging 和诊断输出

## Architecture

改动集中在 `src/voidx/agent/goal_resolver.py` 单个文件，三处小改：

### 1. 新增 import（文件头部）

```python
from voidx.llm.service import DeepSeekChatOpenAI
```

### 2. resolve_goal_for_turn 按 model 类型选择 method（line 107 附近）

```python
# Before
runnable = structured(ResolverGoal)

# After
method = "function_calling" if isinstance(model, DeepSeekChatOpenAI) else None
runnable = structured(ResolverGoal) if method is None else structured(ResolverGoal, method=method)
```

### 3. _resolver_system_prompt 移除 `## Output Schema` 块（line 235-244）

移除 JSON 模板（10 行）。`## Field Rules` 和 `## Available Workflows` 保留。
理由：`function_calling` 的 tool definition 已由 `ResolverGoal` Pydantic model 自动提供完整 schema，prompt 中重复定义反而可能让模型困惑。
```

## Data Model

无变更。`ResolverGoal` 保持不变：

```
ResolverGoal
├── intent: Literal["coding", "general"]
├── goal: str | None
├── workflow: WorkflowName | None
└── kind_hint: str | None
```

## API Contract

### resolve_goal_for_turn

函数签名不变，行为变化：

| 条件 | Before | After |
|------|--------|-------|
| `model is DeepSeekChatOpenAI` | method=json_schema → API error → fallback | method=function_calling → 正常返回 |
| `model is ChatOpenAI` (openai/openrouter) | method=json_schema → 正常 | 不变 |
| `model is ChatAnthropic` | method=function_calling (默认) → 正常 | 不变 |
| `model is ChatGoogleGenerativeAI` (gemini) | 未验证（依赖可选包 `voidx[gemini]`） | 不变 |

> **修正**：原设计文档误认为 `ChatAnthropic` 没有 `with_structured_output`。实测 `langchain-anthropic` 的 `ChatAnthropic.with_structured_output` 默认 method 为 `function_calling`，Anthropic 的 goal resolver 一直在正常工作。

### _resolver_system_prompt

移除 `## Output Schema` 块（含 JSON 模板）。其余保持不变：
- `## Field Rules`（字段语义规则）
- `## Available Workflows`（可用工作流列表）

理由：`function_calling` 方式下，output schema 由 `ResolverGoal` 的 Pydantic model 自动转换为 OpenAI tool definition 注入请求，prompt 中再写一份冗余且有冲突风险。

## Error Handling

无新增失败路径。现有 fallback 策略不变：

| 失败场景 | 处理策略 |
|---------|---------|
| function_calling 返回无法 parse（极少见） | `_coerce_resolution` → None → `invalid_structured_output` fallback |
| API 超时 | `asyncio.wait_for` → `structured_output_error` fallback |
| model 不支持 structured_output | line 98~103 提前返回 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 function_calling 而非 json_mode | json_mode（`response_format: {type: "json_object"}`） | json_mode 需要 prompt 包含 "json" 关键词 + JSON 示例，DeepSeek 有 `max_tokens` 不够导致截断的风险；function_calling 更健壮，所有 provider 都支持 |
| 只对 DeepSeekChatOpenAI 改 method | 全局改为 function_calling | OpenAI 的 json_schema 已稳定工作；Anthropic 默认即 function_calling 且正常工作；不动更安全 |
| 移除 prompt 中的 Output Schema | 保留 | tool definition 已包含完整 schema，保留会导致两份定义 |

## Open Questions

- [ ] 无。所有决策已确认。

## Test Impact

无。`## Output Schema` 在更早版本已从 system prompt 中移除，测试代码中不含相关断言。`test_goal_resolver.py:75` 断言的是 `"## Field Rules"` 和 `"## Available Workflows"`（保留），非 `"## Output Schema"`。`test_run_loop_workflow_advanced.py` 中断言 `"## ResolverGoal Schema" not in ...`（验证 prompt 中无 schema 块），与此改动方向一致。

goal_resolver 的 mock 测试（`StructuredModel`）不经过 `with_structured_output` 内部 method 切换，不受 `function_calling` 改动影响。

## 实现差异

最终实现与本文档预计的差异：

1. **import 路径**：实际为 `from voidx.llm.service import DeepSeekChatOpenAI`（通过 service 公开 API），非 `voidx.llm.provider`。功能等价。
2. **Anthropic 并非 fallback**：实测 `ChatAnthropic.with_structured_output` 存在且默认 `method="function_calling"`，Anthropic 的 goal resolver 一直正常工作。本文档 API Contract 表格的 Anthropic 行已在上述修正。
3. **Gemini 未验证**：`langchain-google-genai` 为可选依赖（`voidx[gemini]`），未实测。若新版支持 `with_structured_output`，行为与 Anthropic 类似（默认 function_calling）。
4. **Test Impact**：设计文档预测需移除 3 处 `"## Output Schema"` 断言，实际测试代码早已对齐，无需修改。
