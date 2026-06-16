# Runtime Context 重构方案

> 日期: 2026-06-16
> 状态: 讨论确认，待实施

## 背景

当前 `VOIDX_RUNTIME_CONTEXT` 中存在多处信息重复和不必要的字段，导致 token 浪费和语义混淆。经过逐字段审查，确认以下改动方案。

## 消息流重构

### 重构前

```
1. SystemMessage
2. Workflow Context（HumanMessage）
3. Skill Context（HumanMessage，如有）
4. 历史对话（AiMessage / ToolMessage 交替，含历史 HumanMessage）
5. Goal Resolution Guide（HumanMessage，仅首轮，insert 到当前用户消息之前）
6. Task Context + User Message（HumanMessage）
   ## Runtime State
   ## Current Task State
   ## User Message              ← 与 Latest user request 重复
7. Current Todo（独立 HumanMessage，追加到末尾）  ← 位置不在 Task State 中
8. Inline Compaction Guide（HumanMessage，如有，追加到末尾）
```

### 重构后

```
1. SystemMessage
2. Workflow Context（HumanMessage）
3. Skill Context（HumanMessage，如有）
4. Runtime State（独立 HumanMessage，稳定，可命中 prompt cache）
5. 历史对话（AiMessage / ToolMessage 交替，含历史 HumanMessage）
6. Goal Resolution Guide（HumanMessage，仅首轮，insert 到当前用户消息之前）
7. Current Task State（拼到最新一条消息上，每轮 LLM 调用前迁移）
8. Inline Compaction Guide（HumanMessage，如有，追加到末尾）
```

注：Todo 已合并进 Task State，不再独立；`## User Message` 改为 `## Task Context`（见下方）。

### Task State 跟随最新消息

当前行为：Task State 始终拼在最后一个 HumanMessage 上。在一轮对话中，随着 AIMessage / ToolMessage 不断追加，Task State 离 LLM 的注意力焦点越来越远。

重构后：Task State 每次拼到**消息列表的最后一条消息**上（不管类型），并在下一轮 LLM 调用前从上一条消息上剥离。注意：剥离和重新拼接发生在 `prepare` 节点的 `compile_messages()` 中，不修改 AgentState 中的原始消息——`compile_messages()` 返回的是新的消息列表，原始消息保持干净。

#### 迁移过程示例

```
第 1 次 LLM 调用前（消息列表末尾是 HumanMessage）：
  [HumanMessage: VOIDX_RUNTIME_CONTEXT + 用户输入]   ← Task State 在这里

第 1 次 LLM 返回 AIMessage，execute_tools 返回 ToolMessage 后：
  [HumanMessage: 用户输入]                           ← 剥离 Task State
  [AIMessage: AI 回复 + tool_calls]
  [ToolMessage: VOIDX_RUNTIME_CONTEXT + 工具结果]    ← Task State 迁移到这里

第 2 次 LLM 调用前（消息列表末尾是 ToolMessage）：
  [HumanMessage: 用户输入]
  [AIMessage: AI 回复 + tool_calls]
  [ToolMessage: VOIDX_RUNTIME_CONTEXT + 工具结果]    ← Task State 在这里

第 2 次 LLM 返回 AIMessage，execute_tools 返回 ToolMessage 后：
  [HumanMessage: 用户输入]
  [AIMessage: AI 回复 + tool_calls]
  [ToolMessage: 工具结果]                            ← 剥离 Task State
  [AIMessage: AI 回复 + tool_calls]
  [ToolMessage: VOIDX_RUNTIME_CONTEXT + 工具结果]    ← Task State 迁移到这里
```

#### 实现要点

1. **拼接**：`_prepend_task_context()` 支持所有消息类型（HumanMessage / AIMessage / ToolMessage），不再假设目标是 HumanMessage
2. **剥离**：`raw_semantic_messages()` 中的 `_strip_turn_overlay()` 扩展为对所有消息类型生效，不仅限于 HumanMessage
3. **定位**：`compile_messages()` 中将 `_last_user_index()` 替换为 `len(semantic_messages) - 1`，始终拼到最后一条消息
4. **标记**：`VOIDX_RUNTIME_CONTEXT` 前缀作为统一标记，`_is_turn_overlay_text()` 已通过前缀检测工作，无需修改
5. **分隔符**：`## User Message` 改为 `## Task Context`（见下方）。Task State 拼到消息 content 前面时，以 `VOIDX_RUNTIME_CONTEXT` 标记开头，以 `## Task Context` 分隔 Task State 与原始消息内容。剥离时通过 `_CONTEXT_MARKER` 前缀定位，按 `## Task Context` 分割，取 `## Task Context` 之后的部分作为原始内容。`## Task Context` 是类型无关的分隔符，无论拼到 HumanMessage / AIMessage / ToolMessage 上语义都正确
6. **持久化**：`compile_messages()` 返回的是编译后的消息列表，不修改 AgentState 中的原始消息。持久化时从 AgentState 的原始消息中取内容，天然不含 overlay，无需额外处理
7. **AIMessage content 格式**：AIMessage 的 content 可能是 list（含 tool_calls 的结构化内容），`_prepend_task_context()` 和 `_strip_turn_overlay()` 需要处理 list 格式：拼接时在 list 头部插入 text block，剥离时移除头部 text block。但需注意：AIMessage 的 content 在 `sanitize_todo_replay_messages()` 和 `_sanitize_ai_content_for_replay()` 中已被处理，实际到达 `compile_messages()` 时通常是 string 或已清理的 list
8. **Goal Resolution Guide 定位**：当前代码中 Goal Resolution Guide 通过 `_last_user_index()` 定位插入位置。改为末尾定位后，Goal Resolution Guide 仍应 insert 到**最后一个 HumanMessage** 之前（而非最后一条消息之前），因为 Goal Resolution Guide 的语义是指导 LLM 解析用户意图，应紧邻用户消息

### 历史消息说明

历史对话位于 Runtime State 之后、Goal Resolution Guide 之前。由 AiMessage、ToolMessage 和历史 HumanMessage 交替组成：

- **AiMessage**：LLM 的回复，可能包含文本内容和 tool_call 请求
- **ToolMessage**：工具执行结果，与 AiMessage 中的 tool_call 一一对应
- **历史 HumanMessage**：用户之前发送的消息

所有历史消息在 `compile_messages()` 中被剥离 Task State overlay，保持干净内容。Task State 只存在于编译后消息列表的最后一条消息上。AgentState 中的原始消息始终不含 overlay。

典型模式：

```
HumanMessage → 用户消息（干净，无 overlay）
AiMessage    → 文本回复 + tool_call
ToolMessage  → 工具 A 结果
ToolMessage  → 工具 B 结果
AiMessage    → 基于工具结果的后续回复
...
最后一条消息 → 含当前 Task State overlay
```

历史消息在每轮 LLM 调用中都会完整携带，`_prepare_with_stream()` 每轮调用 `compile_messages()` 重新组装消息列表时，先剥离旧 overlay，再拼到新的最后一条消息上。注意：这是编译时操作，不修改 AgentState 中的原始消息。

### Goal Resolution Guide 位置说明

Goal Resolution Guide 通过 `semantic_messages.insert(current_user_index, guide_msg)` 插入到最后一个 HumanMessage 之前。代码执行顺序：

1. 先将 Task Context 拼到最后一条消息上（`_prepend_task_context`）
2. 再将 Goal Resolution Guide insert 到最后一个 HumanMessage 之前

因此 Goal Resolution Guide 始终在最后一个 HumanMessage 之前。Task State 可能在更后面的 AIMessage/ToolMessage 上，两者位置独立。

### Goal Resolver 上下文重构

> 详见独立方案：`docs/specs/goal-resolver-refactor-2026-06-16.md`

Goal Resolver（`resolve_goal_for_turn()`）是每轮开始前的独立 LLM 调用，与主对话的消息流完全独立。当前它使用 JSON context 对象作为上下文，重构后将改为使用**预记录的对话轮次消息对**（`TurnExchange`）作为上下文。

关键改动：

1. **上下文来源**：从 JSON context 对象（含 workspace、session_time、interaction_mode、current_goal、recent_user_texts）改为 `TaskState.recent_exchanges` 中的预记录消息对（`TurnExchange(user_text, assistant_text)`）
2. **SystemMessage 精简**：移除 JSON schema（`with_structured_output` 已传）和详细 join 说明，从 ~2937 chars 降至 ~800 chars
3. **API 简化**：`resolve_goal_for_turn()` 移除 `workspace`/`session_time` 参数，所有上下文通过 `task_state` 传递
4. **消息对记录**：每轮结束时在 `turn_runner.run_once()` 中记录 `TurnExchange` 到 `task_state.recent_exchanges`，保留最近 3 轮
5. **消息构建**：每个 `TurnExchange` 生成 HumanMessage + "Assistant: ..." HumanMessage，AIMessage 转为 HumanMessage 以兼容 `with_structured_output`

Goal Resolver 的上下文重构不影响主对话消息流中 Goal Resolution Guide 的位置和内容。两者是独立的：Goal Resolution Guide 是主对话 LLM 看到的指令，Goal Resolver 上下文是 goal resolver 自身 LLM 调用的输入。

## Runtime State 重构

### 移除字段

| 字段 | 理由 |
|------|------|
| Model | LLM 不需要知道自己是什么模型，这是调度层信息 |
| Interaction mode | 实际约束已通过 Task State 的 Constraint 行表达，auto 值对 LLM 无指导意义 |
| Permission profile | `custom` 值对 LLM 无语义，具体约束由 Sandbox、Approval policy 表达 |
| Approval reviewer | `user` 是默认值，LLM 只需知道需要审批，不需知道谁来审批 |
| User language | 被 Language instruction 替代，instruction 本身已包含语言信息 |
| User tone | 被 Tone instruction 替代，instruction 本身已包含语气信息 |

### 保留字段

| 字段 | 理由 |
|------|------|
| Workspace | LLM 需要知道工作目录，影响文件路径理解 |
| Sandbox | 直接告诉 LLM 写入权限范围 |
| Approval policy | 告诉 LLM 工具调用是否需要审批 |
| Extra write paths | 告诉 LLM 额外可写路径 |
| Language instruction | 替代 User language，指令形式对 LLM 更有指导意义 |
| Tone instruction | 替代 User tone，指令形式对 LLM 更有指导意义 |

### 重构后 Runtime State 字段

```
- Workspace
- Sandbox
- Approval policy
- Extra write paths
- Language instruction
- Tone instruction
```

### 位置变更

Runtime State 从 Task Context 中拆出，作为独立 HumanMessage 放在 Skill Context 之后。理由：
- Runtime State 基本不变（workspace、permission、language、tone 在会话内稳定）
- 作为独立稳定消息可命中 prompt cache，减少每轮 token 开销
- 偶尔用户通过 slash 命令切换配置时 cache 失效一次，可接受

## Current Task State 重构

### 移除字段

| 字段 | 理由 |
|------|------|
| Workflow run state | 与 Active workflow nodes + Workflow exits 重复，且 source、body_hash、next 是内部调度信息，LLM 不需要 |
| User language preference | 与 Runtime State 重复 |
| User tone preference | 与 Runtime State 重复 |
| Permission gate | 与 Runtime State 的 Sandbox + Approval policy 重复 |

### 移到 Runtime State 的字段

| 字段 | 说明 |
|------|------|
| Language instruction | 移到 _render_envelope() 中，替代 User language |
| Tone instruction | 移到 _render_envelope() 中，替代 User tone |

### 改动字段

| 字段 | 改动 |
|------|------|
| Latest user request | 移除。用户原始消息通过消息本体传递，`## Task Context` 分隔符后即为原始内容，无需在 Task State 中重复 |
| Constraint | Interaction mode 的值合并进 Constraint 行：`plan mode — blocks write/edit...` / `goal mode — keep work scoped...`，auto 模式不输出 |

### 新增字段

| 字段 | 说明 |
|------|------|
| Active todo | 内联渲染 todo items，不截断，放在 Workflow exits 之后 |

### 重构后 Current Task State 字段

```
- Current persona
- Intent
- Goal type / Goal（如有）
- Active workflow nodes
- Workflow route
- Workflow exits
- Active todo              ← 新增
- ~~Latest user request~~   ← 已移除，用户消息通过消息本体传递
- Constraint               ← 仅 plan/goal 模式，合并 interaction mode 值
```

## 其他改动

### `## User Message` 改为 `## Task Context`

`_prepend_task_context()` 中的 `## User Message` 分隔符改为 `## Task Context`，同时不再追加用户原始消息文本。

原因：
- Task State 现在拼到任意类型的消息上（不再仅限 HumanMessage），`## User Message` 分隔符语义不再适用
- `## Task Context` 是类型无关的分隔符，无论拼到 HumanMessage / AIMessage / ToolMessage 上语义都正确
- 用户原始消息通过消息本体传递，不需要在 Task State 中重复
- `Latest user request` 字段因此移除（见下方），不再需要

拼接后的消息结构示例：

```
VOIDX_RUNTIME_CONTEXT
## Runtime State
...
## Current Task State
...

## Task Context
<原始消息内容>
```

剥离时通过 `VOIDX_RUNTIME_CONTEXT` 前缀定位，按 `## Task Context` 分割，取其后的部分作为原始内容。

### Todo 合并进 Task State

- 移除 `core.py` 中 `current_todo_context_message` 的生成和追加逻辑
- 删除 `_render_todo_run_state()`、`current_todo_context_message()` 函数
- Todo items 直接在 `_current_task_state()` 中内联渲染

### Todo 渲染格式

```
- Active todo: 3 items
  - pending: 实现功能 A
  - in_progress: 修复 bug B
  - completed: 编写测试 C
```

无 todo 时不输出任何 todo 相关行。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/voidx/agent/runtime_context.py` | _current_task_state() 字段重构（移除 Latest user request 等）、_render_envelope() 字段重构、Runtime State 拆为独立消息、_prepend_task_context() 将 `## User Message` 改为 `## Task Context` 并支持所有消息类型、_strip_turn_overlay() 扩展到 AIMessage/ToolMessage、compile_messages() 中 _last_user_index 改为末尾定位、删除废弃函数 |
| `src/voidx/agent/graph/core.py` | 移除 current_todo_context_message 的生成和追加、Runtime State 独立消息的组装 |
| `src/voidx/runtime/task_state.py` | 新增 `TurnExchange` 模型和 `TaskState.recent_exchanges` 字段（Goal Resolver 重构所需） |
| `src/voidx/agent/goal_resolver.py` | 上下文从 JSON context 改为 `recent_exchanges` 消息对、SystemMessage 精简、移除 workspace/session_time 参数（详见 `docs/specs/goal-resolver-refactor-2026-06-16.md`） |
| `src/voidx/agent/graph/turn_runner.py` | 一轮结束后记录 `TurnExchange` 到 `task_state.recent_exchanges`、移除传给 `resolve_goal_for_turn()` 的 workspace/session_time 参数 |
| `tests/test_agent/test_runtime_context.py` | 更新测试用例，覆盖 Task State 拼接到 AIMessage/ToolMessage 的场景 |
