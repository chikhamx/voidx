# Goal Evaluator 轮上下文污染问题分析

## 状态

已修复，测试覆盖完成。

## 问题现象

goal 模式下，自动执行的评估轮（evaluator phase）会污染宿主会话，具体表现为：

- 每次 goal attempt 的评估轮产生的 10+ 条 LLM 消息被写入宿主会话（而非 goal 会话）；
- 评估轮的 LLM 输入错误地加载了宿主会话的全部历史对话；
- goal 运行越久，宿主会话被无意义消息撑得越大，导致压缩触发时机异常、上下文管理失效。

## 根因

### 调用链

goal 评估轮由 `GoalEvaluator.run_phase` 发起（`src/voidx/agent/application/automation/goal/evaluator.py`），其设计意图在 docstring 中明确写着：

> The evaluator runs with a detached thread (no session_id) so it never loads the work-phase conversation history.

实现上构造了 `session_id=None` 的 detached thread 和 `session_id=""` 的 `TurnExecutionContext`，期望评估轮不绑定任何会话。

### 实际行为

`TurnExecutionContext` 的 `session_id` 为空字符串 `""` 时，`bind_thread_execution_context` 调用 `_state_for_context(host, "")`（`src/voidx/agent/infrastructure/langgraph/runtime/thread_context.py`）：

```python
state = await _state_for_context(host, session_id)
```

而 `_state_for_context` 对空 `session_id` 没有特殊处理，最终回退到 `_state_from_host(host)`，**直接继承了宿主（host）的 session、消息缓存和任务状态**：

- 评估轮从宿主 session 加载全部历史作为 LLM 输入；
- 评估轮产出的消息经 `turn_runner.py` 的 `_persist_new_messages` 写入宿主 session；
- 评估轮结束后的 `bind_thread_execution_context` finally 块会把 state 回写宿主（`_apply_state`）。

### 复现证据

用模拟脚本（预置 600K token 的 goal session）执行多轮 goal attempt，抓取每轮调用记录：

| 轮次 | 评估轮 LLM 输入 | 宿主 session 消息数（修复前） |
|---|---|---|
| attempt 1 | 2 条（宿主历史） | 0 → 11 |
| attempt 2 | 2 条（宿主历史） | 11 → 22 |
| attempt 3 | 2 条（宿主历史） | 22 → 33 |

修复前：每轮评估轮向宿主 session 写入约 11 条消息（`[persist] to session=<host-id> n=11`），且 LLM 输入包含宿主全部历史（`[spy] llm calls: ('<host-id>', 12, ...)`）。

修复后：评估轮不再产生任何持久化（宿主 session 消息数保持 0），LLM 输入为纯系统提示 + 评估 prompt（`(None, 2, ...)`）。

## 修复方案

引入显式的 `detached` 标记，替代"空 session_id 即 detached"的隐式约定。

### 改动文件

| 文件 | 改动 |
|---|---|
| `src/voidx/agent/domain/turn_context.py` | `TurnExecutionContext` 新增字段 `detached: bool = False` |
| `src/voidx/agent/infrastructure/langgraph/runtime/thread_context.py` | `_state_for_context` 增加 `detached` 参数，为真时直接返回空状态（`session=None`、无消息缓存）；`bind_thread_execution_context` 从 `turn_context` 读取该标记 |
| `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py` | detached 轮跳过三处宿主依赖：创建临时 session、用户消息持久化 + runtime snapshot、turn 结束后的消息持久化与标题更新 |
| `src/voidx/agent/application/automation/goal/evaluator.py` | 评估轮上下文设置 `detached=True` |

### 兼容性

- 空 `session_id` 但未标记 detached 的调用（现有测试直接调用 `graph.run_turn`）保持原有宿主回退行为，不受影响；
- goal 工作轮（session_id=goal 会话）、goal idle 轮、coding/chat/loop 模式均不设置 `detached`，行为不变。

## 验证

### 单元测试

新增 `src/tests/test_infrastructure/runtime/test_thread_context_detached.py`（2 个用例）：

- `detached=True` 时 `_state_for_context` 返回无会话状态；
- 空 `session_id` 无 detached 标记时保持宿主回退。

### 回归

- `test_thread_context_detached`、`test_goal_idle_turn`、`test_goal_resolver`、`test_agent_runtime`：**61/61 PASS**；
- `test_infrastructure/runtime`：518 PASS + 9 失败，与修复前完全一致（pre-existing：测试直接构造 `LangGraphExecution` 且不传 session，`host._session.id` 为 None，与本次改动无关）。

### 端到端

复现脚本验证：

- goal work 轮在 602K token 时正常触发强制压缩（`Context overflow — compacting...`，上下文降至 5.8K）；
- 评估轮不再写入任何 session（宿主 session 消息数保持 0）；
- 评估轮 LLM 输入不再包含宿主历史。

## 遗留问题

1. `turn_runner.py` 的 `save_message_runtime_snapshot` 调用依赖 `user_message_id` 非空；detached 轮和 `persist_user_input=False` 的轮次本就不会写入 snapshot，已加条件保护，但该保护是防御性的，未来若引入其他 detached 轮次需注意。
2. 9 个 pre-existing 失败的测试（不传 session 直接调 `graph.run_turn`）建议后续在测试侧补 session 或明确 detached 语义时一并修复。
