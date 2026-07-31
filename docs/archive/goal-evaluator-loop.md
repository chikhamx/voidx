> **Status: Done** — Goal/Loop runtime implemented and refactored (P0+P1+P2); naming conventions supersede this design doc.

---
name: goal-runtime
display_name: Goal Runtime & Tool-Based Evaluation
description: 将 goal 作为与 coding、chat、loop 同级的 runtime 模式，通过 evaluator 专属 goal 工具提交验收决策
doc_type: tech-design
audience: human+llm
---

# Goal Runtime & Tool-Based Evaluation

## TL;DR

`goal` 是与 `coding`、`chat`、`loop` 同级的 runtime 模式。用户通过 `/goal` 创建一个带 objective、acceptance condition 和可选 method 的 Goal Run。每个 attempt 先执行一个普通工作 turn；工作 turn 结束后，runtime 追加稳定的 evaluator guidance，并在同一上下文中运行 evaluator。Evaluator 可以调用必要的只读/验收工具来验证复杂条件，但只有 evaluator 可以执行 Goal 专属控制工具 `goal`。Evaluator 必须最终调用 `goal(status="finished" | "continue" | "blocked")` 来提交本轮验收判断。

Goal 复用 dispatcher、attempt、outbox、ThreadStore、lifecycle、权限和恢复机制，但不复用 `LoopSpec`，也不在服务层写同步 `while`。逻辑上的循环由 durable `wakeup` 实现：`goal(status="continue")` 经 commit 生成下一轮 wakeup；`goal(status="finished")` 结束整个 Goal Run；没有 `goal` 决策时禁止自动续跑。

## Design Principles

- **一等模式**：Goal 有自己的 `GOAL_PROFILE`、`GoalSpec`、`GoalService`、thread family、runner 和 protocol tool。
- **共享编排，隔离领域**：Goal 与 Loop 共享 runtime orchestration，不共享 spec、状态字段、命令语义或 runner 条件分支。
- **工具化验收**：Evaluator 不返回自由 schema 作为终止判断；它通过 `goal` protocol tool 提交结构化决策。
- **同上下文判断**：Evaluator 复用本次工作 turn 的消息和工具结果上下文，避免另起 evidence 摘要导致裁剪或漏证据。
- **单一续跑路径**：只有 committed `RuntimeDecision(outcome="continue")` 才能创建一个 durable `wakeup`。
- **安全优先**：工作 turn 和 evaluator 验收工具调用都经过现有 sandbox、permission 和 safety authorization；`goal` 控制工具额外受 evaluator-only 执行门控保护。
- **不可变目标契约**：objective、acceptance condition 和 method 在启动后不可被 evaluator 或下一轮 prompt 改写。
- **有限且可恢复**：attempt budget、missing decision、no-progress 和 blocked 状态都从 committed state 派生。

## Current Codebase Alignment

### Existing Runtime Profiles

| Mode | Profile / Service | Responsibility |
|---|---|---|
| coding | `CodingService` / coding profile | 普通 coding turn。 |
| chat | `ChatService` / chat profile | 隔离的 chat turn 与工具范围。 |
| loop | `LoopService` / `LOOP_PROFILE` | runtime-backed 周期性 prompt 执行。 |
| goal | `GoalService` / `GOAL_PROFILE` | 带验收条件的有限自主任务。 |

共享执行边界：

- `src/voidx/agent/runtime/dispatcher.py`：claim outbox、begin attempt、调用 runner、commit decision。
- `src/voidx/agent/domain/thread.py`：`RuntimeDecision`、lifecycle transition 和 typed metadata。
- `src/voidx/memory/thread_store.py`：thread state、attempt、outbox 和原子 commit。
- `src/voidx/agent/runtime/recovery.py`：进程重启后的 attempt/outbox recovery。
- `src/voidx/agent/infrastructure/langgraph/runtime/graph_protocol.py`：根据 profile protocol 注入 `turn` / `loop` / `goal` 控制工具。

两条正交的“模式”轴，命名上不要混用：

- `InteractionMode`（`AUTO` / `PLAN`，见 `src/voidx/agent/application/runtime_context.py`）是**编辑权限轴**：只影响 coding turn 里写工具（write/replace/manage/bash）是否被阻塞，存于 session runtime state。
- `RuntimeProfile`（`coding` / `chat` / `loop` / `goal`）是**协议轴**：决定 prompt 注入、工具视图、thread 命名空间和持久化隔离。

一个会话同时属于两个轴的某个取值；例如 PLAN 模式下的 coding profile 会话只读，但 goal/loop/chat 的自治 turn 由各自的 profile 工具视图约束，与 `InteractionMode` 无关。

### Goal Naming Conflict

旧的 `InteractionMode.GOAL` 表示 coding turn 内部 workflow 路由偏好，不应继续作为用户可见的 Goal Runtime。新的规则：

- `goal` 在 runtime/profile/命令层只表示 `GOAL_PROFILE`。
- `/goal` 不路由到旧 `InteractionMode.GOAL` 或 `goal_resolver`。
- `coding`、`chat`、`loop`、`goal` 的 profile id、thread prefix 和 persisted state 不交叉读取。

### Loop Naming Conflict

代码库中 `loop` 一词有三种含义，阅读时按上下文区分，**不做重命名**（涉及持久化字段、UI 协议和提示词，改名风险大于收益）：

- `/loop` 自治模式：`LOOP_PROFILE`、`LoopService`、`agent/loop/`、`LoopRuntimeScheduler` 的调度循环。
- LLM 调用重试循环：`src/voidx/agent/infrastructure/langgraph/runtime/core/loop.py` 的 `LlmLoopState` / `handle_llm_exception`。
- 调度器 wakeup 轮询：`WakeupPumpMixin._pump_loop`（`src/voidx/agent/runtime/pump.py`），goal 与 loop 调度器共用。

规则：新代码避免引入第四种 `loop` 含义；提到调度轮询时优先使用 `pump` / `wakeup` 词（如 `_dispatch_next_wakeup`），提到 LLM 重试时使用 `retry` 词。

## Goals / Non-Goals

### Goals

- `/goal` 创建并启动带 objective 和 acceptance condition 的 Goal Run。
- 每个 attempt 执行一个普通工作 turn，再运行 evaluator 决策 turn。
- Evaluator 阶段复用同一上下文，可绑定必要验收工具，并且只有 evaluator 能执行 `goal` protocol tool。
- `goal(status="finished")` 映射为 `RuntimeDecision(outcome="completed")`。
- `goal(status="continue")` 映射为 `RuntimeDecision(outcome="continue")`，commit 后生成单个 wakeup。
- `goal(status="blocked")` 映射为 `RuntimeDecision(outcome="blocked")` 或需要用户时的 waiting/needs-user 语义。
- 没有 goal decision 时 fail closed，不自动 continue。
- `max_attempts` 默认 20，范围 1–200。
- 复用现有权限与安全授权，不为 Goal 合成自动批准。

### Non-Goals

- 不把 Goal 实现为 `LoopSpec.evaluator_enabled` 或其他 Loop 分支。
- 不让 `/loop` 创建或管理 Goal；`/loop` 保持周期循环语义。
- 不依赖自然语言总结判断完成；终止判断必须来自 `goal` 工具调用。
- 不在 `AgentService`、slash handler 或 scheduler 中写同步 outer `while`。
- 不把 evaluator 降级成只能返回简单 schema 的无工具判定；复杂验收允许使用受策略限制的工具。
- 不允许 evaluator 修改 objective、acceptance condition、method、权限状态或 lifecycle。
- 不修改 workflow DAG 来承载 Goal lifecycle。
- 不允许无限 attempts。

## Proposed Architecture

### High-Level Flow

```text
用户: /goal <objective> --accept <acceptance_condition>
  │
  ▼
GoalService.start(parent_thread_id, GoalSpec)
  ├─ validate objective / acceptance_condition / method / max_attempts
  ├─ create goal:<parent>:<generation> thread/session
  ├─ persist immutable GoalSpec + initial GoalState
  └─ enqueue one kind="goal_prompt" outbox
  │
  ▼
RuntimeDispatcher
  ├─ claim outbox
  ├─ begin attempt with state version / fencing token
  ├─ GoalRuntimeRunner.run_turn(...)
  └─ ThreadStore.commit_decision(...)
       ├─ completed / blocked / needs_user / failed / cancelled -> no wakeup
       └─ continue -> exactly one durable wakeup
  │
  ▼
GoalRuntimeRunner
  ├─ reconstruct GoalSpec + GoalState from input frame / committed context
  ├─ run one normal work turn with GOAL_PROFILE work context
  ├─ if work turn stops for user/safety/permission -> needs_user or blocked
  ├─ run evaluator phase with same attempt context + evaluator guidance
  ├─ evaluator must call goal(status=finished|continue|blocked)
  └─ map GoalController.final_decision() to RuntimeDecision
```

There is no synchronous service-level loop. The durable sequence is:

```text
goal_prompt / wakeup -> one attempt -> goal decision -> commit -> optional wakeup
```

### Runtime Profile and Services

```text
GOAL_PROFILE = RuntimeProfile(
    profile_id="goal",
    revision=1,
    name="Goal",
    protocol="goal",
)

GoalSpec
├── objective: str
├── acceptance_condition: str
├── achievement_method: str = ""
├── max_attempts: int = 20       # 1..200
├── workflow_enabled: bool = False
└── generation: str              # fresh per run

GoalService
├── start(parent_thread_id, spec) -> GoalStatus
├── stop(parent_thread_id) -> bool
├── resume(parent_thread_id) -> GoalStatus | None
└── status(parent_thread_id) -> GoalStatus | None
```

`GoalService` owns setup, thread identity, session creation, replacement, resume and status. It never executes tools, calls evaluator directly, or writes GoalState outside the runtime commit path.

`GoalRuntimeRunner` owns one attempt boundary. It runs the work turn, then the evaluator phase, and returns exactly one `RuntimeDecision` plus a typed GoalState patch. It never enqueues outbox items or calls `ThreadStore.save_state()` directly.

## Goal Protocol Tool

Goal mode has a dedicated protocol tool named `goal`. It is analogous to `turn` and `loop`: it is lifecycle control, not a normal external tool.

For prefix-cache stability, the `goal` tool definition may be present in Goal-mode tool bindings across phases. It is executable only during the evaluator phase. Calls from work phase, setup, slash handling, recovery or any non-evaluator layer must be no-ops that return guidance such as "goal decisions are evaluator-only" and must not mutate state or submit a decision.

### Tool Schema

```text
goal
├── status: "finished" | "continue" | "blocked"
├── summary: str                 # required, concise durable summary
├── evidence: str = ""           # required for finished; observed proof, not just intent
├── next: str = ""               # required for continue when useful
└── reason: str = ""             # required for blocked when useful
```

### Status Mapping

| `goal.status` | Runtime decision | Meaning |
|---|---|---|
| `finished` | `completed` | Acceptance condition is satisfied. End the Goal Run. |
| `continue` | `continue` | Acceptance condition is not satisfied. Commit progress and schedule one wakeup if budget remains. |
| `blocked` | `blocked` / `needs_user` | Cannot proceed safely or productively without user/external resolution. |

### Example Calls

```json
{
  "status": "finished",
  "summary": "已向 IM集团发送 100 条测试消息。",
  "evidence": "100 次 typex.send_message 调用全部返回 ok:true，recipient_name 均为 IM集团。"
}
```

```json
{
  "status": "continue",
  "summary": "已发送 12 条，还未达到 100 条。",
  "evidence": "已观察到 12 次 typex.send_message ok:true。",
  "next": "继续发送直到累计 100 条成功。"
}
```

```json
{
  "status": "blocked",
  "summary": "TypeX MCP 返回权限错误，无法继续发送。",
  "evidence": "send_message 返回 permission_denied。",
  "reason": "permission_denied"
}
```

## Goal State

Persist under `AgentThreadState.context["goal_run"]`:

```text
GoalState
├── run_id: str
├── objective: str                  # immutable
├── acceptance_condition: str       # immutable
├── achievement_method: str         # immutable after start; may be empty
├── max_attempts: int
├── attempt_count: int
├── last_goal_status: str           # finished | continue | blocked | ""
├── last_goal_summary: str
├── last_goal_evidence: str
├── last_goal_next: str
├── repeated_progress_count: int
├── blocked_reason: str
└── active: bool
```

`ThreadStore.commit_decision()` applies lifecycle transition and GoalState patch atomically. The runner may propose a bounded patch, but may not mutate objective, acceptance condition, method, thread identity, permission state or lifecycle directly.

## Setup and Command Semantics

Bare `/goal` starts interactive setup where supported. Required fields:

1. `objective`: 用户希望完成什么。
2. `acceptance_condition`: 什么证据证明已完成。

Optional fields:

- `achievement_method`: 推进策略，可为空。
- `max_attempts`: 默认 20，范围 1–200。
- `workflow_enabled`: 是否允许 workflow tools during the work turn。

Headless mode prints required fields and example syntax. Invalid or cancelled setup performs no state write and does not stop an active Goal Run. A valid replacement stops the current Goal only after the new setup validates.

| Command | Meaning |
|---|---|
| `/goal` | Guided Goal setup. |
| `/goal <objective> --accept <condition>` | Start a Goal Run explicitly. |
| `/goal stop` | Stop active Goal through `GoalService.stop()`. |
| `/goal status` | Show objective, acceptance condition, attempt budget, lifecycle and last goal decision summary. |
| `/goal resume` | Resume the latest non-terminal Goal Run. |
| `/loop ...` | Ordinary periodic loop; no Goal metadata or goal tool. |

## Work and Evaluation Boundary

A Goal attempt has two phases:

1. **Work phase**：runs normal agent work with the Goal objective/method/acceptance guidance and the normal autonomous tool policy. The `goal` tool definition may be bound for cache-friendly consistency, but any work-phase `goal` call returns guidance only and cannot submit a decision.
2. **Evaluation phase**：runs evaluator over the same attempt context. The evaluator may use policy-approved verification tools for complex acceptance checks, and is the only phase allowed to execute the `goal` control tool.

The evaluator phase is not a separate evidence-summary LLM call. It reuses the turn context and appends stable guidance:

```text
Goal evaluation.

Objective:
{objective}

Acceptance condition:
{acceptance_condition}

Review the completed work in this context. Judge only observed evidence from the context.
If the acceptance condition is satisfied, call goal(status="finished", summary=..., evidence=...).
If it is not satisfied, call goal(status="continue", summary=..., evidence=..., next=...).
If safe progress is blocked, call goal(status="blocked", summary=..., evidence=..., reason=...).
Use verification tools only when needed to judge the acceptance condition. Do not perform new goal work. Do not answer with plain text only; end by calling goal(...).
```

Evaluator constraints:

- It can inspect the existing conversation/tool-result context supplied to the model.
- It may call policy-approved verification tools when the acceptance condition requires fresh or structured validation.
- It must not perform new work toward the objective; verification tools are for checking, not continuing execution.
- Only evaluator may execute the `goal` control tool; all non-evaluator `goal` calls are guidance-only no-ops.
- Its decision is invalid if it mutates or restates the acceptance condition as a new target.
- Missing `goal` decision after bounded repair is `blocked(missing_goal_decision)`, not `continue`.

## Goal Controller

`GoalController` is attached to evaluator `TurnExecutionContext`:

```text
TurnExecutionContext
└── goal_controller: GoalController | None

GoalController
├── spec: GoalSpec
├── state: GoalState
├── submit_decision(args) -> RuntimeDecision
└── final_decision() -> RuntimeDecision | None
```

`GoalTool.execute()` is phase-gated. When `ctx.goal_controller` marks the current phase as evaluator, it validates args and calls `ctx.goal_controller.submit_decision(...)`. When the controller is absent or the phase is not evaluator, it returns guidance only and must not submit, persist or mutate anything. The controller stores one final decision for the evaluator phase. Tool calls after the final goal decision are stripped or ignored like loop commit behavior, so the evaluator cannot keep the turn alive after committing.

## Decision Priority

After the work phase and evaluator phase:

1. cancellation or lifecycle cancelling -> `stop(cancelled)`;
2. work phase needs input, permission or safety confirmation -> `needs_user`;
3. work phase fails before evaluable state -> `failed(turn_failed)`;
4. evaluator calls `goal(status="finished")` -> `completed`;
5. evaluator calls `goal(status="blocked")` -> `blocked` / `needs_user`;
6. evaluator calls `goal(status="continue")` and budget remains -> `continue`;
7. evaluator calls `continue` on the last allowed attempt -> `blocked(budget_exhausted)`;
8. evaluator does not call `goal` after repair -> `blocked(missing_goal_decision)`;
9. evaluator/protocol errors at threshold -> `failed(evaluator_unavailable)`.

Completion has priority over budget exhaustion on the last allowed attempt.

## Continuation and Recovery

`GoalRuntimeRunner` accepts both initial `goal_prompt` frames and committed `wakeup` frames. For wakeup it reconstructs the next work guidance from immutable GoalSpec, committed GoalState and the previous goal decision. It must fail closed with `missing_goal_state` or `invalid_goal_input` instead of inventing a prompt.

Only `ThreadStore.commit_decision()` creates the next `kind="wakeup"` outbox. A committed wakeup remains durable if the process crashes before dispatch; recovery claims it using existing outbox/attempt fencing rules. Completed, blocked, failed, cancelled and needs-user decisions do not create wakeups, and stale wakeups for terminal goal threads must be skipped or acknowledged without dispatching another work attempt.

## Tool and Permission Semantics

Work phase tool visibility is controlled by `GoalToolView`:

- cache-stable lifecycle binding: `goal` may be present but is guidance-only outside evaluator;
- read-only inspection: `read`, `find`, `search`, `lsp`, `document`;
- workspace actions: `bash`, `write`, `replace`, `manage`, `lsp_format`;
- verification and coordination: `websearch`, `webfetch`, `mcp`, `skill`, `todo`, `task_status`;
- `workflow` only when `workflow_enabled=True`.

Evaluator phase tool visibility is verification-scoped:

- bind executable `goal` plus only policy-approved verification tools needed for acceptance checks;
- never bind `turn`, `loop`, `clarify`, `checkpoint`, user-input, OAuth or approval tools;
- workspace/shell/web/MCP access is allowed only when selected by evaluator policy as verification, and still passes existing permission/sandbox checks;
- protocol repair may remind evaluator to call `goal`, but may not grant tools outside evaluator policy.

Visible work-phase and evaluator verification tool calls still flow through existing permission authorization. ASK/BLOCKED/DENY and unavailable safety confirmation fail closed to `needs_user` or blocked; Goal must never synthesize approval.

## Implementation Phases

1. **Goal tool contract**：add `src/voidx/tools/goal.py` with `GoalTool`, `GoalDecisionInput` and phase-gated execution.
2. **Goal controller**：add `src/voidx/agent/goal/controller.py` to translate evaluator `goal` calls into `RuntimeDecision`.
3. **Protocol integration**：replace placeholder `GoalProtocol` with cache-stable `goal` tool definition, evaluator-only `goal` execution, decision-missing detection and repair prompt.
4. **Context wiring**：extend `TurnExecutionContext` and `ToolContext` plumbing with `goal_controller` and evaluator-phase gating.
5. **Runner refactor**：make `GoalRuntimeRunner` run work phase, then evaluator phase, then consume `GoalController.final_decision()`.
6. **State/commit patch**：persist last goal decision fields through `DecisionMetadata.goal_state_patch` inside `ThreadStore.commit_decision()`.
7. **Continuation/recovery**：ensure only continue decisions create wakeups and terminal stale wakeups are not dispatched.
8. **Slash/status integration**：show last goal decision summary/evidence in `/goal status`.
9. **Legacy migration**：keep `/loop` semantics unchanged and prevent `/goal` from routing into old `InteractionMode.GOAL`.
10. **Verification**：focused Goal protocol/controller/runner tests, Loop regression and full backend suite.

## File Plan

| Path | Status | Responsibility |
|---|---|---|
| `src/voidx/tools/goal.py` | new | Goal protocol tool schema, phase-gated execution and guidance-only fallback outside evaluator. |
| `src/voidx/agent/goal/controller.py` | new | Store evaluator-submitted goal decision for one attempt. |
| `src/voidx/agent/domain/goal.py` | update | GoalSpec, GoalState, GOAL_PROFILE and GoalToolView. |
| `src/voidx/agent/goal/runner.py` | update | Work phase, evaluator phase and decision mapping. |
| `src/voidx/agent/infrastructure/langgraph/runtime/graph_protocol.py` | update | Real GoalProtocol with cache-stable `goal` definition, evaluator-only `goal` execution and missing-decision repair. |
| `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py` | update | Route `goal` through `GoalTool`; execute only with evaluator-phase controller, otherwise return guidance. |
| `src/voidx/agent/domain/turn_context.py` | update | Add `goal_controller`. |
| `src/voidx/agent/domain/thread.py` | update | Ensure RuntimeDecision metadata supports GoalState patch. |
| `src/voidx/memory/thread_store.py` | update | Atomic GoalState patch and single wakeup creation. |
| `src/voidx/agent/application/goal_service.py` | update | Status/stop/resume over goal thread family. |
| `src/voidx/agent/slash/commands/mode.py` | update | `/goal` setup/status/stop/resume; no legacy InteractionMode route. |
| `src/tests/test_agent/goal/` | update/new | Tool, controller, protocol, runner, scheduler and service tests. |
| `src/tests/test_tools/test_goal_tool.py` | new | Goal tool validation and controller submission. |
| `src/tests/test_agent/loop/` | existing | Loop remains ordinary periodic execution. |

## Forbidden Changes

- Do not put Goal fields in `LoopSpec`.
- Do not add `evaluator_enabled` branches to Loop runner/service.
- Do not create a synchronous outer loop outside dispatcher/outbox/lifecycle.
- Do not let Goal and Loop share thread prefixes, persisted sessions or mutable state.
- Do not let evaluator call tools other than `goal`.
- Do not let work phase, setup, slash handling, recovery or other non-evaluator layers execute `goal` decisions; they may return guidance only.
- Do not derive completion from plain text, assistant claims alone, or UI rendering.
- Do not auto-continue when evaluator fails to call `goal`.
- Do not write GoalState directly from GoalRuntimeRunner during an active attempt.
- Do not mutate objective, acceptance condition or method after run start.
- Do not synthesize permission, safety or user approval.
- Do not route `/goal` into old `InteractionMode.GOAL`.
- Do not change workflow DAG semantics to represent Goal lifecycle.
- Do not allow infinite attempts.

## Edge Cases

| Case | Expected behavior |
|---|---|
| `/goal` interactive | Prompt for objective and acceptance condition, then optional method/budget. |
| `/goal` headless | Print fields and examples; never block forever. |
| invalid or cancelled setup | No state write; active Goal continues. |
| valid setup while active | Replace only after validation succeeds. |
| missing acceptance condition | Reject before creating thread/outbox. |
| work phase needs user/permission/safety | Return `needs_user` or blocked; skip evaluator phase. |
| evaluator calls `finished` on last attempt | Complete, not budget-blocked. |
| evaluator calls `continue` on last attempt | Block with `budget_exhausted`. |
| evaluator does not call `goal` | Bounded repair, then `blocked(missing_goal_decision)`. |
| work phase calls `goal` | Return guidance that only evaluator can submit Goal decisions; do not persist or end the run. |
| non-evaluator service calls `goal` | Return guidance/no-op; do not submit decision or mutate GoalState. |
| evaluator calls non-goal tool | Reject/ignore, repair once, then block if no valid goal decision. |
| malformed goal args | Tool returns validation error; repair once; no automatic continue. |
| repeated no-progress continue | Block with `no_progress` when configured threshold is hit. |
| crash after continue commit | Durable wakeup is recovery source. |
| stale wakeup after terminal decision | Ack/skip without dispatching another work attempt. |
| `/loop` while Goal active | Manage/start Loop according to Loop policy; no Goal state mutation. |
| `/goal stop` while Loop active | Stop Goal only; Loop unchanged. |
| old legacy goal state | Migrate explicitly or report unsupported legacy state; never reinterpret silently. |

## Test Plan

| Area | Command | Covers |
|---|---|---|
| Goal tool | `./test.py --backend -- src/tests/test_tools/test_goal_tool.py` | schema, required fields, controller submission and invalid args. |
| Goal protocol | `./test.py --backend -- src/tests/test_agent/goal/test_goal_protocol.py` | `goal` tool injection, missing-decision repair, evaluator verification tools and non-evaluator `goal` no-op. |
| Goal controller | `./test.py --backend -- src/tests/test_agent/goal/test_goal_controller.py` | status-to-decision mapping and one final decision per attempt. |
| Goal runner | `./test.py --backend -- src/tests/test_agent/goal/test_goal_runner.py` | work phase, evaluator phase, decision priority and no auto-continue on missing decision. |
| Goal service | `./test.py --backend -- src/tests/test_agent/goal/test_goal_service.py` | start/stop/resume/status, isolated sessions and persistence. |
| Commit/recovery | `./test.py --backend -- src/tests/test_agent/runtime/test_thread_store.py src/tests/test_agent/runtime/test_dispatcher.py -k goal` | atomic patch, durable wakeup, terminal stale wakeup skip. |
| Goal commands | `./test.py --backend -- src/tests/test_agent/slash/test_slash_goal.py` | setup/status/stop/resume and no legacy InteractionMode routing. |
| Loop regression | `./test.py --backend -- src/tests/test_agent/domain/test_loop_domain.py src/tests/test_agent/test_loop_service.py src/tests/test_agent/slash/test_slash_loop.py` | Loop remains evaluator-free and periodic. |
| Full backend | `./test.py --backend` | cross-module integration. |

## Acceptance Criteria

- `goal` appears as a first-class runtime mode alongside `coding`, `chat` and `loop`.
- `goal` tool definition may be cache-stably bound outside evaluator, but execution outside evaluator is guidance-only and has no side effects.
- Goal mode has a dedicated `goal` protocol tool; it does not use `turn` or `loop` for evaluator decisions.
- Evaluator phase reuses the work attempt context, may bind policy-approved verification tools, and is the only phase with executable `goal`.
- Evaluator completion is expressed only by `goal(status="finished")`.
- Evaluator continuation is expressed only by `goal(status="continue")`.
- Missing or malformed goal decision does not create a wakeup.
- Runtime commit atomically persists lifecycle decision, GoalState patch and optional continuation wakeup.
- Continuation creates exactly one durable wakeup; recovery can dispatch it after a crash.
- Terminal decisions do not dispatch stale wakeups.
- `/goal` no longer routes through old coding `InteractionMode.GOAL`.
- `LoopSpec` contains no Goal fields and ordinary `/loop` behavior remains evaluator-free.
- Existing Loop and workflow behavior has focused regression coverage.

## Definition of Done

A complete implementation supports this sequence:

1. User submits `/goal <objective> --accept <condition>`.
2. `GoalService` creates `goal:<parent>:<generation>` with persisted immutable GoalSpec and initial GoalState.
3. Dispatcher claims `goal_prompt` and starts one Goal attempt.
4. Goal runner executes one normal work turn.
5. Goal runner starts evaluator phase with same attempt context, stable guidance, policy-approved verification tools and executable `goal` control tool.
6. Evaluator calls `goal(status="finished" | "continue" | "blocked")`.
7. Goal controller converts the tool call to `RuntimeDecision` plus GoalState patch.
8. ThreadStore atomically commits the decision and creates one wakeup only for `continue`.
9. The run reaches completed, blocked, needs_user, failed or cancelled without an outer synchronous loop.
10. A process restart can recover a committed wakeup without duplicating an attempt or changing the immutable goal contract.

## Open Decisions

- Whether the work phase uses `GOAL_PROFILE` with normal `turn` protocol internally, or a phase-specific protocol selector that exposes normal turn control for work and executable `goal` only for evaluation.
- Whether `blocked` should map to `blocked` or `needs_user` based on an explicit `reason` enum.
- Whether repeated no-progress detection uses evaluator-provided `next`/`summary` similarity or a separate progress key.
- Whether old `InteractionMode.GOAL` is removed in one release or migrated through a temporary alias with a deprecation window.
