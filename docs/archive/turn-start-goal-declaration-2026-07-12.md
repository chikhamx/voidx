# 实现计划：turn(start) — LLM 主动声明 goal

> **Status: Done** — Archived on 2026-07-12.

参考设计文档：`docs/design/turn-start-goal-declaration.md`

## 目标

让主 LLM 通过 `turn(operation="start")` 自己声明 intent + goal，runtime 返回 workflow 状态提示。移除 `resolve_goal_for_turn` 的 LLM 调用路径，改为 fallback。

## 架构

turn 工具从单一 `stop` 操作扩展为 `start` + `stop`。`start` 在 turn 开始时由 LLM 调用，携带 `intent` + `goal`，runtime 构造 `GoalResolution` 更新 task_state，返回 tool result 提示 LLM 考量 workflow。`turn_state` 三态（initial → running → committed）体现在 Current Task State 上下文里。

## 技术栈

- Python 3.11+, pydantic, langchain, langgraph
- 测试：pytest

## 文件结构

| 文件 | 职责 |
|------|------|
| `src/voidx/agent/graph/turn_control.py` | turn 工具定义、分类逻辑、TURN_START_PROMPT / TURN_STOP_PROMPT 常量 |
| `src/voidx/agent/graph/core/llm.py` | `_call_llm` 的 turn_control 拦截逻辑、VALID_START 分支、TURN_START_PROMPT 注入、context 重渲染 |
| `src/voidx/agent/graph/turn_runner.py` | 移除 resolve_goal_for_turn LLM 调用、initial dict 加 turn_state |
| `src/voidx/agent/runtime_context.py` | RuntimeContextBuilder 接收 turn_state、_current_task_state 渲染 |
| `src/voidx/agent/prompts.py` | 全局规则替换为 start + stop 两条 |
| `src/tests/test_agent/test_turn_control.py` | 现有测试更新 + VALID_START 分类测试 |
| `src/tests/test_agent/test_turn_start.py` | 新增：turn(start) 全流程测试 |

## 任务

### Task 1: turn_control.py — 工具定义与分类

- [ ] 1.1 修改 `TURN_TOOL_DEFINITION`：parameters 从 `{decision}` 改为 `{operation, intent, goal}`，required 为 `["operation", "intent", "goal"]`；`intent` enum 为 `["coding", "general", ""]`，其中空字符串是 `stop` sentinel；description 更新
  - **字段语义决策**：用 `operation` 而非 `decision`。`decision` 原本只表达"停止"这一个决定，扩展为 start/stop 后 `operation` 更准确地表达"执行哪种操作"。`decision` 语义偏窄（暗示二选一的判断），`operation` 语义偏动作（start 是动作、stop 是动作），与 start/stop 的动词性更匹配。
  - **strict schema sentinel 风险**：`intent` enum 含空字符串 `""` 作为 stop sentinel。部分 provider（如 OpenAI strict mode）可能拒绝 `""` 作为 enum 值。实现时需先验证目标 provider 支持；若不支持，fallback 方案：`intent` enum 改为 `["coding", "general", "none"]`，stop 时传 `"none"`，runtime 对 stop 忽略 intent/goal 值。
- [ ] 1.2 `TurnClassification` 加 `VALID_START = "valid_start"`
- [ ] 1.3 扩展 `classify_turn_call`：识别 `turn(start)` + `intent in {"coding", "general"}` + `goal` 非空 → `VALID_START`；`start` 缺少/空/非法 intent 或 goal → `INVALID_TURN`；`turn(stop)` → `VALID_TURN`，忽略 intent/goal 值；`turn(start)` + regular tool 混合调用 → `INVALID_TURN`（start 应单独调用）
- [ ] 1.4 加 `TURN_START_PROMPT` 常量（原方案名 `START_PROMPT`，统一重命名见 Task 5）：`"You forgot to call turn with operation='start' to declare this turn's intent and goal. Please call turn with operation='start', intent, and goal now."`
- [ ] 1.5 更新 `validate_turn_call`：`VALID_START` 不需要 pending 消息有文本（start 是开始信号，不是结束信号）；`VALID_TURN` 仍要求 pending 有可提交文本

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/test_turn_control.py -v
```

### Task 2: test_turn_control.py — 更新现有测试

- [ ] 2.1 更新 `_ai_with_turn_call` helper：支持 `operation` 参数（start/stop）+ intent + goal；stop 默认传 `intent="", goal=""`
- [ ] 2.2 更新 schema 测试：验证 `operation`/`intent`/`goal` properties、required 列表、`intent` enum 包含空字符串 sentinel
- [ ] 2.3 更新分类测试：加 `test_classify_valid_start_call`、`test_classify_start_missing_intent_invalid`、`test_classify_start_empty_intent_invalid`、`test_classify_start_missing_goal_invalid`、`test_classify_valid_stop_ignores_intent_goal`、`test_classify_start_mixed_with_regular_tool_invalid`（start + regular tool 混合 → INVALID_TURN）
- [ ] 2.4 更新 description 测试：验证包含 `operation='start'`、`operation='stop'` 和 stop 使用空 sentinel 的说明

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/test_turn_control.py -v
```

### Task 3: runtime_context.py — turn_state 渲染

- [ ] 3.1 `RuntimeContextBuilder.__init__` 加 `turn_state: str = "initial"` 参数
- [ ] 3.2 `_current_task_state()` 加一行：`lines.append(f"- Turn state: {self.turn_state}")`，放在 Intent 行之后
- [ ] 3.3 `_prepare_with_stream`（llm.py:121）传入 `turn_state=state.get("turn_state", "initial")`——turn 开始时渲染 `initial`
- [ ] 3.4 **`_call_llm` 内部重渲染**：`turn(start)` 处理后 `turn_state` 变为 `running`，需让 LLM 下一轮看到 `Turn state: running`。机制：task_context 是通过 `_prepend_task_context` 注入到 `llm_messages` 最后一条 HumanMessage 前缀的（`## Task Context` 分隔符）。`VALID_START` 分支里，用更新后的 `turn_state` 重新构造 `RuntimeContextBuilder` + `ContextCompiler.compile_messages(llm_messages)` 重建 `llm_messages`，使 task_context 前缀反映 `running` 状态。`ContextCompiler.compile_messages` 可独立调用（不依赖 `apply_to_messages` 的原地替换），返回新的消息列表。
  - **注意**：重渲染只更新 task_context 前缀，不重建 SystemMessage（stable prefix 不变，走 cache）
  - **committed 状态不渲染**：`turn(stop)` 后直接 break 退出循环，LLM 不再有下一轮，无需重渲染

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/ -k "runtime_context or task_state" -v
```

### Task 4: turn_runner.py — 移除 resolver LLM 调用 + turn_state 初始化

- [ ] 4.1 `initial` dict（line 256）加 `"turn_state": "initial"`
- [ ] 4.2 将 `resolve_goal_for_turn` 调用（line 203-212）替换为 fallback `GoalResolution(intent=IntentResolution(type=TaskIntent.CODING), goal=None, plan=None)`
- [ ] 4.3 保留 `resolve_plan_mode` / `resolve_goal_mode` 调用不变

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/graph/ -v
```

### Task 5: llm.py — VALID_START 处理 + TURN_START_PROMPT 注入

**常量重命名**（统一命名规范）：
- `TURN_PROMPT` → `TURN_STOP_PROMPT`（结束提示：LLM 输出文本但没调 `turn(stop)` 时注入）
- `START_PROMPT` → `TURN_START_PROMPT`（开始提示：LLM 没调 `turn(start)` 就开始干活时注入）
- `FIRST_MISS_PROMPT` / `SECOND_MISS_PROMPT` 同步引用 `TURN_STOP_PROMPT`
- `INVALID_TURN_PROMPT` 保持不变（它不特指 start 或 stop）
- 所有引用处（llm.py 的 import + 使用处、test_turn_control.py）同步更新

- [ ] 5.1 在 turn_control 循环里加 `VALID_START` 分支：
  - 解析 intent + goal from tool_call args
  - 构造 `GoalResolution`（不走 LLM）
  - 调用 `task_state.update_after_turn()` + `reconcile_workflow_runs_for_turn()`
  - 更新 `runtime_task_state` + `self._task_state`
  - 设置 `turn_state = "running"`
  - **重渲染 context**（Task 3.4）：用更新后的 `turn_state` 重新构造 `RuntimeContextBuilder` + `ContextCompiler.compile_messages(llm_messages)` 重建 `llm_messages`，使 task_context 前缀反映 `Turn state: running`
  - 构造 `ToolMessage(content=<goal accepted text>, tool_call_id=<turn_start_call.id>, name="turn")`
  - 追加 AIMessage + ToolMessage 到 `llm_messages`，然后 `continue`
  - **ToolMessage 生命周期**（理清）：`turn(start)` 的 AIMessage（含 tool_call）+ ToolMessage 只追加到 `llm_messages` 局部变量。`continue` 后下一轮 LLM 响应才是 `terminal_msg`，走到 `:634` 的 `final_msg = terminal_msg` → `replacement_messages(final_msg)`。`replacement_messages`（`:280`）只接收 `assistant_msg` 参数，返回 `[assistant_msg]` 或 compaction 重建列表，**不包含 `turn(start)` 的 AIMessage/ToolMessage**。这两条消息随 `_call_llm` 函数返回丢弃，不进入 langgraph state 的 `messages`，不触发 tool execution 节点（turn 工具被拦截在 `_call_llm` 内部，不经过 ToolRegistry）。
  - **`update_after_turn` 双调用幂等性**：`turn_runner.py:214` 已用 fallback goal 调过一次 `update_after_turn`，`VALID_START` 分支再用 LLM 声明的真实 goal 调第二次。需确认 `update_after_turn` 第二次调用不会重复追加 `recent_exchanges` 或污染 goal 历史——加测试断言双调用后 `recent_exchanges` 不重复。
- [ ] 5.2 `VALID_TURN`（stop）分支：设置 `turn_state = "committed"`（在 break 前更新 state 返回值）
- [ ] 5.3 `TURN_START_PROMPT` 注入逻辑：
  - 只在 `interaction_mode != "plan"` 且 `!= "goal"` 时启用
  - **优先级**：`TURN_STOP_PROMPT` 优于 `TURN_START_PROMPT`。第一轮 LLM 响应如果只输出纯文本（`PLAIN_TEXT`），大概率是结束意图，优先走 `TURN_STOP_PROMPT` 路径（现有 `:514` 逻辑不变）。`TURN_START_PROMPT` 只在 `TURN_STOP_PROMPT` 未触发时（即 `turn_state == "initial"` 且 `missing_turn_count == 0` 且 `classification == PLAIN_TEXT`）注入——但实际上 `PLAIN_TEXT` 必然先命中 `TURN_STOP_PROMPT`（`missing_turn_count` 从 0 变 1），所以 `TURN_START_PROMPT` 的触发条件需调整为：**`turn_state == "initial"` 且 `classification == REGULAR_TOOLS`**——即 LLM 直接开始调工具干活但没先声明 goal 时，在工具执行前注入 `TURN_START_PROMPT`。但这会打断 tool_call → ToolMessage 配对，所以最终决策：**`REGULAR_TOOLS` 时不注入 `TURN_START_PROMPT`，继续执行工具，goal 使用 fallback；`TURN_START_PROMPT` 只在 `PLAIN_TEXT` + `turn_state == "initial"` + `missing_turn_count == 0` 时注入，且注入后 `missing_turn_count` 不递增（避免与 `TURN_STOP_PROMPT` 的计数冲突），用 `start_prompt_injected` flag 确保只注入一次。注入后 `continue`，LLM 重新响应——如果 LLM 仍输出纯文本，下一轮 `missing_turn_count` 仍为 0，但 `start_prompt_injected=True` 不再注入 `TURN_START_PROMPT`，转而走 `TURN_STOP_PROMPT` 路径。**
  - `classification == REGULAR_TOOLS` 时不注入 `TURN_START_PROMPT`，继续执行工具，goal 使用 fallback
  - 用 `start_prompt_injected` flag 确保只注入一次
- [ ] 5.4 `turn(start)` 后再调 `turn(start)`：返回 ToolMessage "Goal already declared."，`continue`
- [ ] 5.5 返回值里加 `"turn_state": turn_state`

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/ -v
```

### Task 6: prompts.py — 全局规则替换

- [ ] 6.1 将 `BASE_SYSTEM.global_rules` 最后一条 turn 规则（line 206）替换为两条：
  - `"Use turn with operation='start' when turn state is initial to declare intent and goal."`
  - `"Use turn with operation='stop' when you need to stop this turn."`

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/test_prompts.py -v
```

### Task 7: test_turn_start.py — 全流程测试

- [ ] 7.1 测试 `turn(start)` 声明 goal → ToolMessage tool result 返回 → turn_state 变为 running
- [ ] 7.2 测试 LLM 不调 start → fallback coding + none goal → turn_state 保持 initial
- [ ] 7.3 测试 `TURN_START_PROMPT` 只在第一轮 PLAIN_TEXT + `turn_state == "initial"` + `missing_turn_count == 0` 时注入、第二轮不注入
- [ ] 7.4 测试 `REGULAR_TOOLS` 不触发 `TURN_START_PROMPT`，保持 tool_call → ToolMessage 协议完整
- [ ] 7.5 测试 `turn(start)` 后再调 `turn(start)` → 用当前 tool_call_id 返回 ToolMessage "Goal already declared."
- [ ] 7.6 测试 `turn(stop)` → turn_state 变为 committed
- [ ] 7.7 测试 `turn(start)` 后 context 重渲染：`llm_messages` 里 task_context 前缀反映 `Turn state: running`（验证 `_current_task_state` 输出含 running）
- [ ] 7.8 测试 `update_after_turn` 双调用幂等性：fallback 调一次 + `VALID_START` 调一次 → `recent_exchanges` 不重复
- [ ] 7.9 测试 `turn(start)` 的 AIMessage + ToolMessage 不进入 `replacement_messages` 返回值（验证 langgraph state messages 不含这两条）

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/test_turn_start.py -v
```

### Task 8: turn_runner.py — auto fallback 与 resolver 移除测试

- [ ] 8.1 新增/更新 turn_runner 级测试：mock `resolve_goal_for_turn` 并断言 auto 模式不再调用它
- [ ] 8.2 断言 auto fallback 使用 `GoalResolution(intent=IntentResolution(type=TaskIntent.CODING), goal=None, plan=None)`
- [ ] 8.3 断言 initial AgentState 包含 `"turn_state": "initial"`

**测试命令**：
```bash
./test.py --backend -- src/tests/test_agent/graph/ -k "turn_runner or turn_start" -v
```

## 风险

1. **ToolMessage 与 langgraph 消息流**：`turn(start)` 创建带原始 `tool_call_id` 的 ToolMessage 后 `continue`，该 ToolMessage 只进入本次 `_call_llm` 内的 `llm_messages` 局部变量，不进入最终 `replacement_messages(...)`（`:280` 只接收 `assistant_msg`）。`continue` 后下一轮 LLM 响应才是 `terminal_msg`，走到 `:634` 的 `final_msg` → `replacement_messages`。turn 工具被拦截在 `_call_llm` 内部，不经过 ToolRegistry，不会触发 langgraph tool execution 节点。Task 7.9 验证此行为。

2. **turn_state 传递与 context 重渲染**：`_prepare_with_stream` 在 `_call_llm` 之前执行，渲染 `turn_state: initial`。`turn(start)` 在 `_call_llm` 内部把 `turn_state` 改为 `running` 后，需通过 Task 3.4 的 context 重渲染（`ContextCompiler.compile_messages`）让 LLM 下一轮看到 `Turn state: running`。重渲染只更新 task_context 前缀（`## Task Context` 分隔符），不重建 SystemMessage（走 cache）。`committed` 状态不渲染（`turn(stop)` 后 break，无下一轮）。

3. **strict schema sentinel**：`strict: True` 要求所有 properties 在 required 里。`intent` enum 含空字符串 `""` 作为 stop sentinel；部分 provider（OpenAI strict mode）可能拒绝 `""` enum。实现时先验证 provider 支持；若不支持，fallback 为 `intent` enum `["coding", "general", "none"]`，stop 传 `"none"`。runtime 只对 `start` 校验 intent/goal 非空，对 `stop` 忽略这两个字段。

4. **`update_after_turn` 双调用幂等性**：`turn_runner.py:214` 用 fallback goal 调第一次，`VALID_START` 分支用 LLM 声明的真实 goal 调第二次。需确认第二次调用不重复追加 `recent_exchanges` 或污染 goal 历史。Task 7.8 验证。

5. **TURN_START_PROMPT 与 TURN_STOP_PROMPT 优先级**：`TURN_STOP_PROMPT` 优于 `TURN_START_PROMPT`。`PLAIN_TEXT` 必然先命中 `TURN_STOP_PROMPT`（`missing_turn_count` 从 0 变 1），所以 `TURN_START_PROMPT` 的实际触发窗口很窄：只在 `PLAIN_TEXT` + `turn_state == "initial"` + `missing_turn_count == 0` 时注入，且注入后 `missing_turn_count` 不递增，用 `start_prompt_injected` flag 确保只注入一次。注入后 `continue`，LLM 重新响应——如果仍输出纯文本，下一轮转走 `TURN_STOP_PROMPT` 路径。`REGULAR_TOOLS` 时不注入 `TURN_START_PROMPT`（避免打断 tool_call → ToolMessage 配对），goal 使用 fallback。

6. **现有测试破坏**：`test_turn_control.py` 里的 schema 测试、分类测试、description 测试都会因为工具定义变更和常量重命名（`TURN_PROMPT` → `TURN_STOP_PROMPT`）而失败。Task 2 负责更新这些测试。
