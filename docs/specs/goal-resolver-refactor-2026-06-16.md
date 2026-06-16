# Goal Resolver 重构方案

> 日期: 2026-06-16
> 状态: 讨论确认，待实施

## 背景

`resolve_goal_for_turn()` 在每轮对话开始前调用一次 LLM，解析用户意图并确定 workflow 入口。当前实现存在以下问题：

1. **上下文冗余**：SystemMessage 包含完整的 JSON schema（~2000 chars）和冗长的规则说明，总上下文 ~3200 chars，对于只返回一个简单结构化对象的调用来说过重
2. **上下文信息不足**：HumanMessage 只包含当前用户文本和 recent_user_texts，缺少对话历史中 AI 的回复——这些信息对判断用户意图至关重要（比如用户说"继续"，需要看上一轮 AI 做了什么才能判断 join 到哪个 workflow）
3. **与主对话上下文割裂**：goal resolver 构建独立的 JSON context，不利用主对话已有的消息列表

## 当前实现

### 调用链

```
turn_runner.run_once()
  → resolve_goal_for_turn(model, user_text, interaction_mode, task_state, workspace, session_time)
    → _resolver_messages(user_text, interaction_mode, task_state, workspace, session_time)
      → [SystemMessage(规则 + schema), HumanMessage(context JSON)]
    → model.with_structured_output(GoalResolution).ainvoke(messages)
    → _normalize_resolution(raw, user_text, interaction_mode, task_state)
```

### 当前上下文

```
SystemMessage (~2937 chars):
  - 角色说明
  - 12 条规则
  - 7 个 join 值说明
  - 完整 GoalResolution JSON schema

HumanMessage (~276 chars):
  {
    "workspace": "D:\\chikham\\voidx",
    "session_time": "2026-06-16 中国标准时间",
    "interaction_mode": "auto",
    "current_intent": "coding",
    "current_goal": {"type": "bugfix", "desc": "fix login bug"},
    "recent_user_texts": ["上一条用户输入"],
    "latest_user_text": "当前用户输入"
  }
```

### 问题分析

| 问题 | 说明 |
|------|------|
| Schema 冗余 | `with_structured_output` 已将 schema 传给 LLM API，SystemMessage 中再写一遍是重复 |
| 规则过长 | 12 条规则 + 7 个 join 说明占 ~1500 chars，可精简 |
| 缺少 AI 回复 | 用户说"继续改"时，需要看上一轮 AI 做了什么才能判断 join 值，但当前只有用户文本 |
| workspace/session_time 对分类无用 | 意图分类不需要知道工作目录和时间 |
| 实时提取复杂 | 从 messages 列表实时提取、过滤、精简逻辑复杂且脆弱 |

## 重构方案

### 核心思路

goal resolver 的上下文应该是**预记录的对话轮次消息对**——每轮结束时记录 `(HumanMessage, AIMessage最终回复)` 对，goal resolver 直接使用这些预记录的消息对，无需实时从 messages 列表中提取和过滤。

### 重构后上下文

```
SystemMessage (~800 chars):
  固定精简提示词（不含 schema，不含详细规则）

HumanMessage: 用户上一条输入
AIMessage: AI 上一轮文本回复
HumanMessage: 用户当前输入
```

### 对话轮次消息对（TurnExchange）

在 `TaskState` 中新增 `recent_exchanges` 字段，每轮结束时记录一个消息对：

```python
class TurnExchange(BaseModel):
    """一轮对话的精简摘要：用户输入 + AI 最终文本回复。"""
    user_text: str
    assistant_text: str = ""
```

```python
class TaskState(BaseModel):
    # ... 现有字段 ...
    recent_exchanges: list[TurnExchange] = Field(default_factory=list)
```

**记录时机**：`turn_runner.run_once()` 中，一轮结束后（graph 执行完毕、消息持久化之后），从 `final["messages"]` 中提取 AI 最终文本回复，构建 `TurnExchange` 并追加到 `task_state.recent_exchanges`。

**窗口大小**：保留最近 3 个 `TurnExchange`（约 3 轮对话），超出时裁剪。

### 消息对记录实现

在 `turn_runner.py` 的 `run_once()` 末尾，一轮结束后：

```python
# 提取 AI 最终文本回复
last_ai = latest_ai_message(final["messages"])
assistant_text = extract_text(last_ai).strip() if last_ai else ""

# 截断：取最后部分，上限 500 chars
ASSISTANT_TEXT_MAX_CHARS = 500
if len(assistant_text) > ASSISTANT_TEXT_MAX_CHARS:
    assistant_text = "..." + assistant_text[-(ASSISTANT_TEXT_MAX_CHARS - 3):]

# 记录本轮消息对
exchange = TurnExchange(
    user_text=payload.title_text,
    assistant_text=assistant_text,
)
final_task_state.recent_exchanges = [
    *final_task_state.recent_exchanges,
    exchange,
][-3:]  # 保留最近 3 轮
```

### recent_exchanges 持久化策略

`recent_exchanges` **不需要持久化到数据库**。理由：

1. **同一 session 连续运行时**：`recent_exchanges` 在内存中通过 `run_once()` 末尾的记录逻辑自然累积，无需持久化
2. **跨 session resume 时**：`run_once()` 在调用 `resolve_goal_for_turn()` 之前已经加载了 `session_msgs`（`list[MessageRow]`），可从中重建

**重建逻辑**：在 `run_once()` 中，`session_msgs` 加载完成后、`resolve_goal_for_turn()` 调用前，如果 `task_state.recent_exchanges` 为空且 `session_msgs` 非空，从 `session_msgs` 末尾提取最近 3 组 `(user, assistant)` 消息对初始化：

```python
def _rebuild_exchanges_from_session_msgs(
    session_msgs: list[MessageRow],
    max_exchanges: int = 3,
) -> list[TurnExchange]:
    """从持久化消息中重建最近对话轮次消息对。"""
    exchanges: list[TurnExchange] = []
    # 从末尾倒序提取 user/assistant 对
    i = len(session_msgs) - 1
    while i >= 0 and len(exchanges) < max_exchanges:
        # 找到一条 assistant 消息
        if session_msgs[i].role != "assistant":
            i -= 1
            continue
        assistant_text = session_msgs[i].content.strip()
        # 截断
        if len(assistant_text) > ASSISTANT_TEXT_MAX_CHARS:
            assistant_text = "..." + assistant_text[-(ASSISTANT_TEXT_MAX_CHARS - 3):]
        # 向前找最近的 user 消息
        j = i - 1
        while j >= 0 and session_msgs[j].role != "user":
            j -= 1
        if j < 0:
            break
        user_text = session_msgs[j].content.strip()
        exchanges.append(TurnExchange(user_text=user_text, assistant_text=assistant_text))
        i = j - 1
    exchanges.reverse()
    return exchanges
```

**重建时机**：在 `turn_runner.py` 的 `run_once()` 中，`base_task_state` 加载后、`resolve_goal_for_turn()` 调用前：

```python
base_task_state = _load_task_state(getattr(host, "_task_state", None))
if not base_task_state.recent_exchanges and session_msgs:
    base_task_state.recent_exchanges = _rebuild_exchanges_from_session_msgs(session_msgs)
```

**边界情况**：
- compaction 后 `session_msgs` 中较早的消息可能已被裁剪，重建只能获取到压缩后保留的部分——这可接受，fallback 行为与当前实现一致
- 新会话第一条消息时 `session_msgs` 为空，`recent_exchanges` 也为空，goal resolver 只凭当前用户文本判断

### 消息构建规则

从 `task_state.recent_exchanges` 构建 goal resolver 的消息列表：

1. 每个 `TurnExchange` 生成两条消息：
   - `HumanMessage(content=exchange.user_text)`
   - `AIMessage(content=exchange.assistant_text)`（保留 Human/AI 对话格式，LLM 天然理解对话结构）
2. 最后追加当前用户输入：`HumanMessage(content=user_text)`
3. `assistant_text` 为空时跳过该条消息（AI 可能只调了工具没有文本回复）

```python
def _resolver_messages_from_exchanges(
    user_text: str,
    task_state: TaskState,
) -> list:
    system = _resolver_system_prompt(task_state)
    messages = [SystemMessage(content=system)]
    for ex in task_state.recent_exchanges:
        messages.append(HumanMessage(content=ex.user_text))
        if ex.assistant_text:
            messages.append(AIMessage(content=ex.assistant_text))
    messages.append(HumanMessage(content=user_text))
    return messages
```

> **对话格式选择**：使用 `HumanMessage` + `AIMessage` 的自然对话格式，而非将 AI 回复包装为 `HumanMessage(content=f"Assistant: ...")`。LLM 对 Human/AI 交替对话格式的理解远优于单角色前缀标注，且避免了语义混淆——LLM 不会将 "Assistant:" 前缀的文本误解为用户引用。`with_structured_output` 要求最后一条是 HumanMessage，当前用户输入自然满足此约束。

### AI 回复截断策略

AI 回复可能极长（包含代码块、diff、工具调用结果时可达数千字符），直接传入 goal resolver 会导致上下文膨胀。截断策略：

- **截断方向**：取文本的**最后部分**（尾部），因为 AI 回复的结尾通常包含结论、总结或下一步行动，对意图判断最有价值
- **长度上限**：`ASSISTANT_TEXT_MAX_CHARS = 500`
- **截断标记**：超出时在开头加 `"..."` 表示已截断
- **截断时机**：在记录 `TurnExchange` 时截断（`run_once()` 末尾），以及从 `session_msgs` 重建时截断

### 精简提示词

```
You are resolving the user's intent and goal for this turn.
Return structured data matching the GoalResolution schema.

Rules:
- intent.type=general only for non-code, non-workspace conversation.
- intent.type=coding for codebase inspection, design, docs, review, debugging, or edits.
- Pick exactly one goal.type when intent is coding and a concrete workspace goal exists.
- plan.join is the workflow node to enter. Required when goal is set; null when goal is null.
- plan.leave is the workflow node after which auto-progression stops. Optional.
- Available join values: brainstorm, debug, design-doc, feedback, plan, review, tdd.
- If intent does not clearly match any join value, set goal=null and plan=null.
- goal and plan are bound: if goal is set, plan.join must be set; if goal is null, plan must be null.
```

相比当前提示词的改动：
- 移除 JSON schema（`with_structured_output` 已传）
- 移除 7 个 join 值的详细说明（LLM 从 join 值名称即可推断语义）
- 移除 "Do not choose brainstorm when..." 等防御性规则（由 `_normalize_resolution` 接管，见下文）
- 保留核心规则

### 防御性规则迁移

当前提示词中的防御性规则移除后，由 `_normalize_resolution()` 接管其语义约束：

| 原提示词规则 | 迁移到 `_normalize_resolution` 的逻辑 |
|---|---|
| "Do not choose brainstorm when the request already contains an approved or sufficiently detailed spec." | 当 `goal.type` 为 feature/refactor/chore 且用户文本包含实施关键词（如"实现"、"implement"、代码路径等）时，将 `join=brainstorm` 修正为 `join=plan` 或 `join=tdd` |
| "Do not set join or leave based on vague or ambiguous approval." | 当用户文本为模糊续接词（如"继续"、"ok"、"好的"）且 `current_goal` 已设置时，保持当前 workflow 路由不变，不因模糊输入切换 join |
| 7 个 join 值的详细语义说明 | `_ALLOWED_JOIN_NODES` 校验已覆盖无效 join 值；join 值名称本身具有足够语义（debug/plan/review 等），LLM 可从名称推断 |

**实现方式**：在 `_normalize_resolution()` 中新增以下逻辑：

```python
# 1. 已有明确实施意图时，brainstorm 降级为 plan/tdd
if plan is not None and plan.join == "brainstorm" and resolution.goal is not None:
    if _has_implementation_signals(user_text):
        plan = PlanResolution(
            join=_default_join_for_goal_type(resolution.goal.type),
            leave=plan.leave,
        )

# 2. 模糊续接时保持当前路由
if plan is not None and _is_vague_continuation(user_text) and task_state.current_goal is not None:
    current_join = _current_active_join(task_state)
    if current_join:
        plan = PlanResolution(join=current_join, leave=plan.leave)
```

其中 `_has_implementation_signals()` 和 `_is_vague_continuation()` 为新增的文本分类辅助函数，基于关键词匹配（与现有 `infer_goal_type()` 风格一致）。

### 上下文状态传递

当 `current_goal` 已设置且用户输入模糊（如"继续"），LLM 需要知道当前 goal 才能正确判断。在 SystemMessage 末尾追加当前状态摘要：

```
Current state:
- intent: coding
- goal: bugfix — fix login bug
- active workflows: debug
```

仅在 `current_goal` 不为 None 时追加，否则省略。

### API 变更

`resolve_goal_for_turn()` 签名简化：

```python
async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    # 移除: workspace, session_time（不再传给 LLM）
) -> GoalResolution:
```

`task_state.recent_exchanges` 替代了原来的 `workspace`、`session_time`、`recent_user_texts` 参数，所有上下文信息都通过 `task_state` 传递。

### 调用方变更

`resolve_goal_for_turn()` 签名变更后，所有调用方需同步更新：

| 调用方 | 文件 | 改动 |
|--------|------|------|
| `turn_runner.py` | `src/voidx/agent/graph/turn_runner.py:182` | 移除 `workspace` 和 `session_time` 参数 |
| 测试 | `tests/test_agent/test_goal_resolver.py`（9 处调用） | 移除 `workspace` 和 `session_time` 参数；更新消息断言（不再检查 schema/旧规则） |

### Fallback 行为

当 `recent_exchanges` 为空时（新会话第一条消息），goal resolver 只看到当前用户输入，行为与当前一致——仅凭用户文本判断意图。

### assistant_text 提取的边界情况

`latest_ai_message()` + `extract_text()` 提取 AI 最终回复存在已知边界情况：

- 多轮工具调用后，最后一条 AIMessage 可能是中间结果而非最终文本回复
- `extract_text()` 对 structured content 只提取 `type=text` 的部分，可能丢失上下文
- compaction 后消息列表被裁剪，`final["messages"]` 中的最后一条 AI 消息可能不是用户感知的"最终回复"

**决策**：先接受这些边界情况，不做额外处理。实际使用中，大多数轮次的最后一条 AIMessage 就是最终文本回复，边界情况的影响有限。

### recent_user_texts 的去留

**决策：移除 `recent_user_texts` 字段**。

当前使用方：
1. goal resolver 的 JSON context → 被 `recent_exchanges` 完全替代
2. `intent_window_text()` 用于 intent classifier → 改为从 `recent_exchanges` 中提取 `user_text` 构建

移除后的影响：
- `TaskState` 移除 `recent_user_texts` 字段
- `TaskState._record_user_text()` 方法移除
- `TaskState.intent_window_text()` 改为从 `recent_exchanges` 构建
- `runtime_state.py` 移除 `recent_user_texts_json` 的持久化/加载逻辑
- `store.py` 移除 `recent_user_texts_json` 数据库列（需 migration）
- `runtime_context.py` 移除 `recent_user_texts` 的传递
- 相关测试更新

## 上下文大小对比

| | 当前 | 重构后 |
|---|---|---|
| SystemMessage | ~2937 chars（含 schema） | ~800 chars（不含 schema） |
| 对话历史 | 无（只有 JSON context ~276 chars） | 最近 3 轮消息对（~300-1500 chars，AI 回复截断至 500 chars） |
| 当前用户输入 | 包含在 JSON context 中 | 独立 HumanMessage |
| **总计** | ~3213 chars | ~1100-2300 chars |

重构后上下文更短，但信息更相关——LLM 能看到对话历史，对模糊输入的判断更准确。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/voidx/runtime/task_state.py` | 新增 `TurnExchange` 模型；`TaskState` 新增 `recent_exchanges` 字段；移除 `recent_user_texts` 字段及 `_record_user_text()`；修改 `intent_window_text()` |
| `src/voidx/agent/goal_resolver.py` | 新增 `_resolver_messages_from_exchanges()`、`_resolver_system_prompt()`；修改 `resolve_goal_for_turn()` 签名（移除 workspace/session_time）；移除旧 `_resolver_messages()`；`_normalize_resolution()` 新增防御性规则迁移逻辑 |
| `src/voidx/agent/graph/turn_runner.py` | 一轮结束后记录 `TurnExchange` 到 `task_state.recent_exchanges`；新增 `_rebuild_exchanges_from_session_msgs()` 重建逻辑；移除传给 `resolve_goal_for_turn()` 的 workspace/session_time 参数 |
| `src/voidx/memory/runtime_state.py` | 移除 `recent_user_texts_json` 的持久化/加载逻辑 |
| `src/voidx/memory/store.py` | 移除 `recent_user_texts_json` 数据库列（需 migration） |
| `src/voidx/agent/runtime_context.py` | 移除 `recent_user_texts` 的传递 |
| `tests/test_agent/test_goal_resolver.py` | 移除 workspace/session_time 参数；更新消息断言 |
| `tests/test_agent/test_task_state.py` | 更新 `recent_user_texts` 相关测试为 `recent_exchanges` |
| `tests/test_agent/test_runtime_context.py` | 更新 `recent_user_texts` 相关断言 |
| `tests/test_agent/test_session.py` | 更新 `recent_user_texts` 相关断言 |
