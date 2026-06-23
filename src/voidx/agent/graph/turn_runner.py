"""Composition component for single-turn agent graph execution."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.attachments import build_user_message_payload, serialize_message_content
from voidx.agent.message_rows import messages_from_rows_incremental
from voidx.agent.goal_resolver import resolve_goal_for_turn, resolve_goal_mode, resolve_plan_mode
from voidx.agent.runtime_context import TaskIntent
from voidx.agent.graph.runtime_guards import RuntimeGuardState
from voidx.agent.state import AgentState
from voidx.agent.task_state import TaskState, TurnExchange, goal_label, goal_type_from_join
from voidx.llm.message_status import message_status
from voidx.memory.service import (
    MessageRow,
    MessageRuntimeSnapshot,
    count_messages,
    create_session,
    delete_messages_from,
    load_messages,
    memory_now,
    save_message,
    save_message_runtime_snapshot,
    touch_session,
    update_title,
)
from voidx.skills.service import skill_reference_message
from voidx.runtime.ui import (
    InputSet,
    StatusFinished,
    StatusUpdated,
    TodoCleared,
    TodoCommitted,
    TurnStarted,
    WarningAppended,
)
from voidx.workflow.service import reconcile_workflow_runs_for_turn
from voidx.workflow.types import WorkflowRunStatus

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


RESUME_FORCE_COMPACT_MESSAGE_COUNT = 500
DEFAULT_RECURSION_LIMIT = 500
RECENT_EXCHANGE_LIMIT = 3
ASSISTANT_TEXT_MAX_CHARS = 500


def _resolve_recursion_limit(*_args, **_kwargs) -> int:
    """Return the graph safety guard, independent of agent step budgets."""
    return DEFAULT_RECURSION_LIMIT


def _initial_persona_for_goal(task_state: TaskState) -> str:
    personas: list[str] = []
    for run in (task_state.workflow_runs or {}).values():
        if run.status == WorkflowRunStatus.ACTIVE:
            personas.extend(persona for persona in run.personas if persona)
    if personas:
        return ",".join(dict.fromkeys(personas))

    join = task_state.workflow_route.join if task_state.workflow_route is not None else ""
    return {
        "brainstorm": "plan",
        "debug": "explore",
        "design": "plan",
        "feedback": "review",
        "plan": "plan",
        "review": "review",
        "tdd": "implement",
        "verify": "review",
    }.get(join, {
        "debug": "explore",
        "review": "review",
        "feature": "implement",
        "design": "plan",
        "doc": "plan",
    }.get(goal_type_from_join(join), "coordinate"))


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
        host._runtime_guards = RuntimeGuardState()
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
            base_task_state = _load_task_state(getattr(host, "_task_state", None))
            if not base_task_state.recent_exchanges and session_msgs:
                base_task_state.recent_exchanges = _rebuild_exchanges_from_session_msgs(session_msgs)
            if interaction_mode == "goal" and base_task_state.current_goal is None:
                base_task_state.set_goal(payload.title_text)
            if interaction_mode == "plan":
                intent_resolution = resolve_plan_mode(payload.title_text, base_task_state)
            elif interaction_mode == "goal":
                intent_resolution = resolve_goal_mode(payload.title_text, base_task_state)
            else:
                intent_resolution = await resolve_goal_for_turn(
                    model=host.model,
                    user_text=payload.title_text,
                    interaction_mode=interaction_mode,
                    task_state=base_task_state,
                    log_diagnostic=bool(getattr(host.config, "log_llm_diagnostic", False)),
                )
            turn_task_state = base_task_state.model_copy(deep=True)
            turn_task_state.update_after_turn(
                intent_resolution,
                payload.title_text,
                scope_text=(
                    goal_label(base_task_state.current_goal)
                    if interaction_mode == "goal"
                    else payload.title_text
                ),
            )
            reconciled_workflow_runs = reconcile_workflow_runs_for_turn(
                goal_resolution=intent_resolution,
                after_state=turn_task_state,
            )
            turn_task_state.workflow_runs = {
                run.name: run
                for run in reconciled_workflow_runs
            }

            # Sync resolved goal and workflow to host so status bar updates immediately
            if host.model is not None:
                host._task_state = turn_task_state.model_copy(deep=True)
                _invalidate_tui(host)

            saved_user_content, user_content_format = serialize_message_content(payload.content)
            user_message_id = await save_message(MessageRow(
                session_id=host._session.id,
                role="user",
                content=saved_user_content,
                content_format=user_content_format,
                created_at=memory_now(),
            ))
            if host._session_msg_cache is not None:
                host._session_msg_cache.append(MessageRow(
                    id=user_message_id,
                    session_id=host._session.id,
                    role="user",
                    content=saved_user_content,
                    content_format=user_content_format,
                    created_at=memory_now(),
                ))
            host._any_messages_sent = True

            initial: AgentState = {
                "messages": msgs,
                "workspace": host._workspace,
                "tool_results": {},
                "step_count": 0,
                "should_continue": True,
                "persona": _initial_persona_for_goal(turn_task_state),
                "plan_mode": host._plan_mode,
                "interaction_mode": interaction_mode,
                "task_state": turn_task_state.model_dump(mode="json"),
                "user_message_id": user_message_id,
            }

            # ── compaction: check overflow before running ──────────────────
            preflight_result, _preflight_metadata = await host._preflight_compact_if_needed(
                msgs,
                session_msgs,
                force=force_resume_compaction,
                ask=not force_resume_compaction,
                reason="resume" if force_resume_compaction else "soft_threshold",
            )
            if preflight_result is not None:
                msgs.clear()
                msgs.extend(preflight_result.live_messages)
                initial["messages"] = msgs
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(status_id="turn:analyzing"))

            recursion_limit = _resolve_recursion_limit()
            final = await host.graph.ainvoke(initial, {"recursion_limit": recursion_limit})
            final_task_state = _load_task_state(final.get("task_state"), fallback=turn_task_state)
            exchange = _turn_exchange_from_final_messages(payload.title_text, final.get("messages", []))
            if exchange is not None:
                final_task_state.recent_exchanges = [
                    *final_task_state.recent_exchanges,
                    exchange,
                ][-RECENT_EXCHANGE_LIMIT:]
            if host.model is not None:
                host._task_state = final_task_state
            await save_message_runtime_snapshot(MessageRuntimeSnapshot(
                message_id=user_message_id,
                session_id=host._session.id,
                interaction_mode=interaction_mode,
                task_intent=final_task_state.current_intent,
                current_goal=final_task_state.current_goal,
                workflow_runs=final_task_state.workflow_runs,
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
                new_messages = sanitize_todo_replay_messages(list(new_messages))

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
                            created_at=memory_now(),
                        ))
                        if host._session_msg_cache is not None:
                            host._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=host._session.id,
                                role="assistant",
                                content=saved,
                                content_format=fmt,
                                tool_calls=msg.tool_calls if msg.tool_calls else None,
                                created_at=memory_now(),
                            ))
                    elif isinstance(msg, ToolMessage):
                        status = message_status(getattr(msg, "status", None))
                        row_id = await save_message(MessageRow(
                            session_id=host._session.id,
                            role="tool",
                            content=str(msg.content),
                            tool_call_id=getattr(msg, "tool_call_id", None),
                            status=status,
                            created_at=memory_now(),
                        ))
                        if host._session_msg_cache is not None:
                            host._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=host._session.id,
                                role="tool",
                                content=str(msg.content),
                                tool_call_id=getattr(msg, "tool_call_id", None),
                                status=status,
                                created_at=memory_now(),
                            ))
                await touch_session(host._session.id)

                # Auto-title on first message
                if is_first_user_message:
                    goal = intent_resolution.goal
                    title = (
                        goal.desc.strip()
                        if goal is not None and goal.desc.strip()
                        else host._temporary_session_title(payload.title_text)
                    )
                    await update_title(host._session.id, title)
                    host._session = host._session.model_copy(update={"title": title})
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


def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    if app is not None and callable(getattr(app, "invalidate", None)):
        app.invalidate()


def _load_task_state(value: TaskState | dict | None, *, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        return TaskState.model_validate(value)
    return fallback.model_copy(deep=True) if fallback is not None else TaskState()


def _turn_exchange_from_final_messages(user_text: str, messages: list[object]) -> TurnExchange | None:
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or last.tool_calls:
        return None
    assistant_text = _truncate_assistant_text(_message_text(last).strip())
    if not assistant_text:
        return None
    return TurnExchange(user_text=user_text, assistant_text=assistant_text)


def _rebuild_exchanges_from_session_msgs(
    session_msgs: list[MessageRow],
    max_exchanges: int = RECENT_EXCHANGE_LIMIT,
) -> list[TurnExchange]:
    exchanges: list[TurnExchange] = []
    i = len(session_msgs) - 1
    while i >= 0 and len(exchanges) < max_exchanges:
        if session_msgs[i].role != "assistant" or session_msgs[i].tool_calls:
            i -= 1
            continue
        assistant_text = _truncate_assistant_text(session_msgs[i].content.strip())
        if not assistant_text:
            i -= 1
            continue
        j = i - 1
        while j >= 0 and session_msgs[j].role != "user":
            j -= 1
        if j < 0:
            break
        user_text = session_msgs[j].content.strip()
        if user_text:
            exchanges.append(TurnExchange(user_text=user_text, assistant_text=assistant_text))
        i = j - 1
    exchanges.reverse()
    return exchanges


def _truncate_assistant_text(text: str) -> str:
    if len(text) <= ASSISTANT_TEXT_MAX_CHARS:
        return text
    return "..." + text[-(ASSISTANT_TEXT_MAX_CHARS - 3):]


def _message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)
