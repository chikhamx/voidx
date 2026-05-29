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
from voidx.agent.graph_parts.runtime import ui
from voidx.agent.state import AgentState
from voidx.llm.provider import get_context_limit
from voidx.memory.session import (
    MessageRow,
    create_session,
    load_messages,
    save_message,
    touch_session,
    update_title,
    _now,
)
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
from voidx.ui.startup import show_startup


class GraphRunLoopMixin:
    async def run(self) -> None:
        """Interactive REPL with orchestrator agent."""
        from voidx.ui.app import McpServerStatus, PromptToolkitTui, UiStatus

        is_new = self._session is None

        self._any_messages_sent = False

        title = self._session.title if self._session else "New session"
        if len(title) > 60:
            title = title[:57] + "..."

        dock.begin_capture()
        active_dock = get_dock()
        if active_dock is not None:
            ui_events.start(DockEventConsumer(active_dock))
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
            await ui_events.emit(startup_event)
        else:
            show_startup(
                console=ui,
                model=self.config.model.model,
                provider=self.config.model.provider,
                workspace=self._workspace,
                session_title=title,
                is_new=is_new,
            )

        if not startup_via_event:
            ui.print(f"[dim]  ? for shortcuts · ← for agents[/dim]")

        if self.model is None and not startup_via_event:
            ui.print()
            ui.print("[yellow]No profile configured — chat is disabled until you set one up.[/yellow]")
            ui.print(f"[dim]  Use [cyan]/model config[/cyan] to create a profile interactively[/dim]")
            ui.print()

        exit_message: str | None = None

        app = PromptToolkitTui(
            UiStatus(
                provider=self.config.model.provider,
                model=self.config.model.model,
                workspace=self._workspace,
                session_title=title,
                context_limit=get_context_limit(self.config.model.provider),
                debug=lambda: self._debug,
                plan_mode=lambda: self._plan_mode,
                mcp_servers=lambda: [
                    McpServerStatus(
                        name=server.name,
                        status="disabled" if server.disabled else "configured",
                        tool_count=server.tool_count,
                    )
                    for server in self._settings.list_mcp_servers()
                ] if self._settings is not None else [],
                mcp_config_path=str(self._settings.path) if self._settings is not None else "",
            ),
            COMMANDS,
        )
        self._app = app

        async def handle_user_input(user_input: str) -> bool:
            nonlocal exit_message
            user_input = user_input.strip()
            if not user_input:
                return True

            if user_input.startswith("/"):
                if user_input in ("/exit", "/quit"):
                    exit_message = "\n[dim]bye.[/dim]"
                    return False
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
            if ui_events.is_running:
                await ui_events.stop()
            dock.deactivate()
            if exit_message:
                ui.print(exit_message)

    async def _run_once(self, user_text: str) -> None:
        t_turn_start = time.monotonic()
        try:
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
                    msgs.append(SystemMessage(content=row.content))
                elif row.role == "user":
                    msgs.append(HumanMessage(content=parse_structured_content(row.content, row.content_format)))
                elif row.role == "assistant":
                    content = parse_structured_content(row.content, row.content_format)
                    msgs.append(AIMessage(content=content, tool_calls=row.tool_calls or []))
                elif row.role == "tool":
                    msgs.append(ToolMessage(content=row.content, tool_call_id=row.tool_call_id or ""))

            for warning in payload.warnings:
                ui.warn(warning)

            turn_msg = HumanMessage(content=payload.content, id=f"user_{time.time_ns()}")
            msgs.append(turn_msg)
            if self._session is None:
                self._session = await create_session(workspace=self._workspace)
            saved_user_content, user_content_format = serialize_message_content(payload.content)
            await save_message(MessageRow(
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
            }

            # ── compaction: check overflow before running ──────────────────
            head, tail_id = await self._maybe_compact(msgs, session_msgs)
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))

            final = await self.graph.ainvoke(initial, {"recursion_limit": self.config.agent.recursion_limit})

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

            elapsed = time.monotonic() - t_turn_start
            if self._debug:
                ui.print(f"[dim]✻  Churned for {elapsed:.0f}s[/dim]")
        finally:
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(status_id="turn:analyzing"))
                await ui_events.emit(StatusFinished(status_id="agent:-1:progress"))
                await ui_events.emit(StatusFinished(status_id="compaction"))
                await ui_events.emit(InputSet(text="", hints=[]))
                await ui_events.drain()
            else:
                dock.set_input("", [])

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._slash.dispatch(inp)
