---
name: recursive-user-summary-replacement
display_name: 递归 User Summary 消息替换压缩
description: 用可见、可恢复的 synthetic user summary message 替换有效历史，并在多次压缩中递归合并。
doc_type: tech-design
audience: human+llm
status: draft
---

# 递归 User Summary 消息替换压缩

## 结论与审批范围

本设计统一定义递归 User Summary replacement 的自动压缩主路径；Long Summary、summary revision 和 canonical append-only cursor projection 不属于本方案。

用户已经确认以下语义：

- 删除 Long Summary system section；
- 压缩结果只持久化一条 `role="user"` 的 synthetic summary message；
- summary message 在 transcript/UI 中可见，resume 时作为普通历史消息恢复；
- 被压缩的原始有效消息被真正替换，`load_messages()`、UI、resume 和后续模型请求都不再看到它们；
- 后续压缩必须把上一条 synthetic summary message 与其后的新消息一起重新压缩；
- 压缩前可以保留最新的完整 AI/tool execution batch，但其 Token 不得超过 context window 的 10%；
- synthetic summary 只代表一个内部 execution segment 的边界，不创建新的真实用户 turn。

本设计是实现前的技术方案，不直接修改实现代码。实现前仍需按本文的阶段计划拆分任务，并在每个任务中遵守 TDD gate。

## 1. 当前实现与问题

当前实现仍是 Long Summary + turn-based selector：

| 位置 | 当前行为 | 本设计的目标行为 |
|---|---|---|
| `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` | 使用 `select_details()` 选择旧 turn，summary 单独保存到 `_compaction_summary`，自动路径调用 `delete_messages_through()` | 选择 source range 和最新 tail，生成一条 synthetic user，并通过 range replacement 替换有效消息 |
| `src/voidx/llm/compaction/service.py` | 按 HumanMessage turn 划分 head/tail | 按 closed AI/tool batch 选择 tail，summary 输入支持上一条 compaction user message |
| `src/voidx/agent/application/runtime_context.py` | 将 Long Summary 编译进 system sections | 不再注入 summary section |
| `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py` | turn 结束时在 live messages 中寻找原始 HumanMessage，再保存其后的消息 | 使用 turn journal 记录已提交 graph message，不依赖被 rollover 移除的 HumanMessage |
| `src/voidx/agent/adapters/persistence/session_repository.py` | JSONL loader 按 message id 得到有效消息，删除通过 delete event 实现 | 增加 replacement event projection，按有效顺序删除 source 并插入 replacement |
| `src/voidx/agent/adapters/langgraph/runtime/topology.py` | `latest_user_text()` 从最近 HumanMessage 反推用户输入 | 优先读取 `ActiveTurnInput`，跳过 synthetic compaction user |

当前 focused compaction 测试验证的是旧行为，不能作为新设计的充分证据。切换前必须新增 replacement、tail、递归 summary、resume 和 turn journal 测试。

## 2. 目标与非目标

### 2.1 Goals

- 每次成功压缩生成且只生成一条可见 synthetic user summary message。
- source range 的旧有效消息在 effective transcript 中被替换，不再参与模型请求或 resume。
- summary message 在原 source range 的位置出现，而不是简单追加到 transcript 尾部。
- 后续压缩把已有 summary message 和新 raw messages 作为同一个 source range 重新总结。
- 尾部只保留完整的 AI/tool batch，且不超过 `floor(context_limit * 0.10)`。
- summary message 不被识别为新的真实 user turn，不触发 Goal intake、标题、recent exchange 或 `TurnStarted`。
- rollover 发生在 tool-call batch 闭合后，且不重置当前 turn 的 task/workflow/todo/persona/permission/guard 状态。
- resume 加载替换后的有效消息，并能继续处理后续真实用户输入。
- replacement 操作支持 source hash、operation id、幂等重放和 crash recovery。
- 主请求预算与 summary 后置验证使用实际 prepared request 的统一 Token 口径。

### 2.2 Non-Goals

- 不保留 Long Summary system section 或独立 previous-summary runtime state 作为模型输入。
- 不把 synthetic summary 伪装成第二条 AIMessage；持久化层只保存一条 user row。
- 不为每个内部 segment 创建新的真实用户消息、`TurnStarted` 或 Goal turn。
- 不在本设计中保证旧 JSONL 文件中的历史字节立即物理擦除；effective projection 必须立即删除旧消息，物理文件 GC 是独立的存储维护操作。
- 不自动恢复一个进程退出时未完成的 LangGraph 工具循环；只保证已提交 replacement 和真实消息在 resume 时一致。
- 不允许 summary agent 执行工具。
- 不在首个实现阶段同时完成 provider-specific prompt cache 命中优化；先保证 replacement 和恢复正确性，cache alignment 作为后续阶段接入同一个 prepared request contract。

## 3. 术语与消息模型

### 3.1 Real User Turn

一次真实用户输入及其最终用户可见响应构成一个 real user turn，拥有唯一 `user_message_id`，对应一次 `TurnStarted` 和一次 `TurnCompleted`/`TurnFailed`/`TurnCancelled`。

Synthetic compaction user 不属于 real user turn，不能被 `is_user_turn_row()`、Goal intake、title、recent exchange 或 turn counter 当作真实用户输入。

### 3.2 Execution Segment

同一个 real user turn 内，两次 replacement boundary 之间的模型执行历史。`segment_index` 在 real user turn 内递增，但 rollover 不重置 `turn_state` 或 guard。

一次 replacement 会：

1. 逻辑关闭被替换 source 所属的旧 segment；
2. 插入一条 synthetic summary user，作为新 segment 的第一条模型可见消息；
3. 将可保留的最新完整 AI/tool batch置于 summary 后；
4. 使用 ephemeral continuation 继续当前 graph loop。

没有第二条 AIMessage。旧 segment 的“完整关闭”通过 replacement metadata 和 closed source range 表示。

### 3.3 Synthetic Compaction User

普通 `role="user"` 消息，内容为结构化 Markdown summary，额外携带内部 metadata：

```python
{
    "_voidx_compaction_message": True,
    "compaction_id": "...",
    "source_range_hash": "...",
    "compaction_depth": 2,
    "closed_segment_index": 1,
    "opened_segment_index": 2,
    "replacement_operation_id": "...",
}
```

必须新增 `is_compaction_message()`。marker 是唯一识别依据，不得通过 summary 文案或 `role="user"` 猜测。

`replacement_operation_id` 是幂等、重放和冲突检测的唯一键；`compaction_id` 仅用于观测与审计（关联同一次逻辑压缩的多个 summary attempt），不参与任何正确性判断。

消息的行为：

- `messages_from_rows()` 恢复为 `HumanMessage`；
- 模型请求正常包含它；
- transcript/UI 正常显示它；
- `is_user_turn_row()` 返回 false；
- summary input 保留它；
- `latest_user_text()`、Goal/workflow scope、title、recent exchange 和 fallback summary 跳过它；
- 不触发新的 `TurnStarted`。

## 4. 压缩算法

### 4.1 预算

每次主模型调用前先构造 `PreparedMainRequest`。触发和验证必须使用同一个 provider-ready snapshot：

```text
main_request_tokens = prepared.total_input_tokens
main_request_limit =
    prepared.context_limit
    - prepared.main_output_reserve
    - safety_margin

should_rollover = main_request_tokens >= main_request_limit
```

最新 tail 的硬上限是：

```text
tail_token_limit = floor(prepared.context_limit * 0.10)
```

这是 raw AI/tool tail 的上限，不是 summary message、system/runtime、工具 schema 或 output reserve 的预算。最终 candidate request 仍必须满足完整主请求预算。

### 4.2 Closed AI/Tool Batch

从 semantic source 尾部向前选择保留 batch：

- 不带 tool call 的 `AIMessage` 是一个 standalone assistant batch；
- 带 tool calls 的 `AIMessage` 必须连同每个 call id 恰好一个 `ToolMessage`；
- orphan ToolMessage、重复 result、未闭合 call 或跨 batch result 都不能进入保留 tail；
- 一个 batch 要么全部保留，要么全部进入 summary source；
- 如果最新完整 batch 本身超过 10% 上限，则 tail 为空，不截断 batch；
- tail 选择不能保留 synthetic continuation、pressure hint、inline compaction guide 或其他 ephemeral control message。

tail 选择结果必须通过 provider-neutral `validate_closed_tool_batches()`，并使用实际 request token estimator 重新验证。

### 4.3 Source Range

给定有效 semantic messages：

```text
[previous compaction user, if any]
[raw execution messages]
[latest complete AI/tool tail, if retained]
```

source range 是除 retained tail 外的前缀。若前缀中存在上一条 compaction user，它必须被保留在 source 中，而不是作为独立 previous summary 参数传给模型。

没有新的 accepted semantic source 时禁止生成 replacement：

- 不能只用同一条 summary user 重新生成另一条 summary；
- `source_range_hash` 不变时禁止重复提交；
- 不允许靠空 replacement 推进 `segment_index`。

真实用户 guidance 属于 semantic source，必须保留并参与 summary；只有系统 guard、pressure hint、inline guide 和 rollover continuation 等 synthetic control 可以过滤。

### 4.4 Summary 输入与输出

summary agent 输入为：

```text
[previous synthetic compaction user, if present]
[new semantic user/assistant/tool messages]
[static compaction suffix]
```

不再有 `previous_summary` 独立参数，也不再把 summary 放入 system message。

输出必须符合现有 `SUMMARY_TEMPLATE` 的固定 Markdown sections，且：

- 不包含 tool call；
- 不回答用户；
- 不执行任务；
- 不重复附加另一份 summary；
- 保留 Goal、约束、进度、决策、下一步、错误、路径和验收事实；
- 对已有 compaction user 进行递归更新，而不是把多个 summary 并列输出。

summary content 作为新 `HumanMessage.content` 保存。若需要提示模型继续当前任务，使用不持久化的 continuation marker/message，不把它混入可见 summary 文本。

### 4.5 Candidate 验证

summary 返回后，在提交 replacement 前构造未提交 candidate projection：

```text
[synthetic summary user]
[retained complete AI/tool tail]
```

必须验证：

1. summary schema 和 marker metadata 合法；
2. source range 的 tool batches 完整；
3. `source_range_hash` 与当前有效 projection 一致；
4. source 中存在新的 accepted semantic source；
5. candidate prepared request 在主预算内；
6. `pre_request_tokens - candidate_tokens >= minimum_net_reclaim`；
7. replacement 后不会连续对同一 prepared request hash rollover；
8. deterministic fallback 也必须经过同样的 candidate 验证。

任何失败都不得写入 replacement event、runtime state 或 live graph state。

## 5. 持久化与有效 transcript

### 5.1 Replacement API

在 `src/voidx/agent/adapters/persistence/session_repository.py` 增加面向有效消息的替换接口，建议形状：

```python
replace_effective_message_range(
    session_id: str,
    source_message_ids: list[int],
    source_range_hash: str,
    replacement: MessageRow,
    operation_id: str,
    *,
    closed_segment_index: int,
    opened_segment_index: int,
) -> ReplacementResult
```

Goal accepted transcript 需要对应的 fenced adapter，额外校验 generation、attempt、lease owner 和 fencing token。

API 必须在 session lock 下：

1. 读取当前 effective projection；
2. 校验 source ids 顺序、存在性和 hash；
3. 校验 replacement row 的 marker、role 和 metadata；
4. 用一个 replacement operation 删除 source rows；
5. 将 replacement 插入 source 第一条消息的位置；
6. 更新 message count、session cache 和 row-message cache；
7. 使用 operation id 保证重试不会重复插入；
8. 返回当前 winner；CAS/hash 冲突时不得覆盖 winner。

自动压缩路径禁止调用 `delete_messages_through()`。

### 5.2 JSONL Event Projection

当前 loader 按 message id 排序，无法直接给 replacement 分配新 id 后追加，否则 summary 会落到 transcript 尾部。因此增加事件重放语义：

```json
{
  "type": "message_replaced",
  "operation_id": "...",
  "source_message_ids": [12, 13, 14],
  "source_range_hash": "...",
  "replacement": {
    "id": 31,
    "role": "user",
    "content": "...",
    "content_format": "text",
    "additional_kwargs": {
      "_voidx_compaction_message": true,
      "compaction_id": "..."
    }
  },
  "closed_segment_index": 1,
  "opened_segment_index": 2,
  "created_at": "..."
}
```

loader 重放规则：

- 首次加载 legacy message records 时按旧规则建立有效顺序；
- `message_replaced` 按 source 第一条记录的位置插入 replacement；
- source ids 从有效 projection 移除；
- 相同 operation id 再次出现时跳过；
- source 不存在或 hash 不一致时报告 corruption，不静默生成另一份 summary；
- 旧 `message_deleted` 事件继续兼容，但自动 rollover 不再生成新的 through-delete event。

文件追加和 SQLite message count 更新不是跨存储原子事务。恢复策略必须保证：

- replacement event 已 fsync、SQLite 更新未完成：重新计算有效 count；
- SQLite 更新完成、cache 未更新：下次从 effective projection 重建 cache；
- event 重放两次：operation id 保证只有一条 replacement；
- source range 已被另一个 writer 替换：当前 writer 丢弃 candidate，加载 winner。

“真正替换”定义为所有有效读路径都不再返回 source rows；底层 event log 在 GC 前保留旧字节不构成可见 transcript。若产品后续要求物理擦除，再增加 locked file rewrite/GC 设计，不能在 replacement API 中静默破坏 crash recovery。

### 5.3 Runtime State

删除 Long Summary 的主路径后：

- `RuntimeContextBuilder` 不再读取或编译 `summary` section；
- `_compaction_summary`、`_pending_summary` 不再作为模型上下文事实源；
- 现有 `session_runtime_state.compaction_summary` 仅用于 legacy migration，迁移完成后不再写入；
- 不新增 `context_compaction_revisions` 作为 summary 真相源；有效 transcript 中的 compaction user row 是唯一模型输入事实。

如果实现需要持久化当前 execution segment，字段只能作为 execution metadata，不能替代 replacement operation 或重新引入 Long Summary。

## 6. LangGraph live state 与 turn journal

### 6.1 ActiveTurnInput

在 `AgentState`/thread execution state 增加结构化 `ActiveTurnInput`，至少包含：

```text
turn_journal_id
user_message_id
raw_text
semantic_text
display_text
title_text
content
content_format
segment_index
```

`RemoveAll` 只清理 live `messages`，不能清理该对象。

以下路径必须优先读取 ActiveTurnInput：

- `runtime/topology.py:latest_user_text()`；
- Goal/workflow scope；
- convergence fallback；
- title、recent exchange；
- error/cancel/finalize 收尾。

### 6.2 Idempotent Journal Flush

`turn_runner.py` 引入 execution-local turn journal：

- 用户 row 在 graph 开始前保存一次；
- 每个 AI/Tool batch 在 rollover 前 flush 一次；
- graph message 通过稳定 key 或 payload hash 标记已提交；
- rollover replacement 记录 source ids 和 operation id；
- turn complete、exception、cancel 共用同一个 flush API；
- synthetic summary 和 continuation 永不进入 real-message flush；
- 同一个 graph message 不得重复追加。

turn runner 不得再通过“在 final live messages 中找原始 HumanMessage”决定保存范围。

### 6.3 Replacement 后的 live state

成功 commit 后，graph state 使用：

```text
RemoveMessage(REMOVE_ALL_MESSAGES)
+ synthetic summary HumanMessage
+ retained complete AI/tool tail
+ ephemeral rollover continuation
```

随后重新编译 runtime context。保留：

- ActiveTurnInput；
- `turn_state`；
- TaskState、Goal、workflow runs、todo；
- persona、interaction mode、permission；
- step/recursion/wall-clock guard；
- 尚未投递的真实 user guidance。

continuation：

- 带专用 marker；
- 不持久化；
- 不触发 Goal intake；
- 不计入 user turn 或 summary source；
- 不要求模型调用新的 `turn(operation="start")`。

## 7. Prepared request 与压缩协调

新增 `src/voidx/agent/adapters/langgraph/runtime/prepared_request.py`，集中生成不可变 `PreparedMainRequest`：

```text
model/config/context/output limits
provider-ready sanitized messages
active tools and order
bind options/tool choice
protocol/endpoint
canonical request representation
request hash
cache capability/fingerprint
full token breakdown
```

`llm_turn.py` 必须在每次 provider call 前只构造一次 candidate snapshot，预算判断、context frame、实际调用和 post-budget 验证使用同一 logical request。

`compaction_coordinator.py` 改为：

```text
prepare main request
  -> under budget: call main
  -> over budget:
       flush accepted AI/tool source
       validate closed batches
       choose <=10% complete tail
       detached summary request
       validate summary/candidate projection
       fenced replacement commit
       RemoveAll + summary user + tail + continuation
       retry main loop
```

首个实现阶段使用 detached summary path，避免在 replacement 语义未稳定时误报 provider cache alignment。后续 aligned summary 必须复用同一 prepared request，并以 exact provider prefix 作为 gate，不能只比较 provider/model 字符串。

provider overflow：

- 成功 replacement 后只允许一次 hash 变化的 main request rebuild；
- 若没有新的 accepted source，不创建同-cursor amendment；
- 仍超限返回明确的 `ContextBudgetExhausted`；
- 不退回 pressure hint 作为默认恢复路径。

## 8. Resume、legacy 和并发

### 8.1 Resume

resume 流程：

1. `load_messages()` 读取 effective projection；
2. synthetic compaction row 恢复为 `HumanMessage`；
3. compaction row 参与模型上下文，但不参与 real user turn 识别；
4. 新用户输入创建新的 `ActiveTurnInput` 和唯一真实 user row；
5. runtime context 不再从 `compaction_summary` 生成 system section；
6. 若存在未完成 replacement operation，按 operation id 重放或拒绝损坏 projection；
7. 不恢复进程退出时未提交的 graph loop，只恢复已提交消息。
8. 若 replacement 已提交但 real user turn 未完成时进程退出：resume 后 transcript 只包含已提交的 summary 和 retained tail，该 turn 不自动续跑、不重复 materialize；代码中没有既有 stale turn 恢复机制，本设计也不新增——用户的下一次真实输入直接开启新 turn。

### 8.2 Legacy session

已有 session 的迁移顺序：

- 若有 `compaction_summary` 且 effective message history 中没有 compaction marker，首次新路径启动时创建一条 legacy synthetic user row；
- legacy row 的 `compaction_depth=0`，`source_range_hash` 使用空/legacy 标识，不反推已经删除的历史；
- materialization 成功后清除或停止读取 runtime summary；
- 旧实现已删除的消息不能恢复；剩余 effective rows 作为该 summary 后的 raw history；
- materialization 必须幂等，重复 resume 不得生成第二条 legacy summary。

### 8.3 并发和 Goal fencing

普通 session 使用 session directory lock + source hash + operation id。

Goal accepted transcript 使用既有 generation/attempt lease/fencing token：

- replacement 必须验证当前 generation 和 attempt；
- lease 失效时拒绝提交；
- loser 不删除 winner 的 source、不覆盖 replacement；
- winner 提交后 loser reload effective projection。

## 9. UI 与协议

summary row 使用普通 `role="user"` 持久化并展示，不能创建新的 turn node。可选的 presentation label 应从 marker 推导，例如“Context summary”，但不改变 user message 的存储 role。

正常路径停止生成新的 model-visible context-pressure HumanMessage。旧 `ContextPressureUpdated`/`ContextPressureFinished` 可以保留兼容周期；旧 marker 仍可在 loader/replay 时清理，但不能成为 rollover 失败的默认兜底。

replacement/rollover status 建议使用现有 `StatusUpdated`/`StatusFinished`，包含：

- source message count；
- source/pre/post tokens；
- retained tail tokens；
- compaction depth；
- operation id；
- fallback 状态。

不要在 UI contract 中新增第二个真实 turn 事件。

## 10. 文件归属

### Domain / marker / policy

- `src/voidx/llm/message_markers.py`：compaction user、continuation marker 和识别函数。
- `src/voidx/agent/domain/compaction.py`：tail/source/replacement result、metadata 和错误模型。
- `src/voidx/llm/compaction/service.py`：closed batch、10% tail、source range 和 candidate budget policy。
- `src/voidx/llm/compaction/summary_input.py`：递归 summary 输入和 control message 过滤。
- `src/voidx/llm/compaction/constants.py`：summary suffix、tail ratio 和过渡期兼容常量。

### LangGraph runtime

- `src/voidx/agent/adapters/langgraph/state.py`：ActiveTurnInput、segment/journal state。
- `src/voidx/agent/adapters/langgraph/runtime/thread_context.py`：execution-local active turn/journal 状态。
- `src/voidx/agent/adapters/langgraph/runtime/prepared_request.py`：prepared provider request contract。
- `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py`：主请求前 rollover decision、overflow retry 和 live replacement。
- `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py`：summary invocation、candidate validation、replacement orchestration。
- `src/voidx/agent/adapters/langgraph/runtime/core/context.py`：RemoveAll、summary user、tail、continuation state replacement。
- `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`：idempotent journal flush 和 turn 收尾。
- `src/voidx/agent/adapters/langgraph/runtime/topology.py`、`convergence.py`：跳过 synthetic summary、读取 ActiveTurnInput。
- `src/voidx/agent/application/runtime_context.py`：移除 Long Summary section。
- `src/voidx/agent/adapters/langgraph/execution.py`：移除自动路径 `_inline_compaction_guide_for` 对 `select_details()`、`_compaction_summary` 和 `inline_compaction_enabled` 的依赖。
- `src/voidx/agent/adapters/langgraph/runtime/context_pressure.py`：正常路径停止生成 model-visible pressure HumanMessage；旧 `ContextPressure*` 事件仅保留兼容读取。
- `src/voidx/agent/adapters/langgraph/graph_compaction.py`：manual compaction 入口切换到同一个 replacement use-case。

### Persistence

- `src/voidx/agent/adapters/persistence/message_rows.py`：marker row hydration 和 real user turn 识别。
- `src/voidx/agent/adapters/persistence/session_repository.py`：effective range replacement、message count、Goal fenced adapter。
- `src/voidx/persistence/jsonl.py`：replacement event 的追加、fsync 和 replay helper。
- `src/voidx/agent/adapters/persistence/runtime_state_repository.py`：legacy summary 读取迁移和停止写入。

### Tests

- `src/tests/test_llm/compaction/test_compaction_replacement.py`
- `src/tests/test_llm/compaction/test_compaction_tail_budget.py`
- `src/tests/test_agent/adapters/persistence/test_message_replacement.py`
- `src/tests/test_agent/adapters/langgraph/runtime/test_context_rollover.py`
- `src/tests/test_agent/adapters/langgraph/runtime/test_turn_journal.py`
- `src/tests/test_agent/adapters/langgraph/runtime/test_compaction_resume.py`

## 11. 实施阶段

### Phase 1 — Marker、policy 和纯函数

1. 新增 compaction/continuation marker 和 metadata 模型。
2. 实现 closed batch validator 和 10% tail selector。
3. 修改 summary input，使上一条 compaction user 与新 source 一起进入 summary。
4. 先写并运行 LLM policy tests。

验证：

```bash
./test.py --backend -- \
  src/tests/test_llm/compaction/test_compaction_replacement.py \
  src/tests/test_llm/compaction/test_compaction_tail_budget.py -v
```

### Phase 2 — Effective transcript replacement

1. 为 JSONL 增加 replacement event replay。
2. 实现 session effective range replacement 和 operation id 幂等。
3. 更新 message row hydration、user turn 识别和 message count。
4. 实现 legacy summary materialization。

验证：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/persistence/test_message_replacement.py \
  src/tests/test_persistence/test_schema_migration.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_persistence.py -v
```

### Phase 3 — Turn journal 与 ActiveTurnInput

1. 增加 ActiveTurnInput、segment index 和 journal state。
2. 将正常完成、异常、取消和 rollover 前 flush 收敛到一个幂等 API。
3. 更新 latest user、Goal/workflow、title、recent exchange 和 fallback 消费路径。
4. 确认 synthetic summary 不产生真实 turn 生命周期事件。

验证：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_turn_journal.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_context_rollover.py -v
```

### Phase 4 — Runtime rollover

1. 引入 PreparedMainRequest 和统一 token breakdown。
2. 在 provider call 前执行 over-budget decision。
3. 在 closed batch 边界 flush source，生成 summary user，验证 candidate。
4. 使用 RemoveAll + summary user + tail + ephemeral continuation 更新 live state。
5. 实现多次 rollover、provider overflow rebuild 和 no-progress guard。
6. 移除 `runtime_context.py` 的 Long Summary section 编译；停止正常路径 pressure hint（`context_pressure.py`）和 inline compaction guide（`execution.py`）。

验证：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_context_rollover.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_call_llm_compaction_advanced.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_compaction_resume.py -v
```

### Phase 5 — Resume、Goal fencing 和兼容清理

1. 增加 crash point、重复 operation、并发 winner/loser 和 Goal lease 测试。
2. manual compaction（`graph_compaction.py`、`CompactContextTool`）切换到同一个 replacement use-case。
3. 确认 runtime state 不再写入 Long Summary。
4. 兼容窗口结束后删除旧 selector、自动 pressure hint 和无效 legacy constants。

验证：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/persistence/test_message_replacement.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_compaction_resume.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_session_runtime_state.py -v
```

### Phase 6 — Cache alignment（后续）

1. 让 aligned summary 复用成功主请求的精确 prepared provider prefix。
2. 增加 fingerprint、tool choice gate 和 provider cache usage 观测。
3. 不兼容时继续使用 detached summary；没有 usage 证据时只标记 estimated。

验证：

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/langgraph/runtime/test_compaction_cache_alignment.py -v
```

## 12. 禁止变更

实现不得：

- 恢复 Long Summary system section 或把 summary 写入 system message；
- 为同一 source range 持久化 assistant summary + user summary 两条消息；
- 把 synthetic summary 当成真实用户输入，创建新的 `TurnStarted`、Goal 或标题；
- 只保留 tool batch 的一部分；
- 用 `DEFAULT_TAIL_TURNS`、最近 N 个 turn 或 turn count 决定自动压缩；
- 自动 rollover 调用 `delete_messages_through()`；
- 在没有 source hash 变化时重复替换；
- 把 replacement summary 追加到 transcript 尾部破坏原始顺序；
- 把 ephemeral continuation 或压力 hint 写入 effective transcript；
- rollover 后重置 task/workflow/todo/persona/permission/step/recursion/wall-clock guard；
- summary 输出 tool call 后执行工具；
- 通过 provider/model 字符串直接声称 cache hit；
- 用旧 pressure hint 作为自动 rollover 失败的默认兜底；
- 绕过 `./test.py` 直接把 pytest/vitest/cargo 作为标准验证命令。

## 13. 验收标准

1. 单个 real user turn 在不增加用户输入的情况下至少完成两次 replacement rollover。
2. 每次 replacement 只留下一个新的 synthetic `role="user"` summary row。
3. effective transcript、UI、resume 和模型请求都看不到被替换的原始 rows。
4. summary row 位于 source range 原位置，后面紧接 retained tail。
5. retained tail 是完整 AI/tool batch，Token 不超过 context window 的 10%。
6. 第二次 summary 输入包含上一条 compaction user 和其后的新 semantic messages。
7. 第二次 replacement 后不存在旧 summary 和新的 summary 并列。
8. summary row 不产生新的真实 turn 生命周期事件。
9. `latest_user_text()`、Goal、title、recent exchange 和 fallback 都读取真实 ActiveTurnInput，不误读 summary。
10. tool-call batch 不被切断，orphan/duplicate result 会拒绝 rollover。
11. replacement operation 在重复重放、进程 crash、source hash 冲突和并发 writer 下不丢失、不重复覆盖。
12. resume 后可以继续处理新的真实 user input，且不重新 materialize 第二条 summary。
13. system/runtime context 中不再出现 Long Summary。
14. 正常路径不再生成 model-visible context-pressure convergence HumanMessage。
15. focused、architecture/contracts 和 full backend tests 通过。

## 14. 风险与缓解

### Summary 漂移

递归 summary 可能逐次丢失事实。缓解措施：固定 Markdown sections、保留 TaskState/workflow/todo 结构化真相、保留 source hash/operation metadata、加入跨两次以上 replacement 的事实保留测试。

### Replacement 顺序损坏

新 row id 通常大于旧 row id，简单按 id 排序会把 summary 放到尾部。必须采用 event replay 的有效顺序，不能通过特殊 id 猜测位置。

### Tool batch 不完整

provider 返回的 call/result 结构可能存在重复或 orphan。所有保留和 source boundary 都必须经过 provider-neutral validator，不能靠 replay sanitizer 补造事实。

### JSONL/SQLite 非原子

replacement event 与 message count 可能跨存储提交。使用 session lock、fsync、operation id、projection 重建和 corruption detection；不宣称跨存储原子事务。

### Turn journal 重复写入

RemoveAll 后 final live messages 不再包含原始 user anchor。使用 stable graph message key/payload hash 和统一 flush tracker，禁止重新从 live list 反推保存范围。

### 回滚

通过 `context_rollover_enabled` feature flag 控制新路径。关闭时只回到旧 compaction，不得让旧删除路径和 replacement writer 同时处理同一 session。replacement event 和 marker 保持向后可读，不做 destructive schema downgrade。


## 15. 迁移保留的通用约束

以下约束来自此前的 rollover 方案，但已经改写为适用于单条 synthetic user replacement 的语义；不恢复 Long Summary、revision/cursor 或 canonical append-only transcript。

### 15.1 固定开销、可回收开销与净收益

prepared request 的 token breakdown 必须明确区分：

```text
mandatory_request_tokens = tokens(
    provider framing
    + active tool schema and bind options
    + stable system/runtime/project instructions
    + Current Task State/workflow/todo/persona/permission facts
    + required provider/control envelope
    + one ephemeral rollover continuation
)
```

如果：

```text
mandatory_request_tokens
+ main_output_reserve
+ safety_margin
>= context_limit
```

则报告 `fixed_context_exceeds_budget`。应先依据既有 tool-surface policy 缩小允许工具面；仍无法满足时明确终止，不得对空 source 或很小 source 反复生成 summary。

summary request 也必须按自身实际请求验证：

```text
summary_request_tokens
+ summary_output_reserve
+ safety_margin
<= summary_context_limit
```

只有 candidate 同时满足以下条件才允许 replacement：

```text
candidate_main_tokens < main_request_limit
pre_request_tokens - candidate_main_tokens >= minimum_net_reclaim
```

`minimum_net_reclaim` 首版至少为 `max(1_024, ceil(main_context_limit * 0.01))`，但最终必须以完整 prepared request 验证为准。summary user、保留 tail、runtime/task sections、工具 schema 和 continuation 都计入 candidate request；10% 只限制 raw retained AI/tool tail，不代表整个 candidate 只占 10%。

### 15.2 无 source、无进展和尝试预算

成功 replacement 必须同时满足：

- source range 中存在新的 accepted semantic source；
- source hash 发生变化；
- replacement 后 candidate request 在预算内；
- candidate 达到最小净回收；
- replacement 不会再次对相同 prepared request hash 立即 rollover。

同一个 source range 不得通过生成空 summary、修改 segment metadata 或重复 materialization 伪造进展。

所有 summary 模式和重试共享一个总 attempt budget，建议为：

```text
COMPACTION_MAX_RETRIES + 1
```

不得让 detached、configured model、exact main model 和 deterministic fallback 各自消费完整重试次数。耗尽后按以下顺序结束：

```text
summary attempt(s)
→ deterministic fallback merge
→ rebuild candidate budget verification
→ explicit ContextBudgetExhausted
```

fallback 必须合并已有 compaction user 的事实，而不是覆盖它；fallback 也必须经过 marker、source hash、closed batch、post-budget 和 net-reclaim 验证。任何 candidate 验证失败都不得产生 durable side effect。

### 15.3 初始超大输入

如果第一次主请求在没有任何 compaction user 的情况下就超限：

1. 不使用 aligned summary；首版直接使用 detached input normalization；
2. normalization 输入为原始用户消息和附件引用，不能依赖从 live messages 反向搜索的 user anchor；
3. 生成的首条 synthetic user 必须保留 Goal、约束、路径、命令、错误和验收标准；
4. 原始用户消息已经被 replacement 后，不再作为 effective transcript row 返回，但其语义必须进入 summary；
5. normalization 后 candidate 仍超限时返回 `initial_input_exceeds_context_budget`。

如果 stable system/runtime、工具 schema 或最小 output reserve 本身已超限，则报告 fixed context 分解并终止；不得无限 rollover 空 source，也不得重新注入 pressure hint 假装可恢复。

### 15.4 Provider overflow 与降级

provider 报告 overflow 时，运行时必须进入同一 replacement/emergency 流程，不盲目重发相同请求：

- 如果已有新的 accepted source，按 emergency 策略裁剪可重建的大工具原文，保留最终结果、错误、路径、diff 引用和 TaskState，再 detached summarize；
- replacement 成功后只允许一次 hash 发生变化的 main request rebuild；
- 如果 replacement 后没有新的 accepted source，禁止创建 amendment summary；
- prepared request hash 未变化时不得重试同一超窗请求；
- 仍无法满足预算时返回 `ContextBudgetExhausted(reason="provider_overflow_without_new_source")` 或等价明确错误。

emergency、detached、deterministic 三种模式都不能执行 summary 输出中的 tool call。summary 空文本、缺少固定 Markdown sections、输出 tool call 或无法通过 candidate 验证时，视为该 attempt 失败，不得提交替换。

### 15.5 Manual、inline 与 UI 兼容

manual/idle compaction 复用同一个 `replace_effective_message_range()` use case，但：

- 不创建新的真实 user turn；
- 不注入 continuation；
- 仍可展示 replacement status；
- 不得与自动 rollover 并发提交同一 source range；
- `CompactContextTool` 在迁移期不能绕过 source hash、operation id 和 replacement validator。

自动路径不使用 `inline_compaction_enabled` 或 `VOIDX_COMPACTION_GUIDE` 驱动模型自行压缩。旧 selector 可以暂留给兼容测试，但不能继续影响自动触发或 tail 选择。

UI 继续只显示一个真实 turn。rollover 使用现有 `StatusUpdated`/`StatusFinished`，建议提供：

- `segment_index` 和 `compaction_depth`；
- source message count；
- pre/post/reclaimed tokens；
- retained tail tokens；
- replacement operation id；
- cache mode 和 provider cache read/write tokens（若有）；
- fallback 或 emergency 状态。

`ContextPressureUpdated`/`ContextPressureFinished` schema 和 gateway adapter 可保留一个兼容周期，但正常主路径不得再生成 model-visible context-pressure HumanMessage。新 status 使用 `display="record_only"`，不得增加 transcript turn node。若后续删除旧事件，必须运行 UI schema export 和 gateway contract tests。

### 15.6 Cache 观测与安全

首阶段 summary 默认 detached。后续启用 aligned summary 时，只有 exact main instance、精确 provider-ready common prefix、工具定义/顺序、bind options、tool choice、protocol、endpoint 和 reasoning 全部兼容才允许 aligned；强制 tool choice 时必须 detached。

缓存 metadata 可以记录：

```text
cache_mode
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
compaction_depth
```

没有 provider 返回的 `cached_tokens`、`cache_read` 等证据时，不得宣称实际 cache hit；本地 common-prefix 只能标记为 estimated。fingerprint 和日志不得包含 API key、credential hash 或敏感 endpoint query。

### 15.7 可观测性、风险和回滚

每次 replacement 至少记录 source hash、operation id、pre/post token breakdown、retained tail、fallback/emergency reason 和 candidate validation outcome。context frame 可以保存主请求和 summary 请求的观测信息，但不能成为 effective transcript 或 replacement state 的唯一真相源。

递归 summary 的事实漂移通过以下方式缓解：

- 固定结构化 Markdown sections；
- TaskState、workflow、todo 和 permission 继续作为结构化真相；
- replacement metadata 和 source hash 可审计；
- 保留 event log 直到 projection 已验证并完成 GC；
- 测试跨至少两次 replacement 的 Goal、约束、错误、路径和验收标准不丢失。

turn journal 必须先完成幂等 flush，再开启自动 segment rollover；否则 live state 与 effective transcript 可能重复或丢失 assistant/tool rows。

迁移期使用 `context_rollover_enabled` feature flag：

- 默认关闭，直到 focused、architecture/contracts 和 broader backend tests 通过；
- 开启后，单个 session 只能有 replacement writer，不能同时运行旧的 delete-through 自动 writer；
- 关闭时可短期回到 legacy compaction，但 legacy path 不得写入 replacement event；
- replacement marker/event 保持向后可读，不做 destructive downgrade；
- pressure hint 只保留旧 session 清理和兼容读取，不作为长期双轨恢复方案。

## 16. 追加验证矩阵

除各阶段 focused tests 外，最终至少执行：

```bash
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
./test.py --backend -- src/tests/test_presentation/gateway/test_adapter.py src/tests/test_presentation/gateway/test_ui_events_dock_status.py -v
./python.py scripts/export_ui_protocol_schema.py
./test.py --backend
```

如果 UI schema 导出发生变化，必须检查并提交对应 fixture/generated contract；不得手改生成文件替代导出命令。现有 permission、workflow、todo、Goal control、recursion、wall-clock 和 session persistence 测试必须保持通过。
