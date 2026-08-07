"""Terminal and web run-loop presentation lifecycle."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.agent.domain.task.state import goal_label
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.llm.service import get_context_limit
from voidx.runtime.ui import (
    COMMANDS,
    CompositeEventConsumer,
    DockEventConsumer,
    GatewayEventConsumer,
    GatewayHeadlessFrontend,
    GatewayServer,
    McpServerStatus,
    UiStatus,
    create_frontend,
    emit_web_gateway_bootstrap,
)
from voidx.agent.application.agent_service import RunLoopStartupError
from voidx.presentation.gateway.command_handler import GatewayCommandHandler
from voidx.presentation.gateway.session_adapter import build_gateway_session
from voidx.presentation.terminal.startup import StartupPresenter
from voidx.presentation.transcript_adapter import TranscriptSnapshotAdapter


class TerminalRunLoop:
    """Own the concrete terminal/web UI lifecycle for an agent service."""

    def __init__(
        self,
        execution: Any,
        service: Any,
        *,
        settings_factory: Any = None,
    ) -> None:
        self._execution = execution
        self._service = service
        self._startup = StartupPresenter(execution)
        self._settings_factory = settings_factory
        execution.bind_startup_presenter(self._startup.show)
        self._command_handler = GatewayCommandHandler(execution, service)
        execution.bind_presentation_snapshots(TranscriptSnapshotAdapter(execution.ui))

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
        self._execution.any_messages_sent = False
        self._execution.ui.session_tracker.clear()

        title = self._startup.title()

        self._execution.ui.dock.begin_capture()
        active_dock = self._execution.ui.get_dock()
        gateway_session = None
        gateway_server = None
        lsp_startup_tasks: list[asyncio.Task] = []
        update_check_task: asyncio.Task[None] | None = None
        if active_dock is not None:
            consumer = DockEventConsumer(active_dock)
            if web:
                gateway_session = build_gateway_session(
                    self._execution,
                    active_dock,
                    settings_factory=self._settings_factory,
                )
                self._execution.ui.events.start(CompositeEventConsumer(
                    primary=consumer,
                    mirrors=[GatewayEventConsumer(gateway_session)],
                ))
            else:
                self._execution.ui.events.start(consumer)
        await self._execution.restore_runtime_state()
        await self._startup.show(append_transcript=True)

        exit_message: str | None = None

        async def cleanup_run_loop() -> None:
            await self._execution.delete_empty_current_session()
            gateway = getattr(self._execution, "agent_gateway", None)
            if gateway is not None and hasattr(gateway, "close_all"):
                try:
                    await gateway.close_all()
                except Exception:
                    pass
            if gateway_server is not None:
                await gateway_server.stop()
            if update_check_task is not None:
                update_check_task.cancel()
                await asyncio.gather(update_check_task, return_exceptions=True)
            if self._execution.mcp_manager is not None:
                await self._execution.mcp_manager.stop_all()
            for task in lsp_startup_tasks:
                task.cancel()
            if lsp_startup_tasks:
                await asyncio.gather(*lsp_startup_tasks, return_exceptions=True)
            if self._execution.lsp_manager is not None:
                await self._execution.lsp_manager.stop_all()
            if self._execution.ui.events.is_running:
                await self._execution.ui.events.stop()
            self._execution.ui.dock.deactivate()

        status = UiStatus(
            provider=self._execution.config.model.provider,
            model=self._execution.config.model.model,
            workspace=self._execution.workspace,
            session_title=title,
            context_limit=get_context_limit(self._execution.config.model.provider, self._execution.config.model.protocol or "", self._execution.config.model.context_window),
            reasoning_effort=(
                self._execution.config.model.reasoning_effort.value
                if self._execution.config.model.reasoning_effort is not None
                else "xhigh"
            ),
            permission_label=lambda: self._execution.permission.permission_mode_label(),
            usage_stats=self._execution.usage_stats,
            debug=lambda: self._execution.debug_enabled,
            plan_mode=lambda: self._execution.plan_mode,
            interaction_mode=lambda: self._execution.interaction_mode.value,
            goal_label=lambda: goal_label(self._execution.task_state.current_goal),
            active_workflows=lambda: active_workflow_names(self._execution.task_state),
            mcp_servers=lambda: [
                McpServerStatus(
                    name=s.name,
                    status=s.status,
                    tool_count=s.tool_count,
                )
                for s in (
                    self._execution.mcp_manager.statuses()
                    if self._execution.mcp_manager is not None
                    else []
                )
            ] if self._execution.settings is not None else [],
            mcp_config_path=str(self._execution.settings.path) if self._execution.settings is not None else "",
            code_ide=lambda: (
                self._execution.settings.get_code_ide().value
                if self._execution.settings is not None
                else "trae"
            ),
            latest_action=lambda: getattr(
                getattr(self._execution.runtime_guards, "wall_clock", None),
                "latest_action",
                "",
            ),
            runtime_profile=lambda: getattr(self._execution.session, "runtime_profile", "coding") if self._execution.session else "coding",
            session_id=lambda: self._execution.session_id,
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
                self._execution.ui.dock.append_message(
                    "[dim]voidx_cli not installed — starting Web UI in headless mode. "
                    "Install voidx-cli for terminal UI: pip install voidx-cli[/dim]",
                    markup=True,
                )
                app = GatewayHeadlessFrontend(status, COMMANDS)
        self._execution.app = app
        app.set_external_command_handler(partial(self._command_handler.handle, app))
        if hasattr(app, "set_mcp_catalog_provider"):
            app.set_mcp_catalog_provider(
                lambda: (
                    self._execution.mcp_manager.catalog_snapshot()
                    if self._execution.mcp_manager is not None
                    else []
                )
            )
        update_check_task = asyncio.create_task(self._startup.show_update_check_if_needed())

        self._execution.gateway_session = gateway_session
        if gateway_session is not None:
            gateway_session.set_command_handler(partial(self._command_handler.handle, app))
            gateway_session.set_thread_id_provider(
                lambda: self._execution.session.id if self._execution.session else ""
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
                self._execution.ui.dock.append_message(f"Web UI gateway: {gateway_server.url}")

        async def show_lsp_startup() -> None:
            manager = self._execution.lsp_manager
            if manager is None:
                return
            try:
                await manager.initialize()
                checks = manager.doctor()
                lsp_lines = []
                for check in checks:
                    if check.available and check.enabled:
                        source = f" [dim][{check.detected_source}][/dim]" if check.detected_source else ""
                        suffix = " [dim](warming...)[/dim]" if hasattr(manager, "warm_up") else ""
                        lsp_lines.append(
                            f"  [cyan]{check.language}[/cyan] [dim]→[/dim] "
                            f"{check.resolved_path}{source}{suffix}"
                        )
                if lsp_lines:
                    self._execution.ui.dock.append_message("\n".join(lsp_lines), markup=True)
                warm_up = getattr(manager, "warm_up", None)
                if callable(warm_up):
                    results = await warm_up()
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
                        self._execution.ui.dock.append_message("\n".join(warmup_lines), markup=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._execution.ui.dock.append_message(f"[dim]LSP setup failed: {exc}[/dim]", markup=True)

        if self._execution.lsp_manager is not None:
            lsp_startup_tasks.append(asyncio.create_task(show_lsp_startup()))

        if self._execution.mcp_manager is not None:
            servers = self._execution.settings.list_mcp_servers() if self._execution.settings else []
            enabled = [s for s in servers if not s.disabled]
            if enabled:
                names = ", ".join(s.name for s in enabled)
                self._execution.ui.dock.append_message(f"[dim]MCP connecting: {names}…[/dim]", markup=True)
            await self._execution.mcp_manager.start_all()

        async def handle_user_input(
            user_input: str,
            *,
            context: TurnExecutionContext | None = None,
            thread_id: str = "",
        ) -> bool:
            nonlocal exit_message
            if context is None:
                session_id = self._execution.session_id or ""
                resolved_thread_id = thread_id or session_id or "coding"
                context = TurnExecutionContext(
                    thread_id=resolved_thread_id,
                    session_id=session_id,
                    runtime_profile=CODING_PROFILE,
                    workspace=self._execution.workspace,
                )
            keep_running, next_exit_message = await self._service._handle_user_input(
                app,
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
                self._execution.ui.ui.print(exit_message)
