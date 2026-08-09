# Subagent Inbox Pressure — Interim Simplification

> **Status: Done** — Archived on 2026-08-10.

Date: 2026-08-08
Revised: 2026-08-10

> 当前阶段不建设子 agent progress 流。完整删除 `progress` 消息类型，将 gateway
> inbox 默认容量从 100 提高到 256，并保证终态 result 不会因队列已满而把已完成的
> child run 变成 failed。

## 1. Problem

长时间运行的 review/inspect child 曾以以下错误结束：

```text
AgentGatewayError("Inbox is full")
```

当前 gateway 为每个 run 创建固定容量队列，默认 100：

```python
class InProcessSubagentGateway:
    def __init__(self, *, inbox_capacity: int = 100, ...): ...
```

子 agent 调用 `message(send, message_type="progress")` 时，消息进入父 run 的
inbox。父 agent 的 `wait()` 只等待 child 的 `done` event，不消费父 inbox；当
progress 持续积累时，下一次普通 `send()` 会在 `_put_message()` 抛错。异常冒出
child runner 后，gateway 将 child 标为 failed。

更严重的是，普通 `result` 当前也先进入同一队列，再把发送者标为 completed。
若队列恰好已满，`result` 的 enqueue 会先抛错，已经完成工作的 child 最终被标为
failed，父 agent 无法取得权威结果。

## 2. Decisions

1. 默认 `inbox_capacity` 从 100 提高到 256。
2. 从消息协议中完整删除 `progress`，不保留历史反序列化兼容。
3. child `message` 工具不再暴露或接受 `message_type="progress"`。
4. gateway 同样拒绝 `progress`，防止绕过工具 schema 直接发送。
5. `AgentRun.result` 是 `agent_control(wait)` 的权威结果。
6. `result` 和 lifecycle 是终态优先消息；队列压力不能把已完成 child 改成 failed。
7. 暂不实现 progress 分级、coalescing、bounded back-pressure 或 wait 期间 drain。
8. 256 是阶段性容量缓解，不是最终消息背压架构。

## 3. Goals

- 长时间 child 不再因自然语言 progress 消息填满父 inbox。
- 最多 256 条待处理消息能为并发 result/lifecycle 和少量 question/answer 留出余量。
- child 已产出的最终结果始终可以通过 `agent_control(wait)` 获取。
- 删除无实际消费方的协议面，避免为 progress 设计暂时不需要的 UI 和背压系统。
- 类型、tool schema、gateway runtime validation 和测试保持一致。

## 4. Non-Goals

- 不保留或迁移历史 `progress` 消息。
- 不提供子 agent 实时进度流或 milestone UI。
- 不增加 progress level、coalescing、sampling 或 drop policy。
- 不修改 `agent_control` 的 64/128/256 秒有限等待档位。
- 不让 `wait()` 负责消费 requester inbox。
- 不改变并发 child 数量限制或 `max_payload_bytes=65536`。
- 不将 inbox 容量配置化；本阶段直接修改 gateway 默认值。

## 5. Protocol Removal

### 5.1 Domain Types

修改 `src/voidx/agent/domain/subagent.py`：

```python
UserMessageType = Literal["message", "question", "answer", "result"]

AgentMessageType = Literal[
    "message",
    "question",
    "answer",
    "result",
    "completed",
    "failed",
    "cancelled",
]

USER_MESSAGE_TYPES = frozenset({"message", "question", "answer", "result"})
```

`AgentMessage.model_validate()` 对 `type="progress"` 直接失败；不增加 legacy union、
迁移器或 fallback parser。

这里删除的是 subagent transport 的消息类型，不影响 goal/loop 等其他领域对象中名为
`progress` 的普通业务字段。

### 5.2 Message Tool

修改 `src/voidx/agent/adapters/tools/subagent_message.py`：

```python
message_type: Literal["message", "question", "answer", "result"] = "message"
```

并将 child-side 描述改为：

```text
Send a message, question, answer, or final result to the parent agent,
or receive messages sent to this child run.
```

修改 `subagent.py` 注册 `MessageTool` 时的描述，删除 “or progress”。

Pydantic schema 不再列出 progress。即使模型手工提交 `progress`，输入校验也返回
validation error，不进入 gateway。

### 5.3 Gateway Validation

`InProcessSubagentGateway.send()` 继续以 `USER_MESSAGE_TYPES` 为单一白名单。
调用方绕过工具直接传 `progress` 时返回 `AgentGatewayError`，且不修改 source/target
run 状态。

## 6. Capacity and Terminal Priority

### 6.1 Capacity

修改：

```python
class InProcessSubagentGateway:
    def __init__(self, *, inbox_capacity: int = 256, max_payload_bytes: int = 65536): ...
```

root 和 child inbox 均沿用同一个容量值。测试仍可显式传入 1、2 等小容量验证边界。

### 6.2 Message Classes

队列写入分两类：

| 类别 | 类型 | 队列满时 |
| --- | --- | --- |
| 普通 | `message`、`question`、`answer` | 保持当前行为，抛 `AgentGatewayError("Inbox is full")` |
| 终态优先 | `result`、`completed`、`failed`、`cancelled` | 淘汰最旧队列项后写入，不向 child runner 抛 inbox-full |

`result` 虽然属于 user message，但语义上会关闭 sender run，因此使用终态优先写入。

### 6.3 Authoritative Result Ordering

`send(message_type="result")` 必须遵守：

1. 校验 route、source/target open 状态和 payload。
2. 将 payload 固化到 sender 的 `AgentRun.result` 并把 sender 标为 completed。
3. 设置 sender `done` event，使 `wait()` 可立即取得权威结果。
4. best-effort 将 `result` 通知写入父 inbox；满时按终态优先规则淘汰最旧项。
5. 发送一次 `completed` lifecycle 通知。

关键不变量：步骤 2/3 不能因为步骤 4 的队列状态回滚或转为 failed。

实现可在 `send()` 内调用专用 `_finish_with_result()`，或调整 `_finish()` 接收 result
payload；不得继续依赖“先普通 enqueue result，成功后再 finish”的顺序。

### 6.4 Duplicate Terminal Notifications

当前显式 `result` 会产生 `result` 和 `completed` 两条通知。暂时保留该行为以减少
调用方变更，但父层不能依赖两条都存在：

- `AgentRun.status/result` 是权威状态。
- inbox 通知只用于唤醒或展示。
- 极小容量下，后写入的 lifecycle 可以淘汰较早的 result 通知。
- `terminal_sent` 继续保证 lifecycle 最多一次。

未来若统一为单一 terminal envelope，应另立设计，不在本阶段顺带修改。

### 6.5 Terminal Run and Runner Task Lifecycle

`result` 固化 run 终态不代表承载 child runner 的 asyncio task 已退出。task 生命周期
必须与 `AgentRun.status` 分开管理：

- `cancel()` 对 completed 但 task 未结束的 run 仍回收 task，但不得覆盖 completed/result。
- `close_session()` 和 `close_all()` 按 `task.done()` 取消所有未结束 task，不能按 run
  是否终态跳过。
- task 取消等待使用 `_CANCEL_ACK_TIMEOUT` 的总时限；runner 吞掉 cancellation 时返回
  `AgentGatewayError(reason="cancel_timeout")`，不能无限阻塞 session 切换或应用关闭。
- close 超时时不删除 run records，调用方释放阻塞后可以重试 teardown。

## 7. Interaction with Agent Convergence

预算收敛后的 child 最终通过以下任一路径关闭：

- 模型自然最终答案，由 `run_subagent().report_result()` 发送 result；
- child 主动调用 `message(result)`；
- step/time/context 硬收敛后的无工具 final call；
- provider overflow/error recovery 的 partial result。

所有路径最终都必须把结果固化到 `AgentRun.result`。inbox 满只能影响通知保留，不能
改变 child 的 completed/failed 判定。

预算检测和无原因 guidance 见：

`docs/archive/subagent-budget-convergence-2026-08-09.md`

## 8. Files

| 文件 | 变更 |
| --- | --- |
| `src/voidx/agent/domain/subagent.py` | 从 user/agent message 类型和白名单删除 progress |
| `src/voidx/agent/ports/subagent.py` | 自动跟随收窄后的 `UserMessageType` contract |
| `src/voidx/agent/adapters/tools/subagent_message.py` | 删除 progress schema 和描述 |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | child message 工具描述删除 progress |
| `src/voidx/agent/adapters/subagent/inprocess_gateway.py` | 默认容量 256；result 先固化；终态优先 enqueue；有限 task 回收 |
| `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py` | progress 删除、容量/终态压力与 post-result task 生命周期测试 |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py` | 删除 progress batch 用例或改为普通 message follow-up |
| `src/tests/test_agent/adapters/tools/test_subagent_message.py` | 新增 message schema/validation 测试（若文件不存在则创建） |

测试中原来只为了填充队列而发送 `progress` 的场景改用 `message`。测试名和断言必须
体现普通消息仍会 overflow、终态消息不会导致 child failed。

## 9. Tests

### 9.1 Focused Commands

```bash
./test.py --backend -- src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py
./test.py --backend -- src/tests/test_agent/adapters/tools/test_subagent_message.py
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_gateway_result.py
./test.py --backend -- src/tests/test_tooling/test_agent_control.py
```

### 9.2 Regression Commands

```bash
./test.py --backend -- src/tests/test_agent -k "subagent or agent_control"
./test.py --backend
```

### 9.3 Required Assertions

- gateway 默认 `_inbox_capacity == 256`。
- root 和 child 创建的 queue `maxsize == 256`。
- `UserMessageType`、`AgentMessageType`、`USER_MESSAGE_TYPES` 和 message tool JSON
  schema 均不包含 `progress`。
- `AgentMessage(type="progress")` 校验失败；没有 legacy compatibility path。
- message tool 发送 `progress` 返回 validation error，gateway 未收到消息。
- 直接调用 gateway 发送 `progress` 返回 `AgentGatewayError`。
- 普通 `message` 在容量 1 的满队列上仍抛 inbox-full。
- 容量 1 的满队列上发送 `result` 后，child 为 completed，`wait()` 返回完整 payload。
- lifecycle 在满队列上仍可写入且每个 child 最多发送一次。
- 51 个 child 各发送 result、父层只 wait 不 receive 时，不再出现第 51 个 child 因
  100 条通知导致 `Inbox is full`；默认 256 容量下全部 run 均 completed。
- 128 个并发 child 的 result/lifecycle 压力场景中，结果均能从 `AgentRun.result`
  取得，即使通知发生淘汰。
- result 后仍挂起的 runner 可由 `cancel()`、`close_session()` 和 `close_all()` 回收，
  且 completed/result 不被覆盖。
- runner 吞掉 cancellation 时，cancel/close 在有限时间内返回 `cancel_timeout`，不无限等待。
- route、open state、payload 校验顺序稳定，前置错误不会被超大 payload 覆盖。

## 10. Risks and Follow-ups

- 256 只延后普通 message/question/answer 的队列上限；若未来恢复高频流式消息，仍需
  bounded back-pressure、coalescing 或独立 event stream。
- 没有 progress 后，父 agent 只能通过 child terminal state、UI 自身的 subagent step
  events和最终结果观察工作；这是当前阶段接受的取舍。
- 终态优先会淘汰最旧普通消息。由于终态结果由 `AgentRun.result` 持有，正确性优先于
  保留全部普通通知。
- 不兼容历史 progress payload 是明确决策；如果未来出现持久化 transport 消息，再
  单独设计版本化迁移。
- 后续完整 inbox 架构应评估单一 terminal envelope、按 run 的结果存储和 UI event
  stream，避免继续依赖共享父 inbox 承载所有语义。


## 11. Verification Record

2026-08-10：

- inbox/message schema/gateway/result/lifecycle 聚焦测试：PASS。
- post-result `cancel()`、`close_session()`、`close_all()`、`cancel_timeout` 与
  `self_reap` 生命周期测试：PASS。
- 终态队列压力重复测试：`20/20 PASS`。
- runtime、subagent gateway、agent-control 与配置回归：PASS。
- `./test.py --backend`：`4565 passed, 30 skipped, 7 warnings`。
- `git diff --check`、关键模块 compileall 与 LSP diagnostics：PASS。
- 独立静态复核 PASS：确认 task 生命周期与 run 终态分离、权威 result 不被覆盖、
  有限取消等待、self-reap 稳定拒绝及 teardown 可重试语义均无剩余问题。
