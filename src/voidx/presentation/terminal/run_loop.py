"""Terminal and web run-loop presentation lifecycle."""

from __future__ import annotations

import asyncio
from functools import partial
from collections.abc import Awaitable, Callable

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.agent.domain.task.state import goal_label
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.ports.presentation import (
    GuidancePort,
    InteractiveInputPort,
    PresentationFrontendBinding,
    PresentationIntegrationLifecycle,
    RuntimeStatusReader,
    SessionLifecycle,
)
from voidx.agent.ports.ui import AgentUiPort
from voidx.agent.ports.workspace_lock import WorkspaceWriteLockBinder
from voidx.llm.service import get_context_limit
from voidx.presentation.commands import COMMANDS
from voidx.presentation.output.events import CompositeEventConsumer, DockEventConsumer
from voidx.presentation.output.types import McpServerStatus, UiStatus
from voidx.presentation.output.dock import reset_dock, set_dock
from voidx.presentation.gateway import GatewayEventConsumer, GatewayHeadlessFrontend, GatewayServer
from voidx.presentation.gateway.bootstrap import emit_web_gateway_bootstrap
from voidx.presentation.terminal.frontend_factory import create_frontend
from voidx.agent.application.agent_service import RunLoopStartupError
from voidx.presentation.gateway.command_handler import GatewayCommandHandler
from voidx.presentation.gateway.session_adapter import build_gateway_session
from voidx.presentation.gateway.thread_registry import GatewayThreadRegistryAdapter
from voidx.presentation.terminal.startup import StartupPresenter
from voidx.presentation.adapters.persistence.transcript_adapter import TranscriptSnapshotAdapter


class TerminalRunLoop:
    """Own the concrete terminal/web UI lifecycle for an agent service."""

    def __init__(
        self,
        status_reader: RuntimeStatusReader,
        sessions: SessionLifecycle,
        integrations: PresentationIntegrationLifecycle,
        frontend_binding: PresentationFrontendBinding,
        input_port: InteractiveInputPort,
        guidance: GuidancePort,
        workspace_write_lock: WorkspaceWriteLockBinder,
        ui: AgentUiPort,
        *,
        settings_factory: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._status_reader = status_reader
        self._sessions = sessions
        self._integrations = integrations
        self._frontend_binding = frontend_binding
        self._input_port = input_port
        self._workspace_write_lock = workspace_write_lock
        self._ui = ui
        self._startup = StartupPresenter(
            status_reader,
            ui,
            restore_snapshot=frontend_binding.restore_transcript_snapshot,
            update_check_due=frontend_binding.update_check_due,
            mark_update_check=frontend_binding.mark_update_check,
        )
        frontend_binding.bind_startup_presenter(self._startup.show)
        self._settings_factory = settings_factory
        self._gateway_session = None
        self._thread_registry = GatewayThreadRegistryAdapter(lambda: self._gateway_session)
        self._command_handler = GatewayCommandHandler(status_reader, guidance, self._thread_registry)


    async def run(
        self,
        *,
        web: bool = False,
        web_headless: bool = False,
        web_host: str = "127.0.0.1",
        web_port: int = 0,
        web_token: str = "",
    ) -> None:
        """Interactive REPL with orchestrator agent."""
        self._frontend_binding.reset_run_state()
        self._ui.session_tracker.clear()

        title = self._startup.title()

        self._ui.dock.begin_capture()
        active_dock = self._ui.get_dock()
        dock_token = set_dock(active_dock)
        gateway_session = None
        gateway_server = None
        lsp_startup_tasks: list[asyncio.Task] = []
        update_check_task: asyncio.Task[None] | None = None
        if active_dock is not None:
            consumer = DockEventConsumer(active_dock)
            if web:
                gateway_session = build_gateway_session(
                    self._status_reader,
                    active_dock,
                    settings_factory=self._settings_factory,
                    settings_update_handler=self._frontend_binding.apply_settings_update,
                    usage_stats_provider=self._frontend_binding.usage_stats,
                    mcp_catalog_provider=self._integrations.mcp_catalog,
                )
                self._workspace_write_lock.bind(gateway_session.workspace_write_lock)
                self._ui.events.start(CompositeEventConsumer(
                    primary=consumer,
                    mirrors=[GatewayEventConsumer(gateway_session)],
                ))
            else:
                self._ui.events.start(consumer)
        await self._sessions.restore_runtime_state()
        await self._startup.show(append_transcript=True)

        exit_message: str | None = None

        async def cleanup_run_loop() -> None:
            await self._sessions.delete_empty_current_session()
            try:
                await self._integrations.close_agent_gateway()
            except Exception:
                pass
            if gateway_server is not None:
                await gateway_server.stop()
            if update_check_task is not None:
                update_check_task.cancel()
                await asyncio.gather(update_check_task, return_exceptions=True)
            for task in lsp_startup_tasks:
                task.cancel()
            if lsp_startup_tasks:
                await asyncio.gather(*lsp_startup_tasks, return_exceptions=True)
            await self._integrations.stop_integrations()
            if self._ui.events.is_running:
                await self._ui.events.stop()
            self._workspace_write_lock.bind(None)
            self._ui.dock.deactivate()
            reset_dock(dock_token)

        initial_status = self._status_reader.runtime_status()
        status = UiStatus(
            provider=initial_status.provider,
            model=initial_status.model,
            workspace=initial_status.workspace,
            session_title=title,
            context_limit=get_context_limit(
                initial_status.provider,
                initial_status.protocol,
                initial_status.context_window,
            ),
            reasoning_effort=initial_status.reasoning_effort,
            permission_label=lambda: self._status_reader.runtime_status().permission_label,
            usage_stats=self._frontend_binding.usage_stats(),
            debug=lambda: self._status_reader.runtime_status().debug,
            plan_mode=lambda: self._status_reader.runtime_status().plan_mode,
            interaction_mode=lambda: self._status_reader.runtime_status().interaction_mode,
            goal_label=lambda: self._status_reader.runtime_status().goal_label,
            active_workflows=lambda: list(self._status_reader.runtime_status().active_workflows),
            mcp_servers=lambda: [
                McpServerStatus(name=item.name, status=item.status, tool_count=item.tool_count)
                for item in self._integrations.mcp_statuses()
            ],
            mcp_config_path=initial_status.mcp_config_path,
            code_ide=lambda: self._status_reader.runtime_status().code_ide,
            latest_action=lambda: self._status_reader.runtime_status().latest_action,
            runtime_profile=lambda: self._status_reader.runtime_status().session.runtime_profile,
            session_id=lambda: self._status_reader.runtime_status().session.session_id,
        )

        if web_headless:
            app = GatewayHeadlessFrontend(status, COMMANDS)
        else:
            try:
                app = create_frontend(status, COMMANDS)
            except RuntimeError as exc:
                if not web:
                    await cleanup_run_loop()
                    raise RunLoopStartupError(f"Cannot start terminal UI: {exc}") from exc
                self._ui.dock.append_message(
                    "[dim]voidx_cli not installed — starting Web UI in headless mode. "
                    "Install voidx-cli for terminal UI: pip install voidx-cli[/dim]",
                    markup=True,
                )
                app = GatewayHeadlessFrontend(status, COMMANDS)
        self._ui.bind_frontend(app)
        self._frontend_binding.bind_input_frontend(app)
        app.set_external_command_handler(partial(self._command_handler.handle, app))
        if hasattr(app, "set_mcp_catalog_provider"):
            app.set_mcp_catalog_provider(
                self._integrations.mcp_catalog
            )
        update_check_task = asyncio.create_task(self._startup.show_update_check_if_needed())

        self._gateway_session = gateway_session
        if gateway_session is not None:
            gateway_session.set_command_handler(partial(self._command_handler.handle, app))
            gateway_session.set_thread_id_provider(
                lambda: self._status_reader.runtime_status().session.session_id
            )
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
                self._ui.dock.append_message(f"Web UI gateway: {gateway_server.url}")

        async def show_lsp_startup() -> None:
            checks = await self._integrations.initialize_lsp()
            try:
                lsp_lines = []
                for check in checks:
                    if check.available and check.enabled:
                        source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                        suffix = " [dim](warming...)[/dim]"
                        lsp_lines.append(
                            f"  [cyan]{check.language}[/cyan] [dim]→[/dim] "
                            f"{check.resolved_path}{source}{suffix}"
                        )
                if lsp_lines:
                    self._ui.dock.append_message("\n".join(lsp_lines), markup=True)
                results = await self._integrations.warm_up_lsp()
                if results:
                    warmup_lines = []
                    for check in checks:
                        if not check.available or not check.enabled or check.language not in results:
                            continue
                        source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                        result = results[check.language]
                        if result == "ok":
                            suffix = " [green]ready[/green]"
                        else:
                            detail = result.removeprefix("error: ").strip()
                            suffix = f" [red]failed[/red] [dim]{detail}[/dim]"
                        warmup_lines.append(
                            f"  [cyan]{check.language}[/cyan] [dim]→[/dim] "
                            f"{check.resolved_path}{source}{suffix}"
                        )
                    if warmup_lines:
                        self._ui.dock.append_message("\n".join(warmup_lines), markup=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ui.dock.append_message(f"[dim]LSP setup failed: {exc}[/dim]", markup=True)

        if self._integrations.has_lsp():
            lsp_startup_tasks.append(asyncio.create_task(show_lsp_startup()))

        if self._integrations.has_mcp():
            enabled_names = self._integrations.enabled_mcp_names()
            if enabled_names:
                names = ", ".join(enabled_names)
                self._ui.dock.append_message(f"[dim]MCP connecting: {names}…[/dim]", markup=True)
            await self._integrations.start_mcp()

        async def handle_user_input(
            user_input: str,
            *,
            context: TurnExecutionContext | None = None,
            thread_id: str = "",
        ) -> bool:
            nonlocal exit_message
            if context is None:
                session_id = self._status_reader.runtime_status().session.session_id
                resolved_thread_id = thread_id or session_id or "coding"
                context = TurnExecutionContext(
                    thread_id=resolved_thread_id,
                    session_id=session_id,
                    runtime_profile=CODING_PROFILE,
                    workspace=self._status_reader.runtime_status().workspace,
                )
            keep_running, next_exit_message = await self._input_port.dispatch_input(
                user_input,
                context=context,
                thread_id=thread_id,
            )
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
            await cleanup_run_loop()
            if exit_message:
                self._ui.ui.print(exit_message)
