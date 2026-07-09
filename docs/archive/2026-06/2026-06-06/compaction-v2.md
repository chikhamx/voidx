# 上下文结构优化 + Compaction v2 设计文档

> **Status: Done** — 归档自 `docs/designs/compaction-v2.md`，实现已于 2026-06-06 完成。

## 1. 目标

1. **最大化 prompt cache 命中率**：稳定内容靠前，易变 runtime/task state 只注入最新用户消息。
2. **消除暴力截断 fallback**：压缩失败也不再保留最后 N 条消息，而是保留完整 tail 并生成兜底 summary。
3. **避免切断当前 run loop 语义**：自动压缩发生在当前 user message append 之后、LLM 调用之前，因此 full path 必须保留"上一轮完整对话 + 当前请求"。
4. **保持跨日时间语义准确**：system 使用稳定 session date，最新用户消息使用动态 current datetime。
5. **Summary 精简去重归纳**：压缩产物注入 system prompt 底部，并通过规则和兜底摘要保留决策、工具结果和文件上下文。

---

## 2. 新消息结构

```
[SystemMessage]                         ← 非压缩轮次稳定
  VOIDX_RUNTIME_CONTEXT
  ## Base System                        ✅ 固定
  ## Role Prompt                        ✅ 固定
  ## Mode Prompt                        ✅ 固定
  ## Tool Contract                      ✅ 固定
  ## Workspace Facts                    ✅ 固定（含 workspace + platform）
  ## Project Facts                      ✅ 固定
  ## Session Date                       ✅ 固定（session 初始化日期 + 时区）
  ## Long Summary                       ⚠️ 仅压缩后变化，位于 system 尾部

[HumanMessage] (历史)                   ← 干净用户原文，无 runtime prepend
  {用户原文}

[AIMessage] / [ToolMessage] (历史)      ← 原样保留

[HumanMessage] (最新)                   ← 只有最新用户消息带易变上下文
  VOIDX_RUNTIME_CONTEXT
  ## Runtime State
  ## Current DateTime
  ## Active Skills
  ## Current Task State
  ## User Message
  {用户原文}
```

### 2.1 时间语义

- `Session Date` 在 graph 初始化时固定，例如 `2026-06-06 CST`。
- `Current DateTime` 每次构建 runtime context 时动态计算，例如 `2026-06-06 01:48 CST`。
- 这样 system prompt 在非压缩轮次保持稳定，同时"今天/昨天/刚刚"等相对时间仍可按当前轮次解释。

### 2.2 Runtime State 位置

- `Runtime State` 从 system prompt 移到 `task_sections`。
- `ContextCompiler` 只 prepend 到最后一个 `HumanMessage`。
- 历史用户消息不再被反复注入 runtime/task context，因此跨轮缓存和语义都更稳定。

### 2.3 Workspace Facts

`Workspace Facts` 包含稳定工作区和平台信息：

```text
## Workspace Facts
- Current workspace: <workspace>
- Platform: macOS arm64 (Apple Silicon)
```

---

## 3. Compaction Selection

`select_details()` 返回结构化对象，而不是用 magic string 表示模式：

```python
@dataclass(frozen=True)
class CompactionSelection:
    head: list
    tail_id: str | None
    keep_from: int
    mode: Literal["none", "normal", "full"]
```

### 3.1 Normal Path

当最近 turns 能按 token budget 保留时：

- `head = messages[:keep_from]`
- `tail = messages[keep_from:]`
- `tail_id` 是 tail 起点 user message id
- `mode = "normal"`

### 3.2 Full Path

当最近 turns 超出 preserve budget 时，不返回"不压缩"。改为计算必须保留的最小 tail：

- 如果最后一个 turn 只有当前 `HumanMessage`，说明这是当前 run loop 的未完成 turn，必须保留上一完整 turn + 当前请求。
- 如果最后一个 turn 已完成，至少保留最后一个完整 turn。
- 如果必须保留的 turn 已经从 index 0 开始，说明没有可压缩 head，`mode = "none"`。

这个规则修复了"保留最近 1 turn"在当前 run loop 中只保留当前 user message 的问题。

---

## 4. Compaction Flow

```
is_overflow?
  └─ no  → 不压缩
  └─ yes → select_details()
             └─ mode=none   → 不压缩
             └─ mode=normal → compact head, keep tail
             └─ mode=full   → compact head, keep minimum complete tail
```

`_maybe_compact()` 对 normal/full 使用同一路径：

1. 使用 `selection.head` 调用 compaction agent。
2. 成功时把 summary 写入 `_pending_summary` 和 `_compaction_summary`。
3. 失败重试后调用 `fallback_summary(selection.head)`。
4. 无论 agent 成功还是 fallback，都用 `selection.keep_from` 保留 tail。
5. 删除旧的按 message 数截断逻辑，避免切断 tool-call turn。

---

## 5. Summary 策略

### 5.1 LLM Summary Prompt

`SUMMARY_TEMPLATE` 保持固定结构，并增加规则：

- 去重重复请求、工具结果、进度更新。
- 优先保留 durable facts、决策、约束、未完成工作和最终工具结果。
- 有 previous summary 时，保留仍然成立的事实，删除 stale 内容，合并新事实，不重复旧 bullets。
- 保留精确路径、命令、错误字符串和标识符。

### 5.2 Prompt Budget

`build_prompt()` 不再按固定条数截断 `context_parts`，而是按字符 budget 拼接 conversation history。这样全量压缩时能包含更多短消息，同时仍限制超长工具输出。

### 5.3 Fallback Summary

fallback 不再只提取 user 文本。它保留：

- user requests
- assistant decisions/progress
- tool calls
- tool results
- detected file/path mentions

fallback 生成同样的 Markdown summary 结构，保证 compaction agent 失败时仍不丢 AI 决策和工具结果。

---

## 6. Cache 边界

`Long Summary` 放在 system prompt 最尾部。

- 非压缩轮次：system prompt 稳定，历史消息不被 runtime prepend。
- 压缩轮次：`Long Summary` 变化导致其后 cache miss，但前面的稳定 system sections 仍可复用。
- context frame 的 stable prefix hash 以 `Long Summary` 为边界，而不是以动态时间为边界。

---

## 7. 测试覆盖

新增或更新测试覆盖：

- 历史 user message 不被 prepend runtime/task context。
- system prompt 在非压缩轮次使用稳定 `Session Date`，动态 `Current DateTime` 只在最新 user message。
- full compaction 在当前 run loop 中保留上一完整 turn + 当前请求。
- fallback summary 保留 assistant 决策、tool calls、tool results 和文件路径。
- `build_prompt()` 使用 char budget，而不是固定消息条数。
- `_maybe_compact()` 不再走最后 N 条暴力截断，失败 fallback 仍保留 selection tail。
