from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.agents import get_agent
from voidx.agent.prompts import BASE_SYSTEM, WORKFLOW_RUNTIME, persona_prompt
from voidx.agent.runtime_context import (
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    raw_semantic_messages,
)
from voidx.agent.state import AgentState
from voidx.agent.task_state import GoalResolution, TaskState, goal_label, goal_type_from_join
from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.message_trimming import trim_superseded_file_tools
from voidx.agent.tool_exchange_sanitizer import sanitize_failed_tool_exchanges
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
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
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.memory.service import save_context_frame_from_messages
from voidx.runtime.ui import (
    StatusFinished,
    StatusUpdated,
    StreamingRenderer,
)

from .helpers import (
    _invalidate_tui,
    _merge_workflow_runs,
    _persona_for_workflow_runs,
    _render_inline_compaction_guide,
    _task_state_for_context,
    _workflow_names,
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

        context, self._context_cache = RuntimeContextBuilder(
            config=self.config,
            workspace=state.get("workspace", "."),
            base_system_prompt=BASE_SYSTEM,
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
        ).build_incremental(self._context_cache)
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

        tool_defs = self.tools.tools_for_llm()
        runtime_task_state = _task_state_for_context(
            state.get("task_state"),
            getattr(self, "_task_state", None),
        )
        tool_defs = filter_unavailable_lsp_tools(tool_defs, getattr(self, "_lsp_manager", None))

        guidance_messages = self._drain_pending_guidance()
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
            runtime_task_state.todo_state = raw_todo_state

        def rebuild_llm_messages(
            messages: list[BaseMessage],
            *,
            allow_inline_compaction: bool,
        ) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
            base_messages = trim_superseded_file_tools([*messages, *guidance_messages])
            if allow_inline_compaction and not compaction_happened:
                inline_compaction_guide = self._inline_compaction_guide_for(base_messages)
                if inline_compaction_guide is not None:
                    base_messages.append(inline_compaction_guide)
            return base_messages, [], False

        async def save_context_frame(
            messages: list[BaseMessage],
            token_estimate: int,
            convergence_messages: list[HumanMessage],
            convergence_forced: bool,
        ) -> None:
            if self._session is None:
                return
            await save_context_frame_from_messages(
                session_id=self._session.id,
                user_message_id=state.get("user_message_id"),
                frame_kind="main",
                agent_persona=persona,
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=messages,
                token_estimate=token_estimate,
                metadata={
                    "step": step,
                    "tool_count": len(tool_defs),
                    "convergence_hint_count": len(convergence_messages),
                    "convergence_forced": convergence_forced,
                },
            )

        def replacement_messages(assistant_msg: AIMessage) -> list[BaseMessage]:
            if not compaction_happened:
                return [assistant_msg]
            return [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *state_messages,
                assistant_msg,
            ]

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
            rebuilt_tokens = estimate_context_tokens(rebuilt, self.config.model.model)
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
        context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        if self._compaction.is_overflow({"total": context_tokens}):
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

        await save_context_frame(llm_messages, context_tokens, convergence_messages, convergence_forced)
        max_retries = 2
        failed_attempts = 0
        overflow_compaction_attempts = 0
        malformed_tool_call_attempts = 0
        retry_status_active = False
        while True:
            try:
                renderer = StreamingRenderer(self._ui.console, debug=self._debug)
                model_with_tools = self.model.bind_tools(tool_defs) if tool_defs else self.model
                assistant_msg = await _stream_llm(model_with_tools, llm_messages, renderer, resolve_protocol(self.config.model))
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
                    fallback_input_tokens=context_tokens,
                    fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
                    messages=llm_messages,
                    model=self.config.model.model,
                    cache_key=f"{self.config.model.provider}/{self.config.model.model}",
                )
                if is_malformed_tool_call_response(assistant_msg):
                    if malformed_tool_call_attempts < 1:
                        malformed_tool_call_attempts += 1
                        llm_messages = [
                            *llm_messages,
                            HumanMessage(
                                content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                additional_kwargs={GUIDANCE_MARKER: True},
                            ),
                        ]
                        context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                        self._usage_stats.update_context(context_tokens)
                        continue
                    if malformed_tool_call_attempts < 2 and compaction_happened:
                        result, _preflight_result = await self._preflight_compact_if_needed(
                            state_messages,
                            force=True,
                            reason="malformed_tool_call",
                            ask=False,
                        )
                        malformed_tool_call_attempts += 1
                        if result is not None:
                            llm_messages, convergence_messages, convergence_forced, context_tokens = (
                                await apply_compaction_result(result)
                            )
                            llm_messages = [
                                *llm_messages,
                                HumanMessage(
                                    content=MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION,
                                    additional_kwargs={GUIDANCE_MARKER: True},
                                ),
                            ]
                            context_tokens = estimate_context_tokens(llm_messages, self.config.model.model)
                            self._usage_stats.update_context(context_tokens)
                            await save_context_frame(
                                llm_messages,
                                context_tokens,
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
                if retry_status_active and self._ui.via_events():
                    await self._ui.events.emit(StatusFinished(status_id="llm:retry"))
                break
            except Exception as e:
                from .helpers import _is_context_overflow_error

                if _is_context_overflow_error(e) and overflow_compaction_attempts < 1:
                    overflow_compaction_attempts += 1
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
                        await save_context_frame(
                            llm_messages,
                            context_tokens,
                            convergence_messages,
                            convergence_forced,
                        )
                        continue
                if failed_attempts < max_retries:
                    failed_attempts += 1
                    delay = failed_attempts * 2
                    if self._ui.via_events():
                        retry_status_active = True
                        await self._ui.events.emit(StatusUpdated(
                            status_id="llm:retry",
                            label=f"LLM error, retrying in {delay}s",
                            detail=str(e),
                        ))
                    else:
                        self._ui.ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
                    await asyncio.sleep(delay)
                else:
                    if retry_status_active and self._ui.via_events():
                        await self._ui.events.emit(StatusFinished(status_id="llm:retry"))
                    self._ui.ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
                    failure_msg = AIMessage(content=f"LLM call failed: {e}")
                    return {
                        "messages": replacement_messages(failure_msg),
                        "step_count": step,
                        "should_continue": False,
                    }

        return {
            "messages": replacement_messages(assistant_msg),
            "step_count": step + 1,
            "convergence_forced": convergence_forced,
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
