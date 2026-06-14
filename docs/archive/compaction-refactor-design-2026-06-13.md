# 移除 title AgentDef + compaction 改为主上下文 HumanMessage 行为 — 技术设计文档

> **Status: Done**

## Context

voidx 的 `BUILTIN_AGENTS` 中有两个 `hidden=True` 的内部 agent 定义：`compaction` 和 `title`。

- **title**：title 生成已由 `src/voidx/agent/graph/title_mixin.py` + `src/voidx/agent/graph/session_runtime.py` 独立实现，`BUILTIN_AGENTS["title"]` 和 `TITLE_PERSONA` 不再被任何运行时路径引用，属于死代码。
- **compaction**：当前作为独立 agent 运行——拼接独立 system prompt（`BASE_SYSTEM_PROMPT + COMPACTION_PERSONA`），构建独立消息列表，调用 LLM 生成 summary。这导致 compaction agent 与主 agent 上下文割裂，信息有损，且维护两套 prompt 组装逻辑。

## Goals and Non-Goals

### Goals

- 移除 `title` AgentDef 及其关联的死代码
- 将 compaction 从独立 agent 调用重构为主 agent 上下文中的 HumanMessage 级别行为
- compaction 复用主 agent 完整上下文（system prompt、tool contract、runtime state、所有消息），通过注入一条 HumanMessage 提示词让 LLM 返回结构化 summary
- 上下文溢出时通过 prune + 回退机制保证不超 context_limit

### Non-Goals

- 不改变 `CompactionService` 的 head/tail 选取、prune、overflow 判断逻辑
- 不改变 summary 写回逻辑（`_pending_summary`、`_compaction_summary`）
- 不改变 UI 状态显示（`StatusUpdated`/`StatusFinished`）
- 不改变 `SUMMARY_TEMPLATE` 的 section 结构

## Architecture

### 当前 compaction 调用链

```
turn_runner / core.py
  → _maybe_compact() / _in_turn_compact()
    → CompactionCoordinator.compact_for_live_state()
      → run_compaction_agent()
        → _build_compaction_messages()          ← 旧实现 helper，位于 src/voidx/agent/graph/compaction_coordinator.py，现已移除
          SystemMessage(BASE_SYSTEM_PROMPT + COMPACTION_PERSONA)
          HumanMessage(workflow_context)
          HumanMessage(previous_summary)
          AIMessage("Understood...")
          ...head_messages...
          HumanMessage(SUMMARY_REQUEST)
        → stream_llm(model, messages, ...)
```

### 重构后 compaction 调用链

```
turn_runner / core.py
  → _maybe_compact() / _in_turn_compact()
    → CompactionCoordinator.compact_for_live_state()
      → run_compaction_agent()
        → 获取主 agent 已编译 LLM 上下文
        → 在副本上 prune 旧 tool 输出（Layer 1）
        → 追加 HumanMessage(COMPACTION_REQUEST)
        → 如果仍超限，回退到 head_messages + HumanMessage(COMPACTION_REQUEST)
        → stream_llm(model, messages, ...)
```

### 数据流

```
主 agent 已编译 LLM 上下文消息列表
        │
        ▼
  host._compaction.prune(messages_copy)
        │
        ▼
  estimate_context_tokens() ─── 超限? ──→ head_messages + HumanMessage(COMPACTION_REQUEST)
        │                                      (回退模式)
      不超限
        │
        ▼
  pruned_context + HumanMessage(COMPACTION_REQUEST)
        │
        ▼
  stream_llm() → summary 文本
        │
        ▼
  写回 _pending_summary / _compaction_summary
  替换 head → _live_messages()
```

## Data Model

### 新增常量

```
COMPACTION_REQUEST (str)
├── 固定前缀: "Summarize the conversation above into the structured format below..."
├── template: SUMMARY_TEMPLATE 内容（section 结构 + rules）
└── previous_summary_section: 可选
    ├── 有前次 summary: "<previous-summary>\n{previous_summary}\n</previous-summary>\nPreserve still-true details..."
    └── 无前次 summary: ""
```

### 移除的数据

| 移除项 | 位置 | 原因 |
|--------|------|------|
| `BUILTIN_AGENTS["title"]` | `src/voidx/agent/agents.py` | 死代码，无运行时引用 |
| `TITLE_PERSONA` | `src/voidx/agent/agents.py` | 死代码，无运行时引用 |
| `PERSONA_PROMPTS["title"]` | `src/voidx/agent/agents.py` | 死代码，无运行时引用 |
| `BUILTIN_AGENTS["compaction"]` | `src/voidx/agent/agents.py` | 不再作为独立 agent |
| `COMPACTION_PERSONA` | `src/voidx/agent/agents.py` | 指令合并到 COMPACTION_REQUEST |
| `PERSONA_PROMPTS["compaction"]` | `src/voidx/agent/agents.py` | 不再需要 |
| `_build_compaction_messages()` | `src/voidx/agent/graph/compaction_coordinator.py` | 旧实现 helper；新实现不再构建独立消息列表 |

## API Contract

### `run_compaction_agent` — 重写后签名

- **Signature**: `async def run_compaction_agent(self, head_messages: list, previous_summary: str | None) -> str | None`
- **输入**: 不变（head_messages 仍用于回退模式）
- **行为变更**:
  1. 从 host 获取当前主 agent 已编译 LLM 上下文消息列表；这必须包含 stable SystemMessage、tool contract、runtime task state、workflow/skill context 与语义消息历史，不能只使用 `raw_semantic_messages()`
  2. 复制该消息列表，调用当前 API `host._compaction.prune(messages_copy)` 执行 Layer 1 prune；`prune()` 会原地修改，因此不得污染 live state 或缓存对象
  3. 构建 `compaction_request_text = COMPACTION_REQUEST.format(template=SUMMARY_TEMPLATE, previous_summary_section=...)`
  4. 估算 token：`estimate_context_tokens(pruned + [HumanMessage(compaction_request_text)])`
  5. 不超限 → `messages = pruned + [HumanMessage(compaction_request_text)]`
  6. 超限 → 回退到 `head_messages + [HumanMessage(compaction_request_text)]`
  7. 回退后仍超限 → 按现有逻辑使用 `truncate_head_to_budget()` 裁剪 head，再重新估算；仍超限才抛 `ValueError("compaction input exceeds context budget")`
  8. `stream_llm(host.model, messages, renderer, ...)`
  9. `save_context_frame_from_messages(..., frame_kind="compaction", agent_persona="compaction-behavior", metadata={...})`
  10. 返回 summary 文本
- **输出**: `str | None`（不变）

### `COMPACTION_REQUEST` — 新增常量

- **位置**: `src/voidx/llm/compaction.py`
- **格式**: `str`，包含 `{template}` 和 `{previous_summary_section}` 占位符

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 主上下文 prune 后仍超 context_limit | 回退到 head_messages + compaction_request |
| 回退模式也超 context_limit | 使用 `truncate_head_to_budget()` 保留 newest complete turns 后重试 |
| 裁剪后的回退模式仍超 context_limit | 抛出 `ValueError("compaction input exceeds context budget")`（与当前行为一致） |
| LLM 返回空文本 | 记录 warning，返回 `None`（与当前行为一致） |
| LLM 调用异常 | 重试最多 `COMPACTION_MAX_RETRIES` 次（与当前行为一致） |

## Test Impact

- 更新 `tests/test_compaction.py::test_run_compaction_agent_uses_main_context_request_and_extracts_text`：断言 compaction 使用主上下文副本 + trailing `HumanMessage(COMPACTION_REQUEST)`，而不是独立 `SystemMessage(BASE_SYSTEM_PROMPT + COMPACTION_PERSONA)`。
- 新增/更新回退测试：当主上下文 prune 后仍超限时，调用 `truncate_head_to_budget()` 并保存 `metadata["input_mode"] == "fallback"`。当前 metadata 只区分 `main_context` 与 `fallback`；直接 fallback 与 fallback 后 truncate 都记录为 `fallback`，不再细分子场景。
- 更新 `tests/test_agent/test_core_flow.py::test_hidden_personas_have_registered_prompts`：改为断言 `get_agent("compaction") is None` 与 `get_agent("title") is None`。
- 保留现有 retry、fallback summary、summary 写回、UI status tests。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| compaction 复用主 agent 上下文 | 保持独立 agent 但共享 system prompt | 复用上下文让 LLM 看到完整信息，summary 质量更高；独立 agent 天然信息有损 |
| HumanMessage 级别注入而非 SystemMessage | 修改 SystemMessage 追加 compaction 指令 | HumanMessage 是用户级行为，语义更准确；SystemMessage 修改会影响缓存 key |
| prune + 回退机制 | 直接截断到 head_messages | 优先使用完整上下文提升 summary 质量；回退保证不超限 |
| `agent_persona` 改为 `"compaction-behavior"` | 保持 `"compaction"` | 区分新旧模式，便于历史数据分析 |
| `COMPACTION_REQUEST` 放在 `compaction.py` | 放在 `agents.py` | 与 `SUMMARY_TEMPLATE` 同文件，减少跨文件耦合 |

## Open Questions

- [x] 主 agent system prompt 中包含 tool contract 等信息，是否会对 summary 生成质量产生干扰？结论：保留主上下文以提升摘要可见性；`COMPACTION_REQUEST` 自包含输出规则，测试覆盖主上下文路径、fallback 路径和 context frame metadata。后续若发现摘要质量问题，再基于真实输出样本调 prompt，而不是恢复独立 agent。
- [x] 回退模式下 head_messages 没有 system prompt，LLM 是否仍能正确遵循 SUMMARY_TEMPLATE？结论：`COMPACTION_REQUEST` 必须自包含全部结构化输出规则；回退路径保留裸 head_messages，但 request 本身包含 `SUMMARY_TEMPLATE` 与 previous summary merge 规则。
