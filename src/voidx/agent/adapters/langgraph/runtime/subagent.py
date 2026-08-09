"""Child agent execution loop."""

from __future__ import annotations

from voidx.agent.domain.ui_events import StatusFinished, StatusUpdated

import asyncio
import json
import re
import time
from typing import Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.application.agents import AgentDef, child_run_agent_def
from voidx.agent.application.prompts import build_base_system, child_workflow_runtime, persona_prompt
from voidx.agent.adapters.langgraph.runtime.runtime_guards import (
    NoProgressState,
    RuntimeGuardState,
    WallClockGuardState,
    build_failure_key,
    cycle_summary_from_tools,
)
from voidx.agent.adapters.langgraph.runtime.budget_convergence import (
    BudgetConvergenceState,
    BudgetReading,
    ConvergenceDecision,
    decide_convergence,
)
from voidx.agent.adapters.langgraph.runtime.subagent_convergence import (
    convergence_guidance,
    subagent_convergence_action,
)
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text, stream_llm
from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind, _classify_llm_error, _LLM_MAX_RETRIES, _llm_retry_delay, _llm_retry_sleep_delay, _clean_error_message
from voidx.agent.adapters.langgraph.runtime.todo_events import todo_updated_event
from voidx.agent.application.todo_state import todo_run_state_from_result
from voidx.agent.domain.workflow_utils import active_workflow_names
from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _execute_file_isolated_batch
from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool
from voidx.agent.adapters.langgraph.runtime.tool_executor.workflow import (
    _state_update_from_executed_tools,
)
from voidx.agent.domain.task.intent import PersonaName, TaskIntent
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.agent.application.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
)
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, TaskState
from voidx.agent.domain.task.todo import TodoRunState
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.agent.application.tool_messages import sanitize_tool_message_content
from voidx.agent.adapters.tools.result_storage import maybe_persist_tool_result
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.adapters.langgraph.runtime.tool_surface import (
    ToolSurfaceContext,
    resolve_tool_surface,
)
from voidx.llm.domain.provider import get_context_limit, resolve_protocol
from voidx.agent.application.instruction import WorkflowRuntimeContext
from voidx.llm.usage import (
    UsageStats,
    estimate_context_tokens_with_tools,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.agent.adapters.persistence.context_frame_repository import save_context_frame_from_messages
from voidx.agent.adapters.persistence.subagent_repository import append_subagent_event
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.application.registry import ToolRegistry
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.plugins import bind_agent_tool_runtime
from voidx.agent.adapters.tools.subagent_message import MessageTool
from voidx.agent.ports.ui import AgentUiPort, NullAgentUiPort


create_chat_model = None
bind_scoped_tools = None


_RESULT_CONTRACT_RETRY_LIMIT = 2
_BLOCKED_CHILD_TOOLS = {"agent", "clarify", "checkpoint"}
_CHILD_WORKFLOW_MODE_BY_JOIN = {
    "review": "review",
    "debug": "debug",
    "tdd": "implement",
}


def _child_workflow_mode(route: WorkflowRoute | None) -> str:
    join = route.join.strip().lower() if route is not None else ""
    return _CHILD_WORKFLOW_MODE_BY_JOIN.get(join, "review")


def _workflow_summary_name(summary: str) -> str:
    return summary.split(" ", 1)[0].strip().lower()


class SubagentConfig(Protocol):
    model: Any
    workspace: str
    user_profile: Any
    lsp_format_after_edit: bool
    subagent_budget: Any
    sandbox_readable_files: list[str]
    sandbox_readable_dirs: list[str]
    sandbox_writable_files: list[str]
    sandbox_writable_dirs: list[str]

    def model_copy(self, *, deep: bool = False) -> "SubagentConfig": ...


async def run_subagent(
    agent_def: AgentDef,
    task_description: str,
    api_key: str | None,
    config: SubagentConfig,
    tracker: TaskTracker | None = None,
    runtime_persona: str | None = None,
    *,
    goal_resolution: GoalResolution,
    result_contract,
    run_metadata: dict[str, object] | None = None,
    capture_tree: Any | None = None,
    parent_node=None,
    sub_messages: list | None = None,
    authorize_tools=None,
    debug: bool = True,
    agent_id: int = -1,
    session_id: str | None = None,
    usage_stats: UsageStats | None = None,
    lsp_manager=None,
    parent_tools: ToolRegistry | None = None,
    workflow_runtime_context: WorkflowRuntimeContext | None = None,
    todo_state_sink=None,
    permission_snapshot=None,
    agent_run_id: str | None = None,
    agent_gateway=None,
    ui_port: AgentUiPort | None = None,
    model_factory=None,
    scoped_tools_binder=None,
) -> str:
    """Run a child agent in its own message context."""
    ui_port = ui_port or NullAgentUiPort()
    ui_factories = ui_port if hasattr(ui_port, "streaming_renderer") else NullAgentUiPort()
    agent_def = child_run_agent_def(agent_def)
    run_identity = agent_run_id or f"agent_{agent_id}"
    persona = (runtime_persona or PersonaName.EXPLORE).strip() or PersonaName.EXPLORE
    model_cfg = config.model.model_copy()
    if agent_def.model:
        model_cfg.model = agent_def.model

    # Child agents inherit the parent registry through a copy so child-only tools
    # do not leak back into the main agent registry.
    if parent_tools is not None:
        agent_tools = parent_tools.filtered_copy(set(parent_tools.ids()))
    else:
        agent_tools = ToolRegistry()
    if hasattr(agent_tools, "register"):
        message_tool = MessageTool(
            description=(
                "Send a message, question, answer, or final result to your parent agent, "
                "or read messages from your parent."
            )
        )
        if agent_tools.get(message_tool.id) is None:
            agent_tools.register_plugin(message_tool)
        else:
            agent_tools.replace(message_tool.id, message_tool, message_tool.description, message_tool.parameters_schema())
    # Child constraints are fixed: delegation/interaction tools never reach a child,
    # regardless of AgentDef.can_delegate.
    agent_tools = agent_tools.filtered_copy(set(agent_tools.ids()) - _BLOCKED_CHILD_TOOLS)
    resolved_model_factory = model_factory or create_chat_model
    if resolved_model_factory is None:
        raise RuntimeError("model_factory is required")
    model = resolved_model_factory(api_key, model_cfg)
    tool_defs = resolve_tool_surface(
        agent_tools,
        ToolSurfaceContext(
            runtime_profile=RuntimeProfile(profile_id="coding", revision=1, name="Coding"),
            child_agent=True,
            lsp_manager=lsp_manager,
            model_protocol=resolve_protocol(model_cfg),
        ),
    ).definitions

    if sub_messages is None:
        sub_messages = []

    messages = [HumanMessage(content=_task_payload(task_description, result_contract))]

    context_config = config.model_copy(deep=True)
    context_config.model = model_cfg
    interaction_mode = InteractionMode.PLAN if persona == PersonaName.PLAN else InteractionMode.AUTO
    workflow_context = workflow_runtime_context or WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])
    context_cache = ContextCompilerCache()
    plan = goal_resolution.plan
    sub_task_state = TaskState(
        current_intent=goal_resolution.intent.type,
        current_goal=goal_resolution.goal,
        workflow_route=WorkflowRoute(join=plan.join, leave=plan.leave) if plan is not None else None,
        workflow_runs={run.name: run for run in workflow_context.runs},
    )
    budget = config.subagent_budget
    context_limit = get_context_limit(
        model_cfg.provider,
        resolve_protocol(model_cfg),
        model_cfg.context_window,
    )
    convergence_state = BudgetConvergenceState()
    started_at = time.monotonic()
    guard_state = RuntimeGuardState(
        no_progress=NoProgressState(for_subagent=True),
        wall_clock=WallClockGuardState(
            started_at=started_at,
            limit_seconds=budget.wall_clock_seconds,
        ),
    )
    pending_guard_guidance: list[str] = []
    contract_retry_count = 0
    has_successful_tool_work = False

    context, context_cache = RuntimeContextBuilder(
        config=context_config,
        workspace=config.workspace,
        base_system_prompt=build_base_system(context_config.user_profile.language),
        workflow_runtime=child_workflow_runtime(_child_workflow_mode(sub_task_state.workflow_route)),
        persona_prompt=persona_prompt(),
        persona=persona,
        interaction_mode=interaction_mode,
        workflow_runs=workflow_context.runs,
        active_workflow_summaries=workflow_context.active,
        task_state=sub_task_state,
    ).build_incremental(context_cache)
    context.apply_to_messages(messages)

    def refresh_context() -> None:
        nonlocal context, context_cache
        active_names = [
            name
            for name in active_workflow_names(sub_task_state.workflow_runs)
        ]
        active_name_set = {name.lower() for name in active_names}
        active_summaries = [
            summary
            for summary in workflow_context.active
            if _workflow_summary_name(summary) in active_name_set
        ]
        known_active = {_workflow_summary_name(summary) for summary in active_summaries}
        for name in active_names:
            normalized_name = name.lower()
            if normalized_name not in known_active:
                active_summaries.append(f"{name} (child state)")
        context, context_cache = RuntimeContextBuilder(
            config=context_config,
            workspace=config.workspace,
            base_system_prompt=build_base_system(context_config.user_profile.language),
            workflow_runtime=child_workflow_runtime(_child_workflow_mode(sub_task_state.workflow_route)),
            persona_prompt=persona_prompt(),
            persona=persona,
            interaction_mode=interaction_mode,
            workflow_runs=list(sub_task_state.workflow_runs.values()),
            active_workflow_summaries=active_summaries,
            task_state=sub_task_state,
        ).build_incremental(context_cache)
        context.apply_to_messages(messages)

    def apply_state_update(update: dict) -> bool:
        nonlocal ctx, persona
        if not update:
            return False
        if "persona" in update and update.get("persona"):
            persona = str(update["persona"])
        if "task_intent" in update:
            try:
                sub_task_state.current_intent = TaskIntent(update["task_intent"])
            except (TypeError, ValueError):
                pass
        if "current_goal" in update:
            raw_goal = update.get("current_goal")
            sub_task_state.current_goal = (
                GoalSpec.model_validate(raw_goal) if raw_goal is not None else None
            )
        if "workflow_route" in update:
            sub_task_state.workflow_route = (
                WorkflowRoute.model_validate(update["workflow_route"])
                if update.get("workflow_route")
                else None
            )
        if "workflow_runs" in update:
            sub_task_state.workflow_runs = {
                run.name: run
                for run in update.get("workflow_runs") or []
            }
        if "todo_state" in update:
            raw_todo = update.get("todo_state")
            sub_task_state.todo_state = (
                TodoRunState.model_validate(raw_todo) if raw_todo is not None else None
            )
        ctx = ctx.model_copy(update={"persona": persona, "turn_count": step})
        ctx.runtime.task_intent = sub_task_state.current_intent.value
        ctx.runtime.goal_type = (
            sub_task_state.workflow_route.join
            if sub_task_state.workflow_route is not None
            else ""
        )
        ctx.runtime.goal_target = sub_task_state.current_goal.label if sub_task_state.current_goal else ""
        ctx.runtime.active_workflow_names = active_workflow_names(sub_task_state.workflow_runs)
        ctx.runtime.workflow_runs = list(sub_task_state.workflow_runs.values())
        ctx.runtime.workflow_route = (
            sub_task_state.workflow_route.model_dump(mode="json")
            if sub_task_state.workflow_route is not None
            else None
        )
        refresh_context()
        return any(
            field in update
            for field in (
                "persona",
                "task_intent",
                "current_goal",
                "workflow_route",
                "workflow_runs",
                "todo_state",
            )
        )

    refresh_context()

    snapshot_grants = None
    if permission_snapshot is not None:
        snapshot_grants = permission_snapshot.get_access_grants()

    agent_runtime = AgentToolRuntime(
        subagent_transport=agent_gateway,
        run_id=run_identity,
        access_grants=(
            (lambda: permission_snapshot.get_access_grants())
            if permission_snapshot is not None
            else None
        ),
        revocation_epoch=(
            (lambda: permission_snapshot.revocation_epoch)
            if permission_snapshot is not None
            else None
        ),
        task_intent=sub_task_state.current_intent.value,
        goal_type=(
            sub_task_state.workflow_route.join
            if sub_task_state.workflow_route is not None
            else ""
        ),
        goal_target=sub_task_state.current_goal.label if sub_task_state.current_goal else "",
        active_workflow_names=active_workflow_names(sub_task_state.workflow_runs),
        workflow_runs=list(sub_task_state.workflow_runs.values()),
        workflow_route=(
            sub_task_state.workflow_route.model_dump(mode="json")
            if sub_task_state.workflow_route is not None
            else None
        ),
    )
    bind_agent_tool_runtime(agent_tools, agent_runtime)

    lsp_operations = None
    if lsp_manager is not None:
        from voidx.lsp.application.service import LspOperationsService

        lsp_operations = LspOperationsService(lsp_manager)

    resolved_scoped_tools_binder = scoped_tools_binder or bind_scoped_tools
    if resolved_scoped_tools_binder is None:
        raise RuntimeError("scoped_tools_binder is required")
    resolved_scoped_tools_binder(
        agent_tools,
        authorization=AuthorizationRuntime(
            read_files=list(snapshot_grants.readable_files) if snapshot_grants is not None else list(config.sandbox_readable_files),
            read_dirs=list(snapshot_grants.readable_dirs) if snapshot_grants is not None else list(config.sandbox_readable_dirs),
            write_files=list(snapshot_grants.writable_files) if snapshot_grants is not None else list(config.sandbox_writable_files),
            write_dirs=list(snapshot_grants.writable_dirs) if snapshot_grants is not None else list(config.sandbox_writable_dirs),
            access_grants_reader=(
                (lambda: permission_snapshot.get_access_grants())
                if permission_snapshot is not None
                else None
            ),
            revocation_epoch_reader=(
                (lambda: permission_snapshot.revocation_epoch)
                if permission_snapshot is not None
                else None
            ),
        ),
        files=FileStateStore(),
        process_sandbox=None,
        lsp_operations=lsp_operations,
        format_after_edit_enabled=config.lsp_format_after_edit,
    )

    ctx = ToolContext(
        workspace=config.workspace,
        runtime=agent_runtime,
        session_id=session_id or "default",
        persona=persona,
        interaction_mode=interaction_mode.value,
        turn_count=0,
    )

    # Register with tracker

    async def report_result(text: str, *, finish_reason: str | None = None) -> None:
        if agent_gateway is None or not run_identity:
            return
        current = agent_gateway.lookup_run(run_identity)
        if current is None:
            return
        if current.status in {"completed", "failed", "cancelled"}:
            return
        parent_run_id = current.parent_run_id
        if not parent_run_id:
            return
        payload: dict[str, object] = {"result": text}
        if finish_reason and finish_reason != "final_answer":
            payload["finish_reason"] = finish_reason
        await agent_gateway.send(
            sender_run_id=run_identity,
            target_run_id=parent_run_id,
            message_type="result",
            payload=payload,
        )
    task_id = f"sub_{agent_def.name}_{persona}_{int(time.time())}"
    if tracker:
        tracker.start(task_id, persona, task_description)

    def mark_finished(reason: str) -> None:
        if run_metadata is not None:
            run_metadata.update({
                "finish_reason": reason,
            })


    def drain_guard_guidance() -> list[HumanMessage]:
        if not pending_guard_guidance:
            return []
        pending_guard_guidance.clear()
        if convergence_state.soft_prompted:
            return []
        convergence_state.soft_prompted = True
        return [
            HumanMessage(
                content=convergence_guidance(
                    final=False,
                    language=str(getattr(config.user_profile, "language", "") or ""),
                ),
                additional_kwargs={GUIDANCE_MARKER: True},
            )
        ]

    def record_convergence(decision: ConvergenceDecision) -> str:
        action = subagent_convergence_action(decision)
        if action != "continue" and run_metadata is not None:
            events = run_metadata.setdefault("convergence_events", [])
            if isinstance(events, list):
                events.append({
                    "action": action,
                    "level": decision.level,
                    "dimensions": sorted(decision.triggered_dimensions),
                    "metadata": dict(decision.metadata),
                })
        return action

    def hard_wall_clock_decision() -> ConvergenceDecision | None:
        elapsed = max(0.0, time.monotonic() - started_at)
        if elapsed < budget.wall_clock_seconds:
            return None
        return decide_convergence(
            [
                BudgetReading(
                    dimension="wall_clock",
                    current=elapsed,
                    soft_limit=budget.wall_clock_seconds * budget.soft_warn_ratio,
                    hard_limit=budget.wall_clock_seconds,
                )
            ],
            convergence_state,
        )

    def finish_reason_for(decision: ConvergenceDecision) -> str:
        if "context" in decision.triggered_dimensions:
            return "context_limit"
        if "wall_clock" in decision.triggered_dimensions:
            return "time_limit"
        return "step_limit"

    async def finalize(finish_reason: str) -> str:
        guidance = HumanMessage(
            content=convergence_guidance(
                final=True,
                language=str(getattr(config.user_profile, "language", "") or ""),
            ),
            additional_kwargs={GUIDANCE_MARKER: True},
        )
        final_messages = [*messages, guidance]
        renderer = ui_factories.streaming_renderer(
            ui_port.console,
            debug=debug,
            agent_id=agent_id,
            headless=True,
        )
        try:
            assistant_msg = await stream_llm(
                model,
                final_messages,
                renderer,
                resolve_protocol(model_cfg),
                ui_port=ui_port,
            )
            text = extract_text(assistant_msg).strip()
            if usage_stats is not None:
                final_context_tokens = estimate_context_tokens_with_tools(
                    final_messages,
                    [],
                    model_cfg.model,
                )
                usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=final_context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, model_cfg.model),
                    messages=final_messages,
                    model=model_cfg.model,
                    cache_key=f"{model_cfg.provider}/{model_cfg.model}",
                )
            if text:
                messages.append(assistant_msg)
                sub_messages.append(assistant_msg)
        except Exception as exc:
            if _classify_llm_error(exc) == LLMErrorKind.UNKNOWN:
                raise
            text = ""
        if not text:
            text = _partial_result_from_messages(messages)
        if tracker:
            tracker.update(task_id, last_output=text[:200])
            tracker.finish(task_id, "completed")
        await report_result(text, finish_reason=finish_reason)
        mark_finished(finish_reason)
        return text

    try:
        step = 0
        while True:
            if capture_tree and parent_node is not None:
                capture = ui_factories.capture_console(capture_tree, parent_node, agent_id=agent_id)
                capture.step_header(persona)
            else:
                ui_port.ui.step_header(persona)

            next_step = step + 1
            ctx = ctx.model_copy(update={"turn_count": next_step})
            llm_messages = [*messages, *drain_guard_guidance()]
            renderer = ui_factories.streaming_renderer(
                ui_port.console,
                debug=debug,
                agent_id=agent_id,
                headless=True,
            )
            context_tokens = estimate_context_tokens_with_tools(
                llm_messages,
                tool_defs,
                model_cfg.model,
            )
            elapsed = max(0.0, time.monotonic() - started_at)
            decision = decide_convergence(
                [
                    BudgetReading(
                        dimension="step",
                        current=step,
                        soft_limit=budget.step_limit * budget.soft_warn_ratio,
                        hard_limit=budget.step_limit,
                    ),
                    BudgetReading(
                        dimension="wall_clock",
                        current=elapsed,
                        soft_limit=budget.wall_clock_seconds * budget.soft_warn_ratio,
                        hard_limit=budget.wall_clock_seconds,
                    ),
                    BudgetReading(
                        dimension="context",
                        current=context_tokens,
                        soft_limit=context_limit * budget.context_soft_ratio,
                        hard_limit=context_limit * budget.context_hard_ratio,
                    ),
                ],
                convergence_state,
            )
            action = record_convergence(decision)
            if action == "finalize":
                return await finalize(finish_reason_for(decision))
            if action == "guide":
                llm_messages.append(
                    HumanMessage(
                        content=convergence_guidance(
                            final=False,
                            language=str(getattr(config.user_profile, "language", "") or ""),
                        ),
                        additional_kwargs={GUIDANCE_MARKER: True},
                    )
                )
                context_tokens = estimate_context_tokens_with_tools(
                    llm_messages,
                    tool_defs,
                    model_cfg.model,
                )
                if context_tokens >= context_limit * budget.context_hard_ratio:
                    hard_context = decide_convergence(
                        [
                            BudgetReading(
                                dimension="context",
                                current=context_tokens,
                                soft_limit=context_limit * budget.context_soft_ratio,
                                hard_limit=context_limit * budget.context_hard_ratio,
                            )
                        ],
                        convergence_state,
                    )
                    if record_convergence(hard_context) == "finalize":
                        return await finalize("context_limit")
            step = next_step
            model_with_tools = model.bind_tools(tool_defs) if tool_defs else model
            if usage_stats is not None:
                usage_stats.update_context(context_tokens)
            if session_id:
                await save_context_frame_from_messages(
                    session_id=session_id,
                    frame_kind="worker",
                    agent_persona=persona,
                    provider=model_cfg.provider,
                    model=model_cfg.model,
                    messages=llm_messages,
                    token_estimate=context_tokens,
                    metadata={
                        "step": step,
                        "tool_count": len(tool_defs),
                        "agent_id": agent_id,
                    },
                )
            llm_failed_attempts = 0
            retry_status_active = False
            while True:
                try:
                    assistant_msg = await stream_llm(
                        model_with_tools,
                        llm_messages,
                        renderer,
                        resolve_protocol(model_cfg),
                        ui_port=ui_port,
                    )
                    if retry_status_active and ui_port.via_events():
                        await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                    break
                except Exception as e:
                    kind = _classify_llm_error(e)
                    if kind == LLMErrorKind.UNKNOWN:
                        raise
                    if kind in {LLMErrorKind.NON_RETRYABLE, LLMErrorKind.CONTEXT_OVERFLOW}:
                        if retry_status_active and ui_port.via_events():
                            await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                        partial = _partial_result_from_messages(messages, require_findings=True)
                        if kind == LLMErrorKind.CONTEXT_OVERFLOW:
                            text = partial or _partial_result_from_messages(messages)
                            if tracker:
                                tracker.update(task_id, last_output=text[:200])
                                tracker.finish(task_id, "completed")
                            await report_result(text, finish_reason="context_limit")
                            mark_finished("context_limit")
                            return text
                        if partial:
                            if tracker:
                                tracker.update(task_id, last_output=partial[:200])
                                tracker.finish(task_id, "completed")
                            await report_result(partial, finish_reason="error_recovered")
                            mark_finished("error_recovered")
                            return partial
                        raise
                    if llm_failed_attempts < _LLM_MAX_RETRIES:
                        llm_failed_attempts += 1
                        delay = _llm_retry_delay(llm_failed_attempts)
                        delay_str = str(int(delay)) if delay == int(delay) else str(delay)
                        retry_detail = f"retrying in {delay_str}s: {_clean_error_message(e)}"
                        if ui_port.via_events():
                            retry_status_active = True
                            await ui_port.events.emit(StatusUpdated(
                                status_id="llm:retry",
                                label="Retrying",
                                detail=retry_detail,
                            ))
                        else:
                            ui_port.ui.print(f"[dim]Retrying ({retry_detail})[/dim]")
                        await asyncio.sleep(_llm_retry_sleep_delay(delay))
                        continue
                    if retry_status_active and ui_port.via_events():
                        await ui_port.events.emit(StatusFinished(status_id="llm:retry"))
                    partial = _partial_result_from_messages(messages, require_findings=True)
                    if partial:
                        if tracker:
                            tracker.update(task_id, last_output=partial[:200])
                            tracker.finish(task_id, "completed")
                        await report_result(partial, finish_reason="error_recovered")
                        mark_finished("error_recovered")
                        return partial
                    raise
            post_llm_decision = hard_wall_clock_decision()
            if (
                post_llm_decision is not None
                and record_convergence(post_llm_decision) == "finalize"
            ):
                return await finalize("time_limit")
            if usage_stats is not None:
                usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, model_cfg.model),
                    messages=llm_messages,
                    model=model_cfg.model,
                    cache_key=f"{model_cfg.provider}/{model_cfg.model}",
                )
            messages.append(assistant_msg)
            sub_messages.append(assistant_msg)
            if session_id:
                tool_calls = getattr(assistant_msg, "tool_calls", None) or []
                tool_refs = [
                    {"name": tc.get("name", ""), "id": tc.get("id", "")}
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                await append_subagent_event(session_id, run_identity, {
                    "type": "assistant_message",
                    "step": step,
                    "content_preview": (assistant_msg.content or "")[:200],
                    "tool_call_refs": tool_refs,
                })

            if not assistant_msg.tool_calls:
                text = extract_text(assistant_msg)
                if has_successful_tool_work and not _satisfies_result_contract(text, result_contract):
                    if contract_retry_count < _RESULT_CONTRACT_RETRY_LIMIT:
                        contract_retry_count += 1
                        guidance = _result_contract_retry_message(result_contract)
                        messages.append(HumanMessage(content=guidance))
                        continue
                    if tracker:
                        tracker.update(task_id, last_output=text[:200])
                        tracker.finish(task_id, "completed")
                    await report_result(text, finish_reason="contract_unsatisfied")
                    mark_finished("contract_unsatisfied")
                    return text
                if tracker:
                    tracker.update(task_id, last_output=text[:200])
                    tracker.finish(task_id, "completed")
                await report_result(text)
                mark_finished("final_answer")
                return text

            # Update tracker with preview
            text_preview = extract_text(assistant_msg)[:200]
            if tracker and text_preview:
                tracker.update(task_id, last_output=text_preview)

            repetitive_decision = guard_state.repetitive_tools.decision_for_pending(list(assistant_msg.tool_calls))
            if repetitive_decision.action in {"skip", "terminate"}:
                guard_tool_msgs = [
                    ToolMessage(
                        content=sanitize_tool_message_content(
                            convergence_guidance(
                                final=False,
                                language=str(getattr(config.user_profile, "language", "") or ""),
                            ),
                            workspace=ctx.workspace,
                        ),
                        tool_call_id=tc.get("id", ""),
                        status="error",
                    )
                    for tc in assistant_msg.tool_calls
                ]
                messages.extend(guard_tool_msgs)
                sub_messages.extend(guard_tool_msgs)
                if repetitive_decision.action == "terminate":
                    return await finalize("guard_terminated")
                continue

            if authorize_tools:
                approved, denied = await authorize_tools(assistant_msg.tool_calls)
            else:
                approved = list(assistant_msg.tool_calls)
                denied = []

            def result_ok(result) -> bool:
                metadata = getattr(result, "metadata", {}) or {}
                if metadata.get("error") or metadata.get("blocked") or metadata.get("timeout"):
                    return False
                if "exit_code" in metadata:
                    try:
                        return int(metadata.get("exit_code") or 0) == 0
                    except (TypeError, ValueError):
                        return False
                return True

            async def run_one(tc):
                tid = tc.get("name", "")
                targs = tc.get("args", {})
                cid = tc.get("id", "")
                if capture_tree and parent_node is not None:
                    capture.tool_call(tid, targs, tool_call_id=cid)
                try:
                    result = await agent_tools.execute_tool(tid, targs, ctx)
                except Exception as exc:
                    result = ToolResult(
                        output=f"Tool execution error: {exc}",
                        metadata={
                            "error": True,
                            "exception": exc.__class__.__name__,
                        },
                    )
                todo_state = todo_run_state_from_result(result) if tid == "todo" else None
                if todo_state_sink is not None and todo_state is not None and todo_state.total > 0:
                    todo_state_sink(todo_state)
                if ui_port.via_events() and tid == "todo":
                    todo_event = todo_updated_event(result, agent_id=agent_id)
                    if todo_event is not None:
                        ui_port.events.emit_direct(todo_event)
                if session_id:
                    await append_subagent_event(session_id, run_identity, {
                        "type": "tool_result",
                        "step": step,
                        "tool_name": tid,
                        "tool_call_id": cid,
                        "args": targs,
                        "content": result.output,
                        "summary": result.summary,
                        "ok": result_ok(result),
                    })
                if capture_tree and parent_node is not None:
                    capture.tool_done(tid, 0.0, True, tool_call_id=cid)
                    capture.tool_result(result.output, tool_call_id=cid)
                llm_content = maybe_persist_tool_result(
                    result.output,
                    cid,
                    tid,
                    session_id=ctx.session_id,
                    workspace=ctx.workspace,
                )
                next_step_hint = str(getattr(result, "next_step_hint", "") or "").strip()
                if next_step_hint:
                    llm_content = f"{llm_content}\n\nNext step hint: {next_step_hint}"
                return {
                    "tool_message": ToolMessage(
                        content=sanitize_tool_message_content(llm_content, workspace=ctx.workspace),
                        tool_call_id=cid,
                        status="success" if result_ok(result) else "error",
                    ),
                    "result": result,
                    "tool_call": tc,
                    "todo_state": todo_state,
                }

            result_tool_call = next((tc for tc in approved if _is_message_result_tool_call(tc)), None)
            if result_tool_call is not None:
                approved = [result_tool_call]

            executed = await _execute_file_isolated_batch(approved, run_one)
            executed = [item for item in executed if item is not None]
            denied_msgs = [
                ToolMessage(
                    content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                    tool_call_id=tc.get("id", ""),
                    status="error",
                )
                for tc, reason in denied
            ]
            tool_msgs = [item["tool_message"] for item in executed]
            messages.extend(tool_msgs + denied_msgs)
            sub_messages.extend(tool_msgs + denied_msgs)


            child_executed = [
                _ExecutedTool(
                    message=item["tool_message"],
                    result=item["result"],
                    tool_call=item["tool_call"],
                    todo_state=item["todo_state"],
                )
                for item in executed
            ]
            previous_todo_state = sub_task_state.todo_state
            state_update = _state_update_from_executed_tools(
                child_executed,
                current_workflow_runs=sub_task_state.workflow_runs,
                current_workflow_route=sub_task_state.workflow_route,
                turn_count=step,
            )
            workflow_changed = apply_state_update(state_update)

            post_tool_decision = hard_wall_clock_decision()
            if (
                post_tool_decision is not None
                and record_convergence(post_tool_decision) == "finalize"
            ):
                return await finalize("time_limit")

            for item in executed:
                metadata = getattr(item["result"], "metadata", {}) or {}
                if (
                    item["tool_call"].get("name") == "message"
                    and metadata.get("message_type") == "result"
                    and result_ok(item["result"])
                ):
                    text = str(getattr(item["result"], "output", "") or "")
                    if agent_gateway is not None:
                        try:
                            current = agent_gateway.lookup_run(run_identity)
                            if current is not None:
                                text = _result_text(current.result) or text
                        except Exception:
                            pass
                    if tracker:
                        tracker.update(task_id, last_output=text[:200])
                        tracker.finish(task_id, "completed")
                    mark_finished("message_result")
                    return text
            for item in executed:
                metadata = getattr(item["result"], "metadata", {}) or {}
                if metadata.get("runtime_guard"):
                    continue
                if result_ok(item["result"]):
                    has_successful_tool_work = True
                    guard_state.tool_failures.record_success(item["tool_call"])
                    continue
                key = build_failure_key(item["tool_call"], item["result"])
                guidance = guard_state.tool_failures.record_failure(
                    key,
                    str(getattr(item["result"], "summary", "") or getattr(item["result"], "output", ""))[:500],
                )
                if guidance is not None:
                    pending_guard_guidance.append(guidance.message)


            next_todo_state = sub_task_state.todo_state
            for item in executed:
                if item["todo_state"] is not None:
                    next_todo_state = item["todo_state"]
            summary = cycle_summary_from_tools(
                executed,
                previous_todo_state=previous_todo_state,
                next_todo_state=next_todo_state,
                workflow_changed=workflow_changed,
                result_ok=result_ok,
            )
            sub_task_state.todo_state = next_todo_state
            guidance = guard_state.repetitive_tools.record_cycle(summary)
            if guidance is not None:
                pending_guard_guidance.append(guidance.message)
            guidance = guard_state.no_progress.record_cycle(summary)
            if guidance is not None:
                pending_guard_guidance.append(guidance.message)
            no_progress_decision = guard_state.no_progress.decision()
            if no_progress_decision.action == "terminate":
                return await finalize("guard_terminated")

    except Exception as e:
        if tracker:
            tracker.update(task_id, last_output=str(e)[:200])
            tracker.finish(task_id, "error")
        mark_finished("error")
        raise




def _task_payload(task_description: str, result_contract) -> str:
    schema_name = str(getattr(result_contract, "schema_name", "") or "agent_result")
    result_format = str(getattr(result_contract, "format", "") or "").strip()
    parts = [task_description]
    if result_format:
        parts.append(
            "Result contract:\n"
            f"- schema_name: {schema_name}\n"
            f"- format: {result_format}\n"
            "Return the final answer using this contract."
        )
    return "\n\n".join(parts)


def _is_message_result_tool_call(tool_call: dict) -> bool:
    args = tool_call.get("args")
    return (
        tool_call.get("name") == "message"
        and isinstance(args, dict)
        and args.get("action", "send") == "send"
        and args.get("message_type") == "result"
    )





def _partial_result_from_messages(messages: list, *, require_findings: bool = False) -> str:
    findings: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        text = extract_text(message).strip()
        if text and text not in findings:
            findings.append(text)
        if len(findings) >= 3:
            break
    if not findings and require_findings:
        return ""
    if not findings:
        return "The task could not be fully completed. No verified findings were available."
    return (
        "\n---\n".join(reversed(findings))
        + "\n\nThe task may be incomplete. Review the findings, verification, and remaining work."
    )




def _result_text(result: dict | None) -> str:
    if not result:
        return ""
    for key in ("result", "output", "content", "text"):
        if key in result:
            return str(result.get(key) or "")
    return json.dumps(result, ensure_ascii=False, default=str)


def _result_contract_fields(result_contract) -> list[str]:
    result_format = str(getattr(result_contract, "format", "") or "")
    fields: list[str] = []
    for raw_part in result_format.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", part)
        if match:
            fields.append(match.group(1))
    return fields


def _satisfies_result_contract(text: str, result_contract) -> bool:
    fields = _result_contract_fields(result_contract)
    if not fields:
        return True
    if not text.strip():
        return False

    matched = [
        field
        for field in fields
        if re.search(rf"(?im)^\s*(?:[-*]\s*)?{re.escape(field)}\s*[:=]", text)
    ]
    required = 1 if len(fields) == 1 else 2
    if fields[0] not in matched:
        return False
    return len(matched) >= required


def _result_contract_retry_message(result_contract) -> str:
    schema_name = str(getattr(result_contract, "schema_name", "") or "agent_result")
    result_format = str(getattr(result_contract, "format", "") or "").strip()
    return (
        "Your previous response did not satisfy the child-agent result contract. "
        "Do not return raw tool output or code snippets as the final answer.\n"
        "Summarize the completed delegated task using the required contract:\n"
        f"- schema_name: {schema_name}\n"
        f"- format: {result_format}"
    )
