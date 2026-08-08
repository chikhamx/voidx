"""LLM turn call orchestration for LangGraph execution."""

from __future__ import annotations

StreamingRenderer = None

from voidx.agent.domain.ui_events import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    ContextPressureFinished,
    ContextPressureUpdated,
    GuidanceCommitted,
    StatusFinished,
)

from voidx.agent.adapters.langgraph.runtime.core.context import (
    rebuild_llm_messages as build_llm_context_messages,
    replacement_messages as compacted_replacement_messages,
    rerender_task_context,
    save_main_context_frame,
)
from voidx.agent.adapters.langgraph.runtime.core.loop import LlmLoopState, handle_llm_exception
from voidx.agent.adapters.langgraph.runtime.core.turn import handle_turn_control_response
from voidx.agent.adapters.langgraph.runtime.core.helpers import _invalidate_tui, _merge_workflow_runs, _persona_for_workflow_runs, _task_state_for_context, _LLM_MAX_RETRIES, _LLM_TIMEOUT_MAX_RETRIES
from voidx.agent.domain.compaction import CompactionResult
from typing import Any
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from voidx.agent.application.agents import get_agent
from voidx.agent.application.prompts import (
    CODING_PROFILE_SPEC,
    WORKFLOW_RUNTIME,
    assemble_base_system,
    build_base_system,
    persona_prompt,
)
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.agent.adapters.langgraph.state import AgentState
from voidx.agent.domain.task.state import TaskState, goal_label, goal_type_from_join
from voidx.agent.domain.task.todo import TodoRunState
from voidx.agent.adapters.langgraph.runtime.tool_surface import (
    ToolSurfaceContext,
    resolve_tool_surface,
)
from voidx.agent.adapters.langgraph.runtime.streaming import (
    extract_text,
    is_malformed_tool_call_response,
    stream_llm as _stream_llm,
)
from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text, prepare_state
from voidx.agent.domain.workflow_utils import active_workflow_names
from voidx.observability.request_log import log_llm_exchange
from voidx.llm.domain.provider import resolve_protocol
from voidx.llm.usage import estimate_context_tokens_with_tools, estimate_message_tokens, extract_token_usage
from voidx.agent.adapters.langgraph.runtime.control_protocol import (
    ControlContext,
    resolve_control_protocol,
    strip_tool_calls_after_loop_commit,
)
from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state
from voidx.agent.adapters.langgraph.runtime.context_pressure import (
    current_context_pressure,
    evaluate_context_pressure,
    hard_pressure_decision,
    upsert_context_pressure_hint,
)
from voidx.agent.application.runtime_context import raw_semantic_messages
from voidx.llm.message_markers import (
    CONTEXT_PRESSURE_MARKER,
    GUIDANCE_MARKER,
)


MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION = (
    "Your previous response looked like an incomplete tool call. Re-emit a valid "
    "tool call using the bound tool schema, or answer normally without tool-call markup."
)






class LlmTurn:
    def __init__(self, host: Any) -> None:
        self.host = host

    async def call(self, state: AgentState) -> dict:
        host = self.host
        step = state.get("step_count", 0)

        if host.model is None:
            return {
                "messages": [AIMessage(content=(
                    "No model configured. Use /model new to create a profile."
                ))],
                "step_count": step,
                "should_continue": False,
            }

        interaction_mode_value = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else host._interaction_mode.value
        )
        turn_state = str(state.get("turn_state") or "initial")
        state_context = current_thread_execution_state()
        chat_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        loop_turn_context = getattr(state_context, "turn_context", None) if state_context else None
        runtime_profile = getattr(state_context, "runtime_profile", None) if state_context else None
        control_protocol = resolve_control_protocol(runtime_profile)
        protocol_controller = control_protocol.controller(
            ControlContext(
                runtime_profile=runtime_profile,
                turn_context=loop_turn_context,
                interaction_mode=str(interaction_mode_value),
                turn_state=turn_state,
            )
        )
        tool_defs = resolve_tool_surface(
            host.tools,
            ToolSurfaceContext(
                runtime_profile=runtime_profile,
                goal_phase=getattr(loop_turn_context, "goal_phase", None),
                loop_phase=getattr(loop_turn_context, "loop_phase", None),
                tool_policy=chat_tool_view,
                turn_context=loop_turn_context,
                lsp_manager=getattr(host, "_lsp_manager", None),
                model_protocol=resolve_protocol(host.config.model),
            ),
        ).definitions
        runtime_task_state = _task_state_for_context(
            state.get("task_state"),
            getattr(host, "_task_state", None),
        )

        guidance_pairs = host._drain_pending_guidance()
        guidance_messages = [msg for msg, _, _ in guidance_pairs]
        if host._ui.via_events() and guidance_pairs:
            user_guidance = [
                str(msg.content)
                for msg, _, source in guidance_pairs
                if source == "user"
            ]
            if user_guidance:
                host._ui.events.emit_direct(
                    GuidanceCommitted(
                        text="\n".join(user_guidance),
                        truncated=any(
                            truncated for _, truncated, source in guidance_pairs
                            if source == "user"
                        ),
                        source="user",
                    )
                )
        state_messages = list(state["messages"])
        compaction_happened = False
        pending_pressure_delta: list[BaseMessage] = []
        raw_todo_state = (
            state["todo_state"]
            if "todo_state" in state
            else getattr(getattr(host, "_task_state", None), "todo_state", None)
        )
        if raw_todo_state is not None:
            try:
                runtime_task_state.todo_state = (
                    raw_todo_state
                    if isinstance(raw_todo_state, TodoRunState)
                    else TodoRunState.model_validate(raw_todo_state)
                )
            except (TypeError, ValueError):
                runtime_task_state.todo_state = None

        def rebuild_llm_messages(
            messages: list[BaseMessage],
            *,
            allow_inline_compaction: bool,
        ) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
            return build_llm_context_messages(
                messages,
                guidance_messages,
                allow_inline_compaction=allow_inline_compaction,
                compaction_happened=compaction_happened,
                inline_compaction_guide_for=host._inline_compaction_guide_for,
            )

        async def save_context_frame(
            messages: list[BaseMessage],
            token_estimate: int,
            convergence_messages: list[HumanMessage],
            convergence_forced: bool,
        ) -> None:
            await save_main_context_frame(
                session=host._session,
                user_message_id=state.get("user_message_id"),
                persona=persona,
                provider=host.config.model.provider,
                model=host.config.model.model,
                messages=messages,
                token_estimate=token_estimate,
                step=step,
                tool_count=len(tool_defs),
                convergence_messages=convergence_messages,
                convergence_forced=convergence_forced,
            )

        def replacement_messages(assistant_msg: AIMessage) -> list[BaseMessage]:
            return compacted_replacement_messages(
                assistant_msg,
                compaction_happened=compaction_happened,
                state_messages=state_messages,
                pending_message_delta=pending_pressure_delta,
            )

        def _rerender_task_context(messages: list[BaseMessage], new_turn_state: str, task_state: TaskState | None = None) -> list[BaseMessage]:
            return rerender_task_context(
                getattr(host, "_last_context_builder", None),
                messages,
                new_turn_state,
                task_state,
            )

        def estimate_llm_context_tokens(messages: list[BaseMessage]) -> int:
            return estimate_context_tokens_with_tools(
                messages,
                tool_defs,
                host.config.model.model,
            )

        async def apply_compaction_result(result: CompactionResult) -> tuple[list[BaseMessage], list[HumanMessage], bool, int]:
            nonlocal compaction_happened, state_messages, runtime_task_state
            compaction_happened = True
            state_messages = list(result.live_messages)
            if result.summary:
                reprepare_state = {
                    **state,
                    "messages": state_messages,
                    "task_state": runtime_task_state.model_dump(mode="json"),
                }
                prepared = await host._prepare_with_stream(reprepare_state)
                runtime_task_state = _task_state_for_context(
                    prepared.get("task_state"),
                    runtime_task_state,
                )
            rebuilt, conv_messages, conv_forced = rebuild_llm_messages(
                state_messages,
                allow_inline_compaction=False,
            )
            rebuilt_tokens = estimate_llm_context_tokens(rebuilt)
            host._usage_stats.update_context(rebuilt_tokens)
            return rebuilt, conv_messages, conv_forced, rebuilt_tokens

        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(
            state_messages,
            allow_inline_compaction=getattr(host.config, "inline_compaction_enabled", False),
        )

        persona = state.get("persona", "coordinate")
        if host._debug:
            host._ui.ui.print()

        # ── LLM call with retry ────────────────────────────────────────
        loop = LlmLoopState(
            context_tokens=estimate_llm_context_tokens(llm_messages),
        )
        host._usage_stats.update_context(loop.context_tokens)
        pressure_decision = evaluate_context_pressure(
            raw_semantic_messages(state_messages),
            loop.context_tokens,
            compaction_service=host._compaction,
        )
        active_pressure = current_context_pressure(state_messages, pressure_decision.turn_id)
        if pressure_decision.should_inject:
            pressure_update = upsert_context_pressure_hint(state_messages, pressure_decision)
            state_messages = pressure_update.state_messages
            pending_pressure_delta = pressure_update.message_delta
            active_pressure = current_context_pressure(state_messages, pressure_decision.turn_id)
            if pressure_update.message_delta:
                llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(
                    state_messages,
                    allow_inline_compaction=getattr(host.config, "inline_compaction_enabled", False),
                )
                loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                host._usage_stats.update_context(loop.context_tokens)
            if pressure_update.outcome in {"hint_injected", "hint_upgraded"} and host._ui.via_events():
                await host._ui.events.emit(ContextPressureUpdated(
                    pressure_id=pressure_update.pressure_id,
                    level=pressure_decision.pressure_level,
                    outcome=pressure_update.outcome,
                    reason=pressure_decision.reason,
                    can_compact=pressure_decision.can_compact,
                    turn_count=pressure_decision.turn_count,
                    pre_tokens=pressure_decision.pre_tokens,
                    soft_threshold=pressure_decision.soft_threshold,
                    hard_threshold=pressure_decision.hard_threshold,
                ))
        if host._compaction.is_overflow({"total": loop.context_tokens}):
            result, _preflight_result = await host._preflight_compact_if_needed(
                state_messages,
                force=True,
                reason="hard_threshold",
                ask=False,
            )
            if result is not None:
                llm_messages, convergence_messages, convergence_forced, context_tokens = (
                    await apply_compaction_result(result)
                )
                loop.context_tokens = context_tokens
                if active_pressure is not None and host._ui.via_events():
                    await host._ui.events.emit(ContextPressureFinished(
                        pressure_id=active_pressure[0],
                        level=active_pressure[1],
                        outcome="compacted",
                    ))

        await save_context_frame(llm_messages, loop.context_tokens, convergence_messages, convergence_forced)
        max_retries = _LLM_MAX_RETRIES
        while True:
            try:
                renderer_factory = StreamingRenderer or host._ui.streaming_renderer
                renderer = renderer_factory(
                    host._ui.console,
                    debug=host._debug,
                    headless=loop.turn_prompt_active,
                )
                model_with_tools = host.model.bind_tools(tool_defs) if tool_defs else host.model
                assistant_msg = await _stream_llm(
                    model_with_tools,
                    llm_messages,
                    renderer,
                    resolve_protocol(host.config.model),
                    ui_port=host._ui,
                )
                log_llm_exchange(
                    llm_messages,
                    assistant_msg,
                    model=host.config.model.model,
                    provider=host.config.model.provider,
                    step=step,
                    session_id=host._session.id if host._session else None,
                    enabled=host.config.log_llm_exchange,
                )
                host._usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=loop.context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, host.config.model.model),
                    messages=llm_messages,
                    model=host.config.model.model,
                    cache_key=f"{host.config.model.provider}/{host.config.model.model}",
                )
                if is_malformed_tool_call_response(assistant_msg):
                    if loop.malformed_tool_call_attempts < 1:
                        loop.malformed_tool_call_attempts += 1
                        llm_messages = [
                            *llm_messages,
                            HumanMessage(
                                content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                additional_kwargs={GUIDANCE_MARKER: True},
                            ),
                        ]
                        loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                        host._usage_stats.update_context(loop.context_tokens)
                        continue
                    if loop.malformed_tool_call_attempts < 2 and compaction_happened:
                        result, _preflight_result = await host._preflight_compact_if_needed(
                            state_messages,
                            force=True,
                            reason="malformed_tool_call",
                            ask=False,
                        )
                        loop.malformed_tool_call_attempts += 1
                        if result is not None:
                            llm_messages, convergence_messages, convergence_forced, context_tokens = (
                                await apply_compaction_result(result)
                            )
                            loop.context_tokens = context_tokens
                            llm_messages = [
                                *llm_messages,
                                HumanMessage(
                                    content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                    additional_kwargs={GUIDANCE_MARKER: True},
                                ),
                            ]
                            loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                            host._usage_stats.update_context(loop.context_tokens)
                            await save_context_frame(
                                llm_messages,
                                loop.context_tokens,
                                convergence_messages,
                                convergence_forced,
                            )
                            continue
                    failure_msg = AIMessage(
                        content="LLM call failed: model returned an invalid or incomplete tool call."
                    )
                    return {
                        "messages": replacement_messages(failure_msg),
                        "step_count": step,
                        "should_continue": False,
                    }
                if host._debug or not assistant_msg.tool_calls:
                    host._ui.ui.print()
                if loop.retry_status_active and host._ui.via_events():
                    await host._ui.events.emit(StatusFinished(status_id="llm:retry"))

                turn_result = await handle_turn_control_response(
                    graph=host,
                    assistant_msg=assistant_msg,
                    llm_messages=llm_messages,
                    loop=loop,
                    turn_state=turn_state,
                    runtime_task_state=runtime_task_state,
                    state_messages=state_messages,
                    interaction_mode_value=interaction_mode_value,
                    estimate_tokens=estimate_llm_context_tokens,
                    rerender_task_context=_rerender_task_context,
                    loop_controller=protocol_controller,
                    protocol=control_protocol,
                )
                llm_messages = turn_result.llm_messages
                turn_state = turn_result.turn_state
                runtime_task_state = turn_result.runtime_task_state
                if turn_result.action == "retry":
                    host._usage_stats.update_context(turn_result.context_tokens)
                    continue
                if turn_result.action == "fail":
                    return {
                        "messages": replacement_messages(turn_result.failure_msg),
                        "step_count": step + 1,
                        "should_continue": False,
                    }
                if turn_result.action == "break":
                    break

                break
            except Exception as e:
                from voidx.agent.adapters.langgraph.runtime.core.helpers import _classify_llm_error

                kind = _classify_llm_error(e)

                retry_result = await handle_llm_exception(
                    ui=host._ui,
                    loop=loop,
                    error=e,
                    kind=kind,
                    max_retries=max_retries,
                    timeout_max_retries=_LLM_TIMEOUT_MAX_RETRIES,
                )
                if retry_result.action == "overflow":
                    result, _preflight_result = await host._preflight_compact_if_needed(
                        state_messages,
                        force=True,
                        reason="provider_overflow",
                        ask=False,
                    )
                    if result is not None:
                        llm_messages, convergence_messages, convergence_forced, context_tokens = (
                            await apply_compaction_result(result)
                        )
                        loop.context_tokens = context_tokens
                        if active_pressure is not None and host._ui.via_events():
                            await host._ui.events.emit(ContextPressureFinished(
                                pressure_id=active_pressure[0],
                                level=active_pressure[1],
                                outcome="compacted",
                            ))
                        await save_context_frame(
                            llm_messages,
                            loop.context_tokens,
                            convergence_messages,
                            convergence_forced,
                        )
                        continue
                    hard_update = upsert_context_pressure_hint(
                        state_messages,
                        hard_pressure_decision(pressure_decision),
                    )
                    state_messages = hard_update.state_messages
                    if hard_update.message_delta:
                        pending_pressure_delta = hard_update.message_delta
                    active_pressure = current_context_pressure(
                        state_messages,
                        pressure_decision.turn_id,
                    )
                    llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(
                        state_messages,
                        allow_inline_compaction=False,
                    )
                    loop.context_tokens = estimate_llm_context_tokens(llm_messages)
                    host._usage_stats.update_context(loop.context_tokens)
                    if hard_update.outcome in {"hint_injected", "hint_upgraded"} and host._ui.via_events():
                        await host._ui.events.emit(ContextPressureUpdated(
                            pressure_id=hard_update.pressure_id,
                            level="hard",
                            outcome=hard_update.outcome,
                            reason="hard_threshold",
                            can_compact=False,
                            turn_count=pressure_decision.turn_count,
                            pre_tokens=loop.context_tokens,
                            soft_threshold=pressure_decision.soft_threshold,
                            hard_threshold=pressure_decision.hard_threshold,
                        ))
                if retry_result.action == "retry":
                    continue
                if retry_result.action == "fail":
                    if kind == "overflow" and host._ui.via_events():
                        failure_pressure = active_pressure or (
                            f"voidx:context-pressure:{pressure_decision.turn_id}",
                            "hard",
                        )
                        await host._ui.events.emit(ContextPressureFinished(
                            pressure_id=failure_pressure[0],
                            level="hard",
                            outcome="model_overflow_failed",
                            detail=str(e),
                            ok=False,
                        ))
                    return {
                        "messages": list(pending_pressure_delta),
                        "step_count": step,
                        "should_continue": False,
                    }

        final_msg = loop.terminal_msg if loop.terminal_msg is not None else assistant_msg
        final_msg = strip_tool_calls_after_loop_commit(final_msg)
        if loop.terminal_msg is not None and not loop.terminal_msg_visible:
            final_text = extract_text(final_msg).strip()
            if final_text:
                if host._ui.via_events():
                    await host._ui.events.emit(AssistantStreamUpdated(text=final_text, phase="text"))
                    await host._ui.events.emit(AssistantStreamCommitted())
                else:
                    host._ui.ui.print(final_text)
        if active_pressure is not None and not final_msg.tool_calls and host._ui.via_events():
            await host._ui.events.emit(ContextPressureFinished(
                pressure_id=active_pressure[0],
                level=active_pressure[1],
                outcome="turn_converged",
            ))
        return {
            "messages": replacement_messages(final_msg),
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
            "turn_state": turn_state,
            "task_state": runtime_task_state.model_dump(mode="json"),
        }


    async def prepare_with_stream(self, state: AgentState) -> dict:
        host = self.host
        base = prepare_state(state)
        agent_id = "voidx"
        runtime_persona = state.get("persona", "coordinate")
        host._current_agent = get_agent(agent_id)
        rendered_persona_prompt = persona_prompt() if host._current_agent else ""

        state_context = current_thread_execution_state()
        active_profile = getattr(state_context, "runtime_profile", None) if state_context else None
        prompt_policy = getattr(active_profile, "prompt_policy", None)
        turn_context = getattr(state_context, "turn_context", None) if state_context else None
        persona_prompt_value = rendered_persona_prompt
        workflow_runtime_value = WORKFLOW_RUNTIME
        profile_sections_value = (
            prompt_policy.profile_sections(turn_context)
            if prompt_policy is not None
            else []
        )
        suppress_sections_value = (
            prompt_policy.suppress_sections()
            if prompt_policy is not None
            else set()
        )
        base_system_spec = (
            prompt_policy.base_system_spec()
            if prompt_policy is not None and prompt_policy.base_system_spec() is not None
            else CODING_PROFILE_SPEC
        )
        active_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        available_tools = (
            set(active_tool_view.bound_tool_ids)
            if active_tool_view is not None
            else None
        )
        base_system = assemble_base_system(
            base_system_spec,
            available_tools=available_tools,
        )

        interaction_mode = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else host._interaction_mode.value
        )
        current_user_text = latest_user_text(state.get("messages", []))
        instructions = await host._instruction.system()
        task_state = _task_state_for_context(state.get("task_state"), getattr(host, "_task_state", None))
        current_goal = task_state.current_goal
        existing_workflow_runs = list((task_state.workflow_runs or {}).values())
        workflow_start = (
            task_state.workflow_route.join
            if task_state.workflow_route and task_state.workflow_route.join
            else None
        )
        workflow_context = await host._workflow_context_for(
            goal_type=goal_type_from_join(workflow_start),
            scope=goal_label(current_goal) or current_user_text,
            active_names=active_workflow_names(existing_workflow_runs),
            workflow_start=workflow_start,
        )
        workflow_runs = _merge_workflow_runs(
            existing_workflow_runs,
            workflow_context.runs,
        )
        runtime_persona = _persona_for_workflow_runs(workflow_runs, fallback=runtime_persona)
        summary = host._pending_summary or host._compaction_summary
        host._pending_summary = None

        host._last_context_builder = RuntimeContextBuilder(
            config=host.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=build_base_system(
                host.config.user_profile.language,
                base_system=base_system,
            ),
            workflow_runtime=workflow_runtime_value,
            persona_prompt=persona_prompt_value,
            persona=runtime_persona,
            interaction_mode=interaction_mode,
            instructions=instructions,
            workflow_runs=workflow_runs,
            active_workflow_summaries=workflow_context.active,
            summary=summary,
            task_state=task_state,
            session_date=host._session_date,
            turn_state=state.get("turn_state", "initial"),
            profile_sections=profile_sections_value,
            suppress_sections=suppress_sections_value,
            historical_tool_context_stripper=host._historical_tool_context_stripper,
        )
        context, host._context_cache = host._last_context_builder.build_incremental(host._context_cache)
        context.apply_to_messages(state.get("messages", []))

        task_state.workflow_runs = {run.name: run for run in workflow_runs}
        host._task_state = task_state.model_copy(deep=True)
        _invalidate_tui(host)
        return {
            **base,
            "persona": runtime_persona,
            "task_state": task_state.model_dump(mode="json"),
        }
