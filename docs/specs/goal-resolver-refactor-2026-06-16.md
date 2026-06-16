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
HumanMessage: "Assistant: AI 上一轮文本回复"（AIMessage 转为 HumanMessage，仅文本）
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

### 消息构建规则

从 `task_state.recent_exchanges` 构建 goal resolver 的消息列表：

1. 每个 `TurnExchange` 生成两条消息：
   - `HumanMessage(content=exchange.user_text)`
   - `HumanMessage(content=f"Assistant: {exchange.assistant_text}")`（AIMessage 转为 HumanMessage，因为 `with_structured_output` 要求最后一条是 HumanMessage）
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
            messages.append(HumanMessage(content=f"Assistant: {ex.assistant_text}"))
    messages.append(HumanMessage(content=user_text))
    return messages
```

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
- 移除 "Do not choose brainstorm when..." 等防御性规则（由 `_normalize_resolution` 处理）
- 保留核心规则

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

### Fallback 行为

当 `recent_exchanges` 为空时（新会话第一条消息），goal resolver 只看到当前用户输入，行为与当前一致——仅凭用户文本判断意图。

### `recent_user_texts` 的去留

`recent_user_texts` 当前用于两个地方：
1. goal resolver 的 JSON context → 被 `recent_exchanges` 替代
2. `intent_window_text()` 用于 intent classifier → 保留

`recent_user_texts` 字段保留，但 goal resolver 不再使用它。

## 上下文大小对比

| | 当前 | 重构后 |
|---|---|---|
| SystemMessage | ~2937 chars（含 schema） | ~800 chars（不含 schema） |
| 对话历史 | 无（只有 JSON context ~276 chars） | 最近 3 轮消息对（~300-1500 chars） |
| 当前用户输入 | 包含在 JSON context 中 | 独立 HumanMessage |
| **总计** | ~3213 chars | ~1100-2300 chars |

重构后上下文更短，但信息更相关——LLM 能看到对话历史，对模糊输入的判断更准确。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/voidx/runtime/task_state.py` | 新增 `TurnExchange` 模型；`TaskState` 新增 `recent_exchanges` 字段 |
| `src/voidx/agent/goal_resolver.py` | 新增 `_resolver_messages_from_exchanges()`、`_resolver_system_prompt()`；修改 `resolve_goal_for_turn()` 签名（移除 workspace/session_time）；保留旧 `_resolver_messages()` 作为 fallback |
| `src/voidx/agent/graph/turn_runner.py` | 一轮结束后记录 `TurnExchange` 到 `task_state.recent_exchanges`；移除传给 `resolve_goal_for_turn()` 的 workspace/session_time 参数 |
