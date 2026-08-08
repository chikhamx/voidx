# 单轮长会话上下文超阈值：整轮压缩 + 本轮收敛提示

> **Status: Done** — Archived on 2026-08-09.

## 状态

设计已确认，待实现。

- 根因：已定位
- 约束：只整轮压缩，不切断单轮中间
- 方案：上下文压力检测 → 提示 LLM 收敛本轮 → 轮次足够后再整轮压缩
- 审查：已关闭 soft 触发点 / hint 生命周期 / hard 失败路径 三个 blocker

## 问题现象

一轮会话过长（例如一个轮次内产生大量工具调用与输出）导致上下文 tokens 超过硬阈值（90% `context_limit`）时，压缩流程未触发：LLM 继续带着超阈值上下文运行，可能触发模型侧报错或质量下降。

## 根因

### 触发链路

压缩在所有检查点最终都汇聚到同一个选择逻辑：

1. 轮次开始前 preflight 检查（软阈值 75%）：
   `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`
   → `host._preflight_compact_if_needed(...)`，reason=`"soft_threshold"`。
2. 每步 LLM 调用前硬阈值检查（90%）：
   `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py`
   → `host._compaction.is_overflow(...)` 为真时
   `force=True, reason="hard_threshold"` 调用 `_preflight_compact_if_needed`。
3. 汇聚点：`compaction_coordinator.py:compact_for_live_state`
   → `select_preflight_details` / `select_details`（`src/voidx/llm/compaction/service.py`）。

### 失效点：轮次数不足时选择返回 `"none"`

`select_details` 与 `select_preflight_details` 都依赖“轮次”（用户消息为起点）划分 head/tail，并要求至少保留一个完整旧轮次作为 tail 起点：

- `select_preflight_details`：`minimum_keep_index = max(0, len(turns) - 2)`。
  当 `turns` 只有 1–2 个时 `keep_start` 恒为 0 → `mode="none"`。
- `select_details`：`_minimum_tail_turn` 在单轮时返回唯一轮次（`start == 0`）→ `"none"`；
  双轮时最小保留轮次是第一轮 → head 为空 → `"none"`。

`CompactionSelection.should_compact` 要求 `mode != "none"` 且有 head 和 `tail_id`，
因此单轮/双轮巨型场景恒为 `False`。

`compact_for_live_state` 在 `should_compact=False` 时直接 `return None`，即使 `over_hard=True` 或 `force=True`；
UI 显示 `Compaction skipped: no older complete turn to summarize`。

在该跳过分支上，**本轮 live 消息不会被压缩替换**。
`llm_turn` 只在 compaction result 非 `None` 时更新消息列表 → LLM 继续使用超阈值上下文。

说明（避免过度绝对）：

- `truncate_head_to_budget` 主要用于 compaction agent 输入裁剪，不是 live-state 主路径的通用兜底；
- fallback summary 存在于其他路径，但 **不能替代** “live 超限且 `should_compact=False`” 时的处理；
- 因此问题不是“代码里完全没有 fallback 函数”，而是 **该失败分支没有对 live 上下文生效的兜底动作**。

### 设计意图 vs 实际缺陷

保留完整轮次的约束（不切断单个轮次中间）是合理的对话连贯性设计；但它隐含假设
“上下文超限时至少已积累 3 个轮次”。单轮内产生海量工具输出的场景（长任务、大文件遍历、
批量 MCP 调用）不满足该假设，导致硬阈值失效。

### 复现证据

脚本构造单轮/双轮/三轮消息序列（`context_limit = 128_000`，硬阈值 115_200，软阈值 96_000），
验证 `CompactionService`：

| 场景 | tokens | is_overflow(90%) | select_preflight_details | select_details | 结果 |
|---|---|---|---|---|---|
| 单轮巨型（823 条消息） | 120,015 | True | none | none | 跳过 |
| 双轮（第一轮巨型 + 当前请求） | 100,165 | — | none | none | 跳过 |
| 三轮对照组（第一轮巨型） | 100,171 | — | normal，head 687 条 | normal | 正常压缩 |

- 场景 A 中 `is_overflow == True` 且 `is_soft_overflow == True`，但两个选择函数均返回 `mode="none"`；
- 场景 C 表明机制本身正常，仅在轮次数 < 3 时失效。

## 目标与非目标

### 目标

1. **保持整轮压缩语义**：只压缩完整旧轮次，不切断当前轮中间。
2. **单轮/双轮超限时有主动响应**：不再静默继续。
3. **用上下文压力提示驱动本轮收敛**：让 LLM 停止扩大探索、尽快结束本轮。
4. **轮次足够后恢复正常整轮压缩**：当语义轮次达到可压缩条件（通常 ≥ 3）时，现有 preflight/hard 压缩路径继续生效。

### 非目标

1. 不实现“单轮截断 summary”（切断 turn 中间做 head/tail）。
2. 不把 `inline_compaction_guide`（提示模型调用 `compact`）作为本问题的主修复。
   它同样依赖 `select_details`，单轮场景仍会失效。
3. 本阶段不做 hard 阈值下的强制 prune/截断（可作为后续增强）。
4. 不改变 3 轮及以上且 `can_compact=True` 场景的现有压缩行为。
5. 不承诺“任意下一轮立刻可压缩”：单轮后再开一轮常仍是双轮，`should_compact` 仍可能为 `False`。

## 已确认方案

**整轮压缩保持不变 + 无法整轮压缩时注入上下文压力收敛提示。**

核心思想：

- 压缩边界 = 完整用户轮次边界。
- 当 tokens 已超阈值，但选择逻辑无法产生可压缩 head 时，系统不假装压缩成功，
  而是告诉模型“上下文压力高，请收敛并结束本轮”。
- 提示本身不减少已有 tokens；它通过改变模型行为，避免本轮继续膨胀，
  并为后续整轮压缩创造条件（需要积累足够完整旧轮）。

### 为什么可行

| 条件 | 说明 |
|---|---|
| 与用户约束一致 | 不切半轮，只整轮压缩 |
| 复用现有注入通道 | `STEP_HINT_MARKER` 消息不计入语义轮次（`_turns` 会跳过） |
| 检查点可扩展 | 现有 soft preflight + hard overflow 可复用判定；soft 逐步注入需在 `llm_turn` 新增轻量检查 |
| 失败模式明确 | 提示无法回退 tokens；hard 场景必须显式暴露状态，不能静默 |

### 为什么不能只靠提示当硬兜底

1. 模型可能忽略提示，继续广搜/读大文件。
2. 单次超大 tool 输出可能直接打爆窗口。
3. 提示不会删除历史消息，tokens 不会立刻下降。

因此本方案的成功标准是：

- soft 阶段显著提高“本轮收敛”概率；
- hard 阶段不再静默跳过，至少给出明确压力信号/可观测状态；
- 模型仍 overflow 失败时，错误路径可区分“已注入压力提示但仍失败”；
- 有完整旧轮可压时，行为与今天一致。

## 冻结决策（审查 blocker 关闭）

### D1. Soft 触发点：每步 LLM 前都检测

**决定：soft 不是只靠 turn 开始 preflight；`llm_turn` 每步调用模型前都做压力判定。**

理由：

- 现网 `compact_for_live_state` 中 `over_soft = preflight and is_soft_overflow(...)`，非 preflight 不算 soft；
- `llm_turn` 主路径今天主要在 hard 时才走 compaction；
- 若 soft 只放在 `turn_runner` preflight，本轮中途涨过 75% 时无法及时干预。

因此本方案 **新增** 一个轻量检查点，而不是声称“完全复用现有检查点”：

| 检查点 | 现网 | 本方案 |
|---|---|---|
| `turn_runner` preflight | soft 压缩尝试 | 保持；只尝试整轮压缩，不注入 pressure hint |
| `llm_turn` 每步 LLM 前 | 主要 hard 压缩尝试 | **新增** soft/hard pressure 判定与 hint 注入/升级；hard 且 `can_compact` 仍走整轮压缩 |

判定本身纯函数、不发 LLM；只有 hint 注入是副作用。

### D2. Hint 生命周期：稳定 id + 显式 LangGraph state delta

**决定：压力提示写入 `AgentState.messages`，使用 `STEP_HINT_MARKER` 和稳定 message id；不能只修改 `llm_turn` 的本地 list。**

当前 `AgentState.messages` 使用 `add_messages` reducer，`llm_turn` 又会先复制
`state_messages = list(state["messages"])`。因此本地 append/replace 不会自动持久到 graph；而
`replacement_messages()` 在未压缩时当前只返回 assistant message。实现必须显式把 pressure
message delta 放进该 node 的每个返回结果。

固定消息形状：

```python
HumanMessage(
    id="voidx:context-pressure:<turn-id>",
    content=render_pressure_hint(level),
    additional_kwargs={
        STEP_HINT_MARKER: True,
        CONTEXT_PRESSURE_MARKER: True,
        "pressure_level": level,
        "pressure_turn_id": turn_id,
    },
)
```

其中：

- `CONTEXT_PRESSURE_MARKER = "_voidx_context_pressure"`，用于精确区分普通 convergence step hint；
- `<turn-id>` 取当前真实 top-level `HumanMessage.id`；缺失时使用其在 semantic state 中的稳定位置派生值，禁止使用随机 UUID/当前时间；
- 同一用户轮的 soft/hard **共用同一个 id**；`add_messages` 因 id 相同执行替换而不是追加；
- soft→hard 通过同 id replacement 完成，不需要先发 `RemoveMessage`；
- hard 后再次评估 soft 仍保留 hard，不降级；
- 新真实 user message 有不同 turn id，因此新 hint 使用新 id；旧 hint继续作为历史 synthetic message存在，但不再是当前有效控制信号，也不会被重复升级；后续整轮压缩输入会过滤它；
- 若兼容旧实现中无 id/重复 pressure hint，helper 可返回针对旧 id 的 `RemoveMessage` 做一次性清理，但正常新路径只产生一条稳定 id 消息。

State delta 规则：

1. **首次 soft/hard**：返回 `[pressure_hint, assistant_or_other_node_messages...]`；
2. **同级重复**：不返回 pressure message delta；state 中已有消息仍用于本次 LLM context；
3. **soft→hard**：返回同 id 的 hard `HumanMessage`，由 reducer 替换；
4. **hard→soft**：不返回 delta；
5. **发生整轮压缩**：走现有 `RemoveMessage(REMOVE_ALL_MESSAGES) + result.live_messages` full replacement，且 `result.live_messages` 不保留已压缩 head 内的旧 pressure hint；
6. **任何提前失败/控制返回**：只要本次生成了 hint delta，都必须与该返回分支的其他消息一起提交，不能只覆盖成功尾返回。

轮次划分与可见性：

- `CompactionService._turns()` 因 `STEP_HINT_MARKER` 不把 pressure hint 计为 user turn；
- 主 LLM context 可看见当前有效 hint；
- `Long Summary` 输入必须通过 `compaction_summary_messages(selection.head)` 排除所有 pressure/step hint；
- 默认 UI 不把该 `HumanMessage` 渲染成普通用户发言，用户可见状态走类型化 pressure 事件。

### D3. Hard 失败路径：提示 ≠ 压缩成功；失败可观测

**决定：hard 压力提示不改变现有模型错误/重试主路径；若最终仍因上下文失败，必须可观测为 context-pressure failure。**

行为：

1. hard + `can_compact=False`：
   - 注入/升级 hard hint；
   - 继续按现有路径尝试模型调用；
   - **不**把 skip compaction 伪装成成功。
2. 模型调用因 context overflow / provider length 类错误失败：
   - 保留现有 retry 策略；
   - 重试前若仍 hard 且无 hard hint，补齐 hard hint；
   - 重试耗尽或不可重试：本轮以失败结束，不生成“假压缩成功”状态。
3. 类型化 pressure 事件必须能区分：
   - `action=converge_hint`（已提示收敛）；
   - `outcome=model_overflow_failed`（提示后仍失败，若发生）。
4. 本阶段 **不** 在 hard 失败后自动 prune / 半轮 summary；这些是后续增强。

### D4. 职责边界：`llm_turn` 是唯一 state 注入 owner

**决定：coordinator 只负责整轮压缩；`turn_runner` 只保留轮前 preflight；pressure hint 只由 `llm_turn` 注入。**

```text
evaluate_context_pressure(...)    -> 纯判定
upsert_context_pressure_hint(...) -> updated local messages + explicit state delta
```

- `compact_for_live_state`：继续只返回 `CompactionResult | None`，不注入 hint；
- `turn_runner`：继续在 graph 启动前尝试现有 preflight compaction，不创建 pressure message；若无法压缩，第一步 `llm_turn` 会在真实 LLM 调用前处理；
- `llm_turn`：每个模型调用前使用实际编译后的 LLM token 数评估；可压缩则优先压缩，不可压缩才 upsert hint；
- provider overflow retry：复用当前 state/local hint，不创建第二条；最终失败时提交 pending hint delta 并发送 failure event。

这样避免 turn runner 直接改初始 list、coordinator 改 live state、llm node 又返回 reducer delta三种提交语义并存。

禁止：

- coordinator 或 `turn_runner` 再注入另一份 hint；
- 只修改 `state_messages` / `llm_messages` 本地 list 而不返回 state delta；
- 各返回分支自行拼 delta，必须通过一个统一 `messages_for_return(...)` helper。

## 行为设计

### 触发条件与 token 输入

pressure API 必须分开接收：

- `semantic_messages`：从 state 消息清洗出的语义序列，只用于 turn selection；
- `llm_context_tokens`：`rebuild_llm_messages` 后、包含当前 system/runtime overlay 与 tool definitions 的真实调用估算，只用于 soft/hard threshold。

禁止把 runtime/system overlay 塞进 `select_details`，也禁止只按 raw semantic messages 估算实际 LLM 压力。

```text
over_soft = is_soft_overflow({"total": llm_context_tokens})
over_hard = is_overflow({"total": llm_context_tokens})
selection = select_details(semantic_messages)
can_compact = selection.should_compact
pressure_level = hard if over_hard else soft if over_soft else none
should_inject = pressure_level != none and not can_compact
```

`turn_runner` 的 preflight 仍可尝试 soft compaction，但不负责 pressure hint。第一步 `llm_turn`
使用已编译实际上下文评估并保证在模型调用前注入。

仅当 `should_inject` 时进入收敛提示路径。若 `can_compact=True` 且命中现有压缩触发条件，整轮压缩优先，不注入 converge hint。

### 分级动作

| 级别 | 条件 | 动作 | 消息历史 | 切半轮 |
|---|---|---|---|---|
| Soft pressure | `over_soft and not over_hard and not can_compact` | 注入/保持一条 soft step-hint | 追加或替换合成 hint | 否 |
| Hard pressure | `over_hard and not can_compact` | 注入/升级一条 hard step-hint；标记可观测状态 | 追加或替换合成 hint | 否 |
| Normal compact | `can_compact` | 现有整轮压缩 | 压缩替换旧轮 | 否 |
| Insufficient turns after N user turns | 仍 `not can_compact` | 继续 pressure 路径，不假装可压缩 | 仅 hint | 否 |

### 提示内容（语义）

提示必须是“收敛本轮”，不是“现在去 compact 半轮”。

Soft 必须包含：

- 当前上下文已接近软阈值；
- 暂无完整旧轮可压缩；
- 停止扩大探索面；
- 避免继续读取大文件/批量工具；
- 优先基于已有证据给出结论并结束本轮；
- 后续在轮次足够时可做整轮压缩（不要承诺“下一轮一定可以”）。

Hard 在 soft 基础上加强：

- 已超过硬阈值；
- 继续扩大上下文可能导致模型调用失败；
- 必须立即收敛并结束本轮；
- 不要再发起非必要工具调用。

### 提示文案草案

#### Soft

```text
Context pressure (soft): the conversation is near the context budget, and there is no older complete turn available to compact yet.
Stop expanding exploration. Prefer finishing this turn with the evidence already gathered.
Avoid large file reads, broad searches, and non-essential tool batches.
Whole-turn compaction can run only after enough complete turns exist; do not assume the immediate next turn will compact.
```

#### Hard

```text
Context pressure (hard): token usage is at or above the hard context threshold, and no older complete turn can be compacted yet.
Converge immediately. Do not start non-essential tools. Summarize current findings and finish this turn now.
Continuing to grow the context may cause the model call to fail.
```

### UI / 可观测性

当前 `StatusUpdated` / `StatusFinished` 没有 metadata 字段，`TurnMetadata` 也只包含
profile/protocol/category；把 pressure 字段写在文档里不会自动经过 gateway 到 UI。

本设计选择新增正式类型化事件，不复用无结构 detail 文本承载机器字段：

```python
class ContextPressureUpdated(UiEventBase):
    kind: Literal["context_pressure.updated"] = "context_pressure.updated"
    pressure_id: str
    level: Literal["soft", "hard"]
    action: Literal["converge_hint"] = "converge_hint"
    outcome: Literal["hint_injected", "hint_present", "hint_upgraded"]
    reason: str
    can_compact: bool = False
    turn_count: int
    pre_tokens: int
    soft_threshold: int
    hard_threshold: int

class ContextPressureFinished(UiEventBase):
    kind: Literal["context_pressure.finished"] = "context_pressure.finished"
    pressure_id: str
    level: Literal["soft", "hard"]
    outcome: Literal["turn_converged", "compacted", "model_overflow_failed"]
    detail: str = ""
    ok: bool = True
```

事件语义：

- 首次注入：`updated/outcome=hint_injected`；
- 同级后续 step：默认不重复发事件；若需要心跳则 `hint_present`，但 UI 必须按 `pressure_id` upsert；
- soft→hard：相同 `pressure_id`，`updated/level=hard/outcome=hint_upgraded`；
- 后续整轮压缩成功：`finished/outcome=compacted`；
- 模型正常结束当前轮：`finished/outcome=turn_converged`；
- provider context overflow 在现有一次强制压缩机会后仍失败：`finished/outcome=model_overflow_failed, ok=False`。

用户展示：

```text
Context pressure: converging current turn (soft|hard)
```

文案必须表明“已要求模型收敛”，不得显示成“已压缩”。

## 与现有机制的关系

| 机制 | 现状 | 本方案关系 |
|---|---|---|
| `select_details` / `select_preflight_details` | 轮次不足时 `none` | 保持；只消费 semantic messages |
| `compact_for_live_state` | `should_compact=False` 返回 `None` | 继续只负责整轮压缩；不注入 hint |
| `llm_turn` | 编译上下文后仅 hard 尝试压缩 | 扩展为每步 soft/hard pressure 判定、注入和 state delta 提交 |
| `turn_runner` preflight | turn 开始前 soft 压缩 | 保持；不新增第二条 pressure 注入路径 |
| `replacement_messages` | 未压缩时只返回 assistant | 扩展为合并 pending pressure delta 的统一返回 helper |
| `inline_compaction_guide` | 默认关闭；依赖可压缩 head | 不作为本修复主路径，也不得进入 `Long Summary` |
| Layer1 `prune_messages` | 可裁旧 tool output | 本阶段不启用为 hard 兜底 |
| `Long Summary` | 压缩后注入 runtime system section | 必须排除所有 pressure/step hint，只总结 removed historical head |

## API 形状（实现冻结）

### `evaluate_context_pressure`

```python
def evaluate_context_pressure(
    semantic_messages: list[BaseMessage],
    llm_context_tokens: int,
    *,
    compaction_service: CompactionService,
) -> ContextPressureDecision: ...
```

`ContextPressureDecision` 至少包含：

```text
over_soft: bool
over_hard: bool
can_compact: bool
pressure_level: "none" | "soft" | "hard"
should_inject: bool
turn_id: str
turn_count: int
pre_tokens: int
soft_threshold: int
hard_threshold: int
reason: "soft_threshold" | "hard_threshold" | ""
```

要求：

- 不发 LLM，不修改 messages；
- token threshold 只读取 `llm_context_tokens`；
- turn selection 只读取 `semantic_messages`；
- `turn_id` 指向当前真实 user turn，跳过 step hint/guidance；
- soft/hard 判定复用 `is_soft_overflow` / `is_overflow`；
- `can_compact` 与 coordinator live selection 规则一致；若为了避免漂移抽 shared selection helper，coordinator 和 pressure 必须共同使用。

### `upsert_context_pressure_hint`

```python
@dataclass(frozen=True)
class ContextPressureHintUpdate:
    state_messages: list[BaseMessage]
    message_delta: list[BaseMessage | RemoveMessage]
    pressure_id: str
    outcome: Literal["none", "hint_injected", "hint_present", "hint_upgraded"]


def upsert_context_pressure_hint(
    state_messages: list[BaseMessage],
    decision: ContextPressureDecision,
) -> ContextPressureHintUpdate: ...
```

要求：

- 输入/输出 list 不修改原 state list；
- 只识别 `CONTEXT_PRESSURE_MARKER`，不误删普通 convergence hint；
- 使用 `voidx:context-pressure:<turn-id>` 稳定 id；
- 同级返回空 delta，soft→hard 返回同 id replacement；
- `state_messages` 与本次 rebuild LLM context 使用升级后的消息；
- `message_delta` 只用于 graph node return。

### `messages_for_return`

```python
def messages_for_return(
    assistant_or_control_messages: list[BaseMessage],
    *,
    pending_pressure_delta: list[BaseMessage | RemoveMessage],
    compaction_happened: bool,
    state_messages: list[BaseMessage],
) -> list[BaseMessage]: ...
```

规则：

- 未压缩：`pending_pressure_delta + assistant_or_control_messages`；
- 已压缩：现有 full replacement 为权威结果，不重复追加已包含在 `state_messages` 的 pressure delta；
- malformed response、turn control fail、provider failure、正常结束等所有 `llm_turn` 返回路径都调用同一 helper。

### 调用顺序（`llm_turn`）

```text
1. state_messages = copy(state.messages)
2. rebuild compiled llm_messages with runtime context/tools
3. semantic_messages = raw_semantic_messages(state_messages)
4. decision = evaluate_context_pressure(semantic_messages, actual llm context tokens)
5. if decision.can_compact and existing compaction trigger matched:
     run whole-turn compaction
     rebuild messages/tokens
6. elif decision.should_inject:
     update = upsert_context_pressure_hint(state_messages, decision)
     state_messages = update.state_messages
     pending_pressure_delta = update.message_delta
     rebuild llm_messages so current model call sees the hint
     emit ContextPressureUpdated when injected/upgraded
7. call model through existing retry path
8. provider context overflow:
     retain current hint; perform existing one forced-compaction attempt
     if compaction still impossible/failure becomes terminal:
       emit ContextPressureFinished(model_overflow_failed)
       return through messages_for_return so pending delta persists
9. normal terminal response:
     emit ContextPressureFinished(turn_converged)
     return through messages_for_return
```

重新 build 后的 hint token 会略增 context estimate；可更新 usage/context frame，但不得因为 hint 自身造成循环重复注入。

## 失败模式与后续增强

### 本阶段接受的风险

1. 模型忽略提示，本轮继续膨胀。
2. 单次 tool 输出过大，提示来不及阻止。
3. hard 时 tokens 不会因提示而下降。
4. 单轮结束后进入双轮，仍可能 `can_compact=False`，需要继续 pressure 或再积累轮次。

### 后续增强（非本设计必做）

1. **Hard prune 兜底**（仍不切半轮语义）：复用 Layer1 `prune_messages`。
2. **工具结果入链前限流**：超大 tool output 写入前截断。
3. **双轮放宽**：允许压缩更早完整轮，同时禁止切断当前轮中间。

## 验收标准

1. **整轮语义不变**：有完整旧轮可压时走现有 whole-turn compaction，不切当前轮中间。
2. **真实 token 判定**：soft/hard 使用编译后的 LLM context + tools token estimate；turn selection 不读取 system/runtime overlay。
3. **单轮 soft/hard**：`can_compact=False` 时当前模型调用能看到相应 hint，且 graph node return 含显式 state delta。
4. **稳定去重**：同一 turn 的 soft/hard 共用稳定 id；连续 soft 不堆叠；soft→hard 在 reducer 后只剩 hard。
5. **跨 graph step**：第一步注入后，第二步从 `AgentState.messages` 能读到同一 hint，而不是只存在于前一步本地 list。
6. **所有返回路径**：正常结束、malformed tool-call failure、turn control fail、provider failure 均不会丢失本次 pending hint delta。
7. **Hard provider failure**：保留现有一次 overflow compaction 机会；仍无法压缩并失败时发送 `ContextPressureFinished(outcome="model_overflow_failed", ok=False)`，不生成压缩成功态。
8. **新用户轮**：新 turn id 产生新的 pressure id；旧 hint 不影响当前 level，也不造成虚假 user turn。
9. **可压缩优先**：同一步可整轮压缩时不注入 converge hint；压缩完成事件可结束已有 pressure item。
10. **类型化可观测性**：pressure 字段通过 `ContextPressureUpdated/Finished` 穿过 event union、TUI consumer、gateway adapter 和 frontend，不依赖解析 detail 文本。
11. **Long Summary 隔离**：pressure hint、普通 step hint、guidance、runtime context 和 inline guide 均不进入 summary LLM/fallback/`## Long Summary`。
12. **双轮现实**：下一轮若仍 `can_compact=False`，继续 pressure 路径，不声称必然已可压缩。

## 测试计划与命令

### Pressure 单元

建议新增：

- `src/tests/test_llm/compaction/test_context_pressure.py`
  - semantic messages 与 `llm_context_tokens` 分离；
  - 单/双轮 soft、hard，三轮 can compact；
  - runtime/system sentinel 不改变 turn count；
  - hard 优先于 soft；
  - current real turn id 稳定。
- `src/tests/test_infrastructure/runtime/test_context_pressure_hint.py`
  - 首次 soft/hard delta；
  - 同级空 delta；
  - soft→hard 同 id replacement；
  - hard 不降级；
  - 不误识别普通 `STEP_HINT_MARKER`；
  - `add_messages` reducer 后只保留一条当前 pressure hint；
  - 新 user turn 得到新 pressure id。

### Runtime 多步 graph

扩展 `src/tests/test_infrastructure/runtime/test_call_llm_compaction.py` 或新增
`test_call_llm_context_pressure.py`：

1. step 1 达 soft，模型收到 soft hint，node return 含 hint + assistant；
2. reducer 应用后 step 2 仍能从 state 读到该 hint；
3. step 2 达 hard，同 id 更新，模型只收到一条当前 hard hint；
4. normal terminal return 后 state 中只有一条当前 hard hint；
5. malformed/control failure 与 terminal provider overflow 分别验证 pending delta 不丢；
6. hard + can_compact 验证 full replacement 优先且无额外 pressure delta。

### 事件、gateway 与 frontend

- `src/tests/test_presentation/gateway/test_ui_events_dock_status.py`：TUI pressure updated/finished/upgraded；
- `src/tests/test_presentation/gateway/test_adapter.py`：类型化事件映射为同一 `status` item id，data 保留所有字段；
- `frontend/test/ui/workbench.test.ts`：soft→hard upsert、completed/failure 展示；
- schema 生成：

```bash
cd frontend && npm run schema
```

不得手写 `frontend/src/rpc/protocol.d.ts`；由 Python protocol schema 生成。

### Long Summary 隔离

与 `docs/design/compaction-summary-model.md` 共用
`src/tests/test_llm/compaction/test_compaction_summary_input.py`：构造 pressure、generic step hint、guidance、runtime、inline guide 和 tail sentinel，断言都不进入 summary 输入或 fallback。

### 验证命令

```bash
./test.py --backend -- \
  src/tests/test_llm/compaction/test_context_pressure.py \
  src/tests/test_infrastructure/runtime/test_context_pressure_hint.py \
  src/tests/test_infrastructure/runtime/test_call_llm_context_pressure.py \
  src/tests/test_presentation/gateway/test_ui_events_dock_status.py \
  src/tests/test_presentation/gateway/test_adapter.py -v

./test.py --frontend -- test/ui/workbench.test.ts

./test.py --backend -- \
  src/tests/test_infrastructure/runtime/test_call_llm_compaction.py \
  src/tests/test_infrastructure/runtime/test_compaction_flow.py \
  src/tests/test_llm/compaction/ -v
```

最终运行：

```bash
./test.py --backend
./test.py --frontend
```

## 涉及文件

| 文件 | 责任 |
|---|---|
| `src/voidx/llm/message_markers.py` | `CONTEXT_PRESSURE_MARKER` 与识别 helper |
| `src/voidx/llm/compaction/service.py` | overflow/selection；与 coordinator 共享 can-compact 规则 |
| `src/voidx/agent/adapters/langgraph/runtime/context_pressure.py` | decision、hint render、stable id、upsert delta |
| `src/voidx/agent/adapters/langgraph/runtime/llm_turn.py` | 唯一注入 owner、rebuild、failure/finish events、所有返回路径接线 |
| `src/voidx/agent/adapters/langgraph/runtime/core/context.py` | `messages_for_return` 合并 pressure delta 与 compaction replacement |
| `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py` | 保持 preflight，只验证不注入第二份 hint |
| `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` | 只整轮压缩；过滤 summary 输入；pressure compacted finish 协调（如需要） |
| `src/voidx/agent/adapters/langgraph/ui_events.py` | 新增 pressure updated/finished 类型并加入 `UiEvent` union |
| `src/voidx/presentation/output/events/consumers.py` | TUI status upsert/finish |
| `src/voidx/presentation/gateway/adapter.py` | pressure event → v2 status item mapping及稳定 item correlation |
| `src/voidx/presentation/protocol/v2/threads.py` | 若继续映射为 `kind="status"` 则无需扩 kind；确认 schema data 可承载字段 |
| `frontend/src/utils/render-types.ts` | pressure status data 字段类型 |
| `frontend/src/utils/render-notice-status.ts` | soft→hard/upshot 展示 |
| `frontend/src/rpc/protocol.schema.json` | 由 export script 生成 |
| `frontend/src/rpc/protocol.d.ts` | `npm run schema` 生成，不手改 |
| `src/tests/...`、`frontend/test/...` | pressure、state reducer、gateway、UI、summary isolation |

## 实现顺序

1. `CONTEXT_PRESSURE_MARKER`、decision 和 stable-id upsert helper，先写 reducer 单测。
2. `messages_for_return` 及所有 llm node return path 单测。
3. `llm_turn` 接入实际 LLM token 判定、注入后 rebuild 和 multi-step graph 测试。
4. provider overflow terminal outcome 与 normal convergence finish event。
5. 新增类型化 UI 事件、TUI consumer、gateway mapping 和 frontend rendering。
6. 实现并复用 `Long Summary` historical-only 过滤。
7. 运行 focused backend/frontend tests、schema generation 和完整回归。

## 决策记录

| 决策 | 选择 | 原因 |
|---|---|---|
| 是否切断单轮中间 | 否 | 用户明确要求只整轮压缩 |
| 主修复手段 | 上下文压力收敛提示 | 与整轮边界兼容并能主动止涨 |
| token 输入 | 编译后真实 LLM context tokens | system/runtime/tools 也是 provider 请求成本 |
| turn 输入 | 清洗后的 semantic messages | 防止 runtime overlay 污染 turn selection |
| 注入 owner | 仅 `llm_turn` | 单一 LangGraph state 提交语义 |
| state 提交 | 稳定 message id + explicit node delta | 本地 list 修改不会自动写回 reducer state |
| soft→hard | 同 id replacement | reducer 天然去重，无需 append+delete |
| 新轮 | 新 turn id；旧 hint inert | 不需每轮批量删除历史 synthetic 消息 |
| hard 失败 | 保留现有 retry/fail + typed failure event | hint 不能冒充压缩成功 |
| 可观测性 | 新增类型化 pressure events | 现有 status/turn metadata 无法承载机器字段 |
| coordinator | 只负责 whole-turn compaction | 避免双注入 |
| Long Summary | 排除所有 pressure/control injections | summary 只表示 removed historical turns |
| hard prune | 本阶段否 | 保持现有完整轮次约束；后续增强 |
| 下一轮一定可压 | 不承诺 | 双轮仍可能 selection=none |

## 开放问题

无实现前必须再决策的开放问题。
若实现中发现 marker 展示层细节不足，可在不改变上述冻结决策的前提下微调 UI 映射。
