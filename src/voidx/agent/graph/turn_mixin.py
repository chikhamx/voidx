"""Single-turn execution for the agent graph."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.attachments import build_user_message_payload, serialize_message_content
from voidx.agent.graph.runtime import ui
from voidx.agent.message_rows import messages_from_rows
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
from voidx.runtime.ui import (
    InputSet,
    StatusFinished,
    StatusUpdated,
    TurnStarted,
    dock,
    session_tracker,
    ui_events,
    via_events,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


RESUME_FORCE_COMPACT_MESSAGE_COUNT = 500


class GraphTurnMixin:
    async def _run_once(self: GraphRunLoopHost, user_text: str) -> None:
        t_turn_start = time.monotonic()
        t_turn_calls_start = self._usage_stats.total_calls
        t_turn_in_start = self._usage_stats.total_input_tokens
        t_turn_out_start = self._usage_stats.total_output_tokens
        user_message_id: int | None = None
        try:
            session_tracker.begin_turn(self._workspace)
            payload = build_user_message_payload(user_text, self._workspace)
            self._current_tree = dock.tree
            if via_events():
                self._turn_node = await ui_events.request(TurnStarted(text=payload.display_text))
                await ui_events.emit(StatusUpdated(
                    status_id="turn:analyzing",
                    label="Analyzing",
                    detail="loading session and preparing context",
                    stage="analyzing",
                ))
            else:
                self._turn_node = dock.start_turn(payload.display_text)
            # Load session messages — use in-memory cache when available
            force_resume_compaction = False
            if self._session_msg_cache is not None:
                session_msgs = list(self._session_msg_cache)
            else:
                if self._session:
                    message_count = await count_messages(self._session.id)
                    force_resume_compaction = (
                        self.model is not None
                        and message_count > RESUME_FORCE_COMPACT_MESSAGE_COUNT
                        and not (self._pending_summary or self._compaction_summary)
                    )
                    if force_resume_compaction:
                        if via_events():
                            await ui_events.emit(StatusUpdated(
                                status_id="turn:analyzing",
                                label="Resuming long session",
                                detail=f"{message_count} persisted messages; preparing compaction",
                                stage="analyzing",
                            ))
                        else:
                            ui.warn(
                                f"Session has {message_count} messages; compacting older context before continuing"
                            )
                    session_msgs = await load_messages(self._session.id)
                else:
                    session_msgs = []
                if self._session:
                    self._session_msg_cache = list(session_msgs)

            msgs = messages_from_rows(session_msgs)

            for warning in payload.warnings:
                ui.warn(warning)

            turn_msg = HumanMessage(content=payload.content, id=f"user_{time.time_ns()}")
            msgs.append(turn_msg)
            if self._session is None:
                self._session = await create_session(workspace=self._workspace)

            interaction_mode = getattr(
                getattr(self, "_interaction_mode", None),
                "value",
                "plan" if getattr(self, "_plan_mode", False) else "auto",
            )
            task_run = getattr(self, "_task_run", None)
            if interaction_mode == "goal" and task_run is not None and not task_run.goal:
                task_run.set_goal(payload.title_text)
            intent_resolution = resolve_turn_intent(
                payload.title_text,
                interaction_mode,
                getattr(self, "_task_state", None),
            )
            task_intent = intent_resolution.intent
            goal_scope = (
                task_run.goal
                if interaction_mode == "goal" and task_run is not None and task_run.goal
                else payload.title_text
            )
            pending_approval = _active_pending_approval(
                getattr(self, "_task_state", None),
                task_run,
                interaction_mode,
            )

            saved_user_content, user_content_format = serialize_message_content(payload.content)
            user_message_id = await save_message(MessageRow(
                session_id=self._session.id,
                role="user",
                content=saved_user_content,
                content_format=user_content_format,
                created_at=_now(),
            ))
            if self._session_msg_cache is not None:
                self._session_msg_cache.append(MessageRow(
                    id=user_message_id,
                    session_id=self._session.id,
                    role="user",
                    content=saved_user_content,
                    content_format=user_content_format,
                    created_at=_now(),
                ))
            self._any_messages_sent = True

            initial: AgentState = {
                "messages": msgs,
                "workspace": self._workspace,
                "tool_results": {},
                "step_count": 0,
                "max_steps": 50,
                "should_continue": True,
                "agent": "orchestrator",
                "plan_mode": self._plan_mode,
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
            await self._maybe_compact(
                msgs,
                session_msgs,
                force=force_resume_compaction,
                ask=not force_resume_compaction,
            )
            if via_events():
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))

            final = await self.graph.ainvoke(initial, {"recursion_limit": self.config.agent.recursion_limit})
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
            if self.model is not None and hasattr(self, "_task_state"):
                self._task_state.update_after_turn(
                    final_resolution,
                    payload.title_text,
                    scope_text=final_scope,
                )
            if self.model is not None and interaction_mode == "goal" and task_run is not None:
                task_run.update_after_turn(
                    final_resolution,
                    payload.title_text,
                    scope_text=final_scope,
                )
            if self.model is not None and task_run is not None:
                task_run.merge_skill_runs(final.get("skill_runs", []))
            await save_message_runtime_snapshot(MessageRuntimeSnapshot(
                message_id=user_message_id,
                session_id=self._session.id,
                interaction_mode=interaction_mode,
                task_intent=final_task_intent,
                intent_resolution_reason=final_intent_resolution_reason,
                goal=task_run.goal if task_run is not None else "",
                goal_phase=task_run.phase.value if task_run is not None else "",
                goal_status=task_run.status.value if task_run is not None else "",
                goal_turn_count=task_run.turn_count if task_run is not None else 0,
                pending_approval=_active_pending_approval(
                    getattr(self, "_task_state", None),
                    task_run,
                    interaction_mode,
                ),
                intent_confidence=final.get("intent_confidence"),
                intent_source=final.get("intent_source", ""),
                intent_refined=bool(final.get("intent_refined", False)),
                available_tool_ids=list(final.get("available_tool_ids", []) or []),
            ))
            await self._persist_runtime_state()

            # ── prune old tool outputs after turn ──────────────────────────
            self._compaction.prune(final["messages"])

            # Persist new messages
            if self._session:
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
                            session_id=self._session.id,
                            role="assistant",
                            content=saved,
                            content_format=fmt,
                            tool_calls=msg.tool_calls if msg.tool_calls else None,
                            created_at=_now(),
                        ))
                        if self._session_msg_cache is not None:
                            self._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=self._session.id,
                                role="assistant",
                                content=saved,
                                content_format=fmt,
                                tool_calls=msg.tool_calls if msg.tool_calls else None,
                                created_at=_now(),
                            ))
                    elif isinstance(msg, ToolMessage):
                        row_id = await save_message(MessageRow(
                            session_id=self._session.id,
                            role="tool",
                            content=str(msg.content),
                            tool_call_id=getattr(msg, "tool_call_id", None),
                            created_at=_now(),
                        ))
                        if self._session_msg_cache is not None:
                            self._session_msg_cache.append(MessageRow(
                                id=row_id,
                                session_id=self._session.id,
                                role="tool",
                                content=str(msg.content),
                                tool_call_id=getattr(msg, "tool_call_id", None),
                                created_at=_now(),
                            ))
                await touch_session(self._session.id)

                # Auto-title on first message
                if len(session_msgs) <= 1:
                    title_source = payload.title_text
                    title = title_source[:80] + ("..." if len(title_source) > 80 else "")
                    await update_title(self._session.id, title)
                await self._persist_transcript_snapshot()

            elapsed = time.monotonic() - t_turn_start
            stats = self._usage_stats
            turn_calls = stats.total_calls - t_turn_calls_start
            turn_in = stats.total_input_tokens - t_turn_in_start
            turn_out = stats.total_output_tokens - t_turn_out_start
            from voidx.llm.usage import format_token_count
            dock.append_message(
                f"[dim]✻  {elapsed:.0f}s[/dim]"
                f"  [dim]·[/dim]  [cyan]{turn_calls}[/cyan] [dim]llm calls[/dim]"
                f"  [dim]·[/dim]  [cyan]{format_token_count(turn_in)}[/cyan] [dim]in[/dim]"
                f"  [cyan]{format_token_count(turn_out)}[/cyan] [dim]out[/dim]",
                markup=True,
            )
            session_tracker.finish_turn()
            change_lines = session_tracker.change_summary_lines()
            if change_lines:
                dock.append_message(
                    "\n".join(change_lines),
                    markup=True,
                )
        except (KeyboardInterrupt, asyncio.CancelledError):
            if self._session is not None and user_message_id is not None:
                await delete_messages_from(self._session.id, user_message_id)
                if self._session_msg_cache is not None:
                    self._session_msg_cache = [
                        r for r in self._session_msg_cache
                        if r.id is None or r.id < user_message_id
                    ]
            raise
        finally:
            session_tracker.finish_turn()
            if via_events():
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))
                await ui_events.emit(StatusFinished(status_id="agent:-1:progress"))
                await ui_events.emit(StatusFinished(status_id="compaction"))
                await ui_events.emit(InputSet(text="", hints=[]))
                await ui_events.drain()
            else:
                dock.set_input("", [])


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
