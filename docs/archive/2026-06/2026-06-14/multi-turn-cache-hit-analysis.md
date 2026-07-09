> **Status: Done**
# 多轮会话 LLM 缓存命中机制分析

## Context

voidx 在同一 session 内发起多轮会话时，每轮可能产生多次 LLM 请求（goal resolver、主循环 step、compaction、子 agent）。LLM provider（Anthropic、OpenAI 等）支持 prompt caching，按消息前缀匹配复用已处理的 token。本文分析 voidx 当前消息拼接流程中哪些因素会破坏前缀缓存命中。

## 消息拼接顺序

一次主循环 LLM 请求的最终消息结构（由 `ContextCompiler.compile_messages` 产出，`runtime_context.py:121-155`）：

```
[SystemMessage: stable sections]                    ← 位置 0
[HumanMessage: workflow context]                    ← 位置 1
[HumanMessage: skill context]                       ← 位置 2
[HumanMessage: 历史用户消息1]                        ← 位置 3
[AIMessage: 历史AI回复1]                             ← 位置 4
[ToolMessage: 历史工具结果1]                          ← 位置 5
...历史对话...
[HumanMessage: 最新用户消息 + task context prepend]   ← 位置 N
[AIMessage / ToolMessage: 当前turn新增]              ← 位置 N+1~
[HumanMessage: guidance]                            ← 末尾（可选）
[HumanMessage: todo context]                        ← 末尾（可选）
```

主循环当前不追加 convergence hints。`build_convergence_messages()` 只在子 agent 循环中追加 step hint / final prompt。

### 各层内容来源

| 层 | 来源 | 渲染函数 |
|---|---|---|
| SystemMessage (stable) | `_build_stable_sections()` | Base System + Agent Role + Mode + Tool Contract + Workspace Facts + Project Facts + Session Time + Long Summary |
| Workflow Context | `render_workflow_context(nodes, active_names=...)` | 全量 DAG：所有 workflow 节点固定展开全文；active 状态不改变该消息内容 |
| Skill Context | `render_skill_context(instructions)` | 当前激活的 skill 指令 |
| Task Context | `_build_task_sections()` → `render_task_context()` | Runtime State + Current Task State，**prepend 到最新 user message 前面** |
| Todo Context | `current_todo_context_message()` | 当前 todo 列表，追加到消息末尾 |
| Guidance | `_drain_pending_guidance()` | 临时指导，追加到消息末尾 |
| Convergence | `build_convergence_messages()` | 仅子 agent 使用；主循环当前不追加 convergence message |

### Task Context 的拼接方式

Task Context **不是独立消息**，而是通过 `_prepend_task_context()` 拼到**最后一条 HumanMessage** 的内容前面（`runtime_context.py:662-671`）：

```
原始 user message: "帮我实现一个新功能"
拼接后: "Runtime State\n...\nCurrent Task State\n...\n\n## User Message\n帮我实现一个新功能"
```

`_last_user_index()` 从后往前找最后一条 HumanMessage，task context 拼在它前面。

## Active 状态的归属

Workflow 的 active 状态同时出现在两个位置：

| 位置 | 内容 | 变化时机 |
|---|---|---|
| **Workflow Context**（HumanMessage） | 固定全量 workflow definitions | 不随 active workflow 改变 |
| **Task Context**（prepend 到 user msg） | `Active workflow nodes`、`Workflow run state`、`Workflow exits` | 每步可能变 |

**Workflow Context 在不同 active 节点之间也保持不变。** active 状态的每步变化只体现在 Task Context 中；tool schema 广告也保持固定，实际 workflow gate 在工具执行授权层生效。

## LLM 请求类型

同一 session 内可能产生的 LLM 请求：

| # | 请求类型 | 触发时机 | 调用位置 |
|---|---|---|---|
| A | Goal Resolver | 每轮用户输入后 | `turn_runner.py:180` → `goal_resolver.py:39` |
| B | 主循环 call_llm | 每轮 step 1~N | `core.py:776` → `streaming.py:41` |
| C | Compaction Agent | 上下文溢出时 | `compaction_coordinator.py:420` |
| D | 子 Agent | agent 工具触发 | `subagent.py:205` |

### Goal Resolver

独立短请求，消息结构与主对话无共享前缀：

```
[SystemMessage: 固定 resolver prompt + GoalResolution schema]
[HumanMessage: {workspace, session_time, interaction_mode, current_intent, current_goal, pending_approval, recent_user_texts, latest_user_text}]
```

HumanMessage 每轮不同（`latest_user_text`、`recent_user_texts` 变化），SystemMessage 固定。理论上只有 resolver system prompt 具备稳定前缀；请求很短（~500 tokens），收益有限。

### Compaction Agent

消息结构与主循环不同，但并非必然零命中。当前 compaction agent 会优先使用 `source_messages + compaction request`（`input_mode=main_context`），如果未超 context limit，可能与刚才的主循环消息共享相当长的前缀；只有 fallback 到裁剪后的 `head_messages + compaction request` 时，才更接近“与主循环无共享前缀”。

参考 `goal-resolution-merge-design.md` 的合并思路，compaction request 也可以从独立 agent 请求演进为“主循环临时 guide”。区别是 compaction 本身受 context budget 约束，不能无条件内联：

- 如果编译后的主循环上下文仍能容纳 guide、预留输出和必要 tail，可在消息末尾追加临时 `VOIDX_COMPACTION_GUIDE`，让主循环 LLM 通过结构化通道提交 summary。
- 如果上下文已无法安全容纳 inline guide，继续走当前 dedicated compaction/fallback summary。
- compaction guide 应放在消息末尾，而不是像 goal resolution guide 那样贴近最新用户消息：它需要总结前面的对话，且末尾追加只影响自身缓存。

### 子 Agent

每次创建全新 `ContextCompilerCache()`，对象级增量缓存与主循环隔离。子 agent 内部 step 间可天然命中（SystemMessage + Workflow/Skill + task_description 不变），但与主循环之间没有显式复用；provider 级缓存仍取决于最终序列化前缀是否相同。实际通常不同，因为子 agent 有独立 persona、runtime constraints、tool contract 和 task_description。

## 动态因素分析

### 同一 workflow 节点内

| 层 | 是否每步变 | 对前缀缓存的影响 |
|---|---|---|
| SystemMessage (stable) | ❌ 不变 | — |
| Tool Defs (`bind_tools`) | ❌ 不变 | — |
| Workflow Context | ❌ 不变 | — |
| Skill Context | ❌ 不变 | — |
| 历史对话 | ❌ 不变 | — |
| **Task Context** | ⚠️ **每步可能变** | 最新 user message 之后失效 |
| Todo Context | ⚠️ 不定期 | 只影响末尾 |
| Guidance | ⚠️ 不定期 | 只影响末尾 |
| Convergence | N/A | 主循环当前不追加；子 agent 中只影响末尾 |

**同一节点内，真正每步可能破坏前缀缓存的只有 Task Context。** 它包含以下每步可能变化的字段：

- `Current persona` — persona 切换时变
- `Workflow run state` — workflow 状态推进时变
- `Latest user request` — 多轮对话中每轮不同（同一 turn 的 step 内通常不变）
- `Pending approval` — 审批状态变化时变
- `Workflow exits` — workflow 节点切换时变

### Workflow 节点切换时

| 层 | 变化 | 影响 |
|---|---|---|
| **Tool Defs** | 固定为 agent identity 可见工具集 | ✅ 不因 workflow 切换破坏 provider tool-schema 前缀 |
| **Workflow Context** | 固定全量 workflow definitions | ✅ 不因 active workflow 切换破坏消息前缀 |
| **Task Context** | persona、workflow state 等变化 | 🟡 最新 user message 之后失效 |

各 workflow 节点仍声明自己的允许工具和 gate，但这些规则不再改变 LLM 看到的 tool schema。`_call_llm` 固定绑定 agent identity 的工具集；`execute_tools` / permission layer 根据当前 active workflow_runs 拦截不允许的工具。

## 缓存命中率估算

以下比例是基于消息形状的经验估算，不是 provider 实测值。真实命中率还取决于 provider 的 prompt cache 规则、tool schema 是否参与前缀、模型阈值，以及 LangChain 对消息和工具定义的序列化方式。

### Goal/Compaction 合并后的请求形状

`goal-resolution-merge-design.md` 提议将 resolver 的独立 LLM 请求合并到主循环 step 1：在历史对话之后、最新用户消息之前注入临时 goal resolution guide。这样会消除 resolver 的额外请求；step 1 → step 2 会因为 guide 消失，在最新用户消息附近产生一次额外断点，但 System/Workflow/Skill 和历史对话前缀仍可命中。

Compaction 可以采用相同的“临时 guide + runtime 状态产出”原则，但 guide 放置位置不同：

| Guide | 放置位置 | 原因 | 缓存影响 |
|---|---|---|---|
| Goal Resolution Guide | 历史对话之后、最新用户消息之前 | 判断的是最新 turn 的意图，应贴近 latest user message | step 1→2 时最新 user message 前移，断点在最新 user 附近；历史前缀仍命中 |
| Compaction Guide | 消息末尾 | 需要总结前面的对话；末尾追加保留前面全部公共前缀 | 只影响 compaction guide 自身和后续输出 |

两者都应是临时消息，不写入历史。区别是 goal resolution 默认在 turn 初始 step 内联；compaction 只有在 soft budget 检查通过且仍能容纳 guide 时内联，否则保留当前 dedicated compaction/fallback 路径。inline compaction 通过 `compact_context` 工具提交 summary，而不是解析普通 assistant 文本。

### 同一 workflow 节点内，step N → step N+1

```
[SystemMessage]          ✅ 命中
[Workflow Context]       ✅ 命中
[Skill Context]          ✅ 命中
[历史对话]                ✅ 命中
[最新user + task ctx]    ❌ task context 可能变了
[AI/Tool 新增]           ❌ 新增内容
[Todo/Guidance]          ❌ 可能变了
```

命中率取决于历史对话占比。历史对话多时通常明显更高；对话刚开始时较低，因为稳定前缀之外的新增内容占比更大。

### Workflow 节点切换时

```
[SystemMessage]          ✅ 命中
[Workflow Context]       ✅ 命中
[Skill Context/历史对话] ✅ 命中
[最新user + task ctx]    ❌ task context 中的 workflow/persona 状态变了
```

节点切换现在主要影响最新 user message 前的 Task Context，不再因为 tool schema 或 Workflow Context 变化而从前缀早段失效。

### Compaction 后

Long Summary 变化会导致 SystemMessage 变化，主循环下一次请求的稳定前缀会从第 0 条开始失效。但 compaction agent 本身若使用 `input_mode=main_context`，运行 compaction 的那次请求仍可能复用部分主循环前缀。

inline compaction guide 运行时不再需要独立 agent 请求，而是主循环请求的末尾增量；这能最大化复用已有前缀。模型通过 `compact_context` 工具提交 summary 后，运行时替换 live messages 并写入 Long Summary。summary 写回后，下一次主循环仍会因为 Long Summary 变化而重建 SystemMessage。

## 增量缓存机制

voidx 已有的增量缓存优化：

| 机制 | 位置 | 效果 |
|---|---|---|
| `stable_prefix_key` | `build_incremental()` | stable sections 不变时复用 SystemMessage 对象 |
| `workflow_context_cache_key` | `_incremental_context_content()` | workflow content 不变时复用 HumanMessage 对象 |
| `skill_context_cache_key` | `_incremental_context_content()` | skill content 不变时复用 HumanMessage 对象 |
| `_estimate_cache_reuse_tokens` | `usage.py:180-191` | provider 不报告缓存 token 时，本地估算公共前缀的 token 数 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|---|---|---|
| Task Context prepend 到最新 user message | 放到 SystemMessage 尾部 | prepend 到最新 user message 可以保留 System/Workflow/Skill/历史对话前缀；放到 SystemMessage 尾部会让第 0 条消息随 task context 变化，缓存效果更差 |
| Todo/Guidance 放在消息末尾 | 放在对话历史之前 | 末尾位置只影响自身缓存，不影响前缀 |

## Open Questions

- [ ] Task Context 是否可以拆分为稳定部分和动态部分，稳定部分保持前缀命中？
- [ ] 子 agent 是否可以复用父级的 Workflow/Skill Context 缓存？
- [x] Tool defs 在 workflow 节点切换时是否可以通过保留公共工具集来部分命中？已改为固定 advertised tool schemas；workflow 限制只在执行授权层生效。
- [x] Compaction 是否实现 inline guide + `compact_context` 工具路径，在预算允许时避免独立 compaction LLM 请求？
