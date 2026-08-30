# Turn Rollover Compaction — 技术规格

> **Status: Revision in review**
> **Date: 2026-08-30**
> **Audience: Human + LLM**

## TL;DR

voidx 的自动压缩不再等待“足够多的旧 turn”，也不再通过模型可见的 context-pressure hint 要求当前任务提前收敛。每次主模型调用前，系统按**实际待发送请求**的 Token 占用（消息、system/runtime context、Long Summary、工具 schema 和内部控制消息）决定是否 rollover。

一次用户输入仍对应一个用户可见 turn；该 turn 内可以产生任意多个内部 execution segment。达到预算时，系统在安全点把“最新累计 Long Summary + 当前已完成 segment”合并成下一版 Long Summary，清空模型 live context 中已覆盖的 raw segment，追加不落库的 continuation 指令，然后自动继续工具循环。用户不需要再次输入“继续”。

同一缓存作用域下，summary 请求优先复用最后一笔主请求的**精确 provider 请求前缀**，只在末尾追加静态 compaction suffix；不兼容或放不下时退化为 detached summary。持久化层保存多个 summary revision，但模型上下文始终只注入最新累计 Long Summary。

## 1. 决策与替代关系

本规格确认以下决策：

1. 自动压缩边界从“完整用户 turn”改为“内部 execution segment”。
2. 一个用户 turn 可以 rollover 多次，但 UI、transcript、Goal 生命周期和 turn 统计仍只出现一个用户 turn。
3. 自动触发仅取决于实际请求预算，不取决于 turn 数、`DEFAULT_TAIL_TURNS` 或固定 head/tail 选择。
4. 正常路径不再向主模型注入 soft/hard context-pressure 收敛提示；预算压力由系统自动 rollover 解决。
5. Long Summary 是累计快照：每次 revision 覆盖此前 revision 和一个新 segment；模型只看到最新 revision。
6. canonical transcript 永不因自动 rollover 删除；模型 live context 通过 durable cursor 投影。
7. 同缓存作用域优先使用 cache-aligned summary；“同 provider/model”只是必要条件，不是缓存命中的充分条件。

本规格在自动压缩范围内替代以下已归档设计的约束：

- `docs/archive/compaction-skip-on-long-single-turn.md` 中“只整轮压缩、无法压缩时提示当前 turn 收敛”的策略；
- `docs/archive/2026-06/2026-06-10/in-turn-compaction-design-2026-06-10.md` 中“不改变 turn-based head/tail 选择”的约束；
- `docs/archive/compaction-summary-model.md` 中“Long Summary 只读取被移除的完整历史 turn”的输入边界。

仍然保留：独立 compaction profile、reasoning、timeout、结构化 summary 模板、deterministic fallback、usage/context-frame 观测，以及临时 summary 模型不得写回主模型配置。

## 2. 当前行为与问题

### 2.1 当前触发和选择使用不同口径

- `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py` 使用 `estimate_context_tokens_with_tools()` 估算真实主请求，包含工具 schema；
- `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` 的 `compact_for_live_state()` 又只用 `estimate_context_tokens(messages, model)` 判断压缩；
- `src/voidx/llm/compaction/service.py` 的 `select_details()` / `select_preflight_details()` 按 HumanMessage turn 切 head/tail，并固定保留最近 turn。

结果是 system/runtime context 或工具 schema 把真实请求推到窗口边缘时，选择器可能只找到两条很小的旧消息，却仍发起一次完整 summary 调用；单个当前 turn 很大时又可能没有合法旧 turn 可压缩。

### 2.2 当前 pressure hint 会主动结束任务

`src/voidx/agent/adapters/langgraph/runtime/context_pressure.py` 在无法按完整 turn 压缩时生成 soft/hard HumanMessage，要求模型停止扩展并结束当前 turn。对应事件定义在 `src/voidx/agent/domain/ui_events.py`，调用点位于 `llm_turn.py`。

这能避免继续超窗，但会把本应自动运行完成的任务变成“结束 → 用户输入继续 → 再压缩”。

### 2.3 当前 summary 请求形式后置但没有对齐主请求缓存

`CompactionCoordinator.run_compaction_agent()` 当前发送：

```text
[selected semantic head messages]
[COMPACTION_REQUEST HumanMessage]
```

compaction 指令已经在最后，但请求缺少主请求的真实 system/runtime 前缀、Long Summary 位置、工具 schema、tool choice 和 replay sanitizer 结果，因此通常不是上一笔主请求的精确前缀。

### 2.4 当前持久化把 canonical history 和 live context 混在一起

- `turn_runner.py` 在用户 turn 结束时通过查找原始 HumanMessage 来切出并持久化 AI/Tool 消息；
- `compaction_coordinator.py:persist_compaction()` 调用 `delete_messages_through()` 删除已压缩消息；
- `turn_runner.py` 恢复时再次加载剩余消息并编译为 live context。

如果在同一用户 turn 内移除当前 HumanMessage，现有 turn-end 持久化会找不到锚点；若继续删除 canonical rows，则 UI transcript、恢复、审计和 summary 重建都会丢失原始证据。

## 3. 目标与非目标

### 3.1 Goals

- 单次用户输入可跨多个内部 segment 自动执行，直到正常完成或触发既有权限、取消、step、wall-clock 等终止条件。
- 每次主模型调用前基于实际 prepared request 统一做预算决策。
- rollover 可以覆盖当前用户 turn 中已经完成的 User/Assistant/Tool 历史。
- rollover 只能发生在 tool-call 配对闭合的安全点。
- 多次 rollover 生成累计 Long Summary revisions；只把最新 revision 注入模型。
- canonical messages、summary revision 和 cursor crash-consistent，不因进程中断丢消息或重复覆盖。
- session resume 只向模型加载 cursor 之后的 raw messages，但完整 canonical transcript 仍可查询和展示。
- 同一缓存作用域下使用后置 compaction suffix 最大化 prompt-cache reuse。
- 缓存模式、预期复用前缀和 provider 实际 cache-read/write Token 可观测。
- provider overflow 自动进入 emergency rollover，不盲目重复同一超窗请求。

### 3.2 Non-Goals

- 不自动恢复一个因进程退出而中断的 LangGraph 工具循环；本规格只保证下次恢复不会丢失已提交的 summary/cursor 和 canonical messages。
- 不保证任意 provider 都支持或命中 prompt cache。
- 不把 summary revision 当作 canonical transcript 的替代品。
- 不重置 workflow、todo、persona、权限、turn control、recursion 或 wall-clock guard。
- 不在本规格中重做附件存储；超大首条用户输入仍使用现有附件/引用能力，无法规范化时明确失败。
- 不允许 summary agent 执行工具。

## 4. 术语与核心不变量

### 4.1 User Turn 与 ActiveTurnInput

一次真实用户输入及其最终响应构成一个 user turn。它拥有唯一 `user_message_id`，对应一次 `TurnStarted` 和一次 `TurnCompleted`/`TurnFailed`/`TurnCancelled`。内部 rollover **不得**创建新的真实 HumanMessage、保存新的 user row、发出新的 TurnStarted，或更新 recent exchange 为多个 turn。

`ActiveTurnInput` 是整个 user turn 都保留在 AgentState 中、但不会自动注入模型的结构化事实：

```text
turn_journal_id
user_message_id
user_message_cursor
raw_text
semantic_text          # clean text used by goal/workflow/control logic
display_text
title_text
content                # original str | structured content blocks
content_format
```

`TurnRunner` 在创建 `UserMessagePayload` 后初始化该对象；rollover 的 RemoveAll 只清理 `messages`，不得清理 `active_turn_input`。以下消费者必须优先读取 `ActiveTurnInput`，不能再从 live messages 反向寻找最近 HumanMessage：

- `runtime/topology.py:latest_user_text()`；
- `runtime/core/turn.py` 的 Goal/turn start；
- `runtime/llm_turn.py` 的 workflow scope；
- `runtime/convergence.py` 的 fallback summary；
- turn title、recent exchange、错误/取消收尾。

兼容期可以在 `active_turn_input` 缺失时回退旧 messages 搜索；启用 `context_rollover_enabled` 后，当前 turn 必须有该对象。完整 structured content 只保存在 graph/execution state 和 canonical row，不应被重复渲染进 system prompt。

### 4.2 Execution Segment

同一个 user turn 内、两次 rollover 之间的 raw 模型执行历史。`segment_index` 从 0 开始，只在当前 user turn 内递增。

segment 的 semantic source 可以包含：

- 当前用户消息（segment 0）；
- Assistant 文本或 tool calls；
- 与 tool calls 完整配对的 ToolMessage；
- 真实用户 guidance；
- durable errors 和最终工具结果。

segment 不包含：

- stable system/runtime prefix；
- 既有 Long Summary；
- step hint、context-pressure、rollover continuation、guard guidance 等 synthetic control；
- 未闭合的 AI tool call。

真实用户 guidance 虽然不创建新 user turn，但属于 semantic source；不得被 generic guidance marker 从 summary 中删除。详见 9.3。

### 4.3 Closed Tool Batch

rollover source 必须通过 provider-neutral `validate_closed_tool_batches()`：

1. 每个 AI tool call 都有非空且在 source range 内唯一的 call id；
2. 一个 AIMessage 的多个 calls 后紧邻零个或多个 ToolMessage，且每个 call id **恰好一个** terminal result；
3. result id 必须与 call id 相同；success、error、rejected 和明确标记的 synthetic terminal error 均可闭合；
4. 不允许重复 result、orphan ToolMessage、跨 batch result 或在 batch 闭合前出现下一条非 ToolMessage；
5. validator 只验证 canonical 事实，不得像 replay sanitizer 一样补造缺失 ToolMessage。

`tool_choice_forces_call()` 由 provider adapter 把 `required`、`any`、named-function object 及 provider-specific 强制调用编码统一归一化为布尔值。返回 true 时禁止 cache-aligned summary，因为 summary 不允许调用工具。

### 4.4 Long Summary Revision

`revision N+1` 的逻辑输入为：

```text
LongSummary(N) + CanonicalSemanticSource(previous_cursor, candidate_cursor]
```

输出是一份新的累计 `LongSummary(N+1)`。不得把多份 segment summary 并列注入主模型。revision 在所有候选验证通过前只是内存对象，不得提前写入 runtime state 或 SQLite。

### 4.5 Context Cursor

单调递增的 `canonical_cursor` 表示 latest Long Summary 已覆盖到哪一条 **accepted canonical message**；cursor 之后的 accepted raw messages 才进入 live model context。revision schema 中对应字段统一命名为 `covered_through_cursor`。

普通 session 使用 accepted message cursor，Goal accepted transcript 使用 canonical `session_sequence`。source range 定义为实际 accepted rows 的有序集合：

```text
rows where previous_cursor < canonical_cursor <= covered_through_cursor
```

整数 cursor 可以有历史 gap；range 完整性由 ordered `(cursor, message_key, payload_hash)` 的 `source_range_hash` 验证，不要求整数连续。domain/application 不依赖 JSONL、SQLite 或 Goal transcript 的具体编号方式。

### 4.6 Prepared Provider Request

`PreparedMainRequest` 是真正准备发送给 provider 的不可变逻辑快照；其中包含一个由 provider adapter 生成的 `PreparedProviderRequest`：

```text
model instance/config + context/output limits
provider-ready sanitized ordered messages
active tool definitions and order
tool_choice/bind/invocation options
protocol/endpoint/cache scope
canonical provider request representation
cache capability and cache breakpoints/resources
stable hashes and token breakdown
```

预算检查、主请求调用、cache alignment 和 context-frame 观测必须消费同一份 snapshot。`ProviderRequestPreparer.prepare()` 与 `invoke_prepared()` 是同一 adapter contract：hash/token 计算和真实调用必须使用同一 canonical representation；若某 LangChain/provider adapter 无法保证该等价关系，其 cache capability 必须为 `none`。

每次主请求成功返回后，运行时才把 snapshot 记为 `last_sent_main_request`；provider 调用前的 candidate 不能假装已经进入缓存。进程恢复不依赖该内存快照。

### 4.7 必须保持的不变量

1. **一个用户 turn，多个内部 segment。**
2. **模型上下文始终只有最新 committed Long Summary。**
3. **canonical transcript append-only；cutover 后任何路径都不调用 `delete_messages_through()`。**
4. **每条 canonical graph message 有稳定 `canonical_message_key`，loader 只读取 durable accepted records。**
5. **candidate summary、closed-pair、source hash、post-budget 和 net-reclaim 全部验证通过后，cursor 才能 CAS 前移。**
6. **rollover 不重置 ActiveTurnInput、`turn_state`、TaskState、workflow、todo、persona 或 guard。**
7. **真实用户 guidance 必须被 summary 覆盖或继续留在 cursor 之后，不能被过滤后跨过。**
8. **没有新的 accepted semantic source 时不得创建同-cursor revision。**
9. **Goal session 的 revision commit 必须受当前 generation/attempt lease/fencing token 约束。**
10. **没有 provider cache usage 证据时不得宣称实际 cache hit。**

## 5. 目标架构

```text
Accepted Canonical Journal (完整、append-only)
        │
        ├── compaction_cursor 之前 ──> committed Long Summary revision N
        │
        └── compaction_cursor 之后 ──> current accepted semantic source
                                           │
Stable Runtime + Summary N + Raw Source + Tool Schema
                                           │
                                  PreparedMainRequest
                                           │
                                 main request over budget?
                          │ no                              │ yes
                          ▼                                 ▼
                      main LLM          flush/accept → summarize candidate
                                                              │
                                              build candidate live projection
                                                              │
                                         validate pairs/hash/post-budget/reclaim
                                              │ invalid                    │ valid
                                              ▼                            ▼
                                      fallback or explicit error       fenced CAS commit
                                                                           │
                                                               live replace + continuation
                                                                           │
                                                                      main LLM 自动继续
```

LangGraph 拓扑仍可保持：

```text
prepare → call_llm → execute_tools → call_llm → ... → finalize
```

rollover 是 `call_llm` 发起 provider 请求前的内部状态转换，不是新的图节点，也不是新的用户 turn。

## 6. 预算与触发算法

### 6.1 单一主请求预算

自动路径不再使用 turn count、soft pressure 或 hard pressure 选择。每次准备主请求时，由 `PreparedProviderRequest` 计算：

```text
main_request_tokens = prepared.total_input_tokens
main_request_limit =
    prepared.context_limit
    - prepared.main_output_reserve
    - safety_margin

should_rollover = main_request_tokens >= main_request_limit
```

`total_input_tokens` 必须覆盖 provider framing、system/runtime/Long Summary/task sections、messages、工具 schema、tool choice 和必需控制消息。首版 `safety_margin = COMPACTION_BUFFER`；不得再叠加 `compaction_soft_ratio`，也不得因 summary model 不同改变主请求触发点。

同一 token breakdown 还必须给出不可通过 summary 回收的：

```text
mandatory_request_tokens = tokens(
    provider framing
    + active tool schema/bind options
    + stable system/runtime/project instructions
    + Current Task State/workflow/todo/persona/permission facts
    + required provider/control envelope
    + one rollover continuation
)
```

如果 `mandatory_request_tokens + main_output_reserve + safety_margin >= context_limit`，报告 `fixed_context_exceeds_budget`；先按既有 tool-surface policy 缩小工具面，仍失败则明确终止，不得对空或很小的 semantic source 反复 summary。

### 6.2 Summary 可行性与净收益

aligned/detached 分别按各自实际请求判断：

```text
summary_request_tokens + summary_output_reserve + safety_margin
    <= summary_context_limit

projected_new_summary_tokens = summary_output_reserve
reclaimable_tokens = tokens(committed_summary + accepted_semantic_source)
projected_net_reclaim = reclaimable_tokens - projected_new_summary_tokens
minimum_net_reclaim = max(1_024, ceil(main_context_limit * 0.01))
```

只有 projected net reclaim 达标才启动 summary LLM。返回后，以候选 summary 构造**未提交**的 main projection，并要求：

```text
candidate_main_tokens < main_request_limit
pre_request_tokens - candidate_main_tokens >= minimum_net_reclaim
```

任一验证失败时，候选无 durable side effect；可以在总 attempt budget 内尝试下一策略，最终失败则 `ContextBudgetExhausted`。

### 6.3 防循环与无新 source overflow

成功 rollover 必须满足 cursor 前进、source hash 未变、closed-tool-batch 合法、候选请求在预算内且达到最小净回收。同一个 `prepared_request_hash` 不得连续 rollover。

成功 commit 后，若第一笔 main request 因 provider 计数差异 overflow，而 cursor 后没有新的 accepted semantic source：

1. 禁止创建同-cursor amendment revision；
2. 允许提高 safety margin、重新执行 provider preparation，并在语义允许时缩小工具面；
3. 只允许一次 hash 必须变化的重建重试；
4. 仍 overflow 则返回 `ContextBudgetExhausted(reason="provider_overflow_without_new_source")`。

不得设置很小的每-turn rollover 上限；recursion、step 和 wall-clock guard 继续约束总成本。

## 7. Rollover 安全点与状态机

### 7.1 安全点

正常安全点是 `execute_tools` 返回、下一次 `PreparedMainRequest` 已构造但尚未调用 provider 时。上一批 tool calls 必须形成 closed tool batch；不闭合或 orphan result 直接报告 `invalid_rollover_boundary`，不得通过 replay repair 补造 canonical 事实后压缩。

segment 0 的第一次模型调用前也可 rollover，此时 source 只有已 accepted 的用户消息。若输入本身过大，使用 detached input normalization；没有 `last_sent_main_request` 时禁止 aligned。

### 7.2 状态机

```text
READY
  ├── under budget ─────────────────────────────> CALL_MAIN
  └── over budget ──────────────────────────────> FLUSH_AND_ACCEPT_SOURCE

FLUSH_AND_ACCEPT_SOURCE
  ├── no new accepted semantic source ─────────> NO_SOURCE_OVERFLOW
  └── source accepted ──────────────────────────> VALIDATE_SOURCE

VALIDATE_SOURCE
  ├── bad pair/key/hash ────────────────────────> ERROR(no commit)
  └── valid ────────────────────────────────────> SELECT_SUMMARY_MODE

SELECT_SUMMARY_MODE
  ├── aligned eligible and fits ────────────────> SUMMARIZE_ALIGNED
  ├── detached fits ────────────────────────────> SUMMARIZE_DETACHED
  └── otherwise ────────────────────────────────> SUMMARIZE_EMERGENCY

SUMMARIZE_*
  ├── attempt failed/invalid ───────────────────> NEXT_STRATEGY
  └── candidate summary ────────────────────────> BUILD_CANDIDATE_PROJECTION

BUILD_CANDIDATE_PROJECTION
  └── candidate summary + continuation ─────────> VALIDATE_CANDIDATE

VALIDATE_CANDIDATE
  ├── bad shape/pair/hash/post-budget/reclaim ──> NEXT_STRATEGY (no commit)
  └── valid ────────────────────────────────────> FENCED_CAS_COMMIT

FENCED_CAS_COMMIT
  ├── CAS/lease conflict ───────────────────────> RELOAD_WINNER (no overwrite)
  └── committed ────────────────────────────────> LIVE_REPLACE

LIVE_REPLACE
  └── RemoveAll + preserve state + continuation > CALL_MAIN
```

`deterministic fallback` 也必须经过 BUILD/VALIDATE，不能绕过 post-budget 或净收益检查。

### 7.3 自动 continuation

rollover commit 后追加带专用 marker 的 synthetic HumanMessage：

```text
Continue the active task autonomously from Long Summary and Current Task State.
Do not wait for the user or ask them to type “continue”.
```

该消息不写入 canonical journal、不计入 semantic turn、不参与 recent exchanges/title、下一次 summary 输入中过滤，且不重置 `turn_state`。真实用户输入始终来自 `active_turn_input`，因此 RemoveAll 后 Goal start、workflow scope、fallback 和 `latest_user_text()` 仍读取原始请求。marker 定义在 `src/voidx/llm/message_markers.py`，不按文案识别。

## 8. Summary 请求模式与缓存对齐

### 8.1 Cache-aligned 模式

运行时保存最近一笔**成功发送并得到响应的主请求** `last_sent_main_request`。只有以下条件全部满足时启用：

1. `ResolvedCompactionModel.is_exact_main_instance is True`；
2. provider、model、protocol、normalized base URL 和 effective reasoning 完全一致；
3. 使用同一个 in-memory model/credential scope；
4. active tool definitions、顺序、bind options 和 tool choice 完全一致；
5. provider 最终序列化后，`last_sent_main_request` 是当前 aligned candidate 的精确前缀；
6. 预期可复用前缀达到 provider 的缓存最低 Token 要求或本地最小收益门槛；
7. aligned candidate 加 summary output reserve 和 safety margin 后仍在 summary model context limit 内；
8. 前一请求不是会强制工具调用的 `tool_choice="required"`；否则 summary 无法禁止工具，必须 detached。

aligned candidate 使用本次尚未发送的 `PreparedMainRequest`，其尾部已包含最近主响应和闭合 ToolMessage：

```text
[last_sent_main_request exact provider prefix]
[new Assistant/Tool suffix already present in PreparedMainRequest]
[COMPACTION_SUFFIX as final HumanMessage]
```

因此 summary 请求的逻辑构造是 `current PreparedMainRequest + COMPACTION_SUFFIX`；缓存可复用范围由它与 `last_sent_main_request` 的 provider-ready common prefix 决定，而不是假定整笔当前请求已经缓存。第一次 main call 前没有 `last_sent_main_request`，必须 detached。

工具 schema 为保持 provider 前缀一致而继续绑定，但 summary 输出若包含任何 tool call，视为无效，不得执行。`last_sent_main_request` 只保留当前进程内最近一笔快照，不作为恢复正确性的持久化前提；进程恢复后的第一次 summary 默认 detached。

`COMPACTION_SUFFIX` 必须静态、后置，并要求：

- 用已有 Long Summary 和当前已完成 segment 更新累计 summary；
- 只返回 `SUMMARY_TEMPLATE`；
- 不回答用户、不继续任务、不调用工具；
- 不重复嵌入 previous summary，因为 previous summary 已在请求前缀中。

### 8.2 Cache scope fingerprint

逻辑模型建议为：

```text
CacheScopeFingerprint = hash(
  provider,
  model,
  protocol,
  normalized_base_url,
  effective_reasoning_config,
  active_tool_schema_hash,
  tool_bind_options_hash,
  stable_system_prefix_hash,
  replay_sanitizer_version,
)
```

API key 不得写入 fingerprint 或日志。首版 gate 以“同一主模型实例 + 精确 request prefix”作为严格条件；后续只有 provider capability 有证据时才允许放宽。

### 8.3 Stable prefix 分层

`RuntimeContextBuilder` 当前把 Long Summary 合并进单个 system content。目标编译顺序应显式区分：

```text
provider tools
stable system/runtime/project instructions
latest dynamic Long Summary
current task context
raw segment
```

provider adapter 若支持显式 cache breakpoint，应把 breakpoint 放在 stable tools/system prefix 末尾，不把动态 Long Summary 纳入稳定 breakpoint。实际 fingerprint 必须基于 provider 最终序列化结果，而不是逻辑 section 名称。

### 8.4 Detached 模式

以下情况使用无工具 detached summary：

- compaction profile/model 或 reasoning 与主请求不同；
- cache fingerprint 不兼容；
- aligned 请求放不下；
- provider overflow 后无法安全复用原请求；
- forced tool choice；
- aligned 输出了 tool call 或格式无效。

请求只包含：

```text
[previous Long Summary]
[current canonical raw segment]
[COMPACTION_SUFFIX]
```

继续使用现有 `compaction_summary_messages()` 的控制消息过滤规则，但输入边界改为 cursor range，而不是 turn-based selected head。

### 8.5 Emergency 与总尝试预算

`emergency` 先裁剪可重建的大工具原文，保留最终结果、错误、路径、diff 引用和 TaskState；随后 detached summarize。所有 aligned、detached、main-model fallback 的外部尝试共享同一个总 attempt budget：

```text
COMPACTION_MAX_RETRIES + 1
```

不得像当前双 stage 嵌套循环一样让每个 stage 各自消费完整重试次数。耗尽后使用 deterministic fallback。

### 8.6 缓存观测

每个 revision/context frame 记录：

```text
cache_mode: aligned | detached | emergency | deterministic
source_main_frame_id
cache_scope_fingerprint
prepared_request_hash
tool_surface_hash
expected_reusable_prefix_tokens
provider_cache_read_tokens
provider_cache_write_tokens
pre_request_tokens
post_request_tokens
segment_index
revision
```

只有 provider 返回 `cached_tokens` / `cache_read` 等 usage 字段时才标记真实 hit；`UsageStats` 的 common-prefix 估算只能标记为 estimated。

## 9. 持久化与恢复

### 9.1 Canonical journal 与 live projection 分离

`messages.jsonl`（以及 Goal accepted transcript）继续保存完整用户、Assistant 和 Tool 历史。自动 rollover 不再追加 delete tombstone。

新增模型投影：

```text
live raw messages = canonical messages where canonical_cursor > compaction_cursor
model context = stable runtime + latest summary + live raw messages
```

`turn_runner.py` 不再通过在最终 live messages 中查找原始 HumanMessage 来决定持久化范围。需要一个 idempotent turn journal/flusher：

- 用户消息仍在进入 graph 前保存；
- rollover 前保存尚未落库的完整 AI/Tool pairs，并把 canonical id 回写到 live message 或 execution journal；
- turn 完成和异常时只保存尚未持久化的消息；
- synthetic continuation 和控制消息永不保存；
- 同一 graph message 不得重复追加。

### 9.2 Summary revision schema

SQLite 当前 `SCHEMA_VERSION = 14`。实现新增 v15 migration 和表：

```sql
CREATE TABLE context_compaction_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    user_message_id INTEGER,
    segment_index INTEGER NOT NULL,
    previous_revision_id INTEGER,
    covered_through_cursor INTEGER NOT NULL,
    source_range_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    previous_summary_hash TEXT NOT NULL DEFAULT '',
    cache_mode TEXT NOT NULL CHECK (
        cache_mode IN ('aligned', 'detached', 'emergency', 'deterministic')
    ),
    source_main_frame_id INTEGER,
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    pre_request_tokens INTEGER NOT NULL DEFAULT 0,
    post_request_tokens INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, revision),
    UNIQUE (session_id, covered_through_cursor),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (previous_revision_id)
        REFERENCES context_compaction_revisions(id) ON DELETE SET NULL
);
```

`session_runtime_state` 增加：

```text
compaction_revision_id INTEGER
compaction_cursor INTEGER NOT NULL DEFAULT 0
```

现有 `compaction_summary` 保留为 latest materialized summary，避免大范围兼容破坏。

### 9.3 Commit 顺序、CAS 与 crash consistency

由于 canonical messages 是 JSONL，而 revision/runtime pointer 是 SQLite，不能宣称跨存储原子事务。必须使用以下有序提交：

1. append/fsync 当前 segment 尚未保存的 canonical messages，并冻结 `[previous_cursor + 1, covered_through_cursor]` source range 与 `source_range_hash`；
2. 调用 summary model 并验证输出；昂贵调用期间不持有 session 文件锁；
3. 重新读取 canonical 最大 cursor，确认 source range 仍存在且 hash 未变化；
4. `compaction_revision_repository.commit_revision()` 在单个 SQLite transaction 内执行 CAS：latest `compaction_revision_id`、`compaction_cursor` 和 previous summary hash 必须等于步骤 1 的 expected values；CAS 成功后插入 revision，并同时更新 `compaction_summary`、`compaction_revision_id` 和 `compaction_cursor`；
5. 成功后再替换 LangGraph live state。

`compaction_revision_repository` 是上述三个 runtime 字段的唯一写入者。通用 `save_session_runtime_state()` 的 conflict-update 不得覆盖 `compaction_summary`、`compaction_revision_id` 或 `compaction_cursor`，否则陈旧的 turn snapshot 可能把已提交 cursor 回退。manual compaction 也必须调用同一 repository。

如果 CAS 失败，说明另一个 writer 已推进 revision：丢弃本次未提交 summary，加载 winner 的 latest revision，重新构建请求；不得覆盖、删除或重复提交同一 source range。`source_main_frame_id` 是可被 GC 的观测引用，不设置强外键。

恢复规则：

- crash 在步骤 1 后、步骤 4 前：旧 cursor + 新 raw messages，恢复时重新看到 raw segment，不丢数据；
- crash 在步骤 4 后、步骤 5 前：新 cursor/summary 已 durable，恢复时排除已覆盖 raw segment；
- revision cursor 大于当前 canonical 最大 cursor、source range 缺失或 hash 不一致时视为损坏，拒绝应用并记录 internal error。

### 9.4 Revision 与 segment 编号

- `revision` 在 session 内全局单调递增；
- `segment_index` 在每个 user turn 内从 0 开始；
- 新 user turn 不清空 summary/cursor，只重置 execution-local `segment_index`；
- 仅最新 committed revision 注入模型；旧 revisions 用于审计、恢复诊断和未来重建。

### 9.5 Legacy migration

已有 session：

- 保留现有 `compaction_summary`；
- `compaction_revision_id = NULL`；
- `compaction_cursor = 0`；
- 旧实现已删除的消息继续不可见，剩余 canonical rows 全部视为 summary 之后的 raw tail；
- 第一次新 rollover 以 legacy summary 作为 `previous_summary`，并创建 revision 1。

不得试图从旧 summary 反推已删除 message cursor。

## 10. LangGraph live-state 更新

rollover commit 后返回的 graph message delta 必须使用：

```text
RemoveMessage(REMOVE_ALL_MESSAGES)
+ synthetic rollover continuation
```

随后 `_prepare_with_stream()` 依据最新 runtime summary 重建 system/runtime context。保留：

- `TaskState`、workflow runs、todo；
- persona 与 interaction mode；
- 原始 `user_message_id`；
- `turn_state`（通常为 `running`）；
- step/recursion/wall-clock/permission guard；
- guidance inbox 中尚未投递的真实用户 guidance。

不得把内部 continuation 当作新的 Goal intake，也不得再次要求模型调用 `turn(operation="start")`。

## 11. 初始超大输入与固定上下文超限

### 11.1 第一次 main call 前超限

如果当前 user row 本身使请求超过预算：

1. 无上一笔主请求，禁止 aligned 模式；
2. detached input-normalization 读取原始用户消息和附件引用；
3. summary 必须保留 Goal、约束、路径、命令、错误和验收标准；
4. 原始输入继续保留在 canonical journal；
5. normalization 后仍超限则明确返回 `initial_input_exceeds_context_budget`。

### 11.2 固定上下文超限

若 `stable system/runtime + active tool schema + minimum output reserve` 已超过窗口，summary 任何 raw segment 都无效。应：

- 记录 fixed/system/tool Token 分解；
- 使用既有 tool-surface policy 缩小当前允许工具（仅当业务语义允许）；
- 仍无法放入时终止并明确报告配置问题；
- 不得无限 rollover 空 segment，也不得注入 pressure hint 假装可恢复。

## 12. Failure 与降级策略

降级顺序：

```text
aligned summary
→ detached configured summary model
→ detached exact main model（如果尚未尝试）
→ deterministic fallback merge
→ rebuild budget verification
→ explicit ContextBudgetExhausted
```

约束：

- aligned 输出 tool call：不执行，直接判无效；
- summary 空文本或缺失固定 Markdown sections：判无效；
- deterministic fallback 必须合并 previous summary，而不是覆盖；
- 任何模式 commit 前都验证 cursor、summary hash、source range 和 rebuilt request；
- provider overflow 在成功 rollover 后只重试原 main action一次；若 prepared request hash 未变化，不得重试。

## 13. UI、协议与用户体验

用户界面仍显示一个 turn。rollover 使用现有 `StatusUpdated` / `StatusFinished`，例如：

```text
Compacting execution segment 2 · 48 messages · ~36.2k tokens
Compacted · reclaimed ~31.4k tokens · continuing
```

建议字段通过 detail 和 observability metadata 提供：

- segment/revision；
- source message count；
- pre/post/reclaimed tokens；
- cache mode；
- provider cache read tokens；
- fallback 状态。

迁移期：

- `ContextPressureUpdated` / `ContextPressureFinished` schema 和 gateway adapter 保留一个兼容周期，但正常主路径停止发送；
- `CONTEXT_PRESSURE_MARKER` 与旧 hint 过滤保留，用于清理旧持久化消息；
- 不再生成新的 model-visible context-pressure HumanMessage；
- 新 rollover status 使用 `display="record_only"`，不增加 transcript turn node。

如果后续删除旧事件类型，必须运行 UI schema 导出和 gateway contract tests。

## 14. Manual/Inline compaction 兼容

- 自动 rollover 是唯一自动预算恢复路径；不使用 `inline_compaction_enabled` 或 `VOIDX_COMPACTION_GUIDE` 驱动模型自行压缩。
- idle/manual session compact 应改为创建同样的 revision/cursor，不删除 canonical rows；它不注入 continuation。
- `CompactContextTool` 在迁移期不得与自动 rollover 同时提交同一 cursor。建议先停止自动注入 guide，再单独弃用该工具。
- legacy `select_details()` / `select_preflight_details()` 可暂留给兼容测试，但自动和 manual revision 路径完成后应移除；不得继续影响自动触发。

## 15. 代码归属与文件变更

### 15.1 Domain / application

- `src/voidx/agent/domain/compaction.py`
  - 新增 rollover decision/result、cursor 和 revision metadata 的 provider-neutral 模型；
  - 不导入 LangGraph、SQLite 或具体 provider。
- `src/voidx/agent/domain/state.py`
  - `SessionRuntimeState` 增加 latest revision/cursor。
- `src/voidx/agent/ports/compaction.py`
  - 将旧 `compact(messages, force, preflight)` 收敛为 rollover use-case 所需 port；
  - persistence 和 summary invocation 使用独立 port，不把 callback 泄漏到 domain。
- `src/voidx/agent/application/compaction_service.py`
  - 编排 decision → summarize → commit → result；
  - 发布通用 compaction completed 事件，不感知 LangGraph reducer。

### 15.2 LLM policy

- `src/voidx/llm/compaction/service.py`
  - 用 request budget、net reclaim 和 cursor range 替换自动 turn selection；
  - 保留纯函数 token policy 和 deterministic prune。
- `src/voidx/llm/compaction/constants.py`
  - 新增静态 rollover continuation/suffix；
  - 废弃自动路径的 `DEFAULT_TAIL_TURNS`、`MIN/MAX_PRESERVE_RECENT` 和 90% turn-based threshold。
- `src/voidx/llm/compaction/summary_input.py`
  - 输入改为 canonical cursor range；继续过滤控制消息。
- `src/voidx/llm/message_markers.py`
  - 新增 rollover continuation marker。

### 15.3 LangGraph adapter

- `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py`
  - 在每次 provider call 前创建唯一 PreparedMainRequest 并调用 rollover decision；
  - 移除正常 pressure hint 注入；
  - provider overflow 走 emergency rollover。
- `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py`
  - 实现 aligned/detached/emergency summary invocation、总 attempt budget 和 rebuilt validation；
  - 停止按 turn head/tail 和停止删除 canonical messages。
- `src/voidx/agent/adapters/langgraph/runtime/core/context.py`
  - 实现 rollover 后 RemoveAll + continuation 的 state replacement。
- `src/voidx/agent/application/runtime_context.py`
  - 分离 stable system prefix 与 dynamic Long Summary；
  - 只注入 latest summary。
- 建议新增 `src/voidx/agent/adapters/langgraph/runtime/prepared_request.py`
  - 集中 provider-ready message sanitization、tool bind options、token estimate 和 cache fingerprint；
  - 主请求和 aligned summary 共享同一构建结果。
- `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`
  - 用 idempotent canonical journal flush 替代“在 final live list 查原始 user anchor”。
- `src/voidx/agent/adapters/langgraph/state.py`
  - 增加 execution-local `segment_index` 和必要的 journal state。

### 15.4 Persistence

- 建议新增 `src/voidx/agent/adapters/persistence/compaction_revision_repository.py`
  - revision CRUD、latest valid checkpoint、transactional runtime pointer update。
- `src/voidx/agent/adapters/persistence/runtime_state_repository.py`
  - 持久化 revision id/cursor。
- `src/voidx/agent/adapters/persistence/runtime_state_mapper.py`
  - 映射新增 domain state。
- `src/voidx/agent/adapters/persistence/session_repository.py`
  - 提供 cursor-after projection 和 idempotent message flush；
  - 不得改变 full `load_messages()` 的 canonical 语义。
- `src/voidx/persistence/sqlite.py`
  - `SCHEMA_VERSION` 从 14 升到 15，注册连续 migration。
- `src/voidx/agent/adapters/persistence/context_frame_repository.py`
  - 继续保存 main/compaction prepared request 和 cache metadata；不作为 latest revision 的唯一真相源。

### 15.5 UI compatibility

- `src/voidx/agent/domain/ui_events.py`
- `src/voidx/presentation/gateway/adapter.py`
- `src/voidx/presentation/output/events/consumers.py`

首阶段只停止 pressure event emission，避免无必要协议破坏；后续删除时同步 fixture/schema。

## 16. 实施阶段

### Phase 1 — Durable cursor/revision

1. 增加 v15 schema、repository、domain/runtime mapping。
2. 增加 canonical `load_after_cursor` 投影。
3. 实现 crash-consistent revision commit 和 legacy restore。
4. 此阶段不切换自动触发。

### Phase 2 — Canonical turn journal

1. 引入 idempotent AI/Tool flush tracker。
2. rollover、turn completion、exception 共用同一 flush API。
3. 保证 live state 移除原始 HumanMessage 后最终响应仍可落库。

### Phase 3 — Segment rollover

1. PreparedMainRequest 成为唯一 token/调用输入。
2. 加入 request-budget decision 和 safe-boundary validation。
3. 实现累计 summary、RemoveAll、continuation 和多次 rollover。
4. provider overflow 使用同一 emergency path。

### Phase 4 — Cache alignment

1. 增加 strict fingerprint 和 exact-main-instance gate。
2. 复用 active tools/bind options 和精确 sanitized prefix。
3. 增加 detached fallback、tool-call rejection 和 cache usage metadata。
4. 分离 stable system 与 dynamic summary cache boundary。

### Phase 5 — Pressure/legacy cleanup

1. 停止 model-visible pressure hint emission。
2. manual compaction 切到 revision/cursor。
3. 停止 inline compaction guide 自动注入。
4. 确认兼容窗口后移除旧 selector、constants、events 和测试。

## 17. 测试设计

### 17.1 LLM policy/unit

新增或更新：

- `src/tests/test_llm/compaction/test_rollover_budget.py`
  - 真实 request/tool tokens 决定触发；
  - turn 数不参与；
  - 小 segment/fixed overhead 不调用 summary；
  - 多次 rollover 要求 cursor 前进。
- `src/tests/test_llm/compaction/test_compaction_summary_input.py`
  - cursor range、控制消息过滤、tool error 保留。
- `src/tests/test_llm/compaction/test_compaction_retry.py`
  - 所有 mode 共享总 attempt budget。

命令：

```bash
./test.py --backend -- src/tests/test_llm/compaction -v
```

### 17.2 LangGraph integration

新增或更新：

- `src/tests/test_agent/adapters/langgraph/runtime/test_context_rollover.py`
  - 一个 user turn rollover 2 次仍只发一次 TurnStarted/Completed；
  - segment 0/1/2 自动续跑，无用户“继续”；
  - turn state、workflow、todo、persona、guards 不重置；
  - rollover 前后提交的真实用户 guidance 只投递一次且不会被 summary/continuation 吞掉；
  - tool-call pair 不被切断；
  - final assistant/tool messages 即使原始 HumanMessage 已从 live state 移除仍全部落库。
- `test_call_llm_compaction.py`
  - 移除 pressure convergence 期望；hard/provider overflow 走 rollover。
- `test_call_llm_compaction_advanced.py`
  - post-request budget/no-progress guard。
- `test_compaction_summary_model.py`
  - exact main instance aligned；reasoning/profile override detached。

命令：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_context_rollover.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction_advanced.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_compaction_summary_model.py -v
```

### 17.3 Cache alignment

新增 `src/tests/test_agent/adapters/langgraph/runtime/test_compaction_cache_alignment.py`：

- aligned candidate 由当前 `PreparedMainRequest + COMPACTION_SUFFIX` 构成，最近成功的 `last_sent_main_request` 是其精确 provider-ready 前缀；
- compaction suffix 是最后一条 message；
- tools/order/bind options 一致；
- forced tool choice、不同 reasoning、不同 endpoint、tool hash 变化均 detached；
- summary tool call 不执行；
- provider cache usage 与 estimated cache usage 分开记录。

命令：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_compaction_cache_alignment.py -v
```

### 17.4 Persistence/recovery

新增 `src/tests/test_agent/adapters/persistence/test_compaction_revision_repository.py`，并更新：

- `src/tests/test_persistence/test_schema_migration.py`；
- `src/tests/test_agent/adapters/langgraph/runtime/test_session_runtime_state.py`；
- `src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py`；
- `src/tests/test_agent/adapters/langgraph/runtime/test_session_persistence.py`。

覆盖 crash points：

1. canonical flush 后、revision commit 前；
2. revision commit 后、live replacement 前；
3. cursor 指向不存在 canonical message；
4. legacy summary + cursor 0；
5. Goal accepted transcript cursor；
6. 自动 rollover 后 full transcript 仍完整，live projection 只加载 cursor 后 rows；
7. 两个 writer 使用同一 expected revision/cursor 时只有一个 CAS 成功，loser 不覆盖 winner；
8. 通用 runtime-state save 不能回退已提交的 summary/revision/cursor。

命令：

```bash
./test.py --backend -- \
  src/tests/test_persistence/test_schema_migration.py \
  src/tests/test_agent/adapters/persistence/test_compaction_revision_repository.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_runtime_state.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_persistence.py -v
```

### 17.5 UI/contracts

```bash
./test.py --backend -- \
  src/tests/test_presentation/gateway/test_adapter.py \
  src/tests/test_presentation/gateway/test_ui_events_dock_status.py -v
./python.py scripts/export_ui_protocol_schema.py
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
```

如果 schema 导出产生变化，必须检查并提交对应 fixture/generated contract；不得手改生成文件替代导出命令。

### 17.6 Broader verification

```bash
./test.py --backend
```

## 18. 验收标准

1. 构造单个用户 turn、每个 segment 产生大工具输出的场景，可在不增加用户输入的情况下至少 rollover 两次并最终回答。
2. 自动触发测试改变 HumanMessage turn 数但保持 prepared request tokens 不变时，decision 不变。
3. prepared request tokens 包含 active tool schema；system/tool 固定开销过大时不调用无收益 summary。
4. 每个 committed revision 的 cursor 严格递增；模型只注入 latest summary。
5. rollover 前后的 canonical transcript 字节级保留所有真实 user/assistant/tool rows，不写 synthetic continuation。
6. crash-recovery 测试不丢消息、不双重覆盖、不加载 cursor 前 raw rows。
7. aligned 模式请求以最近 main prepared request 为精确前缀，suffix 后置；不兼容时 detached。
8. aligned summary 即使输出 tool call，也没有任何工具被执行。
9. provider cache-read/write usage 被记录；没有 provider 证据时只标 estimated。
10. 正常路径不再生成 context-pressure convergence HumanMessage，任务不会因为上下文压力主动等用户输入“继续”。
11. 现有 permission、workflow、todo、goal control、recursion 和 wall-clock 测试保持通过。
12. focused、architecture/contracts 和 full backend tests 通过。

## 19. 禁止变更

实现不得：

- 用新的真实 HumanMessage 或 TurnStarted 表示 segment；
- 继续以 `DEFAULT_TAIL_TURNS`、最近 3 turn 或 turn count 作为自动压缩门槛；
- 自动 rollover 时删除 canonical messages；
- 同时向模型注入多份 Long Summary revisions；
- rollover 后重置 `turn_state`、workflow、todo、persona、step 或 guard；
- summary agent 输出 tool call 时执行工具；
- 只比较 provider/model 字符串就标记 cache aligned/hit；
- 记录 API key、credential hash 或敏感 endpoint query 到 cache fingerprint；
- 在没有 cursor 前进时重复 rollover；
- 用早前 pressure hint 作为 automatic rollover 失败的默认兜底；
- 绕过 `./test.py` 直接把 pytest/vitest/cargo 命令写成标准验证步骤。

## 20. 风险与回滚

### 20.1 Summary 漂移

累计 `summary(summary + segment)` 可能逐步丢事实。缓解：固定结构、TaskState/workflow/todo 作为结构化真相源、canonical revisions 可审计、保留原始 journal、测试关键约束跨多 revision 不丢失。

### 20.2 持久化重复或丢失

live state 与 canonical journal 分离后，turn-end slice 不再可靠。必须先完成 Phase 2 idempotent flush，才能启用当前 segment rollover。

### 20.3 Cache alignment 误判

provider 缓存规则不同。首版使用 strict exact-instance/prefix gate，并以 provider usage 观测真实 hit；任何不确定条件 detached。

### 20.4 固定上下文过大

summary 无法缩小 tools/system。预算报告必须分解 fixed 与 reclaimable tokens，避免无限 rollover。

### 20.5 回滚

迁移期增加内部 `context_rollover_enabled` feature flag：

- 默认在完整 focused/broader tests 通过后开启；
- 关闭时可回到 legacy compaction，但不得同时运行两个 cursor writer；
- v15 table 和 cursor columns 向后兼容保留，不做 destructive downgrade；
- legacy path 只作为短期回滚，pressure hint 不应成为长期双轨方案。

## 21. 参考

- 当前主请求构建：`src/voidx/agent/adapters/langgraph/runtime/llm_turn.py`
- 当前压缩协调：`src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py`
- 当前 turn selection：`src/voidx/llm/compaction/service.py`
- 当前 runtime context：`src/voidx/agent/application/runtime_context.py`
- 当前 session/runtime persistence：
  - `src/voidx/agent/adapters/persistence/session_repository.py`
  - `src/voidx/agent/adapters/persistence/runtime_state_repository.py`
  - `src/voidx/agent/adapters/persistence/context_frame_repository.py`
- OpenAI Prompt Caching：<https://platform.openai.com/docs/guides/prompt-caching>
- Anthropic Prompt Caching：<https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- Gemini Context Caching：<https://ai.google.dev/gemini-api/docs/caching>
