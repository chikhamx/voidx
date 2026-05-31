"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
import json
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.attachments import (
    build_user_message_payload,
    parse_structured_content,
    serialize_message_content,
)
from voidx.agent.graph_components.runtime import ui
from voidx.agent.state import AgentState
from voidx.agent.task_state import resolve_turn_intent
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
from voidx.ui.dock import dock, get_dock
from voidx.ui.events import (
    DockEventConsumer,
    InputSet,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    TurnStarted,
    ui_events,
)
from voidx.ui.session_changes import session_tracker
from voidx.ui.startup import show_startup
from voidx.ui.transcript import transcript_rows_to_tree, tree_to_transcript_rows


class GraphRunLoopMixin:
    async def _show_startup(self, *, append_transcript: bool = False) -> None:
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

    def _startup_title(self) -> str:
        title = self._session.title if self._session else "New session"
        if len(title) > 60:
            title = title[:57] + "..."
        return title

    async def run(self) -> None:
        """Interactive REPL with orchestrator agent."""
        from voidx.ui.app import McpServerStatus, PromptToolkitTui, UiStatus

        self._any_messages_sent = False
        session_tracker.clear()

        title = self._startup_title()

        dock.begin_capture()
        active_dock = get_dock()
        if active_dock is not None:
            ui_events.start(DockEventConsumer(active_dock))
        await self._restore_runtime_state()
        await self._show_startup(append_transcript=True)

        exit_message: str | None = None

        app = PromptToolkitTui(
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
                goal_awaiting_approval=lambda: getattr(getattr(self, "_task_run", None), "awaiting_implementation_approval", False),
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

        if hasattr(self, '_lsp_manager'):
            lsp_lines = []
            for check in self._lsp_manager.doctor():
                if check.available and check.enabled:
                    source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                    lsp_lines.append(f"  [cyan]{check.language}[/cyan] [dim]→[/dim] {check.resolved_path}{source}")
            if lsp_lines:
                app.show_transient_output("\n".join(lsp_lines), title="LSP")

        if hasattr(self, '_mcp_manager'):
            await self._mcp_manager.start_all()

        async def handle_user_input(user_input: str) -> bool:
            nonlocal exit_message
            user_input = user_input.strip()
            if not user_input:
                return True

            if user_input.startswith("/"):
                if user_input in ("/exit", "/quit"):
                    exit_message = "\n[dim]bye.[/dim]"
                    return False
                if app.consume_quiet_command(user_input):
                    app.hide_command_output()
                    with ui.capture_command_output(
                        lambda _text: None,
                        width=app.command_output_width,
                    ):
                        await self._dispatch_slash(user_input)
                    app.hide_command_output()
                    return True
                if user_input == "/":
                    app.begin_command_output(user_input)
                    with ui.capture_command_output(
                        app.append_command_output,
                        width=app.command_output_width,
                    ):
                        ui.print("[bold]Commands:[/bold]")
                        for name, desc in COMMANDS:
                            ui.print(f"  [cyan]{name}[/cyan] — {desc}")
                    return True
                app.begin_command_output(user_input)
                with ui.capture_command_output(
                    app.append_command_output,
                    width=app.command_output_width,
                ):
                    dispatched = await self._dispatch_slash(user_input)
                return True if dispatched else True

            try:
                await self._run_once(user_input)
            except (KeyboardInterrupt, asyncio.CancelledError):
                ui.print(f"\n[dim]Interrupted.[/dim]")
            return True

        try:
            await app.run(handle_user_input)
            if exit_message is None:
                exit_message = "\n[dim]bye.[/dim]"
        finally:
            if hasattr(self, '_mcp_manager'):
                await self._mcp_manager.stop_all()
            if hasattr(self, '_lsp_manager'):
                await self._lsp_manager.stop_all()
            if ui_events.is_running:
                await ui_events.stop()
            dock.deactivate()
            if exit_message:
                ui.print(exit_message)

    async def _run_once(self, user_text: str) -> None:
        t_turn_start = time.monotonic()
        user_message_id: int | None = None
        try:
            session_tracker.begin_turn(self._workspace)
            payload = build_user_message_payload(user_text, self._workspace)
            self._current_tree = dock.tree
            if dock.active and ui_events.is_running:
                self._turn_node = await ui_events.request(TurnStarted(text=payload.display_text))
                await ui_events.emit(StatusUpdated(
                    status_id="turn:analyzing",
                    label="Analyzing",
                    detail="loading session and preparing context",
                    stage="analyzing",
                ))
            else:
                self._turn_node = dock.start_turn(payload.display_text)
            session_msgs = await load_messages(self._session.id) if self._session else []
            # Safety: if session is huge, only load recent messages
            if len(session_msgs) > 500:
                ui.warn(f"Session has {len(session_msgs)} messages — loading last 200")
                session_msgs = session_msgs[-200:]

            msgs = []
            for row in session_msgs:
                if row.role == "system":
                    msgs.append(SystemMessage(content=row.content, id=str(row.id) if row.id is not None else None))
                elif row.role == "user":
                    msgs.append(HumanMessage(
                        content=parse_structured_content(row.content, row.content_format),
                        id=str(row.id) if row.id is not None else None,
                    ))
                elif row.role == "assistant":
                    content = parse_structured_content(row.content, row.content_format)
                    msgs.append(AIMessage(
                        content=content,
                        tool_calls=row.tool_calls or [],
                        id=str(row.id) if row.id is not None else None,
                    ))
                elif row.role == "tool":
                    msgs.append(ToolMessage(
                        content=row.content,
                        tool_call_id=row.tool_call_id or "",
                        id=str(row.id) if row.id is not None else None,
                    ))

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
            implementation_allowed = intent_resolution.implementation_allowed
            self._current_implementation_allowed = implementation_allowed
            goal_scope = (
                task_run.goal
                if interaction_mode == "goal" and task_run is not None and task_run.goal
                else payload.title_text
            )

            saved_user_content, user_content_format = serialize_message_content(payload.content)
            user_message_id = await save_message(MessageRow(
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
                "implementation_allowed": implementation_allowed,
                "intent_resolution_reason": intent_resolution.reason,
                "awaiting_implementation_approval": intent_resolution.awaiting_implementation_approval,
                "approved_scope": intent_resolution.approved_scope,
                "goal": task_run.goal if task_run is not None else "",
                "goal_phase": task_run.phase.value if task_run is not None else "",
                "goal_status": task_run.status.value if task_run is not None else "",
                "goal_turn_count": task_run.turn_count if task_run is not None else 0,
                "user_message_id": user_message_id,
            }

            # ── compaction: check overflow before running ──────────────────
            head, tail_id = await self._maybe_compact(msgs, session_msgs)
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))

            final = await self.graph.ainvoke(initial, {"recursion_limit": self.config.agent.recursion_limit})
            if self.model is not None and hasattr(self, "_task_state"):
                self._task_state.update_after_turn(
                    intent_resolution,
                    payload.title_text,
                    scope_text=goal_scope,
                )
            if self.model is not None and interaction_mode == "goal" and task_run is not None:
                task_run.update_after_turn(
                    intent_resolution,
                    payload.title_text,
                    scope_text=goal_scope,
                )
            await save_message_runtime_snapshot(MessageRuntimeSnapshot(
                message_id=user_message_id,
                session_id=self._session.id,
                interaction_mode=interaction_mode,
                task_intent=task_intent,
                implementation_allowed=implementation_allowed,
                intent_resolution_reason=intent_resolution.reason,
                goal=task_run.goal if task_run is not None else "",
                goal_phase=task_run.phase.value if task_run is not None else "",
                goal_status=task_run.status.value if task_run is not None else "",
                goal_turn_count=task_run.turn_count if task_run is not None else 0,
                awaiting_implementation_approval=(
                    task_run.awaiting_implementation_approval
                    if interaction_mode == "goal" and task_run is not None
                    else getattr(
                        getattr(self, "_task_state", None),
                        "awaiting_implementation_approval",
                        False,
                    )
                ),
                approved_scope=(
                    task_run.approved_scope
                    if interaction_mode == "goal" and task_run is not None
                    else getattr(getattr(self, "_task_state", None), "approved_scope", "")
                ),
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
                        await save_message(MessageRow(
                            session_id=self._session.id,
                            role="assistant",
                            content=saved,
                            content_format=fmt,
                            tool_calls=msg.tool_calls if msg.tool_calls else None,
                            created_at=_now(),
                        ))
                    elif isinstance(msg, ToolMessage):
                        await save_message(MessageRow(
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
            if self._debug:
                ui.print(f"[dim]✻  Churned for {elapsed:.0f}s[/dim]")
        except (KeyboardInterrupt, asyncio.CancelledError):
            if self._session is not None and user_message_id is not None:
                await delete_messages_from(self._session.id, user_message_id)
            raise
        finally:
            session_tracker.finish_turn()
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))
                await ui_events.emit(StatusFinished(status_id="agent:-1:progress"))
                await ui_events.emit(StatusFinished(status_id="compaction"))
                await ui_events.emit(InputSet(text="", hints=[]))
                await ui_events.drain()
            else:
                dock.set_input("", [])
            self._current_implementation_allowed = True

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._slash.dispatch(inp)

    async def _restore_runtime_state(self) -> None:
        if self._session is None:
            return
        snapshot = await load_runtime_state(self._session.id)
        self._interaction_mode = snapshot.interaction_mode
        self._task_state = snapshot.task_state
        self._task_run = snapshot.task_run
        self._compaction_summary = snapshot.compaction_summary

    async def _persist_runtime_state(self) -> None:
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

    async def _clear_runtime_state(self) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskRun, TaskState

        if self._session is not None:
            await clear_runtime_state(self._session.id)
        self._interaction_mode = InteractionMode.AUTO
        self._task_state = TaskState()
        self._task_run = TaskRun()
        self._compaction_summary = ""
        self._pending_summary = None

    async def _persist_transcript_snapshot(self) -> None:
        if self._session is None:
            return
        active_dock = get_dock()
        if active_dock is None:
            return
        rows, turn_count = tree_to_transcript_rows(self._session.id, active_dock.tree)
        await replace_transcript(self._session.id, rows, turn_count=turn_count)

    async def _restore_transcript_snapshot(self, *, append: bool = False) -> bool:
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
