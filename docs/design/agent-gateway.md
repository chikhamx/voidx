---
name: agent-gateway
display_name: Agent Gateway
description: 为 voidx 提供多 agent 异步运行、父子双向通信和生命周期管理
doc_type: tech-design
audience: human+llm
---

# Agent Gateway — 技术设计文档

## TL;DR

当前 `agent` 工具会同步等待 `run_subagent()` 完成，主 agent 在此期间不能继续推理，因此即使增加消息队列也无法实现有效的双向通信。本设计在 `src/voidx/agent/gateway/` 增加每个 graph 实例独立的进程内 `AgentGateway`：`agent(background=true)` 创建后台任务并立即返回 `run_id`，主子 agent 通过 `agent_message` 工具发送、接收、等待消息、获取结果和取消任务。现有 `background=false` 同步行为保持不变。首期不支持主 agent 在 turn 结束后被消息自动唤醒，也不恢复进程重启前的后台任务。

## Context

### 当前执行链路

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
- 让主 agent 和直属子 agent 在运行期间双向发送结构化消息。
- 为后台 run 提供查询、等待结果和取消能力。
- 保证 completed、failed、cancelled 均有唯一、可观察的终态。
- 按 session 隔离 run 和消息，并在 session 清理或切换时回收后台 task。
- 保持现有同步 `agent` 调用兼容。
- 复用现有 result contract、权限快照、UI lifecycle event 和 transcript 逻辑。

### Non-Goals

- 首期不在主 agent turn 已结束后自动触发新的推理轮次。
- 首期不允许任意拓扑通信；只允许父子直连。
- 首期不支持子 agent 启动孙 agent；现有 `can_delegate` 规则保持不变。
- 首期不跨进程恢复后台 run 或未读 inbox。
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

### 2. Gateway 是运行事实源，TaskTracker 是状态投影

`AgentGateway` 独占以下状态：

- run 拓扑与 session ownership；
- `asyncio.Task`；
- inbox；
- 运行状态、结果和错误；
- 完成事件与取消。

`TaskTracker` 继续服务 `task_status` 和 UI 预览。runner 在状态变化时更新 tracker，但不得由 tracker 反向推断 gateway 生命周期。

### 3. 同步兼容，显式后台派发

`AgentInput` 增加：

```python
background: bool = False
```

- `false`：保持现有 runner await 和最终 `ToolResult` 语义。
- `true`：gateway 立即创建后台 task，工具返回 `run_id`、`status=running` 和用于后续通信的提示。

不把所有调用默认改成后台模式，以免现有 workflow 在拿不到最终结构化结果时错误推进。

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

## Data Model

在 `src/voidx/agent/gateway/models.py` 定义 Pydantic 模型和字面量类型。

```python
AgentRunStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled"
]

AgentMessageType = Literal[
    "message", "question", "answer", "progress",
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
    agent_name: str
    description: str
    status: AgentRunStatus
    result: str | None = None
    error: str | None = None
    created_at: float
    updated_at: float
```

内部 runtime record 可使用 dataclass，额外保存 `asyncio.Task`、有界 `asyncio.Queue[AgentMessage]` 和 `asyncio.Event`；这些对象不得进入 Pydantic 序列化或 transcript。

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
        runner: Callable[[str], Awaitable[str]],
    ) -> AgentRun: ...

    async def send(
        self,
        *,
        sender_run_id: str,
        target_run_id: str,
        message_type: AgentMessageType,
        payload: dict[str, Any],
    ) -> AgentMessage: ...

    async def receive(
        self, *, run_id: str, limit: int = 1, timeout: float = 0
    ) -> list[AgentMessage]: ...

    async def wait(
        self, *, requester_run_id: str, target_run_id: str, timeout: float
    ) -> AgentRun: ...

    def get_run(self, *, requester_run_id: str, target_run_id: str) -> AgentRun: ...

    async def cancel(
        self, *, requester_run_id: str, target_run_id: str
    ) -> AgentRun: ...

    async def close_session(self, session_id: str) -> None: ...
```

### Spawn 和终态规则

1. 校验 parent 属于同一 session。
2. 创建 run record、inbox 和完成 event。
3. 用 `asyncio.create_task()` 执行包装后的 runner。
4. runner 正常返回时写入 result，状态变为 completed。
5. runner 抛出 `CancelledError` 时状态变为 cancelled，并继续正确处理取消。
6. runner 抛出其他异常时捕获摘要，状态变为 failed；后台 task 不得泄漏未检索异常。
7. 每次终态转换只执行一次，并向 parent inbox 发送对应 lifecycle message。
8. `close_session()` 取消该 session 的非终态 task，等待回收后删除 inbox 和 runtime record。

### 消息背压

- inbox 必须有固定容量；建议默认 100。
- 单条 payload 序列化后设置大小上限；建议默认 64 KiB。
- inbox 已满或消息过大时明确返回错误，不静默丢弃。
- lifecycle 终态消息不能因普通 inbox 已满而消失；实现可为 lifecycle 保留槽位，或使用独立 completion event 并让 `wait/get_run` 成为可靠终态通道。

## `agent_message` Tool

新增 `src/voidx/tools/agent_message.py`，注册到 builtins。输入使用单一 action discriminant：

```python
class AgentMessageInput(BaseModel):
    action: Literal["send", "receive", "wait", "result", "cancel"]
    target_run_id: str | None = None
    message_type: Literal["message", "question", "answer", "progress"] = "message"
    payload: dict[str, Any] = Field(default_factory=dict)
    limit: int = 1
    timeout: float = 0
```

语义：

- `send`：要求 target，向 parent 或直属 child 发送消息。
- `receive`：读取当前 run inbox；`timeout=0` 为非阻塞。
- `wait`：等待直属 child 进入终态，timeout 必须大于 0 且受最大值限制。
- `result`：读取直属 child 当前状态和结果，不阻塞。
- `cancel`：取消直属 child。

工具从 `ToolContext.agent_gateway` 和 `ToolContext.agent_run_id` 获取调用身份。gateway 缺失、参数不适用或路由越权时返回 `ToolResult(metadata={"error": True, ...})`。

`agent_message` 不加入 `src/voidx/agent/graph/subagent.py:_BLOCKED_CHILD_TOOLS`。不能委派的子 agent 仍需要它与父 agent 通信。

## Integration

### Tool wiring

- `src/voidx/tools/registry.py` 注册 `AgentMessageTool`。
- `src/voidx/agent/graph/wiring.py:build_tool_registry()` 接收 gateway，并在创建 `AgentTool` 时注入。
- `src/voidx/agent/graph/core/voidx_graph.py:__init__()` 在 `build_tool_registry()` 前创建 gateway。
- `_reload_parallel_subagents_from_settings()` 重新注册 `AgentTool` 时继续使用同一 gateway。

### Main agent context

`src/voidx/agent/graph/tool_executor/executor.py:make_context()`：

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

同步路径同样可分配 run identity，但不由 gateway 创建后台 task；首期为减少改动，允许同步路径继续使用现有身份，仅后台路径必须完整接入 gateway。

`_subagent_runner()` 中现有 `_next_agent_id` 继续只负责 UI 排序。`SubagentStarted.subagent_id`、`SubagentFinished.subagent_id` 和 `append_subagent_event(..., agent_run_id, ...)` 使用 gateway run id。

### Session cleanup

在以下入口调用 `await gateway.close_session(old_session_id)`：

- `VoidxGraph.clear_current_session()`；
- `VoidxGraph.resume_session()` 切换到新 session 之前；
- graph 应用退出或现有统一 cleanup 路径（若有）。

cleanup 顺序必须先取消旧后台任务，再替换 `_session` 和 workspace，防止旧 runner 使用新 session context。

## Interaction Example

```text
main -> agent({background: true, ...})
agent -> {run_id: "run_...", status: "running"}

child -> agent_message({
  action: "send",
  target_run_id: "root:<session>",
  message_type: "question",
  payload: {"text": "Should the fallback preserve legacy metadata?"}
})

main -> agent_message({action: "receive", timeout: 5, limit: 10})
agent_message -> [{source_run_id: "run_...", type: "question", ...}]

main -> agent_message({
  action: "send",
  target_run_id: "run_...",
  message_type: "answer",
  payload: {"text": "Yes, preserve it."}
})

main -> agent_message({action: "wait", target_run_id: "run_...", timeout: 60})
agent_message -> {status: "completed", result: "..."}
```

主 agent 在存在后台子任务时必须继续工作、轮询 `receive`，或调用 `wait`。首期 gateway 不会在主 turn 已 finalize 后自动恢复 graph。

## Implementation Plan

### Task 1 — Gateway core

Files:

- Create `src/voidx/agent/gateway/__init__.py`.
- Create `src/voidx/agent/gateway/models.py`.
- Create `src/voidx/agent/gateway/gateway.py`.
- Create `src/tests/test_agent/gateway/test_gateway.py`.

Steps:

- [ ] 先测试 root/run 注册、父子 send/receive、timeout 和路由拒绝，运行并确认 RED。
- [ ] 实现最小 run registry 和有界 inbox，使消息测试 GREEN。
- [ ] 增加 completed/failed/cancelled、唯一终态和 completion wait 测试，确认 RED。
- [ ] 实现 task wrapper、结果保存、异常捕获、取消和 session cleanup，使测试 GREEN。

Verification:

```bash
./test.py --backend -- src/tests/test_agent/gateway/test_gateway.py -v
```

Expected: gateway 测试全部通过，无 pending task 或 unhandled task exception 警告。

### Task 2 — Communication tool

Files:

- Create `src/voidx/tools/agent_message.py`.
- Create `src/tests/test_tools/test_agent_message.py`.
- Modify `src/voidx/tools/registry.py`.

Steps:

- [ ] 为五个 action、gateway 缺失和无效参数编写失败测试。
- [ ] 实现 Pydantic input、ToolResult 映射和 registry 注册。
- [ ] 为跨 session、sibling 和未知 run 拒绝编写测试并实现统一 gateway 校验。

Verification:

```bash
./test.py --backend -- src/tests/test_tools/test_agent_message.py -v
```

Expected: 所有 action 返回结构化 metadata，越权请求明确失败。

### Task 3 — Background AgentTool

Files:

- Modify `src/voidx/tools/agent.py`.
- Modify `src/tests/test_tools/test_interactive_tools.py`.

Steps:

- [ ] 测试 `background` 默认值保持同步 runner await 行为。
- [ ] 测试 `background=true` 立即返回 run id，确认 RED。
- [ ] 注入 gateway 并实现 spawn 分支；保留现有校验、permission snapshot 和同步异常处理。
- [ ] 测试后台 runner 的结果和异常可由 gateway 查询。

Verification:

```bash
./test.py --backend -- src/tests/test_tools/test_interactive_tools.py -k agent -v
```

Expected: 现有 agent tests 与新增后台 tests 全部通过。

### Task 4 — Graph and context integration

Files:

- Modify `src/voidx/tools/base.py`.
- Modify `src/voidx/agent/graph/wiring.py`.
- Modify `src/voidx/agent/graph/core/voidx_graph.py`.
- Modify `src/voidx/agent/graph/tool_executor/executor.py`.
- Modify `src/voidx/agent/graph/subagent.py`.
- Modify `src/tests/test_agent/graph/test_subagent_runner.py`.
- Modify `src/tests/test_agent/graph/test_run_loop_startup.py`.

Steps:

- [ ] 测试主 context 获得 root identity、子 context 获得 child identity，确认 RED。
- [ ] 添加 `ToolContext` 字段并贯穿 runner 链路。
- [ ] 测试 lifecycle event 和 subagent JSONL 使用 gateway run id。
- [ ] 测试 clear/resume 取消旧 session 后台 task，确认 RED 后实现 cleanup。
- [ ] 确认过滤 child tools 时保留 `agent_message`。

Verification:

```bash
./test.py --backend -- \
  src/tests/test_agent/graph/test_subagent_runner.py \
  src/tests/test_agent/graph/test_run_loop_startup.py -v
```

Expected: context、lifecycle、persistence 和 cleanup 测试全部通过。

### Task 5 — Regression

Focused regression:

```bash
./test.py --backend -- \
  src/tests/test_agent/gateway \
  src/tests/test_tools/test_agent_message.py \
  src/tests/test_tools/test_interactive_tools.py \
  src/tests/test_agent/graph/test_subagent_runner.py \
  src/tests/test_agent/graph/test_subagent_persistence.py -v
```

Full backend regression:

```bash
./test.py --backend
```

Expected: 命令退出码为 0；不得出现 pending task、跨 session 消息或现有同步 agent 行为回归。

## Acceptance Criteria

- `agent` 不传 `background` 时行为和返回结构保持兼容。
- `agent(background=true)` 在 child 完成前返回唯一 `run_id`。
- 一个 root 可以同时拥有多个 running child。
- parent 和 child 可在 child 运行期间互发消息。
- sibling、跨 session 和非父子目标不可通信或取消。
- parent 可等待、查询结果和取消直属 child。
- completed、failed、cancelled 只发生一次，且 parent 可可靠观察。
- clear/resume 后旧 session 不留存 running task 或可访问消息。
- 子 agent 无委派权限时仍能调用 `agent_message`。
- 聚焦测试和完整 backend 测试通过。

## Forbidden Changes

- 不把 `AgentGateway` 实现为进程级单例。
- 不将 gateway 状态混入 `TaskTracker` 作为双重事实源。
- 不默认把所有 `agent` 调用改为后台模式。
- 不允许工具自行绕过 gateway 做路由授权。
- 不在首期引入自动 graph resume、跨进程恢复或 sibling broadcast。
- 不回退或覆盖工作区中与本设计无关的未提交修改。

## Risks and Follow-ups

- **主 agent 忘记等待**：工具描述必须提示后台 run 的后续操作；未来可在 finalize 前检测 running child 并给出 guidance。
- **上下文并发**：后台 runner 仍共享 graph 的部分服务；接入时应确认 thread execution state、权限和 UI capture 不依赖可变的主 turn 字段。
- **消息堆积**：有界 inbox 和 payload 限制必须在首期实现。
- **重启丢失**：后续可将 message/lifecycle event 追加到 `memory/subagents.py`，但恢复 task 需要单独设计。
- **自动唤醒**：第二期可把 gateway arrival event 接入 session scheduler；在此之前不得声称支持 push/resume。
- **结构化终止结果**：可与 `docs/design/subagent-report-protocol.md` 协同，gateway 的 `result` 最终升级为结构化 report，而不是长期只保存 `str`。
