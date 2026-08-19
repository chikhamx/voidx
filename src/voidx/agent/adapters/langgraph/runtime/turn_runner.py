"""Composition component for single-turn agent graph execution."""

from __future__ import annotations

from voidx.agent.domain.ui_events import GuidanceCommitted, InputSet, StatusFinished, StatusUpdated, TodoCleared, TodoCommitted, TurnCancelled, TurnCompleted, TurnFailed, TurnStarted

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from voidx.agent.adapters.langgraph.runtime.convergence import generate_fallback_summary

from voidx.agent.application.attachments import build_user_message_payload, serialize_message_content
from voidx.agent.adapters.persistence.message_rows import (
    is_user_turn_row,
    messages_from_rows_incremental,
)
from voidx.agent.application.automation.goal.goal_resolver import build_goal_resolution, resolve_plan_mode
from voidx.agent.application.runtime_context import TaskIntent
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.domain.turn_metadata import turn_metadata_from_context
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    bind_thread_execution_context,
    current_thread_execution_state,
)
from voidx.agent.adapters.langgraph.state import AgentState
from voidx.agent.domain.task.state import (
    GoalResolution,
    IntentResolution,
    TaskState,
    TurnExchange,
    goal_label,
    goal_type_from_join,
)
from voidx.llm.message_status import message_status
from voidx.observability.tool_log import log_tool_event
from voidx.agent.adapters.persistence.session_repository import MessageRow, count_messages, create_session, delete_messages_from, load_messages, save_message, touch_session, update_title
from voidx.agent.adapters.persistence.runtime_state_repository import MessageRuntimeSnapshot, save_message_runtime_snapshot
from voidx.persistence.sqlite import now as memorynow
from voidx.agent.application.automation.workflow.service import reconcile_workflow_runs_for_turn
from voidx.agent.domain.automation.workflow import WorkflowRunStatus

class _EmptyReferenceMessage:
    prefix = ""
    remove_spans: list[tuple[int, int]] = []


RESUME_FORCE_COMPACT_MESSAGE_COUNT = 500
DEFAULT_RECURSION_LIMIT = 2000
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


class TurnRunner:
    """Runs one top-level user turn for a graph host."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.idle_event = asyncio.Event()
        self.idle_event.set()

    async def run_once(
        self,
        user_text: str,
        *,
        display_text: str | None = None,
        context: TurnExecutionContext,
        persist_user_input: bool = True,
    ) -> None:
        host = self.host
        context_session_id = context.session_id
        context_thread_id = context.thread_id

        async with bind_thread_execution_context(
            host,
            session_id=context_session_id,
            thread_id=context_thread_id,
            turn_context=context,
        ):
            t_turn_start = time.monotonic()
            host._usage_stats.begin_turn()
            host._pending_turn_stop_commit = None
            self.idle_event.clear()
            user_message_id: int | None = None
            streamed_messages: list = []
            payload = None
            try:
                host._ui.session_tracker.begin_turn(host._workspace)
                has_ref = "$" in user_text
                skill_refs = (
                    host._resolve_skill_references(user_text)
                    if has_ref
                    else _EmptyReferenceMessage()
                )
                mcp_refs = _EmptyReferenceMessage()
                if has_ref and host._mcp_reference_resolver is not None:
                    mcp_refs = await host._mcp_reference_resolver(
                        user_text,
                        settings=host._settings,
                        manager=host.mcp_manager,
                    )
                combined_prefix = "\n\n".join(
                    p for p in [skill_refs.prefix, mcp_refs.prefix] if p
                )
                combined_spans = sorted(
                    [*skill_refs.remove_spans, *mcp_refs.remove_spans],
                    key=lambda span: span[0],
                )
                payload = build_user_message_payload(
                    user_text,
                    host._workspace,
                    text_prefix=combined_prefix,
                    extra_removed_spans=combined_spans,
                )
                turn_display_text = display_text or payload.display_text
                turn_metadata = turn_metadata_from_context(context)
                host._current_tree = host._ui.dock.tree
                if host._ui.via_events():
                    host._turn_node = await host._ui.events.request(
                        TurnStarted(text=turn_display_text, raw_text=payload.raw_text, metadata=turn_metadata)
                    )
                    await host._ui.events.emit(StatusUpdated(
                        status_id="turn:analyzing",
                        label="Analyzing",
                        detail="loading session and preparing context",
                        stage="analyzing",
                        display="record_only",
                    ))
                else:
                    host._turn_node = host._ui.dock.start_turn(
                        turn_display_text, metadata=turn_metadata, raw_text=payload.raw_text
                    )
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
                                    display="record_only",
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
                is_first_user_message = not any(is_user_turn_row(row) for row in session_msgs)

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
                if host._session is None and not context.detached:
                    host._session = await create_session(workspace=host._workspace)

                interaction_mode = getattr(
                    getattr(host, "_interaction_mode", None),
                    "value",
                    InteractionMode.PLAN.value if getattr(host, "_plan_mode", False) else InteractionMode.AUTO.value,
                )
                base_task_state = _load_task_state(getattr(host, "_task_state", None))
                if not base_task_state.recent_exchanges and session_msgs:
                    base_task_state.recent_exchanges = _rebuild_exchanges_from_session_msgs(session_msgs)
                if interaction_mode == InteractionMode.GOAL.value and base_task_state.current_goal is None:
                    base_task_state.set_goal(payload.title_text)
                if interaction_mode == InteractionMode.PLAN.value:
                    intent_resolution = resolve_plan_mode(payload.title_text, base_task_state)
                elif interaction_mode == InteractionMode.GOAL.value:
                    intent_resolution = build_goal_resolution(payload.title_text, base_task_state)
                else:
                    intent_resolution = GoalResolution(
                        intent=IntentResolution(type=TaskIntent.CODING),
                        goal=None,
                        plan=None,
                    )
                host._task_state = base_task_state
                current_state = current_thread_execution_state()
                if current_state is not None:
                    current_state.task_state = base_task_state
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
                current_state = current_thread_execution_state()
                if current_state is not None:
                    current_state.task_state = turn_task_state
                host._task_state = turn_task_state
                if current_state is not None:
                    default_session = getattr(host, "_default_session", None)
                    if (
                        current_state.session is None
                        or default_session is None
                        or current_state.session.id == default_session.id
                    ):
                        host._default_task_state = turn_task_state
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

                if persist_user_input:
                    saved_user_content, user_content_format = serialize_message_content(payload.content)
                    user_message_id = await save_message(MessageRow(
                        session_id=host._session.id,
                        role="user",
                        content=saved_user_content,
                        content_format=user_content_format,
                        created_at=memorynow(),
                    ))
                    if host._session_msg_cache is not None:
                        host._session_msg_cache.append(MessageRow(
                            id=user_message_id,
                            session_id=host._session.id,
                            role="user",
                            content=saved_user_content,
                            content_format=user_content_format,
                            created_at=memorynow(),
                        ))
                host._any_messages_sent = True

                initial: AgentState = {
                    "messages": msgs,
                    "workspace": current_thread_execution_state().workspace,
                    "tool_results": {},
                    "step_count": 0,
                    "should_continue": True,
                    "persona": _initial_persona_for_goal(turn_task_state),
                    "plan_mode": host._plan_mode,
                    "interaction_mode": interaction_mode,
                    "task_state": turn_task_state.model_dump(mode="json"),
                    "user_message_id": user_message_id,
                    "turn_state": "initial",
                }

                # ── compaction: check overflow before running ──────────────────
                if host._ui.via_events():
                    await host._ui.events.emit(StatusFinished(status_id="turn:analyzing"))
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

                recursion_limit = _resolve_recursion_limit()
                final: dict[str, Any] = {}
                async for chunk in host.graph.astream(initial, {"recursion_limit": recursion_limit}, stream_mode="values"):
                    final = chunk
                    streamed_messages = list(final.get("messages", []))
                final_task_state = _load_task_state(final.get("task_state"), fallback=turn_task_state)
                exchange = (
                    _turn_exchange_from_final_messages(payload.title_text, final.get("messages", []))
                    if persist_user_input
                    else None
                )
                if exchange is not None:
                    final_task_state.recent_exchanges = [
                        *final_task_state.recent_exchanges,
                        exchange,
                    ][-RECENT_EXCHANGE_LIMIT:]
                if host.model is not None:
                    host._task_state = final_task_state
                if host._session and user_message_id is not None and not context.detached:
                    await save_message_runtime_snapshot(MessageRuntimeSnapshot(
                        message_id=user_message_id,
                        session_id=host._session.id,
                        interaction_mode=interaction_mode,
                        task_intent=final_task_state.current_intent,
                        current_goal=final_task_state.current_goal,
                        workflow_runs=final_task_state.workflow_runs,
                    ))
                # Runtime facade owns the final session runtime-state commit.

                # ── prune old tool outputs after turn ──────────────────────────
                host._compaction.prune(final["messages"])

                # Persist new messages — detached turns (empty session_id,
                # e.g. goal evaluator) must not write into any session.
                if host._session and not context.detached:
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
                    await _persist_new_messages(host, new_messages)

                    # Update session title to match current goal after turn completes
                    goal = final_task_state.current_goal
                    if goal is not None and goal.desc.strip():
                        title = goal.desc.strip()
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
                    await host._ui.events.emit(TurnCompleted())
                    await host._ui.events.drain()
                else:
                    host._ui.dock.commit_todo_state()
                if host._session:
                    await host._persist_transcript_snapshot()
            except GraphRecursionError:
                # 达到 recursion limit：用最后一次 state 生成总结，优雅收尾而非报错
                final["max_steps"] = _resolve_recursion_limit()
                fallback_text = generate_fallback_summary(final)
                fallback_msg = AIMessage(content=fallback_text)
                streamed_messages.append(fallback_msg)
                await _persist_streamed_messages(
                    host,
                    streamed_messages,
                    payload.content if payload else None,
                )
                if host._ui.via_events():
                    await host._ui.events.emit(TurnCompleted())
                    await host._ui.events.drain()
                else:
                    host._ui.ui.print(f"[yellow]{fallback_text}[/yellow]")
            except (KeyboardInterrupt, asyncio.CancelledError):
                await _persist_streamed_messages(host, streamed_messages, payload.content if payload else None)
                if host._ui.via_events():
                    await host._ui.events.emit(TurnCancelled())
                    await host._ui.events.drain()
                raise
            except Exception as exc:
                await _persist_streamed_messages(host, streamed_messages, payload.content if payload else None)
                if host._ui.via_events():
                    await host._ui.events.emit(TurnFailed(message=str(exc)))
                    await host._ui.events.drain()
                raise
            finally:
                host._usage_stats.end_turn()
                host._pending_turn_stop_commit = None
                discard_guidance = getattr(host, "_discard_pending_guidance", None)
                if callable(discard_guidance):
                    guidance_discarded = discard_guidance()
                else:
                    pending_guidance = getattr(host, "_pending_guidance", None)
                    guidance_discarded = bool(pending_guidance)
                    if pending_guidance is not None:
                        pending_guidance.clear()
                if guidance_discarded:
                    message = "Guidance discarded: no LLM call to inject into."
                    log_tool_event("guidance_discarded", message=message)
                    if host._ui.via_events():
                        await host._ui.events.emit(GuidanceCommitted(source="system"))
                    else:
                        clear_guidance_preview = getattr(host._ui.dock, "clear_guidance_preview", None)
                        if callable(clear_guidance_preview):
                            clear_guidance_preview()
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
                self.idle_event.set()


async def _persist_new_messages(host: Any, new_messages: list) -> None:
    """Persist a batch of new AIMessage/ToolMessage rows to session storage and cache.

    Called incrementally during graph streaming and from exception handlers so
    that messages generated before a crash or interrupt are not lost.
    """
    if not host._session:
        return
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
                created_at=memorynow(),
            ))
            if host._session_msg_cache is not None:
                host._session_msg_cache.append(MessageRow(
                    id=row_id,
                    session_id=host._session.id,
                    role="assistant",
                    content=saved,
                    content_format=fmt,
                    tool_calls=msg.tool_calls if msg.tool_calls else None,
                    created_at=memorynow(),
                ))
        elif isinstance(msg, ToolMessage):
            status = message_status(getattr(msg, "status", None))
            row_id = await save_message(MessageRow(
                session_id=host._session.id,
                role="tool",
                content=str(msg.content),
                tool_call_id=getattr(msg, "tool_call_id", None),
                status=status,
                created_at=memorynow(),
            ))
            if host._session_msg_cache is not None:
                host._session_msg_cache.append(MessageRow(
                    id=row_id,
                    session_id=host._session.id,
                    role="tool",
                    content=str(msg.content),
                    tool_call_id=getattr(msg, "tool_call_id", None),
                    status=status,
                    created_at=memorynow(),
                ))
    if new_messages:
        await touch_session(host._session.id)


async def _persist_streamed_messages(host: Any, streamed_messages: list, payload_content: str | None) -> None:
    """Persist assistant/tool messages collected during streaming before an exception or interrupt.

    Locates the user turn message by content, sanitizes the trailing new messages,
    and delegates to _persist_new_messages so partial replies survive crashes.
    """
    if not host._session or not streamed_messages or not payload_content:
        return
    turn_index = None
    for i, msg in enumerate(streamed_messages):
        if isinstance(msg, HumanMessage) and msg.content == payload_content:
            turn_index = i
            break
    new_messages = streamed_messages[turn_index + 1:] if turn_index is not None else []
    await _persist_new_messages(host, new_messages)


def _invalidate_tui(host: object) -> None:
    host._ui.invalidate()


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
        while j >= 0 and not is_user_turn_row(session_msgs[j]):
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
