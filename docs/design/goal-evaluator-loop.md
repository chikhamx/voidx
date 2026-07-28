---
name: goal-evaluator-loop
display_name: Goal Evaluator & Autonomous Loop
description: 为 /loop 增加引导式目标输入、独立验收调用和可取消的自主循环
doc_type: tech-design
audience: human+llm
---

# Goal Evaluator & Autonomous Loop — 技术设计文档

## TL;DR

目标型自主执行应挂在现有 runtime-backed `/loop` 上，而不是新增 `/goal <desc> when <condition>` 入口。用户先输入 `/loop` 进入 setup；系统引导输入两类必填提示词：**目标**、**验收条件**，并可选输入**达成目标的方法**。setup 完成后复用现有 loop runtime 基建启动循环。每次 attempt 先跑一个普通 agent turn，再追加只读 evaluator；通过则 `completed`，未通过则由 runtime lifecycle 继续调度。

## Design Principle

Evaluated goal loops are `/loop` runs with structured setup, not a separate `/goal` runtime.

- **入口统一**：用户通过 `/loop` 进入自主循环；无参数 `/loop` 触发引导式 setup。
- **复用基建**：使用现有 `LOOP_PROFILE`、loop thread、outbox、dispatcher、runner、lifecycle；不新增 `GOAL_PROFILE` / `GoalService` / `GoalRuntimeScheduler`。
- **结构化提示**：setup 收集 objective、acceptance condition，并可选收集 achievement method，再生成 deterministic loop prompt。
- **验收只读**：evaluator 只读最后一次真实 LLM 上下文和 post-turn state，不执行工具、不读文件、不改状态。
- **runtime 收尾**：runner 只返回 `RuntimeDecision`；commit、continuation、恢复和终态仍由 runtime/lifecycle 负责。

## Current Codebase Alignment

### Existing Runtime-Backed Loop

当前 `/loop` 已经是目标功能的承载点：

| Area | Path | Current Role |
|---|---|---|
| loop domain | `src/voidx/agent/domain/loop.py` | `LOOP_PROFILE`、`LoopSpec`、`LoopDecision`、`LoopToolView`。 |
| loop service | `src/voidx/agent/application/loop_service.py` | 创建/重置 loop thread，调用 scheduler。 |
| loop scheduler/runner | `src/voidx/agent/loop/scheduler.py` | `LoopRuntimeScheduler` enqueue outbox；`LoopRuntimeRunner.run_turn()` 调用 runtime turn 并返回 `RuntimeDecision`。 |
| loop attempt controller | `src/voidx/agent/loop/controller.py` | 收集 `loop` 决策并转换为 runtime decision。 |
| runtime dispatcher | `src/voidx/agent/runtime/dispatcher.py` | claim outbox、begin attempt、调用 runner、commit decision。 |
| runtime lifecycle | `src/voidx/agent/domain/thread.py` | `RuntimeDecision` 和 lifecycle transition。 |
| runtime turn engine | `src/voidx/agent/infrastructure/langgraph/adapter.py` | `LangGraphTurnEngine.run()` 执行普通 LangGraph turn 并返回 post-turn runtime state。 |

目标型 evaluator loop 应扩展这条 `/loop` 线，而不是在 `AgentService._handle_user_input()`、slash handler 或新的 goal service 中手写 outer while-loop。

### Current Slash Behavior

| Area | Path | Current Role |
|---|---|---|
| slash `/loop` | `src/voidx/agent/slash/handler.py` | 当前无参数打印 usage；`/loop [interval] <prompt>` 启动 runtime-backed loop；`stop` / `status` 控制活动 loop。 |
| slash `/goal` | `src/voidx/agent/slash/handler.py` | legacy：设置/清除 `TaskState.current_goal`，切 `InteractionMode.GOAL`，持久化。 |
| legacy goal route | `src/voidx/agent/goal_resolver.py` | `resolve_goal_mode()` 固定返回 `PlanResolution(join="plan", leave=None)`。 |
| LLM call site | `src/voidx/agent/infrastructure/langgraph/execution.py` | `_stream_llm()` 成功返回处可捕获最后一次 LLM input/output snapshot。 |
| graph state | `src/voidx/agent/state.py` | 当前没有 evaluation snapshot / loop evaluator stop reason 字段。 |

`/goal` 不参与本设计的 runtime-backed evaluator loop；保留 legacy 行为。

## Goals / Non-Goals

### Goals

- 支持用户先输入 `/loop`，再由系统引导输入必要提示词并启动 evaluated loop。
- 引导式 setup 必须收集 **目标** 和 **验收条件**，可选收集 **达成目标的方法**。
- 复用 `/loop` runtime 基建：`LOOP_PROFILE`、`LoopSpec`、`LoopService`、`LoopRuntimeScheduler`、`LoopRuntimeRunner`、loop thread/outbox state。
- 每个 loop attempt 执行一个普通 LangGraph turn，然后在 turn 结束边界追加 evaluator。
- evaluator 通过已有证据验收 acceptance condition；通过后 loop terminal completed，未通过则返回 `RuntimeDecision(outcome="continue")`。
- continuation 走 runtime/lifecycle 单一路径；完整 wakeup 规则见 **Continuation Semantics**。
- 提供有限预算：`max_turns` 取值 1–200，默认 20。
- Ctrl+C、UI Cancel、`/loop stop`、有效新 `/loop` setup、session cleanup 均能取消或替换活动 evaluated loop。
- 保持 `/loop [interval] <prompt>` 快捷模式兼容；它可以继续启动普通 loop，或在未来通过显式 flag 进入 evaluated mode，但不作为本设计的主要目标体验。

### Non-Goals

- 不新增 `/goal ... when ...` 语法作为 runtime-backed goal 入口。
- 不新增 `GOAL_PROFILE`、`GoalService`、`GoalRuntimeScheduler` 或 goal-specific thread family，除非后续证明 loop 基建无法承载。
- 不把 evaluated loop 实现成脱离 runtime dispatcher 的后台 while-loop。
- 不让 evaluator 执行工具、命令、文件读取或外部验证。
- 不让 continuation 使用 scheduler-created prompt 和 runtime-created `wakeup` 两套并行队列。
- 不修改 workflow DAG、transition 规则或 `InteractionMode` 枚举。
- 不做无限预算、多目标编排或文件化里程碑。
- 不强制 evaluator 使用不同供应商；独立性来自独立 prompt、结构化输出、timeout 和 failure handling。

## Proposed Architecture

### High-Level Flow

```text
用户: /loop
  │
  ▼
SlashHandler._loop(args="")
  │ enter interactive evaluated-loop setup instead of printing usage
  │
  ├─ prompt 1: Objective / 目标
  ├─ optional prompt: Achievement method / 达成目标的方法
  ├─ prompt 2: Acceptance condition / 验收条件
  ├─ optional: max_turns, interval/dynamic mode
  │
  ▼
Build EvaluatedLoopSpec / extended LoopSpec
  │ prompt = deterministic prompt(objective, optional method, acceptance condition)
  │ evaluator_enabled = true
  │
  ▼
LoopService.start(parent_session/thread, spec)
  │ create/reset loop thread with LOOP_PROFILE
  │ persist evaluated-loop metadata in thread.state.context["loop"]
  │ enqueue first loop_prompt outbox only
  │
  ▼
RuntimeDispatcher.dispatch_outbox()
  │ begin attempt + lease + lifecycle guard
  │ input_frame = {"kind": outbox.kind, **outbox.payload}
  │
  ▼
LoopRuntimeRunner.run_turn(thread, profile, input_frame)
  │
  ├─ load committed loop state from ThreadStore
  ├─ if kind=loop_prompt → use payload prompt/spec for first attempt
  ├─ if kind=wakeup → use payload.decision + loop state to build next prompt
  ├─ build TurnExecutionContext(runtime_profile=LOOP_PROFILE, tool_policy=composite loop policy, optional evaluated-loop context adapter)
  ├─ runtime.run_turn(TurnRequest(... user_text=loop prompt ...))
  │    └─ normal LangGraph turn runs until runtime says that turn is complete
  │
  ├─ if turn needs user input / failed / cancelled → RuntimeDecision(needs_user/failed/stop)
  │
  └─ LoopEvaluator.evaluate(post_turn_result, acceptance_condition)
        ├─ achieved=True  → RuntimeDecision(completed)
        ├─ not achieved   → RuntimeDecision(continue, next_delay_seconds=<dynamic/default interval>)
        ├─ budget hit     → RuntimeDecision(blocked, reason="budget_exhausted")
        ├─ no progress    → RuntimeDecision(blocked, reason="no_progress")
        └─ evaluator down → RuntimeDecision(failed, reason="evaluator_unavailable")
```

Key point: evaluated-loop continuation is a runtime decision, not a synchronous recursive call. The detailed single-source wakeup contract is defined in **Continuation Semantics**.

### Loop Spec Extension

Extend `src/voidx/agent/domain/loop.py:LoopSpec` rather than adding a separate goal domain.

```text
LoopSpec
├── prompt: str                         # existing; deterministic prompt for current/next turn
├── interval_seconds: float | None      # existing; fixed vs dynamic schedule
├── workflow_enabled: bool              # existing
├── evaluator_enabled: bool = False     # new; true for guided evaluated loop
├── objective: str = ""                 # new; what the user wants done
├── achievement_method: str = ""        # new; optional guidance, may be empty
├── acceptance_condition: str = ""      # new; what evidence proves completion
├── max_turns: int = 20                 # new; ge=1, le=200
└── run_id: str = ""                    # new; UUID generated for evaluated loop setup
```

For backward compatibility:

- Existing `/loop [interval] <prompt>` can keep `evaluator_enabled=False` and only use `prompt`.
- Guided `/loop` sets `evaluator_enabled=True`, requires objective and acceptance condition, and fills `achievement_method` only when the user supplies it.
- `prompt` remains the executable instruction sent to the ordinary turn; for guided setup it is generated from objective, optional method guidance, and acceptance condition.
- `LoopSpec.model_config` should continue tolerating older persisted specs if current behavior requires it.

`AgentThreadState.context["loop"]` should hold evaluated-loop metadata that is needed across attempts:

```text
thread.state.context["loop"]
├── run_id: str
├── evaluator_enabled: bool
├── objective: str
├── achievement_method: str              # optional; may be empty
├── acceptance_condition: str
├── max_turns: int
├── evaluator_failure_count: int
├── last_progress_key: str
├── repeated_progress_count: int
├── last_summary: str
├── last_evaluator_note: str
├── last_next_hint: str
├── blocked_reason: str
└── active: bool
```

State persistence rule: evaluated-loop state advances only at the dispatcher commit boundary. `LoopRuntimeRunner` must not call `ThreadStore.save_state()` or otherwise update `agent_thread_state` during an active attempt, because `RuntimeDispatcher` commits with the attempt's fixed `state_version`. The default implementation should keep the existing `ThreadStore.commit_decision()` contract unchanged: return a `RuntimeDecision`, let commit persist `lifecycle_decision`, and derive continuation counters/hints on the next attempt from committed loop state, prior `lifecycle_decision`, runtime attempt records, and the wakeup's previous decision. Only add a narrow commit-time merge hook if those existing records are proven insufficient; never write loop state independently before `commit_decision()`.

### Guided Setup UX

`/loop` with no args becomes an interactive setup command instead of only printing usage.

Required prompts:

1. **目标**："你希望我自动完成什么？"
2. **验收条件**："什么证据证明目标已完成？例如指定测试通过、文件存在、命令输出包含某内容。"

Optional prompts should stay short and skippable:

- **达成目标的方法**：可跳过；若提供，用作推进策略，例如先定位失败、再最小修改、最后跑测试。
- **最大轮次**：默认 20。
- **执行节奏**：dynamic 默认，或固定 interval。
- **workflow tools**：默认沿用 loop 当前行为；只有明确需要时再打开 workflow-enabled path。

Setup validation:

- objective 和 acceptance_condition 必须非空；achievement_method 可为空。
- acceptance_condition 必须是可由 post-turn evidence 判断的条件；不能要求 evaluator 自己联网、读文件或运行命令。
- invalid setup does not stop an active loop until the new setup is valid and user confirms replace/start.
- In headless/non-interactive mode, bare `/loop` should print the required fields, optional method field, and example syntax rather than block forever. UI/TUI can provide structured prompts.

Generated first prompt:

```text
Autonomous loop objective:
<objective>

Method to achieve the objective (optional):
<achievement_method or "Use the default autonomous method: inspect, act minimally, verify, and iterate toward the acceptance condition.">

Acceptance condition:
<acceptance_condition>

Work autonomously within the supplied method when present; otherwise use the default autonomous method: inspect, act minimally, verify, and iterate toward the acceptance condition. At the end of each attempt, report concrete evidence collected or produced. If required information, safety confirmation, or permission is unavailable, stop with needs_user instead of guessing.
```

### Loop Context

Evaluated loop attempts should make the autonomous contract explicit in model context rather than relying on hidden controller state.

Use existing context-builder inputs instead of adding a separate `runtime_profile` branch to `RuntimeContextBuilder`. `LoopRuntimeRunner` already builds `TurnExecutionContext(runtime_profile=LOOP_PROFILE, ...)`; for evaluated loop it should pass structured guidance through existing prompt inputs that already flow into `RuntimeContextBuilder`, primarily `profile_directive` for stable automation policy and the generated loop prompt for turn-local instructions. If richer budget/evaluator fields are needed, add a narrow `evaluated_loop_context` field to `TurnExecutionContext` and have `LangGraphExecution` translate it into `profile_directive` before constructing `RuntimeContextBuilder`; do not require `RuntimeContextBuilder` to inspect `RuntimeProfile` directly.

Render the following deterministic guidance through `profile_directive` or the narrow context adapter:

```text
<evaluated_loop_context>
Objective: <LoopSpec.objective>
Method: <LoopSpec.achievement_method or default autonomous method guidance>
Acceptance condition: <LoopSpec.acceptance_condition>
Run id: <run_id>
Attempt: <attempt_index> of <max_turns>
Previous outcome: <last RuntimeDecision summary or empty>
Evaluator feedback: <last_evaluator_note or empty>
Suggested next step: <last_next_hint or empty>
Automation policy: Operate fully autonomously within the explicit objective and acceptance condition, following the supplied method only when present. Do not ask the user for preferences, approvals, or missing facts; stop with needs_user when required information or permission is not already available.
Stop criteria: Stop only when the acceptance condition is satisfied, a safety/permission/user-input barrier is reached, no progress repeats, or the budget is exhausted.
</evaluated_loop_context>
```

Context rules:

- `LoopRuntimeRunner` computes the next attempt index from committed loop state and current input kind, but it does not persist that increment before the normal turn starts.
- Prompt/context rendering uses only `LoopSpec`, committed loop thread state, and the previous `RuntimeDecision` from the `wakeup` payload.
- Commit is the only place that may persist updated counters/hints; if the process crashes before commit, the leased attempt can be recovered or retried against the same committed state without a half-advanced budget.
- objective and acceptance_condition are immutable for a run id; achievement_method is immutable when supplied and may be empty. Evaluator hints can influence the next step, but must not rewrite the objective, method, or condition.
- Loop context is execution guidance only; evaluator still judges from the post-turn evidence snapshot, not from optimistic context text.

### Turn End Evaluation Boundary

Loop runner must execute the normal turn first and evaluate only after that turn has genuinely reached its normal end.

Required runtime result extension:

```text
TurnResult optional evaluator metadata
├── runtime: SessionRuntimeState | None
├── evaluation_messages: tuple[BaseMessage, ...]
├── task_state: TaskState
├── loop_stop_reason: "" | "needs_user_input" | "permission_denied" | "safety_blocked"
├── loop_stop_detail: str
└── final_assistant_summary: str
```

Implementation path:

- Extend reusable `src/voidx/agent/runtime/contracts.py:TurnResult` with optional metadata fields if all runtime engines can safely default them to empty values.
- Update `src/voidx/agent/runtime/runtime.py:AgentRuntime.run_turn()` because it is the facade that constructs `TurnResult` from the turn engine output.
- If metadata should remain LangGraph-specific, return a small post-turn metadata object from `LangGraphTurnEngine.run()` alongside `SessionRuntimeState`, then have `AgentRuntime.run_turn()` adapt it into `TurnResult`.
- Do not assume `LangGraphTurnEngine.run()` already returns `TurnResult`; today it returns `SessionRuntimeState`, while `AgentRuntime.run_turn()` constructs the reusable result.

Snapshot source:

- Add fields to `src/voidx/agent/state.py` for the last successful LLM call snapshot.
- Populate them in `src/voidx/agent/infrastructure/langgraph/execution.py` around the successful `_stream_llm()` call.
- The snapshot should be deep-copied before evaluator use.

Evaluator input should include:

```text
LoopEvaluationInput
├── objective: str
├── achievement_method: str              # optional; may be empty
├── acceptance_condition: str
├── attempt_index: int
├── max_turns: int
├── final_llm_messages: tuple[...]       # final real turn input/output context
├── final_assistant_summary: str
├── task_state_snapshot: TaskState
├── workflow_state_snapshot: list[WorkflowRunState]
├── todo_snapshot: list[TodoItem]
├── tool_result_summaries: tuple[str, ...]
└── previous_eval: LoopEvalResult | None
```

Do not use UI transcript, dock nodes, streamed partial chunks, or filesystem reads as evaluator input.

### Evaluator

Evaluator is an independent, no-tool LLM call with strict structured output. It should be small, deterministic, and conservative.

```text
LoopEvalResult
├── achieved: bool
├── confidence: Literal["low", "medium", "high"]
├── progress: Literal["none", "partial", "meaningful"]
├── progress_key: str          # stable short key for no-progress detection
├── summary: str               # concise evidence-based status
├── next_hint: str             # next autonomous action if not achieved
└── missing: list[str]         # missing evidence, not user questions
```

Evaluator prompt requirements:

```text
System:
You are a strict read-only acceptance evaluator. You cannot use tools, run commands, inspect files, or ask the user. Judge only the supplied evidence. Mark achieved=true only when the acceptance condition is directly satisfied by evidence in the final turn snapshot. If evidence is insufficient, mark achieved=false and explain the missing evidence.

User:
Objective: <objective>
Method: <achievement_method or default autonomous method guidance>
Acceptance condition: <acceptance_condition>
Attempt: <n>/<max_turns>
Previous evaluation: <summary or none>
Final turn evidence: <deep-copied snapshot>
Return LoopEvalResult JSON.
```

Evaluator timeout/failure policy:

- Timeout after a small bounded duration, e.g. 30s.
- Invalid structured output counts as evaluator failure.
- Transient evaluator failure can continue while under failure threshold and budget.
- Failure threshold default: 3 consecutive evaluator failures → `RuntimeDecision(outcome="failed", reason="evaluator_unavailable")`.
- Evaluator must not call `loop`, connector tools, shell, filesystem, or subagents.

### Continuation Semantics

Budget accounting is derived from committed runtime state. The runner computes the current attempt number from committed `thread.state.context["loop"]`, prior `lifecycle_decision`, and attempt/outbox history before running the normal turn; it may include the resulting count in prompts and decisions, but must not persist the increment until the dispatcher commit boundary. Evaluator counters and no-progress keys follow the same rule: derive from committed state plus the previous decision, then persist only through the commit path if a narrow merge hook is added.

Stop/continue priority after normal turn ends:

1. thread lifecycle is cancelling or turn was cancelled → `RuntimeDecision(outcome="stop", reason="cancelled")`;
2. turn/tool signalled missing user input → `RuntimeDecision(outcome="needs_user", reason="needs_user_input")`, no evaluator;
3. permission/safety confirmation is required and not already granted → `RuntimeDecision(outcome="needs_user", reason="permission_required" | "safety_confirmation_required")`, no evaluator;
4. normal turn failed before producing evaluable state → `RuntimeDecision(outcome="failed", reason="turn_failed")`, no evaluator;
5. evaluator achieved → `RuntimeDecision(outcome="completed", progress="meaningful")`;
6. max_turns reached and not achieved → `RuntimeDecision(outcome="blocked", reason="budget_exhausted")`;
7. evaluator failures >= 3 → `RuntimeDecision(outcome="failed", reason="evaluator_unavailable")`;
8. same non-empty progress_key repeated 3 successful evaluations → `RuntimeDecision(outcome="blocked", reason="no_progress")`;
9. otherwise → `RuntimeDecision(outcome="continue", summary=..., progress=..., reason="not_achieved", next_delay_seconds=<spec interval or dynamic default>)`.

Continuation outbox is single-source-of-truth:

- `LoopRuntimeRunner` returns only a `RuntimeDecision`; it does not enqueue the next prompt.
- `RuntimeDispatcher` commits that decision through `ThreadStore.commit_decision()`.
- `ThreadStore.commit_decision()` already creates one `kind="wakeup"` outbox for `outcome="continue"` with payload `{"decision": ...}`.
- `LoopRuntimeRunner` consumes that `wakeup` on the next attempt and reconstructs the prompt from committed loop state plus the previous decision.
- The scheduler may dispatch ready wakeups, but must not create an additional `loop_prompt` for the same continuation.

Deterministic continuation prompt:

```text
Continue the autonomous loop.

Objective: <objective>
Method: <achievement_method or default autonomous method guidance>
Acceptance condition: <acceptance_condition>
Attempt: <next_attempt> of <max_turns>
Previous decision: <decision.summary>
Evaluator status: <last_summary>
Evaluator next hint: <last_next_hint>

Work fully autonomously within the explicit objective and acceptance condition, following the supplied method when present. Do not ask the user unless required information, safety confirmation, or permission is unavailable. Stop when the acceptance condition is proven satisfied.
```

The continuation text may include evaluator `next_hint`, but must not mutate objective, optional method, or acceptance condition. If the previous decision is malformed or evaluated-loop state is missing, return `failed` with reason `missing_loop_state` rather than inventing a prompt.

### Tool / Gate Semantics

Evaluated loop is automatic-only inside its explicit objective and acceptance condition, plus optional method guidance when supplied. The tool list should maximize safe autonomous execution while excluding tools that require live user interaction or approval gates.

`LoopToolView` should remain the primary tool visibility policy, but evaluated loops may need a broader safe set than today's read-oriented default.

```text
LoopToolView.default(workflow_enabled=True, evaluated=True).bind(available_tool_ids)

Always visible when available for evaluated loop:
- read, find, search, lsp, document
- bash, write, replace, manage, lsp_format
- websearch, webfetch
- mcp, skill, task_status, todo
- agent only for read-only inspect/review/debug subagent work, never implement/feedback write delegation unless explicitly covered by loop tool policy

Allowed when workflow_enabled=True:
- workflow, task_status, todo

Never visible in evaluated loop attempts:
- clarify
- checkpoint
- turn
- compact
- any UI prompt / approval / user-choice tool
- any connector action that requires interactive OAuth or human confirmation at call time
```

Policy rules:

- `LoopToolView` is a tool-visibility and runtime-mode constraint, not a replacement for normal permission authorization.
- Existing `execution._authorize_tool_calls()` currently short-circuits to `tool_policy.check_tool_call()` when `TurnExecutionContext.tool_policy` is present. Evaluated loop mode must therefore use a composite policy: first apply `LoopToolView` to deny non-loop/interactive tools, then pass allowed calls through the existing `authorize_tool_call()` / permission-service path so ASK/BLOCKED/DENY semantics remain intact.
- Interactive tools are not visible to the model during evaluated loop attempts; the model should not choose `clarify` or `checkpoint` because they are not in the bound tool set.
- If existing lower-level execution still reaches a user-interaction path, fail closed with an evaluated-loop needs-user signal and convert the attempt to `RuntimeDecision(outcome="needs_user")`.
- Permission and safety confirmations fail closed unless already covered by existing trusted grants or sandbox policy; evaluated loop mode must not synthesize user approval.
- File edits, command execution, formatting, and tests are allowed only within normal workspace/sandbox/permission constraints already enforced by the tool layer.
- Tool executor marks not-yet-started tools in the same batch as skipped after a needs-user barrier; write tools after a user-input barrier must not start.
- needs-user detail is propagated to the post-turn result so evaluator is skipped and runtime decision becomes `needs_user`.

No special auto-approval grant is needed for `checkpoint`; the preferred design is to exclude checkpoint from the evaluated-loop-visible tool set. If a future workflow gate requires approval, it should be represented as a non-interactive runtime decision (`needs_user`) rather than an auto-approved checkpoint.

## Slash Behavior

`/loop` becomes the primary UX for evaluated autonomous execution.

| Command | Behavior |
|---|---|
| `/loop` | Start interactive evaluated-loop setup; ask objective and acceptance condition, optionally ask achievement method and budget/interval. |
| `/loop [interval] <prompt>` | Preserve existing shortcut behavior for ordinary loop; no evaluator unless a future explicit flag enables structured setup. |
| `/loop stop` | Stop active loop/evaluated loop through existing `LoopService.stop()`. |
| `/loop status` | Show active loop status; include evaluated-loop objective and last evaluator summary when present. |
| `/loop help` | Show both shortcut syntax and guided evaluated-loop setup. |
| `/goal <desc>` | Legacy behavior only: set goal/GOAL mode; does not start evaluated loop. |

Guided setup parser/result:

```text
LoopSetupResult
kind: "cancelled" | "invalid" | "evaluated_loop"
objective: str
achievement_method: str
acceptance_condition: str
max_turns: int
interval_seconds: float | None
workflow_enabled: bool
```

Validation rules:

- objective and acceptance_condition must be non-empty after trim; achievement_method may be empty and should be stored as an empty string/default guidance marker.
- invalid setup should show a concise correction prompt and must not stop an active loop until a valid replacement is ready.
- starting a valid new evaluated loop for the same parent stops/replaces the active loop using the same posture as current `/loop` replacement behavior.
- `/goal` commands must not cancel or replace evaluated loops except if they call explicit shared stop behavior in a separate future design.

### Running Command Control

Evaluated loop should reuse the same cancellation posture as runtime-backed loop.

- Valid `/loop stop` stops active loop through `LoopService.stop()`.
- Valid new guided `/loop` setup stops/replaces the active loop for the same parent only after setup validation succeeds.
- Invalid or cancelled setup never cancels an active loop.
- Gateway/TUI busy handling should share setup logic and avoid duplicating syntax.
- Session/model/persistence-changing commands (`/clear`, `/session`, `/resume`, `/model`, `/mode`, `/exit`) must not run concurrently with active evaluated-loop attempts; cancel/wait or return busy consistently with loop.

## Implementation Phases

1. **Loop domain contracts**：扩展 `src/voidx/agent/domain/loop.py:LoopSpec`，新增 evaluator fields、structured setup result、eval result、evaluated-loop tool visibility helpers。
2. **Interactive setup**：在 `src/voidx/agent/slash/handler.py` 或新 `src/voidx/agent/loop/setup.py` 实现 `/loop` 无参数引导；保留 `/loop [interval] <prompt>`、`stop`、`status`、`help`。
3. **Loop service/scheduler compatibility**：复用 `LoopService` / `LoopRuntimeScheduler`；确保 evaluated spec 持久化到 loop thread context，first outbox 仍是一个 `loop_prompt`。
4. **Turn result metadata**：扩展 `TurnResult` 或 LangGraph-specific metadata；更新 `AgentRuntime.run_turn()`，让 loop runner 能读取 final LLM snapshot、stop reason、task/workflow/todo state。
5. **Loop context/prompt rendering**：通过 existing `profile_directive` / generated prompt / optional `TurnExecutionContext` narrow adapter 注入 objective、method、acceptance condition、attempt budget、previous evaluator feedback、automation policy、stop criteria。
6. **Evaluator**：实现只读 structured evaluator 和 `EvaluatorFailure` 分类。
7. **Continuation decisions**：在 `LoopRuntimeRunner` 中实现 budget、evaluator failure、no-progress、achieved/not-achieved 到 `RuntimeDecision` 的映射。
8. **Single-path continuation**：确保 `continue` 只通过 `ThreadStore.commit_decision()` 生成的 `kind="wakeup"` outbox 继续；runner 能消费 wakeup 并从 loop state 构造下一 prompt。
9. **Automatic tool policy**：扩展 `LoopToolView` 作为可见性过滤，并与现有 `authorize_tool_call()` / permission service 组合；排除 interactive tools，补充 lower-level needs-user/permission/safety fail-closed 路径。
10. **Gateway/TUI control**：运行中有效 `/loop` setup/stop/status 行为一致；无效 setup 不取消活动 loop。
11. **Legacy goal regression**：确认 `/goal <desc>`、`/goal clear` 和 `resolve_goal_mode()` 旧行为不变。
12. **Recovery and dispatcher**：补齐 ready `wakeup` dispatch/recovery 行为；不要声称现有 recovery 已覆盖 waiting continuation。

## File Plan

| Path | Status | Required Change |
|---|---|---|
| `src/voidx/agent/domain/loop.py` | existing | extend `LoopSpec` with evaluated-loop fields; add setup/eval result contracts and evaluated tool visibility helpers. |
| `src/voidx/agent/loop/setup.py` | new optional | shared guided `/loop` setup logic for slash/gateway/TUI/headless prompts. |
| `src/voidx/agent/slash/handler.py` | existing | make bare `/loop` start guided setup; preserve shortcut/stop/status/help behavior. |
| `src/voidx/agent/application/loop_service.py` | existing | support evaluated spec metadata in status and active replacement semantics. |
| `src/voidx/agent/loop/scheduler.py` | existing | consume evaluated specs, enqueue first loop outbox, dispatch ready wakeups without duplicate prompts. |
| `src/voidx/agent/loop/evaluator.py` | new | structured no-tool evaluator and failure classification. |
| `src/voidx/agent/loop/controller.py` | existing | optionally carry per-attempt needs-input/evaluator metadata; no outer loop and no independent thread-state writes. |
| `src/voidx/agent/runtime/contracts.py` | existing | add optional post-turn metadata fields to `TurnResult`, or define a narrow metadata carrier. |
| `src/voidx/agent/runtime/runtime.py` | existing | propagate turn-engine post-turn metadata into `TurnResult`; this is where reusable `TurnResult` is constructed. |
| `src/voidx/agent/runtime/dispatcher.py` | existing | no semantic fork; ensure loop runner receives wakeup input unchanged and lifecycle commit remains single-source. |
| `src/voidx/agent/runtime/recovery.py` | existing | add or document recovery behavior for ready continuation wakeups; current attempt-only recovery is insufficient. |
| `src/voidx/agent/infrastructure/langgraph/execution.py` | existing | capture last successful LLM call snapshot and needs-input stop detail. |
| `src/voidx/agent/infrastructure/langgraph/adapter.py` | existing | expose post-turn metadata to `AgentRuntime.run_turn()`. |
| `src/voidx/agent/state.py` | existing | add invocation-local evaluation snapshot and loop stop fields. |
| `src/voidx/agent/domain/turn_context.py` | existing | optionally carry narrow evaluated-loop context metadata if current generic fields are insufficient; keep permission authorization outside this data object. |
| `src/voidx/tools/base.py` | existing | represent evaluated-loop needs-user / permission / safety stop details without interactive callbacks. |
| `src/voidx/tools/checkpoint.py` | existing | ensure checkpoint is not bound in evaluated-loop view; lower-level accidental use fails closed. |
| `src/voidx/tools/clarify.py` | existing | ensure clarify is not bound in evaluated-loop view; lower-level accidental use becomes needs_user. |
| `src/voidx/agent/ports/permission.py` and permission grant files | existing | reuse existing permission decisions; evaluated loop fails closed for ASK/BLOCKED/DENY unless existing grants cover the action. |
| `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py` and `src/voidx/agent/infrastructure/langgraph/execution.py` | existing | skip not-yet-started batched tools after evaluated-loop needs-user/safety/permission barrier. |
| `src/voidx/agent/runtime_context.py` | existing | no direct `runtime_profile` dependency; render evaluated-loop guidance only through existing `profile_directive` / generated prompt or a narrow adapter from `TurnExecutionContext`. |
| `src/voidx/ui/gateway/run_manager.py`、`src/voidx/ui/gateway/frontend.py`、`tui/voidx_cli/app.py` | existing | guided loop setup/status/stop behavior using shared setup result. |
| `src/voidx/agent/goal_resolver.py` | existing | leave `resolve_goal_mode()` behavior unchanged. |

## Forbidden Changes

- Do not implement evaluated loop as a synchronous outer while-loop in `AgentService` or slash handler.
- Do not bypass `RuntimeDispatcher`, thread state, outbox, attempt, or lifecycle decision for autonomous continuation.
- Do not add a runtime-backed `/goal ... when ...` syntax; `/goal` remains legacy goal mode.
- Do not add `GOAL_PROFILE` / `GoalService` unless a later design proves `/loop` cannot support the feature.
- Do not modify `resolve_goal_mode()` returning `PlanResolution(join="plan", leave=None)` for legacy goal mode.
- Do not modify workflow DAG or `InteractionMode` enum.
- Do not bind tools to evaluator.
- Do not infer evaluator input from UI transcript/dock rendering.
- Do not allow infinite budget.
- Do not let invalid `/loop` setup cancel an active loop.

## Edge Cases

| Case | Expected Behavior |
|---|---|
| `/loop` in interactive UI/TUI | Prompt for objective and acceptance condition, optionally method/budget/interval; start evaluated loop after valid setup. |
| `/loop` in headless/non-interactive mode | Print required fields, optional method field, and examples; do not block indefinitely. |
| user cancels setup | No state write; active loop continues unchanged. |
| objective empty | Re-prompt or return invalid; active loop unchanged. |
| achievement method empty | Accept as skipped/empty; use default autonomous method guidance; active loop unchanged until required fields are valid. |
| acceptance condition empty | Re-prompt or return invalid; active loop unchanged. |
| `/loop 60 fix auth tests` | Preserve current shortcut behavior: ordinary fixed loop prompt, no evaluator unless a future explicit flag says otherwise. |
| `/loop stop` | Stop active ordinary/evaluated loop. |
| `/loop status` | Show ordinary/evaluated loop status; include evaluator summary when available. |
| `/goal fix auth` | Legacy behavior: set goal, GOAL mode, no runtime-backed autonomous acceptance loop. |
| model unavailable | Reject before starting evaluated loop; previous state unchanged. |
| normal turn ends, evaluator achieved | RuntimeDecision completed; lifecycle terminal completed. |
| normal turn ends, evaluator not achieved | RuntimeDecision continue; commit creates one `wakeup` outbox; next attempt consumes wakeup. |
| next continuation outbox is `wakeup` with no prompt | Loop runner reconstructs prompt from loop state and previous decision. |
| wakeup payload malformed or loop state missing | failed with reason `missing_loop_state` / `invalid_loop_input`; no invented prompt. |
| last budget turn achieved | completed wins over budget exhausted. |
| budget exhausted and not achieved | blocked with reason `budget_exhausted`. |
| evaluator transient failure | failure count increments; continue while under threshold and budget. |
| evaluator failure >= 3 | failed with reason `evaluator_unavailable`. |
| repeated no progress | blocked with reason `no_progress`. |
| clarify/user preference needed | evaluator skipped; needs_user. |
| missing permission/safety confirmation | evaluator skipped; needs_user or blocked according to existing permission policy; no synthetic approval. |
| checkpoint would normally ask approval | checkpoint is not bound in evaluated-loop tool list; lower-level accidental path becomes needs_user. |
| runtime crash after continue commit | durable `wakeup` outbox remains the recovery/dispatch source; recovery enhancement may dispatch it later. |
| invalid setup while active loop runs | setup returns invalid/cancelled; active loop continues unchanged. |

## Test Plan

| Area | Command | Covers |
|---|---|---|
| loop domain/setup | `./test.py --backend -- src/tests/test_agent/domain/test_loop_domain.py src/tests/test_agent/loop/test_loop_setup.py` | evaluated `LoopSpec` validation, objective/condition required and method optional, budget validation, backward-compatible ordinary `LoopSpec`. |
| loop service/scheduler | `./test.py --backend -- src/tests/test_agent/test_loop_service.py src/tests/test_agent/loop/test_runtime_scheduler.py` | thread creation/reset, first outbox, evaluated metadata, wakeup consumption, no duplicate continuation prompt. |
| loop runner | `./test.py --backend -- src/tests/test_agent/loop/test_loop_runner.py` | `loop_prompt` vs `wakeup`, missing state, RuntimeDecision mapping, budget/no-progress, ordinary loop regression. |
| evaluator | `./test.py --backend -- src/tests/test_agent/loop/test_loop_evaluator.py` | evidence requirement, timeout, invalid structured output, failure classification. |
| slash behavior | `./test.py --backend -- src/tests/test_agent/slash/test_slash_loop.py` | bare `/loop` guided setup, shortcut loop preserved, stop/status/help, invalid setup no cancel/write. |
| tool policy / needs user | `./test.py --backend -- src/tests/test_agent/domain/test_loop_domain.py src/tests/test_tools/test_plan_checkpoint.py src/tests/test_tools/test_clarify_tool.py -k "loop"` | evaluated loop tool view excludes interactive tools; clarify/checkpoint accidental paths fail closed. |
| permission / safety | `./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase*.py -k "loop"` | loop tool visibility is composed with existing authorization; ASK/BLOCKED/DENY do not become synthetic approval. |
| gateway/TUI control | `./test.py --backend -- src/tests/test_ui/gateway/test_run_manager.py src/tests/test_ui/gateway/test_gateway_headless_frontend.py -k "loop" && ./test.py --backend -- tui/tests/test_input_handling.py -k "loop"` | guided setup/status/stop behavior consistent; invalid setup does not cancel. |
| runtime recovery | `./test.py --backend -- src/tests/test_agent/runtime/test_recovery.py -k "loop or wakeup"` | committed `continue` leaves a durable ready `wakeup`; recovery/dispatcher can claim it and run the next loop attempt without recreating a prompt. |
| legacy goal regression | `./test.py --backend -- src/tests/test_agent/test_goal_resolver.py src/tests/test_agent/slash/test_slash_goal.py` | `/goal` and `resolve_goal_mode()` remain unchanged and do not start evaluated loop. |
| workflow regression | `./test.py --backend -- src/tests/test_workflow/` | workflow gates/reconcile unaffected. |

## Acceptance Criteria

- Bare `/loop` starts guided evaluated-loop setup in interactive contexts.
- Guided setup requires objective and acceptance condition before starting; achievement method is optional and may be empty.
- `/loop [interval] <prompt>` shortcut remains compatible with current ordinary loop behavior.
- Evaluated loop autonomous execution is runtime-backed and uses existing `/loop` architecture.
- There is no outer synchronous loop in `AgentService`, slash handler, or scheduler start path.
- A loop attempt runs exactly one normal turn, then runs evaluator at the turn end boundary when `evaluator_enabled=True`.
- Runtime/lifecycle decisions control continuation and termination.
- Continuation uses the single-source wakeup contract defined in **Continuation Semantics**.
- Loop runner can consume both initial `loop_prompt` and continuation inputs.
- Loop context includes objective, optional achievement method, acceptance condition, attempt/budget, prior evaluator feedback, automatic-only policy, and stop criteria through existing context-builder inputs or a narrow adapter.
- Loop tool visibility excludes interactive tools (`clarify`, `checkpoint`, `turn`, UI approval tools) while all visible calls still flow through existing sandbox and permission authorization.
- Evaluator only sees stable, deep-copied last LLM context plus post-turn state; it never uses UI rendering or tools.
- Legacy `/goal <desc>` and `resolve_goal_mode()` behavior remain unchanged.
- Invalid or cancelled guided setup cannot stop or replace an active loop.
- Focused loop/evaluator tests, legacy goal regression, and workflow regression pass.

## Definition of Done

A complete implementation can execute this closed loop:

1. User submits `/loop`.
2. UI/TUI/slash setup prompts for objective and acceptance condition, with optional achievement method and optional budget/interval.
3. Setup creates an evaluated `LoopSpec` and `LoopService` starts a `LOOP_PROFILE` thread.
4. Loop scheduler enqueues and dispatches the first `loop_prompt` through `RuntimeDispatcher`.
5. `LoopRuntimeRunner` runs one normal LangGraph turn using existing runtime turn machinery.
6. At normal turn completion, `TurnResult` metadata includes the final LLM evaluation snapshot and stop details.
7. Loop evaluator checks the acceptance condition against evidence in that snapshot.
8. If achieved, runner returns `RuntimeDecision(completed)` and lifecycle terminates the loop thread.
9. If not achieved and budget/progress/failure limits allow, runner returns `RuntimeDecision(continue)`; runtime commit creates the next wakeup per **Continuation Semantics**.
10. The next loop attempt reconstructs the deterministic continuation prompt from committed loop state plus previous decision, then repeats from step 5.
11. Cancellation, stop, replace, needs-user-input, permission/safety barriers, crash recovery, and budget exhaustion all end in explicit runtime lifecycle state with no hidden in-memory loop.
