> **Status: Done** — Archived on 2026-07-27.

---
name: agent-runtime-loop
display_name: Agent Runtime Loop 模式设计
description: 将现有 /loop scheduler 从 synthetic turn 迁移为 runtime-backed child thread 和结构化 lifecycle
doc_type: tech-design
audience: human+llm
status: approved
source_design: docs/design/agent-runtime-unification.md
---

# Agent Runtime Loop 模式设计

## 1. Summary

本设计把现有 `/loop` 从“session 内后台 task + `run_synthetic_turn()`”迁移为统一 `AgentRuntime` 上的 loop profile、child thread、durable wakeup 和结构化 lifecycle。目标是保留现有 `/loop [interval] <prompt>`、`/loop stop` 和 `/loop status` 用户体验，同时让 loop 拥有独立 transcript、context frame、workflow/todo/compaction state、权限视图和恢复语义。Dynamic loop 的下一轮调度统一通过 `loop_update(next_delay_seconds=...)` 表达。

本设计现在作为可执行迁移规范使用；实现时必须复用现有 `AgentRuntime.run_turn(TurnRequest)`、`LangGraphTurnEngine`、tool/MCP/skill/compaction 基建，不创建第二套 agent executor。当前仓库已有 runtime-backed scheduler 雏形，但尚未满足本设计的 lifecycle、tool-view、durable status 和 transcript-isolation 闭环验收。

## 2. Current State

### 迁移前 legacy path

原始 loop path:

```text
/loop slash
  → SlashHandler._loop()
  → LoopManager.start(PromptSource, interval)
  → LoopManager._run_loop()
  → host.run_synthetic_turn(prompt)
  → TurnRunner.run_once()
```

原始代码边界:

- `src/voidx/agent/slash/handler.py:_loop()` 解析 `/loop`、`stop`、`status`，并直接操作 `host.loop_manager`。
- `src/voidx/agent/loop/manager.py` 持有 session-scoped asyncio task；首轮立即触发，后续使用 fixed interval 或 dynamic `schedule_wakeup` trigger。
- `src/voidx/agent/loop/prompt_source.py` 每轮解析 text/file/script prompt source。
- `src/voidx/tools/schedule_wakeup.py` 通过 `ToolContext.loop_manager` reschedule 或 stop 当前 `LoopManager`。
- `src/voidx/agent/infrastructure/langgraph/execution.py` 在 graph host 上构造 `_loop_manager` 并注入 `ToolRegistry`。

### 当前实现快照

截至本文档批准时，仓库已有部分 runtime-backed adapter，但尚未形成闭合的 loop lifecycle：

- `src/voidx/agent/loop/scheduler.py` 定义 `LoopRuntimeScheduler` 和最小 `LOOP_PROFILE`，创建/加载 `loop:<session>` child thread，enqueue loop prompt outbox，并通过 `RuntimeDispatcher` dispatch 到 `AgentRuntime.run_turn()`。
- `src/voidx/agent/composition.py` 通过 `set_runtime_scheduler(...)` 把 `LoopRuntimeScheduler` 注入现有 `LoopManager`。
- `src/voidx/agent/loop/manager.py` 仍持有 in-memory timer，并在 `_run_loop()` 中调用 runtime scheduler；这是 adapter 阶段，不是最终 durable scheduler。
- `src/voidx/agent/slash/handler.py:_loop()` 仍直接 start/stop/read `host.loop_manager`；尚无 `LoopService` 统一拥有 slash semantics。
- `src/voidx/tools/schedule_wakeup.py` 仍默认注册，`loop_update` 尚不存在。
- `LOOP_PROFILE` 当前只是 `RuntimeProfile(profile_id="loop", revision=1, name="Loop")`；`LOOP_BASE_SYSTEM_SPEC`、`LoopPromptPolicy`、`LoopToolView` 和 lifecycle-specific tool binding 尚未完成。

因此，当前实现可以执行基础 recurring runtime turn，但还不是本设计定义的 lifecycle-closed `/loop` implementation。

主要剩余问题：

1. Loop start/stop/status 仍以 in-memory `LoopManager` 作为 source of truth，而不是 `LoopService` + repository state。
2. Scheduler 仍依赖 legacy timer 和 prompt path；durable wakeup claim/ack、lease、recovery 和 fencing 尚不是 authoritative。
3. `schedule_wakeup` 仍 mutate in-memory manager，dynamic continuation 还没有通过 `loop_update` 表达。
4. Loop turns 尚未具备完整 loop prompt policy、closed-world `LoopToolView` 或 non-interactive lifecycle contract。
5. Parent/child transcript isolation、fixed prompt non-duplication、blocked/needs_user/retry states 和 status reporting 仍需要 e2e tests。

## 3. Goals and Non-goals

### Goals

1. 让 `/loop` 创建或替换一个 loop child thread，并通过 `AgentRuntime.run_turn()` 执行每轮。
2. 保留现有 `/loop` 用户语法、PromptSource 能力和首轮立即触发语义。
3. loop transcript、runtime state、workflow/todo、context cache、compaction summary 和 permission/tool view 与父 session 隔离。
4. 用结构化 `LoopDecision`/`loop_update` 表达 continue/completed/blocked/needs_user/failed/stop，而不是让 scheduler 解析自然语言。
5. fixed interval 与 dynamic loop 都由 policy/controller 计算下一次 durable wakeup；模型只能提出建议。
6. 支持 pause/resume/cancel/stop/status/recovery 的明确状态机和测试矩阵。
7. 迁移期保留旧 `LoopManager` 行为，不长期保留两个 source of truth。

### Non-goals

- 本设计不实现多 loop 并发 UI；第一版仍保持一个 active loop per parent session。
- 本设计不实现完整 thread store schema migration；只定义 loop 需要的契约和落地顺序。
- 本设计不改变 chat/coding prompt、工具权限或 session transcript 行为。
- 本设计不引入第二套 LangGraph topology、tool executor、MCP manager 或 compaction engine。
- 本设计不把 goal evaluator loop 合并进 `/loop`；goal + loop 之后可组合，但 first-class loop 只负责调度和生命周期。

## 4. Target Runtime Model

Loop 由三个职责组成：

```text
LoopService
  parses /loop request, creates/replaces the active loop thread, exposes stop/status/resume
LoopScheduler
  owns durable wakeups and dispatches due attempts through AgentRuntime.run_turn()
LoopLifecycle
  validates loop_update, commits state, and computes the next wakeup or terminal state
```

执行路径：

```text
用户: /loop 5m check build
  → LoopService.start(parent_thread_id, LoopSpec)
  → create/replace child thread loop:<parent-session>:active
  → LoopScheduler enqueue immediate wakeup
  → LoopScheduler claims wakeup, creates attempt, calls AgentRuntime.run_turn(profile=LOOP_PROFILE, thread=child)
  → model calls loop_update(...), or default policy is applied
  → LoopLifecycle commits state and arms the next wakeup when outcome=continue
```

### Thread identity

- First version: one active loop per parent thread.
- `loop_thread_id`: durable thread id for loop transcript/state. First version uses `loop:<parent_thread_id>:active`.
- `parent_thread_id`: parent conversation thread that owns `/loop start/stop/status` and receives loop events.
- Starting a new loop for the same parent replaces the active loop thread.

### Profile, Base System, and Tool View

Add a loop profile without branching in `AgentRuntime`:

```python
LOOP_PROFILE = RuntimeProfile(
    profile_id="loop",
    revision=1,
    name="Loop",
    prompt_policy=LoopPromptPolicy(),
    # Target contract: add tool_policy/ToolView binding when RuntimeProfile grows
    # profile-scoped tool selection beyond today's prompt_policy-only shape.
    tool_policy=LoopToolPolicy(),
)
```

Loop must have its own Base System profile. It is not coding with a timer and it is not chat with auto-submit:

```python
LOOP_BASE_SYSTEM_SPEC = BaseSystemProfile(
    identity="You are voidx, a loop runtime agent executing a recurring user-defined task.",
    style_names=["language", "tone", "concise", "progress_preamble", "summarize_results", "uncertainty"],
    global_section_names={
        "Verification Rules": ["fresh_verification"],
        "Collaboration Rules": ["follow_requests"],
        "Loop Runtime Rules": ["fixed_prompt", "lifecycle_decision", "state_carry_forward", "tool_boundaries"],
    },
)
```

`LoopPromptPolicy` should:

- override `base_system_spec` with `LOOP_BASE_SYSTEM_SPEC`;
- suppress coding personas and coding-only workspace/delegation rules by default;
- inject the fixed loop prompt into the Base System/profile directive, not as a repeated user message;
- render iteration metadata, trigger reason, previous loop summary and lifecycle budget through loop-specific `Current Task State`, and render a short per-iteration guide through non-persistent `Task Context`;
- include workflow runtime only when the loop was started with workflow enabled;
- require a lifecycle decision by `loop_update` or safe fallback;
- keep capability filtering data-driven through `LoopToolView`, not `if profile_id == "loop"` in runtime execution.

#### Fixed prompt placement

The `/loop <prompt>` text is the loop's stable instruction. It must be stored on `LoopSpec` and rendered into the loop Base System/profile directive for every iteration when the prompt source is literal text:

```text
[FIXED LOOP PROMPT]
<literal /loop prompt>
```

This stable section is part of the prefix-cacheable prompt. It must not contain per-iteration trigger metadata, loop state, tool inventory, timestamps, retry counters or changing file/script output. It also must not be appended to the transcript as a new `HumanMessage` on every wakeup.

For file/script prompt sources, separate the stable instruction from the dynamic resolved content:

```text
[FIXED LOOP PROMPT]
Use the resolved loop prompt content for each iteration.

[RESOLVED LOOP PROMPT THIS ITERATION]
<resolved file/script PromptSource output for this wakeup>
```

`[FIXED LOOP PROMPT]` stays in the Base System/profile directive. `[RESOLVED LOOP PROMPT THIS ITERATION]` is a dynamic, non-persistent overlay built for the current wakeup, preferably adjacent to the per-iteration `Task Context` guide, so dynamic prompt resolution does not churn the stable prefix. Each iteration may also have a non-transcript trigger frame such as `scheduled wakeup`, `manual resume`, or `retry after failure`; that trigger frame is runtime metadata, not the user's recurring prompt.

#### Tool policy

Loop uses a dedicated tool view. It must not inherit the coding or chat bound tool list wholesale.

Default first-version loop tools:

| Category | Tool ids | Policy |
|---|---|---|
| Lifecycle | `loop_update` | Loop-only. Single lifecycle decision tool; `next_delay_seconds` is honored for dynamic loops and ignored for fixed loops. |
| Shared retrieval | `read`, `glob`, `grep`, `lsp`, `document`, `websearch`, `webfetch` | Enabled when resource scope allows them. |
| MCP / skills | `mcp`, `skill` plus registered MCP tools | Common across modes; loop gets its own allowlist/call state. |
| Workflow subset | `workflow`, `task_status`, optionally `todo` | Enabled only when `LoopSpec.workflow_enabled` or an explicit workflow is attached. |
| Mutating workspace tools | `write`, `replace`, `manage`, `git`, `bash`/`powershell`, `lsp_format` | Disabled by default. Bind only when the loop was created with an explicit non-interactive execution policy and already-satisfied durable permission; never trigger an approval flow mid-iteration. |
| Approval / interaction tools | `clarify`, `checkpoint`, approval-request tools, `agent`/subagent | Unbound by default. Loop must not ask inline questions, open approval gates, or delegate to subagents during automatic wakeups; surface `needs_user`/`blocked` through lifecycle instead. |

`LoopToolView.bound_tool_ids` is computed from `LoopSpec`, resource scope and execution policy. These ids are bound as the actual model tool list; do not duplicate available tools inside `Current Task State`. The tool view is closed-world: if a capability would require a new approval, a user clarification, an interactive checkpoint or subagent delegation, it is not bound for that wakeup. MCP and skill catalogs are common infrastructure, but their selected server/tool/skill state is loop-scoped. Workflow is only partially common: the workflow engine can be reused, but workflow tools/rules are exposed only for loops that opted into workflow.

Repeated lifecycle calls for the same attempt are idempotent and must not schedule duplicate wakeups.

Non-interactive closure rules:

- Loop wakeups must be self-contained: no `clarify`, `checkpoint`, approval gate or subagent fallback is available during automatic execution.
- If the task cannot proceed because a permission, credential, user choice or external approval is missing, the model records `loop_update(outcome="needs_user" | "blocked", summary=...)` instead of asking inline.
- If an allowed action would require a fresh approval, runtime denies the tool binding before the turn starts; it does not expose the tool and wait for approval inside the loop turn.
- Manual resume can update `LoopSpec`, durable permissions or prompt state, then the next wakeup runs with a new closed-world `LoopToolView`.

## 5. Lifecycle Protocol

### Decision schema

`loop_update` is the single loop lifecycle tool.

```python
class LoopDecision(BaseModel):
    outcome: Literal["continue", "completed", "blocked", "needs_user", "failed", "stop"]
    summary: str
    progress: Literal["none", "partial", "meaningful"] = "none"
    next_delay_seconds: float | None = None
    reason: str = ""
```

Semantics:

- `continue`: commit iteration summary and schedule next wakeup.
- `completed`: terminal; mark loop completed and notify parent session with summary.
- `blocked`: pause automatic scheduling until explicit user action repairs or resumes.
- `needs_user`: pause and surface a parent-session event/question.
- `failed`: `LoopLifecycle` applies retry policy; only terminal after budget is exhausted or error is non-retryable.
- `stop`: user/tool requested stop; terminal cancellation-like outcome, preserving summary.

`next_delay_seconds` is advisory. For dynamic loops it is clamped to policy min/max and used as the next wakeup interval. For fixed loops it is ignored — the configured fixed interval always wins. It is never used for terminal decisions, and never lets the model extend total budget.

If the model produces no `loop_update` call, apply the profile default:

- fixed interval loop: `continue` with configured interval;
- dynamic loop: `continue` with default interval;
- if the turn failed before any safe decision: classify through retry policy.

### State machine

```text
created → waiting → running → waiting
              │        ├→ needs_user → waiting
              │        ├→ blocked → waiting       # after explicit resume/repair
              │        ├→ retry_wait → running
              │        ├→ completed               # terminal
              │        ├→ failed                  # terminal
              │        └→ cancelling → cancelled  # terminal
```

Rules:

- Scheduler can only claim `waiting`/`retry_wait` wakeups.
- `stop` from slash/UI moves `waiting` to `cancelled`; if a turn is running, mark `cancelling`, cancel the active task, then commit `cancelled`.
- A committed terminal state wins over late stop/cancel requests.
- `blocked` and `needs_user` are not terminal, but they do not auto-schedule.
- Repeated identical lifecycle submissions for the same attempt are idempotent.

## 6. Scheduler and Persistence

The scheduler must become a wakeup owner, not an agent executor.

### Minimal migration storage

Before the full `ThreadStore` lands, implementation may use existing session runtime persistence plus a small loop repository, but loop thread state must have one authoritative owner:

```text
loop_runs
  loop_run_id / parent_thread_id / loop_thread_id / profile_id / resource_scope
  prompt_source / interval_seconds / mode / state / iteration
  last_summary / last_error / created_at / updated_at

loop_wakeups
  wakeup_id / loop_run_id / loop_thread_id / available_at / state
  attempt_id / expected_version / lease_owner / lease_expires_at
```

This transitional repository must be deleted or folded into `ThreadStore` when full thread persistence lands. It must not mirror state into `LoopManager` as a second source of truth.

### Attempt safety

A production-grade implementation should align with `agent-runtime-unification` attempt semantics:

1. Claim wakeup with lease and fencing token.
2. Create one turn attempt for one `source_wakeup_id`.
3. Mark `side_effect_started=true` before external side-effect tools.
4. Commit messages/state/lifecycle/outbox atomically.
5. On recovery, safely retry only attempts with no side effects; otherwise transition to `needs_user` with evidence.

First implementation can be single-process if tests prove no concurrent active turn for the same loop, but public contracts should keep the lease/fencing shape so the durable version does not need API redesign.

## 7. Slash and Tool Surface

### Slash commands

Existing syntax remains:

```text
/loop [interval] <prompt>
/loop stop
/loop status
```

Target behavior:

- `/loop <prompt>` starts dynamic mode and immediately enqueues first wakeup.
- `/loop 5m <prompt>` starts fixed mode and immediately enqueues first wakeup.
- `/loop stop` delegates to `LoopService.stop(parent_thread_id)`; it no longer directly cancels an in-memory manager when runtime-backed loop is active.
- `/loop status` reads `LoopService.status(parent_thread_id)` and includes state, mode, next wakeup, iteration, last summary/error and `loop_thread_id`.

### PromptSource

Keep `PromptSource.from_raw()` and per-trigger resolution semantics:

- text prompt is used as-is;
- file prompt is re-read each iteration;
- script prompt executes each iteration through the selected runtime tool context;
- prompt source errors become loop iteration failures and are processed by retry policy, not silently injected as ordinary user messages.

### schedule_wakeup removal

`schedule_wakeup` is not part of the runtime-backed loop contract. Runtime-backed loops expose only `loop_update`; existing loop prompts must migrate from `schedule_wakeup(delay_seconds=X)` to `loop_update(outcome="continue", next_delay_seconds=X)` and from `schedule_wakeup(stop=true)` to `loop_update(outcome="stop")`. `ToolContext.loop_manager` is replaced by `ToolContext.loop_controller`.

## 8. Context and Prompt Requirements

Loop turns must not write the runtime envelope, fixed user prompt, loop state snapshot or per-iteration guide as parent-session user messages. The model should see three distinct prompt/context surfaces:

1. **Fixed loop instruction** — the stable literal `/loop <prompt>` rendered in the loop Base System/profile directive as `[FIXED LOOP PROMPT]`.
2. **Dynamic resolved prompt** — only for file/script PromptSource; rendered as `[RESOLVED LOOP PROMPT THIS ITERATION]` in a non-persistent current-turn overlay so it does not invalidate the stable prefix.
3. **Loop Current Task State** — dynamic state snapshot that reuses the existing `Current Task State` overlay channel, but is constructed from loop state instead of the normal coding task state.
4. **Per-iteration Task Context guide** — a short non-persistent instruction generated from the wakeup trigger, for example `当前是第 2 轮循环，开始检查。`.

Do not create a third ad-hoc runtime-context mechanism for loop. The existing `RuntimeContextBuilder` already separates stable system sections from `task_sections`; `task_sections` render as `Current Task State` and are prepended as a turn overlay, then stripped from semantic history on the next compile. Loop should reuse that channel with a loop-specific task-state builder, and use the current turn's non-persistent overlay for the dynamic resolved prompt, when present, plus the short `Task Context` iteration guide.

The loop-specific `Current Task State` should contain state only, not tool inventory. The retention criterion for every field is: **would the model change its behavior based on this field?** If not, it belongs in runtime metadata, not in the LLM-visible state.

```text
## Current Task State
- Intent: loop
- Trigger: scheduled | manual_resume | retry | startup_recovery
- Iteration: <n>
- Schedule: fixed | dynamic
- Last summary: <previous loop_update.summary; "none" on first iteration>
- Last outcome: continue | blocked | needs_user | failed | completed | stop; "none" on first iteration
- Last error: <none | previous error>
- Last action: <compact summary of previous attempt's primary tool calls; "none" on first iteration>
- Blocked by: <none | blocker description from blocked/needs_user>
- Resume input: <user-provided input; only on manual_resume turns>
- Retries left: <remaining>/<max>
- Workflow: <only when workflow_enabled; reuse existing workflow/todo rendering>
```

Available tools are not rendered in `Current Task State`.

The loop child thread may still keep semantic history from prior iterations: assistant summaries, tool calls/results and lifecycle tool outputs that are useful for continuity. Loop `Current Task State` is not a replacement for that history and must not duplicate full prior messages. It is a compact, authoritative snapshot derived from committed loop/lifecycle state so the model can recover when history is compacted, truncated, reordered, or contains stale failed-attempt details. If history and loop `Current Task State` disagree, lifecycle repository state wins; the model should follow the snapshot and may use history only as supporting detail.

Loop `Current Task State`, dynamic resolved prompt and the per-iteration guide are LLM-visible but not persisted as semantic history. They should be regenerated for every wakeup from durable loop state, scheduler metadata, prompt-source resolution, lifecycle policy and optional workflow state. They may change every iteration, so they should not be cached as if they were the fixed prompt.

### LLM turn contract

From the model's perspective, every loop wakeup has one closed-loop contract:

1. Follow `[FIXED LOOP PROMPT]`; if `[RESOLVED LOOP PROMPT THIS ITERATION]` is present, treat it as the current executable task content.
2. Treat `Current Task State` as authoritative for loop lifecycle, iteration, prior summary, budget and blocker state; use semantic history only as supporting context.
3. Use only the tools actually bound in the model tool list. Tool names are not repeated in `Current Task State`.
4. Do not ask clarifying questions, open checkpoints, request approval or delegate to subagents during an automatic wakeup; if blocked by missing input or permission, record `needs_user` or `blocked`.
5. End the attempt by producing exactly one lifecycle decision via `loop_update`.
6. On automatic wakeups, do not produce parent-visible conversational text unless the outcome is `completed`, `blocked`, `needs_user`, `failed`, or the fixed prompt explicitly requires a report. The durable iteration output is `loop_update.summary` plus any committed tool results.
7. Never restate or append the fixed loop prompt, dynamic resolved prompt, `Current Task State` or iteration guide as transcript user content.

The actual turn input should be a structured, non-transcript wakeup input used to build the `Current Task State` and guide:

```python
class LoopWakeupInput(BaseModel):
    trigger: Literal["scheduled", "manual_resume", "retry", "startup_recovery"]
    iteration: int
    reason: str = ""
```

Implementation options:

1. Preferred: extend `TurnRequest` with an `input_frame`/`transcript_policy` so loop can execute a turn without saving a repeated `HumanMessage`.
2. Migration fallback: pass the short iteration guide as display/current-turn text while explicitly preventing it from being persisted as the user's recurring prompt or runtime state.

For migration, implement this through `ThreadExecutionContext.profile=LOOP_PROFILE` plus a loop-specific `Current Task State` construction path. `LoopPromptPolicy.profile_directive` should carry only stable loop instructions such as `[FIXED LOOP PROMPT]`; `LoopPromptPolicy.task_state_section` should not suppress task state, but should route task-state rendering to the loop-specific builder.

## 9. Resource and Permission Boundaries

Loop inherits stable workspace configuration, but not transient parent state.

Default first-version policy:

- workspace root: same as parent session unless the loop was created from a narrower resource scope;
- tool list: computed by `LoopToolView`, with lifecycle tools and shared retrieval enabled, approval/interaction tools unbound, workflow tools conditional and mutating tools opt-in only when already permitted;
- approvals: loop turns do not initiate approval flows; durable pre-grants may be referenced by policy, while session-scoped temporary grants do not auto-inherit;
- MCP/skills: common catalogs, loop-scoped allowlist and call state;
- workflow: common engine and DAGs, loop-scoped workflow runs only when enabled by `LoopSpec`;
- fixed prompt: stored in loop state and rendered into Base System/profile directive, not transcript user messages;
- todo/workflow/task state: loop thread only;
- UI events: include both `parent_thread_id` and `loop_thread_id`.

Forbidden:

- loop directly mutating parent `TaskState` or workflow runs;
- loop transcript appearing as normal parent messages;
- injecting the fixed `/loop <prompt>` as a fresh user message on each wakeup;
- exposing `clarify`/`checkpoint`/approval request tools/subagents by default; use lifecycle `needs_user` or `blocked` instead;
- exposing `loop_update` outside loop-capable profiles;
- scheduler using `ToolRegistry.filtered_copy()` as isolation proof;
- defaulting loop identity to coding when no goal/workflow is present.

## 10. Closure Gate

本设计在实现者无需重新讨论产品或架构问题即可执行时视为闭环。进入实现前必须满足：

- 责任归属固定：`LoopService` 负责 slash 语义和 run state，`LoopScheduler` 负责 wakeup dispatch，`LoopLifecycle` 负责 lifecycle decision，`AgentRuntime.run_turn()` 仍是唯一 agent execution path。
- 迁移边界明确：当前 `LoopManager` 在迁移期只能作为 adapter/timer 存在；`LoopService` 启用后，`LoopManager` 不能再作为第二个 state source of truth。
- 生命周期输出结构化：continue/completed/blocked/needs_user/failed/stop 都由 `LoopDecision`/`loop_update` 表达，不能依赖自然语言输出或 `schedule_wakeup`。
- 自动 wakeup 是非交互、闭世界执行：需要 clarification、checkpoint approval、runtime approval 或 subagent delegation 的工具不绑定；缺少输入或权限时转成 `needs_user` 或 `blocked`。
- Prompt surface 分离：fixed prompt 进入 loop profile directive，file/script resolved content 是 per-wakeup overlay，loop state 进入 `Current Task State`，这些内容都不能作为重复 parent-session user message 写入。
- 验证路径前置：每个阶段都列出 focused tests；最终完成必须通过 minimum implementation verification 和 full backend。

### Minimum viable implementation closure

第一版可以是 single-process，也可以暂缓 production-grade durable recovery，但必须同时满足：

- `/loop` start/stop/status 经过 `LoopService`，并从 repository-backed state 报告 `loop_thread_id`、mode、iteration、next wakeup、last summary 和 last error。
- Scheduler 用 `LOOP_PROFILE` 和 loop child thread 通过 `AgentRuntime.run_turn()` dispatch loop attempts；任何 loop path 都不能调用 `run_synthetic_turn()`。
- `loop_update` 作为 loop lifecycle tool 存在，对同一个 attempt idempotent，并且是 runtime-backed loop 唯一 dynamic scheduling surface。
- Runtime-backed loop 的 tool binding 默认排除 `schedule_wakeup`、`clarify`、`checkpoint`、approval request tools 和 subagents。
- Tests 证明 parent transcript isolation、fixed prompt non-duplication、dynamic delay behavior，以及 blocked/needs_user pause behavior。

### Final implementation closure

只有 minimum closure 成立，并且剩余 durability contract 已实现或被明确记录为 accepted follow-up 时，implementation 才算完成：

- Wakeups 拥有 durable claim/ack state、attempt ids、lease/fencing fields，以及 retryable vs side-effect-started attempts 的 recovery behavior。
- `LoopManager` 被移除，或缩小为无法拥有 lifecycle state 的 compatibility glue。
- `ToolContext.loop_manager` 和 `schedule_wakeup` 从 runtime-backed loop paths 移除。
- Boundary tests 强制证明不会 mutate parent task/workflow，不会默认使用 coding identity，不会暴露不可用 lifecycle tool，也不会在 automatic wakeups 中绑定 interactive tools。

## 11. Implementation Plan

### Phase L0 — Characterize legacy behavior

Files:

- `src/tests/test_agent/slash/test_slash_loop.py`
- `src/tests/test_agent/loop/test_manager.py`
- `src/tests/test_tools/test_schedule_wakeup.py`

Tasks:

- Add/refresh characterization tests for immediate first fire, fixed/dynamic intervals, prompt source resolution, stop/status and schedule_wakeup bounds.
- Add boundary test proving current loop still uses `run_synthetic_turn()` before migration, so the switch is visible.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/slash/test_slash_loop.py src/tests/test_agent/loop/test_manager.py src/tests/test_tools/test_schedule_wakeup.py -v
```

### Phase L1 — Add loop domain contracts

Files:

- `src/voidx/agent/domain/profile.py`
- `src/voidx/agent/domain/thread.py`
- `src/voidx/agent/domain/loop.py` (new)
- `src/voidx/agent/domain/prompt_policy.py`
- `src/tests/test_agent/domain/test_loop_domain.py` (new)

Tasks:

- Add `LoopSpec`, `LoopDecision`, `LoopLifecycle`, `LoopMode`, `ContinuationPolicy`, `LoopToolView` and validation, including closed-world non-interactive tool binding.
- Add `LoopPromptPolicy`, `LOOP_BASE_SYSTEM_SPEC`, `LOOP_PROFILE` and loop tool policy data declarations.
- Keep profile/data contracts independent of runtime implementation.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/domain/test_loop_domain.py src/tests/test_agent/test_prompt_assembly.py -v
```

### Phase L2 — Add LoopService and repository boundary

Files:

- `src/voidx/agent/application/loop_service.py` (new)
- `src/voidx/agent/loop/repository.py` (new transitional store)
- `src/voidx/agent/slash/handler.py`
- `src/tests/test_agent/test_loop_service.py` (new)
- `src/tests/test_agent/slash/test_slash_loop.py`

Tasks:

- Move start/stop/status semantics behind `LoopService`.
- Keep legacy `LoopManager` as adapter only when repository/runtime dispatcher is disabled.
- Slash handler calls `host.loop_service` first, falling back to legacy manager only during migration.
- Starting a new loop replaces the old active child for the same parent.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/test_loop_service.py src/tests/test_agent/slash/test_slash_loop.py -v
```

### Phase L3 — Runtime-backed dispatcher

Files:

- `src/voidx/agent/loop/scheduler.py` (new or replaces manager responsibilities)
- `src/voidx/agent/runtime/dispatcher.py` (new if not already present)
- `src/voidx/agent/application/loop_service.py`
- `src/voidx/agent/runtime/contracts.py`
- `src/voidx/ui/output/types.py`
- `src/tests/test_agent/loop/test_runtime_scheduler.py` (new)
- `src/tests/test_agent/graph/test_loop_runtime_e2e.py` (new)

Tasks:

- Scheduler enqueues/claims wakeups and calls dispatcher, never `run_synthetic_turn()`.
- Dispatcher builds `TurnRequest(thread=loop_thread, profile=LOOP_PROFILE, context=ThreadExecutionContext(...))` and calls shared `AgentRuntime.run_turn()`.
- Loop Base System/profile directive includes only stable loop instruction content; dynamic file/script PromptSource output goes into a non-persistent current-turn overlay; loop-specific `Current Task State` carries state only; per-iteration `Task Context` carries the short wakeup guide.
- Verify loop-thread messages/runtime do not leak into parent transcript and fixed loop prompt is not duplicated per wakeup.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/loop/test_runtime_scheduler.py src/tests/test_agent/graph/test_loop_runtime_e2e.py -v
```

### Phase L4 — loop_update tool

Files:

- `src/voidx/tools/loop_update.py` (new)
- `src/voidx/tools/schedule_wakeup.py` (remove after migration)
- `src/voidx/tools/base.py`
- `src/voidx/tools/registry.py`
- `src/tests/test_tools/test_loop_update.py` (new)
- `src/tests/test_tools/test_schedule_wakeup.py` (remove or replace with loop_update coverage)

Tasks:

- Add `loop_update` as the single loop lifecycle tool that records a decision on the runtime-scoped lifecycle controller.
- Stop exposing `schedule_wakeup` in runtime-backed loops; migrate dynamic scheduling to `loop_update(next_delay_seconds=...)`.
- Register `loop_update` only for profiles/tool views that include loop capability; `clarify`/`checkpoint`/approval request tools/subagents remain unbound for automatic wakeups.

Verification:

```bash
./test.py --backend -- src/tests/test_tools/test_loop_update.py src/tests/test_tools/test_loop_registry.py -v
```

### Phase L5 — Contract legacy surface

Files:

- `src/voidx/agent/loop/manager.py`
- `src/voidx/agent/infrastructure/langgraph/execution.py`
- `src/voidx/tools/base.py`
- `src/tests/test_agent/domain/test_import_boundaries.py`

Tasks:

- Remove or shrink `LoopManager` to timer-only compatibility if no legacy path remains.
- Remove `host.run_synthetic_turn()` from loop scheduler path.
- Remove `ToolContext.loop_manager`; runtime-backed loops use `loop_controller` through `loop_update` only.
- Add import/boundary tests that loop code cannot call `run_synthetic_turn()` or mutate host `_task_state`.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/domain/test_import_boundaries.py src/tests/test_agent/loop src/tests/test_tools/test_loop_update.py -v
```

## 12. Test Matrix

Required focused tests:

| Area | Tests |
|---|---|
| Slash compatibility | start fixed/dynamic, stop/status, replacement semantics, invalid prompt |
| Prompt source | text/file/script re-resolution, dynamic file/script output outside stable prefix, workspace boundary, prompt source failure classification |
| Lifecycle | `loop_update` decision validation, `next_delay_seconds` honored for dynamic / ignored for fixed, missing decision fallback, terminal idempotency |
| Scheduler | immediate wakeup, fixed interval, dynamic advisory delay, claim/ack, no concurrent same-thread turn |
| Runtime integration | child thread `AgentRuntime.run_turn`, loop Base System/profile directive for fixed instruction, loop-specific `Current Task State` overlay, non-persistent per-iteration `Task Context` guide, dynamic resolved prompt overlay, fixed prompt not persisted per wakeup, parent transcript isolation |
| Tool migration | `LoopToolView`, `loop_update` as single lifecycle tool, disabled `clarify`/`checkpoint`/approval request tools/subagents, closed-world binding, MCP/skill common catalog, workflow conditional exposure |
| Failure/recovery | turn exception, prompt source exception, cancellation while running, crash after commit before wakeup ack |
| Permissions | no temporary grant inheritance, mutating tools opt-in only when already permitted, no mid-turn approval flow, missing permission becomes `blocked`/`needs_user` |

Minimum implementation verification before merge:

```bash
./test.py --backend -- src/tests/test_agent/loop src/tests/test_agent/slash/test_slash_loop.py src/tests/test_tools/test_loop_update.py -v
./test.py --backend -- src/tests/test_agent/graph/test_loop_runtime_e2e.py -v
./test.py --backend -- src/tests/test_agent -q
```

Run full backend before final submission:

```bash
./test.py --backend
```

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Two loop sources of truth during migration | `LoopService` is the only slash owner; legacy manager is adapter-only and has deletion criteria in Phase L5 |
| Runtime-backed loop accidentally inherits coding state | loop thread owns runtime state; tests assert parent transcript/task/workflow isolation and loop-specific Base System/tool view |
| Dynamic wakeup behavior changes existing prompts | migrate prompts to `loop_update(outcome="continue", next_delay_seconds=...)`; fixed loops ignore `next_delay_seconds`, dynamic loops honor it |
| Durable scheduler overbuilt for first version | first implementation may be single-process, but contracts keep wakeup id, attempt id and state fields |
| External side effects replay after crash | mark side-effect start before tools; recovery refuses automatic replay when side effects may have happened |
| User cannot see what loop is doing | status includes `loop_thread_id`, state, iteration, next wakeup, summary and last error |

## 14. Acceptance Criteria

- Existing `/loop` syntax and immediate-first-fire behavior remain compatible.
- Runtime-backed loop calls `AgentRuntime.run_turn()` with `LOOP_PROFILE`, `LOOP_BASE_SYSTEM_SPEC`, `LoopToolView` and the loop `AgentThread`.
- Parent session transcript does not receive loop user prompts or tool outputs as ordinary messages, and the fixed `/loop <prompt>` is not appended as a new user message every iteration.
- `/loop status` is derived from durable loop state, not only in-memory task state.
- `loop_update` is the single lifecycle tool and can produce continue/completed/blocked/needs_user/failed/stop decisions; `next_delay_seconds` is honored for dynamic loops and ignored for fixed loops; missing decision follows safe defaults.
- Runtime-backed loops do not expose `schedule_wakeup`; dynamic scheduling is expressed only through `loop_update(next_delay_seconds=...)`.
- Automatic loop wakeups never bind `clarify`, `checkpoint`, approval request tools or subagents; missing permission/input is expressed through `loop_update(blocked|needs_user)`.
- Boundary tests prove loop scheduler does not call `run_synthetic_turn()` after Phase L5 and automatic loop wakeups do not bind `clarify`/`checkpoint`/approval/subagent tools.
- Minimum viable closure is explicitly satisfied before replacing the legacy path in user-facing flows.
- Final implementation closure is satisfied, or each deferred durability item is listed as an accepted follow-up with owner, risk and verification command.
- Focused loop/tool/agent tests and full backend suite pass before implementation is considered complete.
