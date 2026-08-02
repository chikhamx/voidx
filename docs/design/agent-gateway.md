---
name: agent-gateway
display_name: Agent Gateway
description: 为 voidx 提供多 agent 异步运行、父子双向通信和生命周期管理
doc_type: tech-design
audience: human+llm
---

# Agent Gateway — 技术设计文档

## TL;DR

当前 `agent` 工具如果同步等待 `run_subagent()` 完成，主 agent 在此期间不能继续推理，因此即使增加消息队列也无法实现有效通信。本设计在 `src/voidx/agent/gateway/` 增加每个 graph 实例独立的进程内 `AgentGateway`：`agent(spawn)` 默认异步并立即返回 `run_id`，结果由子 agent 通过 child-only `message(send, message_type=result)` 报告，parent 后续用 `agent(wait)` 获取；`agent(cancel)` 可取消后台 run。首期不支持主 agent 在 turn 结束后被消息自动唤醒，也不暴露 root-side 普通消息收发 UI，不恢复进程重启前的后台任务。首期范围有意克制，但关键选型（run_id 不透明、集中式路由、窄 gateway API、inbox 实现封装、结果走 message 通道）对齐长期演进方向，未来扩展到任意拓扑通信与分布式 transport 时无需推翻本设计，见 Evolution。

## Context

### 设计前的旧执行链路

```text
AgentTool.execute()
  -> await VoidxGraph._subagent_runner()
     -> await run_subagent()
        -> child LLM/tool loop
        -> return final str
  -> ToolResult(output=str)
  -> parent graph resumes
```

关键实现位置：

- `src/voidx/tools/agent.py:AgentTool.execute()` 同步等待 runner。
- `src/voidx/agent/graph/core/voidx_graph.py:_subagent_runner()` 分配展示用 `agent_N`，执行并上报生命周期事件。
- `src/voidx/agent/graph/subagent.py:run_subagent()` 运行完整子 agent loop，并仅在结束时返回 `str`。
- `src/voidx/tools/task_tracker.py:TaskTracker` 保存状态预览，但不拥有后台 task 或消息通道。
- `src/voidx/memory/subagents.py:append_subagent_event()` 只持久化 transcript event，不提供实时通信。

### 问题

1. 主 agent 在 `await run_subagent()` 期间停止运行，不能处理子 agent 的问题或进度消息。
2. 子 agent 没有可寻址的父级通信接口。
3. `TaskTracker` 缺少消息、结果等待、取消和 task ownership 语义，不适合作为运行协调器。
4. 多个并发子 agent 只有工具层 `asyncio.gather()`，没有统一生命周期和 session cleanup。
5. 仅增加队列不能解决调度问题；必须让主 agent 可选择异步派发。

## Goals / Non-Goals

### Goals

- 支持一个主 agent 同时启动多个后台子 agent。
- 在 gateway 层支持主 agent 与直属子 agent 的父子路由；首期公开工具面由 child-only `message` 与 parent-side `agent(wait/cancel)` 组成。
- 为后台 run 提供查询、等待结果和取消能力。
- 保证 completed、failed、cancelled 均有唯一、可观察的终态。
- 按 session 隔离 run 和消息，并在 session 清理或切换时回收后台 task。
- 将 `agent(spawn)` 收口为默认异步、gateway 必需语义；缺少 gateway 时明确返回 `gateway_unavailable`，不提供同步 fallback。
- 复用现有 result contract、权限快照、UI lifecycle event 和 transcript 逻辑。

### Non-Goals

- 首期不在主 agent turn 已结束后自动触发新的推理轮次（演进路径见 Evolution）。
- 首期不允许任意拓扑通信；只允许父子直连（路由结构已预留扩展，见 Decision 5 与 Evolution）。
- 首期不支持子 agent 启动孙 agent；现有 `can_delegate` 规则保持不变。
- 首期不跨进程恢复后台 run 或未读 inbox（演进路径见 Evolution）。
- 不用 gateway 取代 workflow DAG、`TaskTracker` 或终止汇报协议。
- 不在首期增加前端专用通信 UI。

## Decisions

### 1. Gateway 生命周期归属 `VoidxGraph`

每个 `VoidxGraph` 创建一个 `AgentGateway`，而不是使用进程级单例。这样 session、权限、工具 registry 和 graph cleanup 的所有权明确，测试也无需清理全局状态。

主 agent 在每个 session 中使用逻辑 root run id：

```text
root:<session_id>
```

子 run id 使用不可碰撞标识，不复用当前仅用于 UI 排序的 `agent_id`。UI 仍可保留递增 `agent_id`，但 lifecycle event 的 `subagent_id` 应使用 gateway `run_id`，使通信、日志和展示引用同一身份。

run_id 对工具、LLM 和任何调用方都是不透明字符串：不得解析、拼接或假设其格式。`root:<session_id>` 与子 run id 的生成规则是 gateway 内部实现细节，未来可整体替换（例如携带 agent_type 的格式）而不影响工具协议、消息协议和存储。调用方需要拓扑信息时通过 `AgentRun` 字段获取（如 `agent_type`、`parent_run_id`），而不是从 run_id 解析。

### 2. Gateway 是运行事实源，TaskTracker 是状态投影

`AgentGateway` 独占以下状态：

- run 拓扑与 session ownership；
- `asyncio.Task`；
- inbox；
- 运行状态、结果和错误；
- 完成事件与取消。

`TaskTracker` 继续服务 `task_status` 和 UI 预览。runner 在状态变化时更新 tracker，但不得由 tracker 反向推断 gateway 生命周期。

### 3. spawn 默认异步，wait/cancel 管理后台 run

`agent` 的 `spawn` 默认异步：创建 child run 后立即返回 `run_id`，主 agent 可继续推理、启动更多 child，或使用 `agent(wait)` / `agent(cancel)` 管理该 run。不新增 `spawn_async`，也不引入 `background` 字段；`spawn` 本身就是后台派发语义。

结果传递统一走 message 通道：子 agent 通过 `message(send, message_type=result)` 报告结果（见 Decision 6）。`agent(wait)` 等待 child 终态，并从 `AgentRun.result` 的结构化 payload 中提取字符串输出。这样 spawn、wait、message 的结果事实源保持一致。
`agent(spawn)` 必须在携带 `agent_gateway` 和 `agent_run_id` 的 runtime context 中运行；缺少 gateway 时返回 `gateway_unavailable` 错误，不回退为同步 runner 调用。


### 4. 通过 ToolContext 传递执行身份

`ToolContext` 增加两个排除序列化的字段：

```python
agent_gateway: AgentGateway | None
agent_run_id: str
```

主 agent 的 context 使用 root run id；子 agent 的 context 使用自己的 run id。工具不依赖 `contextvars` 或模块级全局变量，因此并行调用不会串用身份。

### 5. 只有父子直连

首期路由规则：

- root 可发送给自己的直属 child；
- child 可发送给 parent；
- parent 可读取自己的 inbox、等待指定直属 child、获取其结果或取消它；
- child 可读取自己的 inbox；
- sibling、跨 session、未知 run 和祖孙直连均拒绝。

规则由 gateway 统一校验，不能只依赖 LLM 工具描述。

路由判定必须实现为 gateway 内单一的显式规则函数（输入 source/target run record 与操作类型，输出允许或拒绝），不得散落成各 API 里的硬编码判断。首期规则集只放行父子直连；未来放开兄弟、祖孙或跨层通信时只需扩展规则集，gateway API、两个通信工具和消息协议保持不变（见 Evolution）。

### 6. 子 agent 结果通过 message result 消息报告

子 agent 的最终结果统一通过 `message(send, message_type=result, payload={...})` 报告给 parent。result 消息兼容两种产生方式：

1. **显式发送（优先）**：子 agent LLM 在完成任务后主动调用 `message(send, message_type=result, payload={...})`。gateway 检测到 result 消息后将其作为该 run 的正式结果，并终止子 agent run。
2. **自动包装（兜底）**：如果子 agent 正常结束（LLM 不再调用工具）但未显式发送 result 消息，runner 自动将最后一轮 LLM 输出包装成 result 消息投递给 parent。这与现有 `extract_text(assistant_msg)` 行为一致，对子 agent LLM 透明。

两种方式产生的 result 消息内容应保持一致——都放在 `payload` 里，结构由 `result_contract` 约定。`agent(wait)` 返回的 `AgentRun.result` 与 result 消息的 payload 是同一份数据。

现有 `run_subagent` 的返回值（`str`）改为从 result 消息的 payload 提取，而非直接取 `extract_text`。`agent(spawn)` 返回 `run_id`；`agent(wait)` 从 gateway 保存的结构化 result payload 中提取字符串并包装成 `ToolResult`。

## Data Model

在 `src/voidx/agent/gateway/models.py` 定义 Pydantic 模型和字面量类型。

```python
AgentRunStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled"
]

UserMessageType = Literal[
    "message", "question", "answer", "progress", "result"
]

LifecycleMessageType = Literal["completed", "failed", "cancelled"]

AgentMessageType = Literal[
    "message", "question", "answer", "progress", "result",
    "completed", "failed", "cancelled",
]

class AgentMessage(BaseModel):
    message_id: str
    session_id: str
    source_run_id: str
    target_run_id: str
    type: AgentMessageType
    payload: dict[str, Any]
    created_at: float

class AgentRun(BaseModel):
    run_id: str
    session_id: str
    parent_run_id: str
    agent_type: Literal["root", "sub"]
    agent_name: str
    description: str
    status: AgentRunStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float
    updated_at: float
```

内部 runtime record 可使用 dataclass，额外保存 `asyncio.Task`、有界 `asyncio.Queue[AgentMessage]` 和 `asyncio.Event`；这些对象不得进入 Pydantic 序列化或 transcript。

`agent_type` 让调用方无需解析 run_id 即可区分 root 与 sub，未来可扩展取值（如 `third_party`）。`AgentRunStatus` 和 `AgentMessageType` 的新增取值视为向后兼容扩展（如未来的 `paused`、`retrying`、控制类消息），但任何扩展不得破坏两条不变量：completed/failed/cancelled 是唯一终态语义来源且每个 run 只进入一次终态；lifecycle 终态消息不得因背压丢失。

## Gateway API

`src/voidx/agent/gateway/gateway.py` 提供：

```python
class AgentGateway:
    def ensure_root(self, session_id: str) -> str: ...

    async def spawn(
        self,
        *,
        session_id: str,
        parent_run_id: str,
        agent_name: str,
        description: str,
        runner: Callable[[str], Awaitable[str | dict[str, Any]]],
    ) -> AgentRun: ...

    async def send(
        self,
        *,
        sender_run_id: str,
        target_run_id: str,
        message_type: UserMessageType,
        payload: dict[str, Any],
    ) -> AgentMessage: ...

    async def receive(
        self, *, run_id: str, limit: int = 1, timeout: float = 0
    ) -> list[AgentMessage]: ...

    async def wait(
        self, *, requester_run_id: str, target_run_id: str, timeout: float
    ) -> AgentRun: ...

    def get_run(self, *, requester_run_id: str, target_run_id: str) -> AgentRun: ...

    def get_parent_run_id(self, run_id: str) -> str | None: ...

    async def cancel(
        self, *, requester_run_id: str, target_run_id: str
    ) -> AgentRun: ...

    async def close_session(self, session_id: str) -> None: ...

    async def close_all(self) -> None: ...
```

`wait` 的 `timeout` 语义：`0` 表示无限等待终态；`>0` 为有界等待，超时返回当前 `AgentRun`（通常仍是 `running`），不抛异常。

### Spawn 和终态规则

1. 校验 parent 属于同一 session。
2. 创建 run record、inbox 和完成 event。
3. 用 `asyncio.create_task()` 执行包装后的 runner。
4. 若 run 尚未终态，runner 正常返回时写入 result，状态变为 completed。若已通过 `message_type=result` 进入终态，runner 返回值不得覆盖已有 result。
5. runner 抛出 `CancelledError` 时状态变为 cancelled，并继续正确处理取消。
6. runner 抛出其他异常时捕获摘要，状态变为 failed；后台 task 不得泄漏未检索异常。
7. 每次终态转换只执行一次，并向 parent inbox 发送对应 lifecycle message。
8. `close_session()` 取消该 session 的非终态 task，等待回收后删除 inbox 和 runtime record；应用退出路径调用 `close_all()`。

### 消息背压

- inbox 必须有固定容量；建议默认 100。
- 单条 payload 序列化后设置大小上限；建议默认 64 KiB。
- inbox 已满或消息过大时明确返回错误，不静默丢弃。
- lifecycle 终态消息不能因普通 inbox 已满而消失；实现可为 lifecycle 保留槽位，或使用独立 completion event 并让 `wait/get_run` 成为可靠终态通道。

inbox 的队列实现封装在 gateway 内部，不出现在 `AgentGateway` 公开 API 的签名中。未来需要跨进程通信时，可在 send/receive 之下引入 `MessageTransport` 抽象，将内存队列替换为外部队列，上层工具与消息协议不变（见 Evolution）。

## Communication & Control Tools

首期涉及两个工具：`agent` 扩展为创建+控制子 agent 的统一入口，新增 `message` 工具供子 agent 与 parent 通信（含结果传递）。按职责拆分——`agent` 管生命周期（spawn/wait/cancel），`message` 管子 agent 消息（send/receive，含 result 类型）。语义边界对齐权限边界：child 需要 `message` 报告结果；parent 通过 `agent(wait/cancel)` 管理直属 child。

### `agent`（创建 + 运行控制）

扩展现有 `src/voidx/tools/agent.py`。`AgentInput` 增加两个字段：

```python
class AgentInput(BaseModel):
    # 现有字段保持不变：name, mode, task, target, success_criteria, result_preset

    action: Literal["spawn", "wait", "cancel"] = "spawn"
    target_run_id: str | None = None  # wait/cancel 必填
    timeout: float = 0  # wait: 0 = 无限等待终态；>0 = 有界等待
```

语义：

- `spawn`（默认）：必须在携带 `agent_gateway` 和 `agent_run_id` 的 runtime context 中运行；创建后台子 agent 后立即返回 `run_id` 和 `status="running"`。缺少 gateway context 时返回 `metadata.error=True`、`reason="gateway_unavailable"`，不调用 runner，也不回退同步执行。
- `wait`：等待 `target_run_id` 指定的直属 child。`timeout=0` 表示同步等待到终态（不超时）；`timeout>0` 为有界等待，超时后返回当前 `AgentRun`（通常仍是 `running`）且不标 error。终态时从 result payload 中提取字符串作为 `ToolResult.output`。
- `cancel`：取消 `target_run_id` 指定的直属 child。

`action != "spawn"` 时，name/mode/task/target/success_criteria 等创建字段忽略。路由与权限统一由 gateway 校验。

### `message`（消息收发）

新增 `src/voidx/tools/message.py`：

```python
class MessageInput(BaseModel):
    action: Literal["send", "receive"]
    target_run_id: str | None = None
    message_type: Literal["message", "question", "answer", "progress", "result"] = "message"
    payload: dict[str, Any] = Field(default_factory=dict)
    limit: int = 1
    timeout: float = 0
```

语义：

- `send`：向 parent 或直属 child 发送消息。`target_run_id` 省略时默认路由到当前 run 的 parent（child 无需知晓 parent 的具体 run id）；root 省略 target 时返回参数错误。`message_type` 只接受 `UserMessageType`；lifecycle 类型（completed/failed/cancelled）由 gateway 在终态转换时内部产生，`AgentGateway.send()` 会拒绝外部发送 lifecycle 类型。
- `receive`：读取当前 run inbox；`timeout=0` 为非阻塞，`timeout>0` 等待消息到达。

`message_type=result` 用于子 agent 向 parent 报告结果，产生机制见 Decision 6。子 agent 显式发送时优先使用；未显式发送时由 runner 自动包装。result 消息与 gateway 的终态 lifecycle 消息（completed/failed/cancelled，系统自动发送）互补——lifecycle 消息通知"结束了"，result 消息携带"结果是什么"。`agent(wait)` 返回的 `AgentRun.result` 与 result 消息的 payload 是同一份数据。

### 共性

两个工具都从 `ToolContext.agent_gateway` 和 `ToolContext.agent_run_id` 获取调用身份。gateway 缺失、参数不适用或路由越权时返回 `ToolResult(metadata={"error": True, ...})`。

### 工具裁剪策略

现有裁剪机制：`_BLOCKED_CHILD_TOOLS = {"agent", "clarify", "checkpoint"}`，`can_delegate=False` 的子 agent 从父 registry 减去这些工具。引入 `message` 后的裁剪规则：

| 工具 | 主 agent（首期） | 子 agent can_delegate=False | 子 agent can_delegate=True |
|---|---|---|---|
| 现有全部工具 | ✅ | ✅（减去 agent/clarify/checkpoint） | ✅ |
| `message` | ❌ 不注册 | ✅ | ✅ |
| `agent` 的 spawn | ✅ | ❌ | ✅ |
| `agent` 的 wait/cancel | ✅ | ❌ | ❌ |

首期裁剪要点：

1. **主 agent 不注册 `message`**：首期 root-side 普通消息 UX 不暴露；主 agent 通过 `agent(wait)` 获取 result、通过 `agent(cancel)` 取消直属 child。
2. **子 agent 注册 `message`**：子 agent 需要它向 parent 发送 result/progress 消息并读取发给自己的消息。`message` 不加入 `_BLOCKED_CHILD_TOOLS`。
3. **子 agent delegation 规则不变**：`can_delegate=False` 仍然屏蔽整个 `agent` 工具；`can_delegate=True` 可保留 `agent`，但当前子 agent 启动孙 agent 仍属于 Non-Goal，不应在首期依赖。
4. **`clarify`/`checkpoint` 保持屏蔽**：子 agent 不应与用户交互或设审批门，现有规则不变。

## Integration

### Tool wiring

- `src/voidx/tools/registry.py`：`AgentTool` 已注册；`MessageTool` 不在主 agent registry 注册——只在子 agent 的 tool registry 中注册（见工具裁剪策略）。
- `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py:run_subagent()`：在现有 `parent_tools` 基础上，为子 agent 额外注册 `MessageTool`。`can_delegate=False` 的子 agent 仍按现有规则减去 `_BLOCKED_CHILD_TOOLS`，但 `message` 不在屏蔽列表中。
- `src/voidx/agent/infrastructure/langgraph/execution.py` 创建 graph-scoped gateway，并在 session cleanup 时关闭对应 gateway session。
- `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py` 在主 agent 工具执行 context 中注入 root run identity。

### Main agent context

`src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py`：

- 调用 `gateway.ensure_root(session_id)`；
- 注入 `agent_gateway`；
- 注入 root run id。

### Child runner context

后台 spawn 必须先分配 `run_id`，再调用 runner。runner 接口增加可选关键字参数 `agent_run_id`，并沿以下链路传递：

```text
AgentGateway.spawn wrapper
  -> VoidxGraph._subagent_runner(agent_run_id=...)
     -> run_subagent(agent_run_id=..., agent_gateway=...)
        -> ToolContext(agent_run_id=..., agent_gateway=...)
```

`agent(spawn)` 不再保留无 gateway 的同步执行路径；缺少 `agent_gateway` 或 `agent_run_id` 时工具直接返回 `gateway_unavailable`，避免同一 action 存在两套语义。

`_subagent_runner()` 中现有 `_next_agent_id` 继续只负责 UI 排序。`SubagentStarted.subagent_id`、`SubagentFinished.subagent_id` 和 `append_subagent_event(..., agent_run_id, ...)` 使用 gateway run id。

### Session cleanup

在以下入口调用 `await gateway.close_session(old_session_id)`：

- `VoidxGraph.clear_current_session()`；
- `VoidxGraph.resume_session()` 切换到新 session 之前；
- graph 应用退出或现有统一 cleanup 路径（若有）。

cleanup 顺序必须先取消旧后台任务，再替换 `_session` 和 workspace，防止旧 runner 使用新 session context。

## Interaction Example

```text
main -> agent({action: "spawn", ...})
agent -> {run_id: "run_...", status: "running"}

child -> message({action: "send", message_type: "progress", payload: {...}})
child -> message({action: "send", message_type: "result", payload: {...}})

main -> agent({action: "wait", target_run_id: "run_...", timeout: 60})
agent -> {status: "completed", result: "..."}
```

主 agent 在存在后台子任务时必须继续工作或调用 `agent(wait)` / `agent(cancel)` 管理 run。首期 gateway 不会在主 turn 已 finalize 后自动恢复 graph；root-side message receive/send UX 也不在首期暴露。


## Implementation Status

本设计已实现，关键变更如下：

- `src/voidx/agent/gateway/` 提供进程内 `AgentGateway`、`AgentRun`、`AgentMessage`，负责 run registry、父子路由、inbox、后台 task、终态、取消和 session cleanup。
- `src/voidx/tools/agent.py` 支持 `action="spawn" | "wait" | "cancel"`；`spawn` 默认异步并立即返回 `run_id`，缺少 gateway context 时返回 `gateway_unavailable`，不执行同步 fallback。
- `src/voidx/tools/message.py` 提供 child-only `message(send/receive)`，支持 `message_type="result"` 将结构化 payload 写入 `AgentRun.result`。
- `src/voidx/tools/base.py` 的 `ToolContext` 携带 `agent_gateway` 和 `agent_run_id`，二者不参与序列化。
- `src/voidx/agent/infrastructure/langgraph/execution.py` 与 runtime tool executor 为主 agent 注入 root run identity，并在 session cleanup 时关闭 gateway session。
- `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` 为子 agent 注入 child run identity，只在子 agent registry 注册 `MessageTool`，并在没有显式 result 消息时自动包装最终文本为 result payload。
- `src/voidx/runtime/goal.py` 承载工具可用的 `GoalSpec`，避免 `src/voidx/tools/*` 反向导入 `voidx.agent.*`。

已覆盖测试：

```bash
./test.py --backend -- \
  src/tests/test_agent/gateway \
  src/tests/test_tools/test_message.py \
  src/tests/test_tools/test_interactive_tools.py \
  src/tests/test_tools/test_interactive_tools_write.py \
  src/tests/test_tools/test_tool_schemas.py \
  src/tests/test_agent/graph/test_subagent_gateway_result.py \
  src/tests/test_agent/graph/test_execute_tools_guard.py \
  src/tests/test_agent/test_permission_phase4.py::test_agent_tool_passes_subagent_permission_snapshot -v

./test.py --backend
```

最近验证结果：focused regression 通过；完整 backend `4047 passed, 30 skipped`。

## Evolution

长期方向参见 `docs/design/agent-gateway-v2.md`。以下能力已在首期选型中预留，扩展时不得推翻已验证的首期不变量（终态唯一、lifecycle 终态消息不丢失、run_id 不透明、路由集中校验）。

已预留，可直接扩展：

- **任意拓扑通信**：路由集中在 gateway 单一规则函数（Decision 5），放开兄弟、祖孙或跨层通信只扩展规则集，gateway API、`agent`/`message` 工具和消息协议不变。
- **更深层嵌套**：身份经 `ToolContext` 显式传递（Decision 4），不依赖调用深度；放开孙 agent 只需调整 `can_delegate` 与路由规则。
- **新生命周期状态与消息类型**：`paused`、`retrying`、控制类消息等是枚举的向后兼容扩展，但必须保持终态不变量（见 Data Model）。
- **分布式通信**：inbox 实现封装在 gateway 内部（见消息背压），可在 send/receive 之下引入 `MessageTransport` 抽象替换为外部队列，首期不预先抽象。
- **身份体系演进**：run_id 格式可整体替换（如携带 agent_type），因为调用方只通过 `AgentRun.agent_type`、`parent_run_id` 获取拓扑信息。
- **root-side 消息 UX**：gateway API 已支持父子路由，首期用户可见控制面先暴露 `agent(wait/cancel)`；若未来要让主 agent 直接收发普通消息，应在主 agent registry 中有控制地注册 root-safe `message` 能力，并保持 child-only result 终止规则不变。

需要独立设计，本设计不承诺：

- **暂停/恢复**：需要 checkpoint 执行中的 LLM graph（含进行中的工具调用），属于独立的持久化设计。
- **自动重试**：需要定义重跑起点、副作用处理与成本归属，建议作为独立的错误处理设计。
- **自动唤醒 / push**：见 Risks and Follow-ups，需将 gateway arrival event 接入 session scheduler。
- **跨进程恢复**：需要 message/lifecycle 持久化与 task 恢复设计，可与重启丢失风险项一并立项。

## Acceptance Criteria

- `agent(spawn)` 默认异步，立即返回可用于后续控制的 `run_id`。
- 子 agent 显式调 `message(send, message_type=result)` 时，gateway 保存该 payload，`agent(wait)` 返回该 payload 提取出的字符串。
- 子 agent 未显式发 result 时，runner 自动包装最后一轮输出为 result 消息，`agent(wait)` 返回包装内容。
- `message` 的 send/receive 支持子 agent 向 parent 发送 result 消息。
- 主 agent 首期不注册 `message` 工具；子 agent 注册 `message` 且不受 `_BLOCKED_CHILD_TOOLS` 屏蔽。
- `can_delegate=False` 的子 agent 仍被屏蔽 `agent`/`clarify`/`checkpoint`，但保留 `message`。
- sibling、跨 session 和非父子目标不可通信。
- `agent` 的 wait/cancel 接口可用于等待或取消 `spawn` 返回的后台 run。
- completed、failed、cancelled 只发生一次，且 parent 可可靠观察。
- clear/resume 后旧 session 不留存 running task 或可访问消息。
- 聚焦测试和完整 backend 测试通过。

## Forbidden Changes

- 不把 `AgentGateway` 实现为进程级单例。
- 不将 gateway 状态混入 `TaskTracker` 作为双重事实源。
- 不引入 `background` 字段，也不新增 `spawn_async`；`spawn` 默认异步并返回不透明 `run_id`。
- 不允许工具自行绕过 gateway 做路由授权。
- 不在首期引入自动 graph resume、跨进程恢复或 sibling broadcast。
- 不在工具描述、prompt 或调用方代码中解析、拼接或假设 run_id 格式。
- 不回退或覆盖工作区中与本设计无关的未提交修改。

## Risks and Follow-ups

- **主 agent 忘记等待**：`spawn` 默认异步后，工具描述已提示后台 run 的后续操作；graph `_finalize` 会检测当前 session 的 running child 并注入 guidance（含 run_id 列表与 wait/cancel 指引）。
- **上下文并发**：后台 runner 仍共享 graph 的部分服务；接入时应确认 thread execution state、权限和 UI capture 不依赖可变的主 turn 字段。
- **消息堆积**：有界 inbox 和 payload 限制必须在首期实现。
- **重启丢失**：后续可将 message/lifecycle event 追加到 `memory/subagents.py`，但恢复 task 需要单独设计。
- **自动唤醒**：第二期可把 gateway arrival event 接入 session scheduler；在此之前不得声称支持 push/resume。
- **结构化终止结果**：可与 `docs/design/subagent-report-protocol.md` 协同，gateway 的 `result` 最终升级为结构化 report，而不是长期只保存 `str`。
