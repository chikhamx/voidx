"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.attachments import (
    build_user_message_payload,
    serialize_message_content,
)
from voidx.agent.graph.runtime import ui
from voidx.agent.message_rows import messages_from_rows
from voidx.agent.runtime_context import TaskIntent
from voidx.agent.state import AgentState
from voidx.agent.task_state import IntentResolution, PendingApproval, resolve_turn_intent
from voidx.llm.provider import get_context_limit
from voidx.memory.session import (
    MessageRow,
    create_session,
    load_messages,
    save_message,
    touch_session,
    update_title,
    delete_messages_from,
    _now,
)
from voidx.memory.runtime_state import (
    MessageRuntimeSnapshot,
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_runtime_state,
    save_message_runtime_snapshot,
    save_runtime_state,
)
from voidx.memory.transcript import load_transcript, replace_transcript
from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import dock, get_dock
from voidx.ui.output.events import (
    CompositeEventConsumer,
    DockEventConsumer,
    InputSet,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    TurnStarted,
    ui_events,
    via_events,
)
from voidx.ui.gateway import GatewayEventConsumer, GatewayServer, GatewaySession
from voidx.ui.gateway.bootstrap import emit_web_gateway_bootstrap
from voidx.ui.protocol import UiCancelCommand, UiCommand, UiSubmitCommand
from voidx.ui.session import session_tracker
from voidx.ui.session import show_startup
from voidx.ui.transcript import transcript_rows_to_tree, tree_to_transcript_rows

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphRunLoopMixin:
    async def _show_startup(self: GraphRunLoopHost, *, append_transcript: bool = False) -> None:
        is_new = self._session is None
        title = self._startup_title()
        active_dock = get_dock()
        startup_event = StartupShown(
            model=self.config.model.model,
            provider=self.config.model.provider,
            workspace=self._workspace,
            session_title=title,
            is_new=is_new,
            profile_configured=self.model is not None,
        )
        startup_via_event = active_dock is not None and ui_events.is_running
        if startup_via_event:
            await ui_events.request(startup_event)
            if append_transcript:
                await self._restore_transcript_snapshot(append=True)
            return

        if active_dock is not None and active_dock.active:
            active_dock.append_startup(
                model=self.config.model.model,
                provider=self.config.model.provider,
                workspace=self._workspace,
                session_title=title,
                is_new=is_new,
                profile_configured=self.model is not None,
            )
            if append_transcript:
                await self._restore_transcript_snapshot(append=True)
            return

        show_startup(
            console=ui,
            model=self.config.model.model,
            provider=self.config.model.provider,
            workspace=self._workspace,
            session_title=title,
            is_new=is_new,
        )
        if self.model is None:
            ui.print()
            ui.print("[yellow]No profile configured — chat is disabled until you set one up.[/yellow]")
            ui.print(f"[dim]  Use [cyan]/model new[/cyan] to create a profile interactively[/dim]")
            ui.print()

    def _startup_title(self: GraphRunLoopHost) -> str:
        title = self._session.title if self._session else "New session"
        if len(title) > 60:
            title = title[:57] + "..."
        return title

    async def run(
        self: GraphRunLoopHost,
        *,
        web: bool = False,
        web_headless: bool = False,
        web_host: str = "127.0.0.1",
        web_port: int = 0,
        web_token: str = "",
    ) -> None:
        """Interactive REPL with orchestrator agent."""
        from voidx.ui.output.types import McpServerStatus, UiStatus
        from voidx.ui.tui import PureTui

        self._any_messages_sent = False
        session_tracker.clear()

        title = self._startup_title()

        dock.begin_capture()
        active_dock = get_dock()
        gateway_session: GatewaySession | None = None
        gateway_server: GatewayServer | None = None
        if active_dock is not None:
            consumer = DockEventConsumer(active_dock)
            if web:
                gateway_session = GatewaySession(
                    lambda: active_dock.tree,
                    session_id=self._session.id if self._session else "",
                )
                ui_events.start(CompositeEventConsumer(
                    primary=consumer,
                    mirrors=[GatewayEventConsumer(gateway_session)],
                ))
            else:
                ui_events.start(consumer)
        await self._restore_runtime_state()
        await self._show_startup(append_transcript=True)

        exit_message: str | None = None

        app = PureTui(
            UiStatus(
                provider=self.config.model.provider,
                model=self.config.model.model,
                workspace=self._workspace,
                session_title=title,
                context_limit=get_context_limit(self.config.model.provider),
                reasoning_effort=self.config.model.reasoning_effort or "xhigh",
                permission_label=self._permission.status_label,
                sandbox_label=lambda: self._permission._sandbox_label(),
                approval_label=lambda: self._permission._approval_label(),
                approval_reviewer_label=lambda: self._permission._reviewer_label(),
                usage_stats=self._usage_stats,
                debug=lambda: self._debug,
                plan_mode=lambda: self._plan_mode,
                interaction_mode=lambda: getattr(
                    getattr(self, "_interaction_mode", None),
                    "value",
                    "plan" if getattr(self, "_plan_mode", False) else "auto",
                ),
                goal_label=lambda: getattr(getattr(self, "_task_run", None), "goal", ""),
                goal_phase=lambda: getattr(getattr(getattr(self, "_task_run", None), "phase", None), "value", "clarify"),
                goal_status=lambda: getattr(getattr(getattr(self, "_task_run", None), "status", None), "value", "idle"),
                goal_turn_count=lambda: getattr(getattr(self, "_task_run", None), "turn_count", 0),
                goal_awaiting_approval=lambda: bool(getattr(getattr(self, "_task_run", None), "pending_approval", None)),
                mcp_servers=lambda: [
                    McpServerStatus(
                        name=s.name,
                        status=s.status,
                        tool_count=s.tool_count,
                    )
                    for s in (
                        self._mcp_manager.statuses()
                        if hasattr(self, '_mcp_manager')
                        else []
                    )
                ] if self._settings is not None else [],
                mcp_config_path=str(self._settings.path) if self._settings is not None else "",
                code_ide=lambda: (
                    self._settings.get_code_ide().value
                    if self._settings is not None
                    else "trae"
                ),
            ),
            COMMANDS,
        )
        self._app = app

        if gateway_session is not None:
            gateway_session.set_command_handler(partial(self._handle_web_command, app))
            app.set_external_request_handler(gateway_session.request)
            gateway_server = GatewayServer(
                gateway_session,
                host=web_host,
                port=web_port,
                token=web_token,
            )
            await gateway_server.start()
            if web_headless:
                emit_web_gateway_bootstrap(gateway_server.url)
            else:
                dock.append_message(f"Web UI gateway: {gateway_server.url}")

        if hasattr(self, '_lsp_manager'):
            lsp_lines = []
            for check in self._lsp_manager.doctor():
                if check.available and check.enabled:
                    source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                    lsp_lines.append(f"  [cyan]{check.language}[/cyan] [dim]→[/dim] {check.resolved_path}{source}")
            if lsp_lines:
                dock.append_message("\n".join(lsp_lines), markup=True)

        if hasattr(self, '_mcp_manager'):
            servers = self._settings.list_mcp_servers() if self._settings else []
            enabled = [s for s in servers if not s.disabled]
            if enabled:
                names = ", ".join(s.name for s in enabled)
                dock.append_message(f"[dim]MCP connecting: {names}…[/dim]", markup=True)
            await self._mcp_manager.start_all()

        async def handle_user_input(user_input: str) -> bool:
            nonlocal exit_message
            keep_running, next_exit_message = await self._handle_user_input(app, user_input)
            if next_exit_message is not None:
                exit_message = next_exit_message
            return keep_running

        try:
            if web_headless:
                await app.run_headless(handle_user_input)
            else:
                await app.run(handle_user_input)
            if exit_message is None:
                exit_message = "\n[dim]bye.[/dim]"
        finally:
            if gateway_server is not None:
                await gateway_server.stop()
            if hasattr(self, '_mcp_manager'):
                await self._mcp_manager.stop_all()
            if hasattr(self, '_lsp_manager'):
                await self._lsp_manager.stop_all()
            if ui_events.is_running:
                await ui_events.stop()
            dock.deactivate()
            if exit_message:
                ui.print(exit_message)

    async def _handle_web_command(self: GraphRunLoopHost, app, command: UiCommand) -> None:
        if isinstance(command, UiSubmitCommand):
            app.submit_external_input(command.text)
        elif isinstance(command, UiCancelCommand):
            app.cancel_external_input()

    async def _handle_user_input(self: GraphRunLoopHost, app, user_input: str) -> tuple[bool, str | None]:
        user_input = user_input.strip()
        if not user_input:
            return True, None

        if user_input.startswith("/"):
            if user_input in ("/exit", "/quit"):
                return False, "\n[dim]bye.[/dim]"
            is_quiet = app.consume_quiet_command(user_input)
            hide_command_output = getattr(app, "hide_command_output", None)
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            if not is_quiet:
                dock.start_turn(user_input)
            dispatched = await self._dispatch_slash(user_input)
            if not dispatched:
                ui.print(f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]")
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            return True, None

        try:
            await self._run_once(user_input)
        except (KeyboardInterrupt, asyncio.CancelledError):
            ui.print(f"\n[dim]Interrupted.[/dim]")
        return True, None

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
            if self._session_msg_cache is not None:
                session_msgs = list(self._session_msg_cache)
            else:
                session_msgs = (await load_messages(self._session.id)) if self._session else []
                if self._session:
                    self._session_msg_cache = list(session_msgs)
            truncation_notice: str | None = None
            # Safety: if session is huge, only load recent messages
            if len(session_msgs) > 500:
                original_count = len(session_msgs)
                omitted_count = original_count - 200
                ui.warn(f"Session has {original_count} messages — loading last 200")
                session_msgs = session_msgs[-200:]
                truncation_notice = (
                    f"Earlier session context was truncated for this turn: "
                    f"{omitted_count} older persisted messages were omitted. "
                    f"Only the latest {len(session_msgs)} persisted messages "
                    "plus the current user message are available."
                )

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
            head, tail_id = await self._maybe_compact(msgs, session_msgs)
            if truncation_notice:
                existing_summary = self._pending_summary or self._compaction_summary
                self._pending_summary = (
                    f"{truncation_notice}\n\n{existing_summary}"
                    if existing_summary
                    else truncation_notice
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
            change_lines = session_tracker.change_summary_lines()
            if change_lines:
                dock.append_message(
                    "\n".join(change_lines),
                    markup=True,
                )
            session_tracker.finish_turn()
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

    async def _dispatch_slash(self: GraphRunLoopHost, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._slash.dispatch(inp)

    async def _restore_runtime_state(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        snapshot = await load_runtime_state(self._session.id)
        self._interaction_mode = snapshot.interaction_mode
        self._task_state = snapshot.task_state
        self._task_run = snapshot.task_run
        self._compaction_summary = snapshot.compaction_summary

    async def _persist_runtime_state(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun, TaskState

        interaction_mode = getattr(self, "_interaction_mode", None) or InteractionMode.AUTO
        task_state = getattr(self, "_task_state", None) or TaskState()
        task_run = getattr(self, "_task_run", None) or TaskRun()
        await save_runtime_state(
            self._session.id,
            RuntimeStateSnapshot(
                interaction_mode=interaction_mode,
                task_state=task_state,
                task_run=task_run,
                compaction_summary=getattr(self, "_compaction_summary", ""),
            ),
        )

    async def _clear_runtime_state(self: GraphRunLoopHost) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun, TaskState

        if self._session is not None:
            await clear_runtime_state(self._session.id)
        self._interaction_mode = InteractionMode.AUTO
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._compaction_summary = ""
        self._pending_summary = None

    async def _persist_transcript_snapshot(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        active_dock = get_dock()
        if active_dock is None:
            return
        rows, turn_count = tree_to_transcript_rows(self._session.id, active_dock.tree)
        await replace_transcript(self._session.id, rows, turn_count=turn_count)

    async def _restore_transcript_snapshot(self: GraphRunLoopHost, *, append: bool = False) -> bool:
        if self._session is None:
            return False
        active_dock = get_dock()
        if active_dock is None:
            return False
        rows = await load_transcript(self._session.id)
        if not rows:
            return False
        active_dock.restore_tree(transcript_rows_to_tree(rows), append=append)
        return True


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
