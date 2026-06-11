"""Composition component for single-turn agent graph execution."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.attachments import build_user_message_payload, serialize_message_content
from voidx.agent.message_rows import messages_from_rows_incremental
from voidx.agent.runtime_context import TaskIntent
from voidx.agent.state import AgentState
from voidx.agent.task_state import IntentResolution, PendingApproval, resolve_turn_intent
from voidx.memory.runtime_state import MessageRuntimeSnapshot, save_message_runtime_snapshot
from voidx.memory.session import (
    MessageRow,
    _now,
    count_messages,
    create_session,
    delete_messages_from,
    load_messages,
    save_message,
    touch_session,
    update_title,
)
from voidx.skills.references import skill_reference_message
from voidx.ui.output.events.schema import (
    InputSet,
    StatusFinished,
    StatusUpdated,
    TodoCleared,
    TodoCommitted,
    TurnStarted,
    WarningAppended,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


RESUME_FORCE_COMPACT_MESSAGE_COUNT = 500


def _resolve_max_steps(config, agent_name: str) -> int:
    """Resolve max_steps from config, with fallback for test mocks."""
    steps = getattr(config, 'agent_max_steps', None)
    if steps is not None:
        return getattr(steps, agent_name, 100)
    return 100


def _resolve_recursion_limit(steps, agent_name: str) -> int:
    """Derive effective recursion limit from max_steps.

    Each LLM step consumes ~2 graph recursions (call_llm → execute_tools → call_llm),
    so the limit must be at least 2 * max_steps + margin to avoid premature cutoff.
    """
    configured = getattr(steps, 'recursion_limit', 500) if steps is not None else 500
    max_steps = getattr(steps, agent_name, 100) if steps is not None else 100
    return max(configured, 2 * max_steps + 10)


class GraphTurnRunner:
    """Runs one top-level user turn for a graph host."""

    def __init__(self, host: GraphRunLoopHost) -> None:
        self.host = host

    async def run_once(
        self,
        user_text: str,
        *,
        display_text: str | None = None,
    ) -> None:
        host = self.host
        t_turn_start = time.monotonic()
        host._usage_stats.begin_turn()
        user_message_id: int | None = None
        try:
            host._ui.session_tracker.begin_turn(host._workspace)
            skill_service = host._skill_service_for_references() if "$" in user_text else None
            skill_refs = skill_reference_message(
                user_text,
                host._workspace,
                settings=host._settings,
                service=skill_service,
            )
            payload = build_user_message_payload(
                user_text,
                host._workspace,
                text_prefix=skill_refs.prefix,
                extra_removed_spans=skill_refs.remove_spans,
            )
            turn_display_text = display_text or payload.display_text
            host._current_tree = host._ui.dock.tree
            if host._ui.via_events():
                host._turn_node = await host._ui.events.request(TurnStarted(text=turn_display_text))
                await host._ui.events.emit(StatusUpdated(
                    status_id="turn:analyzing",
                    label="Analyzing",
                    detail="loading session and preparing context",
                    stage="analyzing",
                ))
            else:
                host._turn_node = host._ui.dock.start_turn(turn_display_text)
            # Load session messages — use in-memory cache when available
            force_resume_compaction = False
            if host._session_msg_cache is not None:
                session_msgs = list(host._session_msg_cache)
            else:
                if host._session:
                    message_count = await count_messages(host._session.id)
                    force_resume_compaction = (
                        host.model is not None
                        and message_count > RESUME_FORCE_COMPACT_MESSAGE_COUNT
                        and not (host._pending_summary or host._compaction_summary)
                    )
                    if force_resume_compaction:
                        if host._ui.via_events():
                            await host._ui.events.emit(StatusUpdated(
                                status_id="turn:analyzing",
                                label="Resuming long session",
                                detail=f"{message_count} persisted messages; preparing compaction",
                                stage="analyzing",
                            ))
                        else:
                            host._ui.ui.warn(
                                f"Session has {message_count} messages; compacting older context before continuing"
                            )
                    session_msgs = await load_messages(host._session.id)
                else:
                    session_msgs = []
                if host._session:
                    host._session_msg_cache = list(session_msgs)
            is_first_user_message = not any(row.role == "user" for row in session_msgs)

            context_cache = getattr(host, "_context_cache", None)
            if context_cache is not None:
                msgs, context_cache.row_messages = messages_from_rows_incremental(
                    session_msgs,
                    context_cache.row_messages,
                )
            else:
                msgs, _ = messages_from_rows_incremental(session_msgs, {})

            for warning in payload.warnings:
                host._ui.ui.warn(warning)

            turn_msg = HumanMessage(content=payload.content, id=f"user_{time.time_ns()}")
            msgs.append(turn_msg)
            if host._session is None:
                host._session = await create_session(workspace=host._workspace)

            interaction_mode = getattr(
                getattr(host, "_interaction_mode", None),
                "value",
                "plan" if getattr(host, "_plan_mode", False) else "auto",
            )
            task_run = getattr(host, "_task_run", None)
            if interaction_mode == "goal" and task_run is not None and not task_run.goal:
                task_run.set_goal(payload.title_text)
            intent_resolution = resolve_turn_intent(
                payload.title_text,
                interaction_mode,
                getattr(host, "_task_state", None),
            )
            task_intent = intent_resolution.intent
            goal_scope = (
                task_run.goal
                if interaction_mode == "goal" and task_run is not None and task_run.goal
                else payload.title_text
            )
            pending_approval = _active_pending_approval(
                getattr(host, "_task_state", None),
                task_run,
                interaction_mode,
            )

            saved_user_content, user_content_format = serialize_message_content(payload.content)
            user_message_id = await save_message(MessageRow(
                session_id=host._session.id,
                role="user",
                content=saved_user_content,
                content_format=user_content_format,
                created_at=_now(),
            ))
            if host._session_msg_cache is not None:
                host._session_msg_cache.append(MessageRow(
                    id=user_message_id,
                    session_id=host._session.id,
                    role="user",
                    content=saved_user_content,
                    content_format=user_content_format,
                    created_at=_now(),
                ))
            host._any_messages_sent = True

            initial: AgentState = {
                "messages": msgs,
                "workspace": host._workspace,
                "tool_results": {},
                "step_count": 0,
                "max_steps": _resolve_max_steps(host.config, "orchestrator"),
                "should_continue": True,
                "agent": "orchestrator",
                "plan_mode": host._plan_mode,
                "interaction_mode": interaction_mode,
                "task_intent": task_intent.value,
                "intent_resolution_reason": intent_resolution.reason,
                "pending_approval": _dump_pending_approval(pending_approval),
                "goal": task_run.goal if task_run is not None else "",
                "goal_phase": task_run.phase.value if task_run is not None else "",
                "goal_status": task_run.status.value if task_run is not None else "",
                "goal_turn_count": task_run.turn_count if task_run is not None else 0,
                "user_message_id": user_message_id,
            }

            # ── compaction: check overflow before running ──────────────────
            await host._maybe_compact(
                msgs,
                session_msgs,
                force=force_resume_compaction,
                ask=not force_resume_compaction,
            )
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(status_id="turn:analyzing"))

            recursion_limit = _resolve_recursion_limit(
                getattr(host.config, 'agent_max_steps', None), "orchestrator",
            )
            final = await host.graph.ainvoke(initial, {"recursion_limit": recursion_limit})
            final_task_intent = TaskIntent(final.get("task_intent", task_intent.value))
            final_intent_resolution_reason = final.get(
                "intent_resolution_reason",
                intent_resolution.reason,
            )
            final_pending_approval = _load_pending_approval(final.get("pending_approval"))
            final_scope = final_pending_approval.scope if final_pending_approval else goal_scope
            final_resolution = IntentResolution(
                intent=final_task_intent,
                reason=final_intent_resolution_reason,
                confirmed_approval=intent_resolution.confirmed_approval,
            )
            if host.model is not None and hasattr(host, "_task_state"):
                host._task_state.update_after_turn(
                    final_resolution,
                    payload.title_text,
                    scope_text=final_scope,
                )
            if host.model is not None and interaction_mode == "goal" and task_run is not None:
                task_run.update_after_turn(
                    final_resolution,
                    payload.title_text,
                    scope_text=final_scope,
                )
            if host.model is not None and task_run is not None:
                task_run.merge_workflow_runs(final.get("workflow_runs", []))
            await save_message_runtime_snapshot(MessageRuntimeSnapshot(
                message_id=user_message_id,
                session_id=host._session.id,
                interaction_mode=interaction_mode,
                task_intent=final_task_intent,
                intent_resolution_reason=final_intent_resolution_reason,
                goal=task_run.goal if task_run is not None else "",
                goal_phase=task_run.phase.value if task_run is not None else "",
                goal_status=task_run.status.value if task_run is not None else "",
                goal_turn_count=task_run.turn_count if task_run is not None else 0,
                pending_approval=_active_pending_approval(
                    getattr(host, "_task_state", None),
                    task_run,
                    interaction_mode,
                ),
                intent_confidence=final.get("intent_confidence"),
                intent_source=final.get("intent_source", ""),
                intent_refined=bool(final.get("intent_refined", False)),
                available_tool_ids=list(final.get("available_tool_ids", []) or []),
            ))
            await host._persist_runtime_state()

            # ── prune old tool outputs after turn ──────────────────────────
            host._compaction.prune(final["messages"])

            # Persist new messages
            if host._session:
                turn_index = None
                for i, msg in enumerate(final["messages"]):
                    if getattr(msg, "id", None) == turn_msg.id:
                        turn_index = i
                        break
                if turn_index is None:
                    for i in range(len(final["messages"]) - 1, -1, -1):
                        msg = final["messages"][i]
                        if isinstance(msg, HumanMessage) and msg.content == payload.content:
                            turn_index = i
                            break
                new_messages = final["messages"][turn_index + 1:] if turn_index is not None else []

                for msg in new_messages:
                    if isinstance(msg, AIMessage):
                        raw_content = msg.content
                        if isinstance(raw_content, list):
                            saved = json.dumps(raw_content, ensure_ascii=False)
                            fmt = "structured"
                        else:
                            saved = str(raw_content)
                            fmt = "text"
                        row_id = await save_message(MessageRow(
                            session_id=host._session.id,
                            role="assistant",
                            content=saved,
                            content_format=fmt,
                            tool_calls=msg.tool_calls if msg.tool_calls else None,
                            created_at=_now(),
                        ))
                        if host._session_msg_cache is not None:
                            host._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=host._session.id,
                                role="assistant",
                                content=saved,
                                content_format=fmt,
                                tool_calls=msg.tool_calls if msg.tool_calls else None,
                                created_at=_now(),
                            ))
                    elif isinstance(msg, ToolMessage):
                        row_id = await save_message(MessageRow(
                            session_id=host._session.id,
                            role="tool",
                            content=str(msg.content),
                            tool_call_id=getattr(msg, "tool_call_id", None),
                            created_at=_now(),
                        ))
                        if host._session_msg_cache is not None:
                            host._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=host._session.id,
                                role="tool",
                                content=str(msg.content),
                                tool_call_id=getattr(msg, "tool_call_id", None),
                                created_at=_now(),
                            ))
                await touch_session(host._session.id)

                # Auto-title on first message
                if is_first_user_message:
                    title_source = payload.title_text
                    title = host._temporary_session_title(title_source)
                    await update_title(host._session.id, title)
                    host._session = host._session.model_copy(update={"title": title})
                    scheduler = getattr(host, "_schedule_session_title_generation", None)
                    if callable(scheduler):
                        scheduler(host._session.id, title_source, title)
            elapsed = time.monotonic() - t_turn_start
            stats = host._usage_stats
            turn_calls = stats.turn_calls
            turn_in = stats.turn_input_tokens
            turn_out = stats.turn_output_tokens
            from voidx.llm.usage import format_token_count
            host._ui.dock.append_message(
                f"[dim]✻  {elapsed:.0f}s[/dim]"
                f"  [dim]·[/dim]  [cyan]{turn_calls}[/cyan] [dim]calls[/dim]"
                f"  [dim]·[/dim]  [cyan]{format_token_count(turn_in)}[/cyan] [dim]in[/dim]"
                f"  [cyan]{format_token_count(turn_out)}[/cyan] [dim]out[/dim]",
                markup=True,
            )
            host._ui.session_tracker.finish_turn()
            change_lines = host._ui.session_tracker.change_summary_lines()
            if change_lines:
                host._ui.dock.append_message(
                    "\n".join(change_lines),
                    markup=True,
                )
            if host._ui.via_events():
                await host._ui.events.emit(TodoCommitted())
                await host._ui.events.drain()
            else:
                host._ui.dock.commit_todo_state()
            if host._session:
                await host._persist_transcript_snapshot()
        except (KeyboardInterrupt, asyncio.CancelledError):
            if host._session is not None and user_message_id is not None:
                await delete_messages_from(host._session.id, user_message_id)
                if host._session_msg_cache is not None:
                    host._session_msg_cache = [
                        r for r in host._session_msg_cache
                        if r.id is None or r.id < user_message_id
                    ]
                context_cache = getattr(host, "_context_cache", None)
                if context_cache is not None:
                    context_cache.row_messages = {
                        row_id: entry
                        for row_id, entry in context_cache.row_messages.items()
                        if row_id < user_message_id
                    }
            raise
        finally:
            host._in_turn_compaction_count = 0
            host._usage_stats.end_turn()
            pending_guidance = getattr(host, "_pending_guidance", None)
            if pending_guidance is not None:
                if pending_guidance:
                    message = "Guidance discarded: no LLM call to inject into."
                    if host._ui.via_events():
                        await host._ui.events.emit(WarningAppended(message=message))
                    else:
                        host._ui.dock.append_message(f"[dim]{message}[/dim]", markup=True)
                pending_guidance.clear()
            host._ui.session_tracker.finish_turn()
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(status_id="turn:analyzing"))
                await host._ui.events.emit(StatusFinished(status_id="agent:-1:progress"))
                await host._ui.events.emit(StatusFinished(status_id="compaction"))
                await host._ui.events.emit(TodoCleared())
                await host._ui.events.emit(InputSet(text="", hints=[]))
                await host._ui.events.drain()
            else:
                host._ui.dock.clear_todo_state()
                host._ui.dock.set_input("", [])


def _active_pending_approval(task_state, task_run, interaction_mode: str) -> PendingApproval | None:
    if interaction_mode == "goal" and task_run is not None:
        return getattr(task_run, "pending_approval", None)
    if task_state is not None:
        return getattr(task_state, "pending_approval", None)
    return None


def _dump_pending_approval(value: PendingApproval | dict | None) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return value.model_dump(mode="json")


def _load_pending_approval(value: PendingApproval | dict | None) -> PendingApproval | None:
    if value is None:
        return None
    if isinstance(value, PendingApproval):
        return value
    return PendingApproval.model_validate(value)
