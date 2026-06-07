"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from voidx.agent.graph.runtime import ui
from voidx.agent.graph.session_mixin import GraphSessionMixin
from voidx.agent.graph.transcript_mixin import GraphTranscriptMixin
from voidx.agent.graph.turn_mixin import GraphTurnMixin
from voidx.llm.provider import get_context_limit
from voidx.runtime.ui import (
    COMMANDS,
    CompositeEventConsumer,
    DockEventConsumer,
    GatewayEventConsumer,
    GatewayServer,
    GatewaySession,
    McpServerStatus,
    PureTui,
    StartupShown,
    UiStatus,
    dock,
    emit_web_gateway_bootstrap,
    get_dock,
    session_tracker,
    show_startup,
    ui_command_kind,
    ui_events,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphRunLoopMixin(GraphTurnMixin, GraphSessionMixin, GraphTranscriptMixin):
    async def _show_startup(
        self: GraphRunLoopHost,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None:
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
        startup_via_event = active_dock is not None and ui_events.is_running and not prefer_direct
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
        self._any_messages_sent = False
        session_tracker.clear()

        title = self._startup_title()

        dock.begin_capture()
        active_dock = get_dock()
        gateway_session: GatewaySession | None = None
        gateway_server: GatewayServer | None = None
        lsp_startup_tasks: list[asyncio.Task] = []
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
        app.set_external_command_handler(partial(self._handle_web_command, app))

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

        async def show_lsp_startup() -> None:
            manager = getattr(self, "_lsp_manager", None)
            if manager is None:
                return
            try:
                await manager.initialize()
                lsp_lines = []
                for check in manager.doctor():
                    if check.available and check.enabled:
                        source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                        lsp_lines.append(f"  [cyan]{check.language}[/cyan] [dim]→[/dim] {check.resolved_path}{source}")
                if lsp_lines:
                    dock.append_message("\n".join(lsp_lines), markup=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dock.append_message(f"[dim]LSP setup failed: {exc}[/dim]", markup=True)

        if hasattr(self, '_lsp_manager'):
            lsp_startup_tasks.append(asyncio.create_task(show_lsp_startup()))

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
            empty_session_cleanup = getattr(self, "_delete_empty_current_session", None)
            if callable(empty_session_cleanup):
                await empty_session_cleanup()
            if gateway_server is not None:
                await gateway_server.stop()
            if hasattr(self, '_mcp_manager'):
                await self._mcp_manager.stop_all()
            for task in lsp_startup_tasks:
                task.cancel()
            if lsp_startup_tasks:
                await asyncio.gather(*lsp_startup_tasks, return_exceptions=True)
            if hasattr(self, '_lsp_manager'):
                await self._lsp_manager.stop_all()
            if ui_events.is_running:
                await ui_events.stop()
            dock.deactivate()
            if exit_message:
                ui.print(exit_message)

    async def _handle_web_command(self: GraphRunLoopHost, app: Any, command: Any) -> None:
        if isinstance(command, dict) and command.get("kind") == "guide":
            self.submit_guidance(str(command.get("text", "")))
            return
        kind = ui_command_kind(command)
        if kind == "submit":
            text = command.text
            if text.strip().startswith("/guide "):
                self.submit_guidance(text.strip().removeprefix("/guide").strip())
            else:
                app.submit_external_input(text)
        elif kind == "cancel":
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

    async def _dispatch_slash(self: GraphRunLoopHost, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._slash.dispatch(inp)
