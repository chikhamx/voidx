# turn(start) — LLM 主动声明 goal，移除 resolver goal LLM 调用

## Context

当前 `turn` 工具只有一个 `stop` 操作，是 turn 结束信号。turn 开始时，runtime 通过 `resolve_goal_for_turn`（`goal_resolver.py`）发起一次**独立 LLM 调用**来解析 intent + goal + workflow 路由。这带来：

- 额外一次 LLM 调用（延迟 + token 成本）
- goal 解析和实际工作分离——resolver LLM 和主 LLM 看到的上下文不同，可能解析出不匹配的 goal
- workflow 路由由 resolver 决定，但 workflow 本来就是主 LLM 的决策领域

**目标**：让主 LLM 通过 `turn(operation="start")` 自己声明 intent + goal，runtime 返回 workflow 状态提示，由主 LLM 决定是否进入/保持 workflow。移除 `resolve_goal_for_turn` 的 LLM 调用路径。

## 设计

### 1. turn 工具定义变更

**文件**: `src/voidx/agent/graph/turn_control.py`

`TURN_TOOL_DEFINITION` 的 parameters 从 `{decision}` 改为 `{operation, intent, goal}`：

```python
TURN_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "turn",
        "description": (
            "Commit the pending assistant response and end the current user turn. "
            "Call with operation='stop' only when the pending response is complete. "
            "Call with operation='start' at the beginning of a turn to declare intent and goal. "
            "If more work is needed, call another available tool instead. Do not output text with this call."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["start", "stop"],
                    "description": "start: declare turn objective. stop: commit pending response.",
                },
                "intent": {
                    "type": "string",
                    "enum": ["coding", "general", ""],
                    "description": "For start: coding=code task, general=chat/Q&A. For stop: pass empty string.",
                },
                "goal": {
                    "type": "string",
                    "description": "For start: stable objective, short and clear. For stop: pass empty string.",
                },
            },
            "required": ["operation", "intent", "goal"],
            "additionalProperties": False,
        },
    },
}
```

**start 校验**：runtime 校验 `start` 调用时 `intent` 必须是 `coding` / `general`，且 `goal` 必须非空；否则视为 `INVALID_TURN`，走修复路径。

**stop 参数约定**：`strict: True` 要求所有 properties 都在 required 里，所以 `intent` 和 `goal` 也必须列入 required。LLM 调 `stop` 时必须传 `operation="stop", intent="", goal=""`；runtime 不校验这两个字段的非空性，只按 `operation="stop"` 结束 turn。

### 2. TurnClassification 扩展

**文件**: `src/voidx/agent/graph/turn_control.py`

`classify_turn_call` 需要识别 `start` 操作：

```python
class TurnClassification(str, Enum):
    VALID_TURN = "valid_turn"          # turn(stop)
    VALID_START = "valid_start"        # turn(start) with intent+goal
    REGULAR_TOOLS = "regular_tools"
    INVALID_TURN = "invalid_turn"
    PLAIN_TEXT = "plain_text"
```

分类逻辑：
- `turn(start)` + `intent in {"coding", "general"}` + `goal` 非空 → `VALID_START`
- `turn(start)` 缺少 `intent`、`intent` 为空/非法，或 `goal` 为空 → `INVALID_TURN`
- `turn(stop)` → `VALID_TURN`；`intent` / `goal` 因 strict schema 仍需存在，但 runtime 忽略其值
- 其他不变

### 3. START_PROMPT 提示词

**文件**: `src/voidx/agent/graph/turn_control.py`

```python
START_PROMPT = (
    "You forgot to call turn with operation='start' to declare this turn's intent and goal. "
    "Please call turn with operation='start', intent, and goal now."
)
```

**注入规则**：只在第一轮 LLM 响应为 `PLAIN_TEXT` 且未调 `turn(start)` 时注入 `START_PROMPT` 作为 guidance HumanMessage。不要在 `REGULAR_TOOLS` 分支注入，因为 tool_call 后必须先跟对应的 ToolMessage；regular tool calls 继续走执行路径，本轮 goal 使用 fallback。此后不再注入。

### 4. turn(start) 的 runtime 处理

**文件**: `src/voidx/agent/graph/core/llm.py` — `_call_llm` 的 turn_control 拦截逻辑

当 `classification == VALID_START` 时：

1. 从 tool_call args 解析 `intent` + `goal`
2. 构造 `GoalResolution`（复用 `ResolverGoal` → `GoalResolution` 转换逻辑，但**不走 LLM 调用**）：
   ```python
   resolution = GoalResolution(
       intent=IntentResolution(type=TaskIntent.CODING if intent == "coding" else TaskIntent.GENERAL),
       goal=GoalSpec(desc=goal),
       plan=None,  # 不自动 join workflow
   )
   ```
3. 调用 `task_state.update_after_turn(resolution, user_text)` + `reconcile_workflow_runs_for_turn(...)`
4. 更新 `runtime_task_state` + `self._task_state`
5. 设置 `turn_state = "running"`（AgentState 字段）
6. 生成内部 tool result 文本（不是 guidance HumanMessage）：
   ```
   Goal accepted: <goal>. Intent: <coding/general>. Next: consider whether to enter or maintain a workflow (brainstorm/plan/tdd/...) to work on this, or proceed directly.
   ```
   - 不替 LLM 做决定，只提示它考量
   - 简短有力
7. 构造 `ToolMessage(content=<tool result>, tool_call_id=<turn_start_call.id>, name="turn")`
8. 将 `turn(start)` 的 AIMessage + 上述 ToolMessage 追加到 `llm_messages`，然后 `continue`。这两条消息只作为本次 `_call_llm` 内后续模型输入，不进入最终 `replacement_messages(...)`，也不作为用户可见最终输出持久化。

**与 turn(stop) 的区别**：`turn(stop)` 不创建 ToolMessage，直接 `break` 退出循环，把 pending AIMessage 作为最终消息返回。`turn(start)` 创建 ToolMessage 并 `continue`，因为 turn 还没结束，LLM 需要看到 tool result 后继续工作。

**turn(start) 后再调 turn(start)**：用当前 start call 的 `tool_call_id` 返回 ToolMessage `"Goal already declared."`，然后 `continue`。

### 5. turn_state 字段

**三态流转**：`initial` → `running` → `committed`，每轮重置为 `initial`。

| 状态 | 含义 | 触发 |
|------|------|------|
| `initial` | turn 开始，尚未声明 goal | turn 开始（每轮默认） |
| `running` | 已调用 `turn(start)`，goal 已声明，LLM 正在工作 | `turn(start)` 成功 |
| `committed` | 已调用 `turn(stop)`，turn 结束 | `turn(stop)` 成功 |

**AgentState**（dict-based state）：
- `turn_runner.py:256` 的 `initial` dict 加 `"turn_state": "initial"`
- `_call_llm` 处理 `VALID_START` 时设 `turn_state = "running"`，返回 `"turn_state": "running"`
- `_call_llm` 处理 `VALID_TURN`（stop）时设 `turn_state = "committed"`
- `turn_state` 不需要持久化到 TaskState，是 turn 级别的临时状态

**Current Task State 上下文**：
- `src/voidx/agent/runtime_context.py` — `_current_task_state()` 方法加一行：
  ```python
  lines.append(f"- Turn state: {self.turn_state}")
  ```
- `RuntimeContextBuilder` 需要接收 `turn_state` 参数

### 6. 移除 resolver goal LLM 调用

**文件**: `src/voidx/agent/graph/turn_runner.py:196-212`

```python
# Before:
if interaction_mode == "plan":
    intent_resolution = resolve_plan_mode(...)
elif interaction_mode == "goal":
    intent_resolution = resolve_goal_mode(...)
else:
    intent_resolution = await resolve_goal_for_turn(...)  # LLM 调用

# After:
if interaction_mode == "plan":
    intent_resolution = resolve_plan_mode(...)
elif interaction_mode == "goal":
    intent_resolution = resolve_goal_mode(...)
else:
    # 不再调 LLM，fallback 到 coding + none goal
    # LLM 通过 turn(start) 自己声明 goal
    intent_resolution = GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=None,
        plan=None,
    )
```

**`goal_resolver.py` 保留**：`resolve_plan_mode` / `resolve_goal_mode` 继续使用。`resolve_goal_for_turn` 暂时保留备用，后续视效果决定是否移除。

### 7. 全局规则

**文件**: `src/voidx/agent/prompts.py` — `BASE_SYSTEM.global_rules`

将现有的 turn 规则（第 206 行）替换为两条：

```python
# 替换原有规则：
# PromptRule(detail="After you have completed your response to the user's request, call turn() as the only tool to end the current turn; do not finish with ordinary assistant text alone.")

# 改为两条：
PromptRule(detail="Use turn with operation='start' when turn state is initial to declare intent and goal."),
PromptRule(detail="Use turn with operation='stop' when you need to stop this turn."),
```

规则措辞简洁，基于 turn_state 状态驱动：LLM 看到 Current Task State 里 `Turn state: initial` 时知道该调 start，工作完成后调 stop。runtime 不强制——不调 start 则 fallback 到 coding + none goal。

### 8. START_PROMPT 注入时机

在 `_call_llm` 的 turn_control 循环里，第一轮 LLM 响应处理时：

- `interaction_mode == "plan"` 或 `"goal"` → 不注入 START_PROMPT（这两个模式有自己的 goal 设定路径）
- 其他模式 → 只在第一轮响应 `classification == PLAIN_TEXT` 且 `turn_state == "initial"` 时注入 START_PROMPT
- `classification == REGULAR_TOOLS` 时不注入 START_PROMPT，避免打断 tool_call → ToolMessage 配对；继续执行工具，本轮 goal 使用 fallback
- 注入后 `continue`，LLM 重新响应
- 第二轮起不再注入

**与现有 TURN_PROMPT 的关系**：
- `TURN_PROMPT`（结束提示）：LLM 输出文本但没调 `turn(stop)` 时注入
- `START_PROMPT`（开始提示）：LLM 没调 `turn(start)` 就开始干活时注入
- 两者互不干扰，分别管理 turn 的开始和结束

## 影响面

### 需要修改的文件

| 文件 | 改动 |
|------|------|
| `src/voidx/agent/graph/turn_control.py` | `TURN_TOOL_DEFINITION` 扩展、`TurnClassification` 加 `VALID_START`、`classify_turn_call` 扩展、`START_PROMPT` 常量 |
| `src/voidx/agent/graph/core/llm.py` | `_call_llm` turn_control 逻辑加 `VALID_START` 分支、START_PROMPT 注入逻辑、`turn_state` 管理 |
| `src/voidx/agent/graph/turn_runner.py` | `resolve_goal_for_turn` 调用改为 fallback、`initial` dict 加 `turn_state: "initial"` |
| `src/voidx/agent/runtime_context.py` | `_current_task_state()` 加 `Turn state` 行、`RuntimeContextBuilder` 接收 `turn_state` |
| `src/voidx/agent/prompts.py` | `BASE_SYSTEM.global_rules` 替换 turn 规则为 start + stop 两条 |

### 不变的部分

- `resolve_plan_mode` / `resolve_goal_mode` — plan/goal 模式不变
- `goal_resolver.py` — 保留文件，`resolve_goal_for_turn` 暂时保留备用
- `turn(stop)` runtime 行为 — 仍直接 commit pending response，不创建 ToolMessage；schema 调用形式改为 `operation="stop", intent="", goal=""`
- `TaskState` / `GoalResolution` / `GoalSpec` 结构 — 不变
- `reconcile_workflow_runs_for_turn` — 不变，`turn(start)` 复用

### 测试

| 测试文件 | 改动 |
|---------|------|
| `src/tests/test_agent/test_turn_control.py` | 加 `VALID_START` 分类测试、`turn(start)` 参数校验测试、`stop` 空 sentinel schema 测试 |
| `src/tests/test_agent/test_turn_runner.py` 或等价 graph 测试 | auto 模式不再调用 `resolve_goal_for_turn`，fallback 为 coding + none goal，initial state 带 `turn_state: "initial"` |
| 新增 `test_turn_start.py` | `turn(start)` 全流程：声明 goal → ToolMessage tool result → 继续工作 → `turn(stop)`；覆盖 regular tool 不被 START_PROMPT 打断 |

## 风险

1. **LLM 不调 start**：fallback 到 coding + none goal，不阻塞。但 workflow 路由可能不准——原来 resolver 会解析 workflow join，现在靠 LLM 自己调 `workflow(enter)`。这是预期行为变化。

2. **strict schema 与 conditional required**：`strict: True` 要求所有 properties 在 required 里。`intent` enum 必须包含空字符串 sentinel，`stop` 调用统一传 `intent="", goal=""`；runtime 只对 `start` 做非空校验，对 `stop` 忽略这两个字段。

3. **START_PROMPT 只处理 PLAIN_TEXT**：不能在 `REGULAR_TOOLS` 分支插入 START_PROMPT，否则会产生 `AIMessage(tool_call)` 后直接接 `HumanMessage` 的非法工具消息序列。regular tool calls 必须继续执行，goal 使用 fallback。

4. **tool result 机制**：`turn(start)` 需要创建 ToolMessage 返回给 LLM（与 `turn(stop)` 不同，stop 直接 break 不创建 ToolMessage）。当前 turn 工具被设计为"不创建 ToolMessage"的协议信号，`turn(start)` 打破了这个假设。实现时需要在 `_call_llm` 的 `VALID_START` 分支里手动构造带原始 `tool_call_id` 的 ToolMessage 追加到 `llm_messages`，然后 `continue`；该 ToolMessage 不进入最终 `replacement_messages(...)`。需确认 langgraph 的消息流不会因为 turn 工具产生了 ToolMessage 而触发后续 tool execution 节点。

5. **turn_state 与 TaskState 的关系**：`turn_state` 是 turn 级临时状态，不持久化到 TaskState。但 `RuntimeContextBuilder` 需要接收它来渲染 Current Task State。需确认 `turn_state` 如何从 AgentState 传递到 `RuntimeContextBuilder`——可能需要通过 `_prepare_with_stream` 或 `_call_llm` 里的 `state.get("turn_state")` 读取。
