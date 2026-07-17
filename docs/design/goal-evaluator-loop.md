---
name: goal-evaluator-loop
display_name: Goal Evaluator & Autonomous Loop
description: 为 voidx goal 模式增加独立评估调用和可取消的自主循环
doc_type: tech-design
audience: human+llm
---

# Goal Evaluator & Autonomous Loop — 技术设计文档

## TL;DR

voidx 的 goal 模式目前只做“设定目标 + 路由到 plan workflow”，缺少完成判定和自主循环。本设计为带完成条件的 `/goal <desc> when <condition>` 增加立即启动、独立评估调用、有限轮次循环和统一取消机制：每轮 `run_once()` 返回只读 `TurnResult`，评估器根据该轮结束时的有效上下文和 workflow 状态判断目标是否达成；未达成时自动继续，达成、预算耗尽、连续评估失败、无进展或用户取消时停止。无 `when` 的 `/goal <desc>` 保持现有行为。

## Context

### 当前行为

voidx goal 模式（`/goal <desc>`）的执行路径：

1. `src/voidx/agent/slash/handler.py:_goal()` 调用 `task_state.set_goal(desc)` 并切换到 `InteractionMode.GOAL`。
2. slash 命令被 `src/voidx/agent/graph/run_loop.py:_handle_user_input()` 消费后直接返回，不会启动 turn。
3. 用户下一条普通输入触发 `turn_runner.py:run_once()`；goal 模式调用纯函数 `resolve_goal_mode()`。
4. `resolve_goal_mode()` 返回 `PlanResolution(join="plan", leave=None)`，进入 plan workflow。
5. turn 结束后等待下一次用户输入，没有完成判定或自动继续。

`src/voidx/agent/goal_resolver.py:resolve_goal_for_turn()` 是未接入当前 goal-mode 路径的 LLM resolver。

### 问题

- **无完成判定**：没有机制判断目标是否已有可验证证据。
- **无自主循环**：每个 turn 结束后仍需用户手动继续。
- **slash 与 turn 控制流分离**：仅扩展 `_goal()` 无法让命令立即启动首轮。
- **缺少评估输入边界**：当前 `_run_once()` 返回 `None`，其最终 messages 是 `turn_runner.py` 内部局部值。
- **缺少运行中控制通道**：同步连续调用 `_run_once()` 会阻塞后续 `/goal clear` 的命令分发。

## Goals / Non-Goals

### Goals

- 支持 `/goal <desc> when <condition>`，设置成功后立即以 `desc` 启动首轮。
- 每轮结束后通过独立评估调用检查可验证证据。
- 带 condition 的 goal 命令作为本次 run 的显式自主授权，审批类 workflow gate 无需再次询问用户；循环在停止条件前自主运行。
- 提供 1–200 的有限轮次预算，默认 20。
- Ctrl+C、UI Cancel、`/goal clear` 和新 `/goal` 均可终止活动循环。
- 循环状态与轮次计数可持久化，进程异常退出后不会误判为仍在执行。
- 保持无 `when` 的 `/goal <desc>` 行为兼容。

### Non-Goals

- 不做 Codex 风格的文件化里程碑。
- 评估器不执行工具或验证命令；执行模型负责运行命令并留下证据。
- 不做 token 预算控制。
- 不改变 plan/brainstorm/debug 等 workflow 的 DAG 和 transition 规则。
- 不做多目标编排。
- 不保证使用不同供应商或不同底层模型；“独立”指独立调用、prompt、结构化输出和失败处理。

## Proposed Design

### 核心控制流

```text
用户: /goal <desc> when <condition>
  │
  ▼
运行中命令控制层（gateway/TUI adapter）
  │  若当前 session 正在执行且输入是 /goal clear、/goal reset 或语法有效的新 /goal：
  │  先取消并等待当前 submit task，再把原命令作为新的 submit 正常分发
  ▼
run_loop._handle_user_input()
  │  slash handler 解析并持久化 GoalSpec
  │  返回 GoalCommandResult(start_loop=True, initial_text=desc)
  ▼
GoalLoopController.run()  ← 当前 submit task 等待它，不创建脱离 submit 的后台任务
  │
  ├─ 开始一次 attempt：先递增并持久化 goal_turn_count
  ▼
turn_runner.run_once(desc) -> TurnResult  ←────────────────────┐
  │                                                            │
  ▼                                                            │
goal_evaluator.evaluate(turn_result, condition)                 │
  │                                                            │
  ├─ achieved ───────────────→ Goal achieved，停止并清理状态    │
  ├─ budget exhausted ───────→ Goal budget exhausted，停止      │
  ├─ evaluator failures >= 3 → Evaluator unavailable，停止      │
  ├─ no progress >= 3 ───────→ No progress detected，停止        │
  └─ not achieved ───────────→ 下一 attempt(continue_text) ─────┘

取消源：Ctrl+C / UI Cancel / goal 控制命令 / session cleanup
  └─ 取消当前 submit task（覆盖 turn 或 evaluator await）→ finally 清理活动状态
```

### 启动语义

1. `_goal()` 只负责解析、校验、更新状态和返回结构化命令结果，不直接递归调用 `_run_once()`。
2. `_handle_user_input()` 收到 `start_loop=True` 后在当前 submit task 内等待 `GoalLoopController.run()`；controller 不创建脱离 submit 生命周期的后台任务。
3. 带 `when` 的命令使用解析后的 `desc` 作为首轮 `user_text`，不把完整 slash 命令写入 agent 消息历史。
4. 无 `when` 的命令只设置 goal 和 goal mode，返回 `start_loop=False`，保持现有行为。带 condition 的命令在写入 GoalSpec 前要求执行模型可用；若 `host.model is None`，显示配置模型的错误并保持现有目标和模式不变。
5. 无活动 submit 时，所有 `/goal` 仍走现有 slash dispatch。活动 submit 期间，gateway `RunManager.submit()` 和 `GatewayFrontend`/`PureTui` 的入队入口只特殊识别语法有效的 `/goal clear`、`/goal reset` 和新 `/goal`：先 cancel-and-wait，再把未修改的命令重新入队并交给正常 slash dispatch。语法校验复用 slash parser 的只读解析函数，不在 UI 层复制语法。
6. 活动 submit 期间，所有可能改变 session、模型或持久化状态的 slash 命令（至少 `/clear`、`/session`、`/resume`、`/model`、`/mode`、`/goal`、`/exit`）都不得与 loop 并发：退出/切换/清理类命令使用 cancel-and-wait 后正常分发；其他 slash 命令排队或返回 busy。纯只读命令可并发执行，但不得持久化状态。普通文本保持现有排队或 `ERR_TURN_IN_PROGRESS` 行为。

### 取消与并发语义

`GoalLoopController` 是 session 级运行时组件，但 loop 由当前 submit task 拥有；同一 session 最多有一个活动 loop。controller 保存 cancellation event 和 run id，不保存脱离 submit 的后台 task。

- **Ctrl+C / UI Cancel**：取消当前 submit task，同时设置 controller event。取消可发生在 `_run_once()` 或 evaluator await 中；`CancelledError` 由 controller 的 `finally` 清理状态后重新抛给已有中断处理。
- **`/goal clear` / `/goal reset`**：运行中命令控制层先校验命令，再取消并等待当前 submit；旧 loop 的 `finally` 完成后，将原命令作为新 submit 分发，由 `_goal()` 执行 `clear_goal()`、切换 AUTO 并持久化。
- **新 `/goal`**：使用同一 cancel-and-resubmit 操作；旧状态清理完成后才由 slash handler 校验并替换 GoalSpec。无效命令不得取消当前 loop。
- **session cleanup**：取消并等待当前 submit task，防止退出后继续调用模型或写消息。
- **竞态保护**：loop 捕获启动时的 `goal_run_id`；每轮开始、评估后和持久化前都确认 run id 仍匹配。旧 controller 不得修改新目标状态。
- **取消检查点**：每轮开始前、`run_once()` 返回后、评估器返回后检查 cancellation event；task cancellation 本身可立即中断 turn 或 evaluator await，不依赖检查点或 10 秒 timeout。

`goal_loop_active` 是可持久化的观测字段，不是取消原语。数据库恢复时若其值为 `True`，内存中必须归一化为 `False` 并立即回写；系统不自动恢复无人监管的循环，用户可重新执行目标命令。

以 `/goal fix auth tests when all tests in tests/test_auth pass` 为例：

1. slash parser 解析出 `desc="fix auth tests"`、`condition="all tests in tests/test_auth pass"`。
2. 创建 `GoalSpec(done_condition=..., max_turns=20, goal_turn_count=0, goal_run_id=<uuid>)`，切换 GOAL 并持久化。
3. `_handle_user_input()` 进入 loop；controller 将 `goal_loop_active=True` 并持久化。
4. controller 校验 run id 和剩余预算，将 `goal_turn_count += 1` 持久化后，启动首轮 `run_once("fix auth tests")`。
5. `run_once()` 完成消息、workflow/task state 和 runtime state 持久化后，返回 `TurnResult`。
6. evaluator 只读取 `TurnResult.evaluation_messages`、`task_state.workflow_runs` 和 condition。
7. 若未达成且仍有预算，下一轮固定使用 `continue working on: <desc>`，并在启动前再次预增计数。
8. 若执行模型调用 checkpoint 审批工具，tool context 中的 goal autonomous grant 将其解析为结构化 `approved_by="goal"`，无需 UI 交互；workflow 按现有 transition 继续。
9. 若执行模型调用 clarify 等信息收集工具，goal grant 不生成答案；controller 输出 `Goal needs user input` 并停止当前 loop，避免伪造需求。用户补充信息后需以新 `/goal ... when ...` 启动新的 run id。
10. 任一停止条件触发后输出原因；`finally` 将仍属于该 run id 的 `goal_loop_active=False` 持久化。

轮次定义为一次已获预算并开始执行的 `run_once()` attempt。计数在调用前按 run id 原子地递增并持久化，因此 completed、failed 和 cancelled attempt 都计入预算，崩溃恢复后也不会回退。取消仍会沿用现有行为回滚未完成 turn 的消息；轮次计数记录的是已消耗的执行机会，不代表已保留一轮 transcript。checkpoint 自动批准不额外消耗轮次；clarify 停止发生在已启动的 attempt 内，因此该 attempt 正常计数。

## API / Function Contracts

### GoalSpec

```text
GoalSpec (src/voidx/runtime/task_state.py)
├── desc: str (existing, normalized, max 120 chars)
├── done_condition: str | None (new, normalized, max 2000 chars)
├── max_turns: int (new, default 20, ge=1, le=200)
├── goal_turn_count: int (new, default 0, ge=0)
├── goal_run_id: str (new, default ""; loop goals use UUID)
└── model_config = {"extra": "ignore"} (existing, retain)
```

- 空 `done_condition` 归一化为 `None`。
- 不支持 `max_turns <= 0` 或无限预算。
- `/goal clear` 和新 `/goal` 都通过替换 GoalSpec 重置计数。
- 旧数据缺少新增字段时由默认值兼容反序列化。

### Goal Loop State / Persistence

```text
TaskState
├── ... existing fields
└── goal_loop_active: bool (new, default False)
```

运行时 controller 另持有 cancellation event 和 run id；这些对象不可序列化，也不进入 `TaskState`。`goal_loop_active` 必须进入 `session_runtime_state` 的持久化读写：为表增加非空、默认 false 的列和 migration，并在 `save_session_runtime_state()` / `load_task_state_with_session_time()` 中显式映射。恢复出的 true 先归一化为 false，再由 session 初始化路径回写数据库。

GoalSpec 新字段继续随 `current_goal_json` 保存，不新增独立列。session 的所有 runtime-state 写（包括 `turn_runner` 的现有 `_persist_runtime_state()`、slash handler、controller 和恢复回写）必须进入同一个 session-scoped `RuntimeStateWriter`。writer 在单一 asyncio lock 下串行化写入，并把每次底层 `asyncio.to_thread()` future 记录为可等待操作；调用方被取消时使用 `asyncio.shield()` 等待已启动写完成后再传播 `CancelledError`。

loop 启动后，writer 为该 submit 绑定 `goal_run_id`。任何可能写 `current_goal_json` 或 `goal_loop_active` 的旧 run 请求都在同一数据库事务内执行 run-id CAS，并检查 affected row；不得采用“内存检查后无条件整行 upsert”。不属于 loop 的新 slash submit 只有在 cancel-and-wait 已等待 writer 空闲后才能写入。因此 cancel-and-wait 的完成条件是：旧 submit 已退出、其 writer pending future 为空、旧 run 的 active 清理 CAS 已完成。该条件覆盖 `run_once()` 内部发起的 runtime-state 写，旧快照不能在新 GoalSpec 后落库。

### Workflow Gate Semantics

带 condition 的 `/goal` 是用户对该 `goal_run_id` 的显式自主执行授权。授权只覆盖“是否按已形成的方案继续执行”这类 approval gate，不代表用户提供未知需求，也不允许模型替用户选择产品偏好。

```text
ToolContext
└── autonomous_goal_grant: GoalAutonomousGrant | None

GoalAutonomousGrant
├── goal_run_id: str
├── condition: str
└── approved_scope: Literal["workflow_execution"]
```

controller 启动每个 attempt 时将 grant 注入 tool context；run id 失效、loop 停止或普通非-loop submit 时该字段为空。`checkpoint` 等纯 approval 工具在 grant 有效时不调用 UI，而是返回现有结构化 approved decision，并标记 `approved_by="goal"`；workflow service 仍按现有 evidence、gate 和 transition 规则处理，因此不修改 DAG。

`clarify`、权限确认、安全确认及任何要求用户补充事实或偏好的交互不接受该 grant，也不得合成回答。统一返回/抛出内部 `GoalNeedsUserInput(reason, source)` 信号。tool executor 将该信号转换为 terminal `_ExecutedTool`，在 graph state 写入 `goal_stop_reason="needs_user_input"` 和 `goal_stop_detail`、设置 `should_continue=False`，并把当前 batch 尚未开始的工具标记为 skipped；交互类工具作为 barrier 串行执行，因此其后的写工具不得启动。`TurnResult` 透传 stop reason/detail，controller 在 evaluator 前检查并停止 loop、清理 active 状态。用户补充信息后必须显式提交新的 `/goal ... when ...`，创建新 run id。这样 condition goal 在已知范围内无需人工审批，同时不会越权猜测需求或绕过安全边界。

模型在 attempt 间变为不可用时，controller 在预增计数前停止并提示配置模型，不消耗新轮次；配置完成后同样由用户显式重发 `/goal`，不保留隐式后台恢复状态。

### TurnResult / Evaluation Context

```text
TurnResult (src/voidx/agent/graph/turn_runner.py)
├── evaluation_messages: tuple[BaseMessage, ...]
├── task_state: TaskState
├── goal_stop_reason: "" | "needs_user_input"
└── goal_stop_detail: str
```

`GraphTurnRunner.run_once()` 和代理 `_run_once()` 仅在成功完成时从返回 `None` 改为返回 `TurnResult`。取消和普通异常继续按现有契约回滚/发事件并重新抛出，不构造 `TurnResult`，不调用 evaluator；controller 依靠调用前已持久化的计数记录该 attempt，并让异常沿现有上层路径结束 loop。

`evaluation_messages` 精确定义为**本轮最后一次成功执行模型调用的输入 `llm_messages`，再追加该调用返回的 `assistant_msg`**。最后一次调用的输入已经包含该 turn 先前的 assistant/tool 轮次以及当时实际生效的 compaction、runtime context 和 guidance，因此不使用 graph 最终 messages 猜测模型所见上下文。

实现上，LLM node 在每次 `_stream_llm()` 成功后立即对 `llm_messages` 和 `assistant_msg` 做深拷贝（例如逐条 `model_copy(deep=True)`），保存到仅供本次 graph invocation 使用的 state 字段；后续成功调用覆盖前值。不得只做 `tuple(llm_messages)` 浅拷贝，因为 turn 尾部 prune/compaction 可能原地修改共享 message。`turn_runner` 从最终 graph state 读取该不可变快照，拼成 tuple，并在现有消息和 runtime-state 持久化完成后返回。快照不写入 transcript 或 session runtime state；若 graph 成功结束却没有任何成功模型调用，则视为内部契约错误并按现有失败路径抛出。

- 不从 UI transcript tree 或 `final["messages"]` 反向推导模型输入。
- evaluator 不修改 messages 或 TaskState。
- evaluator 看见的是执行模型最后一次真实上下文及其回答；工具证据必须已出现在最后一次调用输入中。

### Evaluator

```text
GoalEvalResult (src/voidx/agent/goal_evaluator.py)
├── achieved: bool
├── reason: str (max 500 chars; 必须引用证据或说明缺失证据)
├── progress_key: str (max 200 chars; 对进展状态的稳定摘要)
└── next_hint: str (max 200 chars; 可为空，仅作为上下文提示)
```

```python
evaluate(
    *,
    turn_result: TurnResult,
    condition: str,
    model: BaseChatModel,
    config: ModelConfig,
) -> GoalEvalResult
```

Evaluator 调用规则：

- 使用独立 system prompt 和 `with_structured_output(GoalEvalResult)`。
- 通过 `create_resolver_model()` 关闭或最小化 reasoning，再显式复制模型配置为 `temperature=0`、`max_tokens=512`；不得假定 `create_resolver_model()` 已设置这两个值。
- timeout 为 10 秒；无工具绑定，不暴露工具 schema。
- 超时、调用异常或无效结构不伪装成正常 `not_achieved`，而是返回/抛出内部 `EvaluatorFailure`，由 controller 单独计数。
- evaluator 只能根据 transcript 中已有证据判断；仅有执行模型的完成声明而无验证输出时应判为未达成。

### Slash Parsing

把位于参数开头或左侧有空白、且右侧有空白的大小写不敏感 `when` 视为候选分隔符，并选择最后一个：

```text
/goal <non-empty desc> when <non-empty condition>
```

- 使用等价于 `re.finditer(r"(?i)(?<!\S)when\s+", arg)` 的候选边界，取最后一个 match；desc 为 match 前文本，condition 为 match 后文本，二者分别 trim。
- 起始位置也可成为候选，因此 `/goal when tests pass` 会得到空 desc 并校验失败；这项校验发生在任何状态写入前。
- 选择最后一个允许 desc 中自然出现较早的 `when`；condition 如需包含分隔形式的 `when`，用户应改写条件，本期不支持 quoting/escaping。
- 任一侧 trim 后为空时显示 usage/error，不设置目标、不切换模式、不取消活动 loop。
- `/goal clear` 和 `/goal reset` 仅在整个 trim 后参数精确匹配时视为控制命令。
- `when` 无单词左边界或右侧空白（如 `somewherewhenready`、`fix when`）不作为分隔符，按普通 desc 处理。

### Command Result

```text
GoalCommandResult
├── handled: bool
├── start_loop: bool
└── initial_text: str
```

`slash/handler._goal()` 返回该结果；slash dispatch 将结果传给 `_handle_user_input()`。若现有通用 dispatch 只能返回 bool，应增加最小的结构化 command outcome，而不是让 handler 直接调用 graph turn。

## Stop Conditions

停止条件按以下优先级检查：

1. cancellation event 已设置、task 被取消或 run id 已失效；
2. tool 返回 `GoalNeedsUserInput`：输出明确原因并停止，不调用 evaluator、不自动恢复；
3. `run_once()` 抛出普通异常：沿现有失败路径结束 loop，不调用 evaluator；
4. evaluator 返回 `achieved=True`；
5. `goal_turn_count >= max_turns`；
6. evaluator 连续失败达到 3 次；
7. 连续 3 次成功 evaluator 调用返回相同非空 `progress_key`。

规则：

- evaluator 成功调用会将连续失败计数清零。
- evaluator failure 不更新 progress key，也不参与“无进展”判断；未达到阈值且仍有预算时继续下一 attempt。
- `not_achieved` 但 progress key 变化时，无进展计数重置。
- 达成优先于预算耗尽：最后一个预算轮次若已达成，输出 achieved，而不是 exhausted。
- turn 普通异常与 evaluator failure 不同：前者已有 `TurnFailed` 事件并立即结束当前 submit，后者是独立评估服务故障，可在预算内重试。
- task cancellation 可中断 evaluator 调用；timeout 只负责 evaluator 自身超时，不是取消延迟上限。
- `next_hint` 可附加到固定 continue 文本后，但不得替代目标描述或修改 condition。

## Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| 带 condition 的命令立即启动 | 等下一条普通消息 | `/goal` 本身已包含首轮任务，避免设置后无执行 |
| session 级可取消 controller | `_handle_user_input()` 内简单 while | 明确 task 所有权，支持 UI cancel、替换目标和 cleanup |
| `run_once()` 返回 TurnResult | 解析 UI transcript 或读取内部局部值 | 建立稳定、只读、可测试的评估边界 |
| 同一底层模型的独立调用 | 强制不同供应商/模型 | 保持部署兼容，同时隔离 prompt、配置和判断职责 |
| 仅有限轮次预算 | `<=0` 表示无限 | 防止误判或服务异常造成无限成本 |
| evaluator failure 独立计数 | 当作普通 not-achieved | 避免服务故障被误判为无进展 |
| 最后一个 `when` 分隔 | 第一个分隔或复杂 quoting | 规则简单，并允许目标描述包含 `when` |
| 恢复时不自动续跑 | 启动后台恢复 | 避免进程重启后在无用户监督下执行和写入 |

## Invariants

- `done_condition is None` 时绝不触发自主循环。
- 同一 session 同时最多一个 goal loop task。
- `goal_turn_count` 单调递增，且不超过 `max_turns`。
- evaluator 不执行工具、命令或文件读取。
- controller 每次写状态前校验 `goal_run_id`。
- 所有退出路径都通过 `finally` 清理 `goal_loop_active`。
- 恢复持久化状态时将残留的 `goal_loop_active=True` 归一化为 False。
- 不修改 `resolve_goal_mode()` 的 `PlanResolution(join="plan", leave=None)`。
- 不修改 workflow DAG、transition 规则或 `InteractionMode` 枚举值。

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|---|---|---|
| 每轮额外 LLM 调用 | 增加延迟和成本 | 512 tokens、无 reasoning、10 秒 timeout |
| 假阳性提前停止 | 目标未真正达成 | 要求引用命令/测试等 transcript 证据 |
| 假阴性持续循环 | 浪费轮次 | 有限预算和无进展检测 |
| evaluator 服务异常 | 无法判断完成 | 连续失败独立计数，3 次后停止并明确报错 |
| 上下文压缩丢失证据 | 早期证据不可见 | 评估与执行使用相同有效上下文；执行模型应在后续轮次重跑关键验证 |
| 取消与新目标竞态 | 旧 loop 污染新状态 | cancel-and-wait + goal_run_id 检查 |
| 自动消息污染用户历史 | intent 或 UI 误认为用户输入 | 标记内部 continuation 来源；UI 可展示但不作为真实用户命令 |

## Implementation Notes for LLM

### Files / Entry Points

| Path | Expected Change |
|---|---|
| `src/voidx/runtime/task_state.py` | 扩展 `GoalSpec` 和 `TaskState.goal_loop_active`，补 normalization/validation |
| `src/voidx/memory/store.py` | 增加 `goal_loop_active` schema 列和 migration |
| `src/voidx/memory/runtime_state.py` | 增加 session-scoped `RuntimeStateWriter`、统一串行写、取消等待与事务性 run-id CAS |
| `src/voidx/tools/base.py`、`checkpoint.py`、`clarify.py`、`src/voidx/permission/service.py`、`src/voidx/agent/graph/permissions.py` 与 tool executor | 注入 grant；approval 自动批准；信息/权限/安全交互 fail closed 为 terminal `GoalNeedsUserInput`，截断后续工具 |
| `src/voidx/agent/goal_evaluator.py` | 新建 evaluator、结构化结果和 failure 分类 |
| `src/voidx/agent/slash/handler.py` | 提供可复用只读 parser，解析 `when` 并返回结构化 command outcome |
| `src/voidx/agent/graph/core/llm.py` 与 graph state 定义 | 暴露本次 invocation 最后一次成功模型调用的输入/输出快照 |
| `src/voidx/agent/graph/turn_runner.py` | 成功时返回 `TurnResult`；保持取消和失败异常契约 |
| `src/voidx/agent/graph/turn_mixin.py` | 透传 `TurnResult` 返回值 |
| `src/voidx/agent/graph/run_loop.py` | 在当前 submit 内运行 GoalLoopController、立即启动并处理停止条件 |
| `src/voidx/agent/graph/contracts.py` | 更新 host 方法返回类型和 controller 所需接口 |
| `src/voidx/ui/gateway/run_manager.py`、`src/voidx/ui/gateway/frontend.py` | gateway 对有效 goal 控制命令执行 cancel-and-wait-and-resubmit，其他并发输入行为不变 |
| `tui/voidx_cli/app.py`（必要时提取专用 mixin） | `PureTui` 忙碌时对有效 goal 控制命令取消当前 submit，待 `_consume()` 清理后将命令重新入队 |
| `src/voidx/agent/runtime_context.py` | 注入 `Goal loop: turn N/N, condition: ...` |
| `src/voidx/agent/goal_resolver.py` | 单独清理未使用的 `resolve_goal_for_turn()`；不与本功能耦合 |

若 controller 超过约 150 行，应提取为 `src/voidx/agent/graph/goal_loop.py`，让 `run_loop.py` 只负责接线。

### Forbidden Changes

- 不改变 `resolve_goal_mode()` 的返回值。
- 不修改 workflow DAG 或 transition 规则。
- 不在 evaluator 中绑定或执行工具。
- 不改变 `InteractionMode` 枚举值。
- 不允许无限 `max_turns`。
- 不从 UI 渲染文本解析 evaluator 输入。
- 不移除 `GoalSpec.model_config = {"extra": "ignore"}`。
- 成功路径仅增加 `TurnResult` 返回；取消和普通异常仍重新抛出并保持现有消息回滚及事件语义。

## Edge Cases / Failure Paths

| Case | Expected Behavior |
|---|---|
| `/goal fix auth` | 设置目标，无自主循环 |
| `/goal fix auth when tests pass` | 设置后立即以 `fix auth` 启动 |
| `/goal fix when flaky when tests pass` | desc=`fix when flaky`，condition=`tests pass` |
| `/goal fix when` | 当作无 condition 的普通 desc，因为没有右侧空白分隔内容 |
| `/goal when tests pass` | 校验失败，不改变已有目标 |
| condition 归一化后为空 | 校验失败，不启动 |
| 带 condition 但 `host.model is None` | 写状态前拒绝，提示配置模型；已有 goal/mode 不变 |
| 第一轮 achieved | 清理 active 状态并停止 |
| 最后预算轮 achieved | 输出 achieved |
| 预算耗尽且未达成 | 输出 `Goal budget exhausted (N/N turns)` |
| evaluator 单次失败 | 继续下一轮，failure count +1 |
| evaluator 连续 3 次失败 | 输出 evaluator unavailable 并停止 |
| 相同 progress key 连续 3 次 | 输出 no progress detected 并停止 |
| evaluator failure 夹在进展之间 | 不参与 no-progress 计数 |
| Ctrl+C / UI Cancel | 取消当前 turn，finally 清理 active 状态 |
| `/goal clear` during loop | cancel-and-wait 后清 goal，切 AUTO |
| 新 `/goal` during loop | cancel-and-wait 后启动新 run id |
| 进程在 active 状态崩溃 | 恢复时 active=False，不自动续跑 |

## Test Plan

所有测试均通过项目入口 `./test.py` 运行。

| Scope | Path / Command | Expected Result |
|---|---|---|
| GoalSpec 校验 | `./test.py --backend -- src/tests/test_agent/test_task_state.py -k "goal"` | 字段约束和旧 GoalSpec JSON 兼容 |
| runtime-state writer/schema/CAS | `./test.py --backend -- src/tests/test_agent/graph/test_session_runtime_state.py -k "goal_loop or stale_run or cancelled_write"` | 所有写串行；active 恢复回写；旧 run 不能覆盖新 goal；取消等待 `run_once` 已启动的线程写 |
| workflow/交互授权 | `./test.py --backend -- src/tests/test_tools/test_checkpoint.py src/tests/test_tools/test_clarify.py src/tests/test_permission/ -k "goal"` | grant 自动批准 checkpoint；clarify/权限/安全不打开 UI并发出 terminal signal；普通调用不变；失效 run 不授权 |
| slash 解析与 command outcome | `./test.py --backend -- src/tests/test_agent/slash/test_slash_goal.py` | 起始 when、多个 when、空侧、无边界、无模型拒绝和兼容行为正确 |
| LLM context snapshot / TurnResult | `./test.py --backend -- src/tests/test_agent/graph/test_session_run_once.py -k "turn_result or evaluation_context"` | 返回最后一次真实调用输入+输出；成功前完成持久化；失败/取消不返回 result |
| evaluator | `./test.py --backend -- src/tests/test_agent/test_goal_evaluator.py` | achieved/not-achieved、证据要求、timeout、无效结构正确分类 |
| loop 核心 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py` | attempt 前预增；继续、达成、预算、异常、评估失败、无进展、checkpoint 自主授权及 needs-input 停止正确 |
| 取消/工具截断竞态 | `./test.py --backend -- src/tests/test_agent/graph/test_goal_loop.py -k "cancel or clear or replace or stale or needs_user"` | 取消均清理；旧 run 不污染新 goal；交互信号跳过 evaluator；同批后续写工具不启动 |
| gateway 运行中命令适配 | `./test.py --backend -- src/tests/test_ui/gateway/ -k "goal or concurrent_command"` | goal 及 session/model 变更命令不与 loop 并发；无效 goal 不取消；普通输入行为不变 |
| TUI 运行中命令适配 | `./test.py --backend -- tui/tests/test_input_handling.py -k "goal or concurrent_command"` | busy 时状态变更命令取消等待或排队；清理后正确重入队 |
| run loop 接线 | `./test.py --backend -- src/tests/test_agent/graph/test_run_loop_workflow.py -k "goal"` | slash outcome 在当前 submit 内正确运行 controller |
| resolver 回归 | `./test.py --backend -- src/tests/test_agent/test_goal_resolver.py` | goal 路由行为不变 |
| workflow 回归 | `./test.py --backend -- src/tests/test_workflow/` | workflow reconcile 不受影响 |

新增测试文件允许按表中路径创建；现有不存在的测试路径不是实现前置条件。

## Acceptance Criteria

- `/goal <desc> when <condition>` 在一次命令中完成设置与首轮启动。
- evaluator 获得稳定、只读、与执行模型一致的有效上下文，不依赖 UI transcript。
- 未达成时自动继续；checkpoint approval 由本次 goal grant 满足，clarify/权限/安全交互不会被伪造并会停止 loop；达成和最后预算轮优先级符合本文定义。
- Ctrl+C、UI Cancel、clear、替换目标和 session cleanup 都能终止 loop，并最终持久化 `goal_loop_active=False`。
- evaluator failure 不会触发 no-progress；两种停止原因有不同用户提示。
- 无 condition 的旧 goal 行为和现有 workflow 路由保持不变。
- 表中 focused tests 与 backend 回归测试通过。
