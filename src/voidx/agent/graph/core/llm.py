from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from voidx.agent.agents import get_agent
from voidx.agent.prompts import WORKFLOW_RUNTIME, build_base_system, persona_prompt
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    raw_semantic_messages,
)
from voidx.agent.state import AgentState
from voidx.agent.task_state import (
    TaskState,
    TodoRunState,
    goal_label,
    goal_type_from_join,
)
from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.tool_exchange_sanitizer import sanitize_failed_tool_exchanges
from voidx.agent.tool_filters import filter_unavailable_lsp_tools, strip_gemini_unsupported_schema_keys
from voidx.agent.graph.streaming import (
    extract_text,
    is_malformed_tool_call_response,
    stream_llm as _stream_llm,
)
from voidx.agent.graph.topology import latest_ai_message, latest_user_text, prepare_state
from voidx.agent.graph.workflow_utils import active_workflow_names
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.logging.request_log import log_llm_exchange
from voidx.llm.service import resolve_protocol
from voidx.llm.usage import (
    estimate_context_tokens,
    estimate_context_tokens_with_tools,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.runtime.ui import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    GuidanceCommitted,
    StatusFinished,
    StreamingRenderer,
)

from voidx.agent.graph.turn_control import TURN_TOOL_DEFINITION
from .context import (
    rebuild_llm_messages as build_llm_context_messages,
    replacement_messages as compacted_replacement_messages,
    rerender_task_context,
    save_main_context_frame,
)
from .loop import LlmLoopState, handle_llm_exception
from .turn import handle_turn_control_response
from .helpers import (
    _invalidate_tui,
    _merge_workflow_runs,
    _persona_for_workflow_runs,
    _render_inline_compaction_guide,
    _task_state_for_context,
    _workflow_names,
    _LLM_MAX_RETRIES,
    _LLM_TIMEOUT_MAX_RETRIES,
)

if TYPE_CHECKING:
    from voidx.agent.graph.compaction_coordinator import CompactionResult


MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION = (
    "Your previous response looked like an incomplete tool call. Re-emit a valid "
    "tool call using the bound tool schema, or answer normally without tool-call markup."
)


class GraphLlmMixin:
    """LLM node methods for the agent graph."""

    async def _prepare_with_stream(self, state: AgentState) -> dict:
        base = prepare_state(state)
        agent_id = "voidx"
        runtime_persona = state.get("persona", "coordinate")
        self._current_agent = get_agent(agent_id)
        rendered_persona_prompt = persona_prompt() if self._current_agent else ""

        interaction_mode = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else self._interaction_mode.value
        )
        current_user_text = latest_user_text(state.get("messages", []))
        instructions = await self._instruction.system()
        task_state = _task_state_for_context(state.get("task_state"), getattr(self, "_task_state", None))
        current_goal = task_state.current_goal
        existing_workflow_runs = list((task_state.workflow_runs or {}).values())
        workflow_start = (
            task_state.workflow_route.join
            if task_state.workflow_route and task_state.workflow_route.join
            else None
        )
        workflow_context = await self._workflow_context_for(
            current_user_text,
            agent=runtime_persona,
            task_intent=task_state.current_intent.value,
            goal_type=goal_type_from_join(workflow_start),
            interaction_mode=interaction_mode,
            scope=goal_label(current_goal) or current_user_text,
            exclude_names=_workflow_names(existing_workflow_runs),
            active_names=active_workflow_names(existing_workflow_runs),
            workflow_start=workflow_start,
        )
        workflow_runs = _merge_workflow_runs(
            existing_workflow_runs,
            workflow_context.runs,
        )
        runtime_persona = _persona_for_workflow_runs(workflow_runs, fallback=runtime_persona)
        summary = self._pending_summary or self._compaction_summary
        self._pending_summary = None

        self._last_context_builder = RuntimeContextBuilder(
            config=self.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=build_base_system(self.config.user_profile.language),
            workflow_runtime=WORKFLOW_RUNTIME,
            persona_prompt=rendered_persona_prompt,
            persona=runtime_persona,
            interaction_mode=interaction_mode,
            instructions=instructions,
            workflow_runs=workflow_runs,
            active_workflow_summaries=workflow_context.active,
            summary=summary,
            task_state=task_state,
            session_date=self._session_date,
            turn_state=state.get("turn_state", "initial"),
        )
        context, self._context_cache = self._last_context_builder.build_incremental(self._context_cache)
        context.apply_to_messages(state.get("messages", []))

        task_state.workflow_runs = {run.name: run for run in workflow_runs}
        self._task_state = task_state.model_copy(deep=True)
        _invalidate_tui(self)
        return {
            **base,
            "persona": runtime_persona,
            "task_state": task_state.model_dump(mode="json"),
        }

    async def _workflow_context_for(self, *args, **kwargs):
        return await self._instruction.workflow_context_for(*args, **kwargs)

    def _invalidate_tui_for_turn(self) -> None:
        _invalidate_tui(self)

    def _inline_compaction_guide_for(self, messages: list[BaseMessage]) -> HumanMessage | None:
        if not getattr(self.config, "inline_compaction_enabled", False):
            return None
        total_tokens = estimate_context_tokens(messages, self.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}
        if self._compaction.is_overflow(tokens):
            return None
        if total_tokens < self._compaction.usable_window():
            return None

        semantic_messages = sanitize_todo_replay_messages(raw_semantic_messages(messages))
        selection = self._compaction.select_details(semantic_messages)
        if not selection.should_compact:
            return None
        content = _render_inline_compaction_guide(
            tail_anchor_id=selection.tail_id or "",
            head_count=len(selection.head),
            previous_summary=self._compaction_summary,
        )
        guide = HumanMessage(content=content)
        guide_tokens = estimate_context_tokens([*messages, guide], self.config.model.model)
        guide_budget = {"total": guide_tokens, "input": guide_tokens, "output": 0, "reasoning": 0}
        if guide_tokens > self._compaction.context_limit or self._compaction.is_overflow(guide_budget):
            return None
        return guide

    async def _call_llm(self, state: AgentState) -> dict:
        step = state.get("step_count", 0)

        if self.model is None:
            return {
                "messages": [AIMessage(content=(
                    "No model configured. Use /model new to create a profile."
                ))],
                "step_count": step,
                "should_continue": False,
            }

        interaction_mode_value = state.get("interaction_mode") or (
            InteractionMode.PLAN.value if state.get("plan_mode", False) else self._interaction_mode.value
        )
        turn_state = str(state.get("turn_state") or "initial")
        tool_defs = self.tools.tools_for_llm()
        turn_control_active = self._turn_control_enabled()
        if turn_control_active:
            tool_defs = [*tool_defs, TURN_TOOL_DEFINITION]
        runtime_task_state = _task_state_for_context(
            state.get("task_state"),
            getattr(self, "_task_state", None),
        )
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))
        tool_defs = strip_gemini_unsupported_schema_keys(tool_defs, resolve_protocol(self.config.model))

        guidance_pairs = self._drain_pending_guidance()
        guidance_messages = [msg for msg, _, _ in guidance_pairs]
        if self._ui.via_events() and guidance_pairs:
            user_guidance = [
                str(msg.content)
                for msg, _, source in guidance_pairs
                if source == "user"
            ]
            if user_guidance:
                self._ui.events.emit_direct(
                    GuidanceCommitted(
                        text="\n".join(user_guidance),
                        truncated=any(
                            truncated for _, truncated, source in guidance_pairs
                            if source == "user"
                        ),
                        source="user",
                    )
                )
        state_messages = sanitize_todo_replay_messages(
            list(state["messages"]),
            preserve_latest_tool_exchange=True,
        )
        state_messages = sanitize_failed_tool_exchanges(
            state_messages,
            preserve_latest=True,
            preserve_rounds=2,
        )
        compaction_happened = False
        raw_todo_state = (
            state["todo_state"]
            if "todo_state" in state
            else getattr(getattr(self, "_task_state", None), "todo_state", None)
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
                inline_compaction_guide_for=self._inline_compaction_guide_for,
            )

        async def save_context_frame(
            messages: list[BaseMessage],
            token_estimate: int,
            convergence_messages: list[HumanMessage],
            convergence_forced: bool,
        ) -> None:
            await save_main_context_frame(
                session=self._session,
                user_message_id=state.get("user_message_id"),
                persona=persona,
                provider=self.config.model.provider,
                model=self.config.model.model,
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
            )

        def _rerender_task_context(messages: list[BaseMessage], new_turn_state: str, task_state: TaskState | None = None) -> list[BaseMessage]:
            return rerender_task_context(
                getattr(self, "_last_context_builder", None),
                messages,
                new_turn_state,
                task_state,
            )

        def estimate_llm_context_tokens(messages: list[BaseMessage]) -> int:
            return estimate_context_tokens_with_tools(
                messages,
                tool_defs,
                self.config.model.model,
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
                prepared = await self._prepare_with_stream(reprepare_state)
                runtime_task_state = _task_state_for_context(
                    prepared.get("task_state"),
                    runtime_task_state,
                )
            rebuilt, conv_messages, conv_forced = rebuild_llm_messages(
                state_messages,
                allow_inline_compaction=False,
            )
            rebuilt_tokens = estimate_llm_context_tokens(rebuilt)
            self._usage_stats.update_context(rebuilt_tokens)
            return rebuilt, conv_messages, conv_forced, rebuilt_tokens

        llm_messages, convergence_messages, convergence_forced = rebuild_llm_messages(
            state_messages,
            allow_inline_compaction=getattr(self.config, "inline_compaction_enabled", False),
        )

        persona = state.get("persona", "coordinate")
        if self._debug:
            self._ui.ui.print()

        # ── LLM call with retry ────────────────────────────────────────
        loop = LlmLoopState(
            context_tokens=estimate_llm_context_tokens(llm_messages),
        )
        self._usage_stats.update_context(loop.context_tokens)
        if self._compaction.is_overflow({"total": loop.context_tokens}):
            result, _preflight_result = await self._preflight_compact_if_needed(
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

        await save_context_frame(llm_messages, loop.context_tokens, convergence_messages, convergence_forced)
        max_retries = _LLM_MAX_RETRIES
        while True:
            try:
                renderer = StreamingRenderer(
                    self._ui.console,
                    debug=self._debug,
                    headless=loop.turn_prompt_active,
                )
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(
                    model_with_tools,
                    llm_messages,
                    renderer,
                    resolve_protocol(self.config.model),
                )
                log_llm_exchange(
                    llm_messages,
                    assistant_msg,
                    model=self.config.model.model,
                    provider=self.config.model.provider,
                    step=step,
                    session_id=self._session.id if self._session else None,
                    enabled=self.config.log_llm_exchange,
                )
                self._usage_stats.record_call(
                    extract_token_usage(assistant_msg),
                    fallback_input_tokens=loop.context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
                    messages=llm_messages,
                    model=self.config.model.model,
                    cache_key=f"{self.config.model.provider}/{self.config.model.model}",
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
                        self._usage_stats.update_context(loop.context_tokens)
                        continue
                    if loop.malformed_tool_call_attempts < 2 and compaction_happened:
                        result, _preflight_result = await self._preflight_compact_if_needed(
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
                            self._usage_stats.update_context(loop.context_tokens)
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
                if self._debug or not assistant_msg.tool_calls:
                    self._ui.ui.print()
                if loop.retry_status_active and self._ui.via_events():
                    await self._ui.events.emit(StatusFinished(status_id="llm:retry"))

                if turn_control_active:
                    turn_result = await handle_turn_control_response(
                        graph=self,
                        assistant_msg=assistant_msg,
                        llm_messages=llm_messages,
                        loop=loop,
                        turn_state=turn_state,
                        runtime_task_state=runtime_task_state,
                        state_messages=state_messages,
                        interaction_mode_value=interaction_mode_value,
                        estimate_tokens=estimate_llm_context_tokens,
                        rerender_task_context=_rerender_task_context,
                    )
                    llm_messages = turn_result.llm_messages
                    turn_state = turn_result.turn_state
                    runtime_task_state = turn_result.runtime_task_state
                    if turn_result.action == "retry":
                        self._usage_stats.update_context(turn_result.context_tokens)
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
                from .helpers import _classify_llm_error

                kind = _classify_llm_error(e)

                retry_result = await handle_llm_exception(
                    ui=self._ui,
                    loop=loop,
                    error=e,
                    kind=kind,
                    max_retries=max_retries,
                    timeout_max_retries=_LLM_TIMEOUT_MAX_RETRIES,
                )
                if retry_result.action == "overflow":
                    result, _preflight_result = await self._preflight_compact_if_needed(
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
                        await save_context_frame(
                            llm_messages,
                            loop.context_tokens,
                            convergence_messages,
                            convergence_forced,
                        )
                        continue
                if retry_result.action == "retry":
                    continue
                if retry_result.action == "fail":
                    return {
                        "messages": [],
                        "step_count": step,
                        "should_continue": False,
                    }

        final_msg = loop.terminal_msg if loop.terminal_msg is not None else assistant_msg
        if loop.terminal_msg is not None and not loop.terminal_msg_visible:
            final_text = extract_text(final_msg).strip()
            if final_text:
                if self._ui.via_events():
                    await self._ui.events.emit(AssistantStreamUpdated(text=final_text, phase="text"))
                    await self._ui.events.emit(AssistantStreamCommitted())
                else:
                    self._ui.ui.print(final_text)
        return {
            "messages": replacement_messages(final_msg),
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
            "turn_state": turn_state,
            "task_state": runtime_task_state.model_dump(mode="json"),
        }

    def _router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "execute"
        return "end"

    async def _finalize(self, state: AgentState) -> dict:
        from voidx.agent.graph.convergence import generate_fallback_summary

        if not state.get("convergence_forced"):
            return {}
        last = latest_ai_message(state.get("messages", []))
        if isinstance(last, AIMessage) and not last.tool_calls:
            if len(extract_text(last).strip()) >= 20:
                return {}
        return {"messages": [AIMessage(content=generate_fallback_summary(state))]}
