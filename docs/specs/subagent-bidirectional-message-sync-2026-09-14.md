# 子 Agent 同步双向通信与自然退出机制技术规格文档

- **日期**：2026-09-14
- **状态**：评审修订版 (Revised after Subagent Independent Review)
- **读者**：架构维护者、Agent 核心开发者与实施代理
- **涉及范围**：
  - `src/voidx/agent/domain/subagent.py`（新增 `PendingQuestion` 与 `AgentRun.pending_question` 领域模型）
  - `src/voidx/agent/adapters/subagent/inprocess_gateway.py`（问答挂起/唤醒、避免重复投递、等待中断与重置）
  - `src/voidx/agent/adapters/tools/subagent_message.py`（消息工具放开 `question`/`message`、同步等待主回复、默认超时 256s）
  - `src/voidx/agent/adapters/tools/subagent_control.py`（批量 `wait` 改为 `FIRST_COMPLETED` 响应、`needs_reply` 状态）
  - `src/voidx/agent/adapters/tools/plugins.py`（主代理暴露 `MessageTool`）
  - `src/voidx/agent/application/subagent_policy.py`（子代理 Tool Surface 放开普通消息通道）
  - `src/voidx/agent/adapters/langgraph/runtime/subagent.py`（区分 `result` 终态交卷与 `question` 同步提问，兼容自然文本退出）
  - `src/voidx/agent/application/runtime_context.py` & `subagent_status.py`（Current Task State 提示区增强，防指纹抖动）
  - 测试套件：`test_subagent_message_protocol.py`、`test_subagent_result_handoff.py`、`test_agent_control.py`、`test_subagent_gateway_result.py` 等

---

## 1. 背景与问题根因

### 1.1 现状与翻车场景
当前子 Agent（Subagent）与主 Agent（Root/Parent）之间的通信架构存在“半闭环”和“假性双向”缺陷：
1. **工具误导**：子 Agent 的 Tool Surface 中注册了名为 `message` 的工具，但实际被收窄为只能发 `message_type="result"`（交卷退出）。
2. **提前终止导致任务残缺**：
   当 Subagent 遇到技术分歧或需要确认边界时（例如：*“等待边界确认”*），大模型会根据常识调用 `message` 工具试图向主 Agent 发送沟通消息。然而，Runtime 检测到 `message` 工具调用后，立刻判定为终态交卷并调用 `mark_finished("message_result")` 强制关停 Subagent。
3. **主 Agent 资产落空**：
   Subagent 尚未开始写代码、写测试或生成交付产物文件（如 `/tmp/voidx-sdk-s3-init-summary.json`），主 Agent 唤醒后读取文件全部失败。

### 1.2 核心诉求
支持真正的**子 Agent 与主 Agent 同步双向通信（第一版同步机制）**，消除通信与退出的概念混淆：
- **通信归通信**：Subagent 遇到不确定性时，可以通过 `message(action="send", message_type="question", payload=...)` 向主 Agent 发送提问并**同步挂起等待**主 Agent 响应（默认 256s 超时）。
- **主 Agent 可感知并可回复**：
  - 主 Agent 的 `agent_control(wait)` 能被子 Agent 的提问提前唤醒（状态为 `needs_reply`），且在多子 Agent 并行等待时不会被其他未结束的 Agent 阻塞（饥饿避免）。
  - 主 Agent 的 `Current Task State` 区域能清晰显示子 Agent 的待办求助事件。
  - 主 Agent 拥有 `MessageTool` 可定向回复。
- **双通道平滑退出**：
  - **自然退出（推荐）**：Subagent 完成全部工作后，输出自然文本总结（Final Answer），Runtime 自动沉淀为最终交付结果 `run.result`。
  - **保留契约兼容**：保留显式 `message(result)` 作为结构化终态交付通道，调用时网关与 Runtime 协同原子退出，不破坏已有契约。

---

## 2. 总体架构与时序

### 2.1 提问与唤醒交互时序

```text
  [Subagent: Lyra]                                [Gateway]                              [Parent: Root]
         │                                            │                                         │
         │  (执行遇到疑问/边界确认)                    │                                         │
         │ ─────────────────────────────────────────> │                                         │
         │   message.execute(send, question)          │                                         │
         │   [同步阻塞等待响应, timeout=256s]           │  (记录 PendingQuestion)                 │
         │   (内部创建 question_future)               │  (触发 pending_question_event)          │
         │                                            │ ──────────────────────────────────────> │
         │                                            │   agent_control(wait) 提前返回:          │
         │                                            │   status="needs_reply",                 │
         │                                            │   pending_message="..."                 │
         │                                            │                                         │
         │                                            │   [Parent LLM 决策]                     │
         │                                            │   (Current Task State 提示待回复事件)   │
         │                                            │ <────────────────────────────────────── │
         │                                            │   message.execute(send, answer,         │
         │                                            │                   target_run_id="Lyra") │
         │                                            │                                         │
         │ <───────────────────────────────────────── │                                         │
         │   question_future.set_result(answer)       │  (只唤醒 Future, 不二次写入 inbox)     │
         │   message 工具返回主 Agent 回复内容         │  (清除 PendingQuestion 与 event)        │
         │                                            │                                         │
         │  (拿到确认，继续执行 red/green/verify)      │                                         │
         │  [写测试 -> 写代码 -> 跑回归]              │                                         │
         │                                            │                                         │
         │  (所有工作就绪，自然文本总结输出)           │                                         │
         │ ─────────────────────────────────────────> │                                         │
         │   subagent 循环结束 (Final Answer)         │                                         │
         │   Gateway 记录 status="completed"          │                                         │
         │                                            │ <────────────────────────────────────── │
         │                                            │   agent_control(wait) 终态返回           │
         │                                            │   获取 run.result (即自然结束总结)      │
```

---

## 3. 核心子系统与详细设计规范

### 3.1 领域模型扩展 (`src/voidx/agent/domain/subagent.py`)

1. **新增 `PendingQuestion` 数据结构**：
   ```python
   class PendingQuestion(BaseModel):
       question_id: str
       sender_run_id: str
       target_run_id: str
       payload: dict[str, Any]
       created_at: float
       timeout: float = 256.0
   ```
2. **`AgentRun` 增加待决状态字段**：
   ```python
   class AgentRun(BaseModel):
       ...
       pending_question: PendingQuestion | None = None
   ```
   - 使得主 Agent 构建 Context、UI 渲染以及 `AgentControlTool` 检查时能够直接读取权威状态，无需穿透私有属性。

---

### 3.2 网关层改造 (`InProcessSubagentGateway`)

1. **问答 Future 注册与单向投递（防止双重消费）**：
   - 内部维护：
     ```python
     _pending_questions: dict[str, asyncio.Future[AgentMessage]] = {}
     ```
   - 提供 `send_and_wait_response(sender_run_id, target_run_id, payload, timeout=256.0)`：
     - 生成 `question_id = f"q_{uuid.uuid4().hex[:8]}"`。
     - 构造 `PendingQuestion`，更新到 `sender_record.run.pending_question`。
     - 将提问消息作为普通消息推入 `target_record.inbox`。
     - 触发目标 `target_record.pending_question_event.set()`（通知父 wait 唤醒）。
     - 创建 Future 并 `await asyncio.wait_for(future, timeout=timeout)`。
     - **清理时机（原子性）**：无论是成功唤醒、超时还是 Cancelled，在 `finally` 块中立即清空 `sender_record.run.pending_question` 并注销 Future。
   - **响应匹配与单向唤醒**：
     - 当父 Agent 调用 `send(target_run_id=child_run_id, message_type="answer"|"message")` 时：
     - 检查 `_pending_questions` 中是否有挂起的 `target_run_id`：
       - **若存在挂起 Future**：直接 `future.set_result(reply_message)` 解除子 Agent 阻塞。**严禁再次将该回复写入子 Agent inbox**，彻底杜绝下轮循环通过 `receive_parent_messages()` 导致重复消费与上下文污染。
       - **若不存在挂起 Future**：作为普通异步消息推入子 Agent `inbox`，供其主动调用 receive 时拉取。
     - 重置父 Agent 的 `pending_question_event.clear()`（若已无其他子 Agent 待回复）。

2. **单 Run 等待逻辑 (`gateway.wait`)**：
   - 等待两个并发事件：
     ```python
     done_task = asyncio.create_task(target.done.wait())
     question_task = asyncio.create_task(target.pending_question_event.wait())
     ```
   - 若 `question_task` 先到达且 `target.run.pending_question is not None`：
     - 返回带有 `wait_outcome="needs_reply"` 的 `target.run` 快照。

---

### 3.3 主代理控制工具改造 (`src/voidx/agent/adapters/tools/subagent_control.py`)

1. **批量等待饥饿与超时修复（P0 级修复）**：
   - 弃用 `asyncio.gather` 等待全部完毕的旧逻辑。
   - 改用 `asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)`：
     ```python
     # 只要任一子 Agent 触发 needs_reply 或者任一子 Agent 到达 terminal status，立即返回
     done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
     ```
   - 取消剩余未结束的临时 wait 任务（注意：只取消 wait 协程，不影响子 Agent 真实的后台执行任务）。
   - 收集当前各个子 Agent 的即时快照，只要存在 `needs_reply`，批次结果立即返回主 Agent 处理，彻底避免由于某个长任务子 Agent 阻塞而导致提问方 256s 超时。

2. **结果渲染**：
   - 针对 `needs_reply` 状态渲染高优先级指引：
     ```text
     Subagent {name} sent a question and is waiting for your reply:
     "{question_text}"

     Action required: Use message tool to reply to this child agent:
     message(action="send", target_run_id="{run_id}", payload={{"reply": "..."}})
     Or call agent_control(action="wait", run_id="{run_id}") to continue waiting.
     ```

---

### 3.4 消息工具改造 (`src/voidx/agent/adapters/tools/subagent_message.py`)

1. **子 Agent Schema 放开**：
   - 子 Agent 的 `parameters_schema` 开放 `message_type: Literal["question", "message", "result"] = "question"`。
2. **执行分流**：
   - **若 `message_type == "result"`**：保留终态结果交卷语义，直接调用 `gateway.send(message_type="result")`，向后兼容已有逻辑。
   - **若 `message_type in {"question", "message"}`**：
     - 调用 `gateway.send_and_wait_response(..., timeout=timeout or 256.0)`。
     - 收到主 Agent 回复时：
       ```text
       Parent agent responded:
       {json.dumps(reply.payload, ensure_ascii=False)}
       ```
     - 发生超时（256s）：优雅降级返回 Guidance：
       ```text
       Waiting for parent agent response timed out after 256s.
       Guidance: Parent did not reply in time. Please make your best autonomous technical decision based on existing project rules and proceed with your implementation.
       ```

---

### 3.5 子 Agent 执行循环改造 (`src/voidx/agent/adapters/langgraph/runtime/subagent.py`)

1. **精准区分提问与终态交卷**：
   - 检查执行结果：
     ```python
     for item in executed:
         if item["tool_call"].get("name") == "message":
             m_type = (item["tool_call"].get("args") or {}).get("message_type")
             if m_type == "result":
                 # 显式终态退出通道，保持兼容
                 text = str(getattr(item["result"], "output", "") or "")
                 mark_finished("message_result")
                 return text
             # question / message 只是普通工具调用，获取输出后继续循环！
     ```
2. **自然文本交卷 (Final Answer) 完备性**：
   - 在模型未调用任何工具时，进入现有的 Final Answer 提交流程，将总结输出作为 `run.result` 提交给 Gateway，标记 `completed`。

---

### 3.6 Current Task State 与防抖动设计 (`runtime_context.py` & `subagent_status.py`)

1. **状态渲染提示**：
   - 检查 `run.pending_question`：
     ```text
     - Child agents: 1 running · 0 recent terminal
       - Lyra [running · needs reply] (Goal: TDD接通真实goal/loop初始化语义审批并验证)
         ⚠️ Question: "{payload_summary}"
         -> Action required: Use message tool to reply to Lyra.
     ```
2. **保护 `TaskStateReminderPolicy`**：
   - 严禁在 `Current Task State` 文本中包含动态秒级计时（例如 “waiting for 12s”），必须使用确定的静态文本描述，避免状态指纹在每一步发生伪变化破坏 token 缓存与提醒计数基线。

---

### 3.7 主 Agent 工具面注册 (`src/voidx/agent/adapters/tools/plugins.py`)

- 在 `build_agent_plugins` 中，将 `MessageTool` 注册进主 Agent 的工具列表中，使其具备直接调用 `message` 工具给子 Agent 发送回复或补充指示的能力。

---

## 4. 影响评估与测试适配清单

| 测试文件 | 影响与改动 |
|---|---|
| `test_subagent_message_protocol.py` | 更新关于 `result_only` 拒绝 `question` 的负向测试，变更为正向验证 `question` 发送、挂起与恢复；补充 256s 超时降级单测 |
| `test_subagent_tool_surface.py` | 验证子 Agent 的 `message` 工具 schema 包含 `question` 和 `send` |
| `test_subagent_gateway_result.py` | 验证 `send_and_wait_response` 机制以及单向分发去重 |
| `test_agent_control.py` | 补充多 Agent 并行等待时，某一个 Agent 触发 `needs_reply` 能够立即通过 `FIRST_COMPLETED` 返回唤醒的单测 |
| `test_subagent_result_handoff.py` | 保持原有 `message(result)` 兼容路径通过，补充通过自然文本 Final Answer 自动完成并带回 review/implement 结果的用例 |

---

## 5. 验收标准 (Acceptance Criteria)

1. **子 Agent 提问不中断**：子 Agent 调用 `message(question)` 时，进程不被 kill，能够同步等待主 Agent 回复后继续执行后续步骤。
2. **主 Agent 及时感知**：主 Agent `wait` 过程中子 Agent 提问，`wait` 立即提前解除并明确指示待办问题。
3. **多 Agent 零死锁**：多个子 Agent 并行执行时，任一子 Agent 提问不会被其他耗时子 Agent 阻塞造成超时。
4. **自然交卷产物齐全**：子 Agent 完成所有 Todo 后，通过纯文本输出自然结束，测试文件与日志文件完备生成，主 Agent 可顺利读取。
5. **既有测试 100% 绿色**：受影响的协议测试更新完毕，全套 backend 测试顺利通过。
