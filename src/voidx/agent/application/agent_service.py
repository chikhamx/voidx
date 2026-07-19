"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from voidx.llm.service import get_context_limit
from voidx.runtime.intent import InteractionMode
from voidx.runtime.ui import (
    COMMANDS,
    CompositeEventConsumer,
    DockEventConsumer,
    GatewayEventConsumer,
    GatewayHeadlessFrontend,
    GatewayServer,
    GatewaySession,
    McpServerStatus,
    StartupShown,
    UiStatus,
    create_frontend,
    emit_web_gateway_bootstrap,
)
from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.runtime.task_state import goal_label
from voidx.logging.tool_log import log_tool_event
from voidx.runtime.ui import ThreadExecutionContext
from voidx.agent.ports.execution_host import ExecutionHost


AgentExecution = ExecutionHost

logger = logging.getLogger(__name__)


class RunLoopStartupError(RuntimeError):
    """Raised when the run loop cannot start the selected frontend."""


def _ui_command_kind(command: Any) -> str:
    return str(getattr(command, "kind", "") or "")


class AgentService:
    """Application-level startup and interactive run-loop service."""

    def __init__(self, execution: AgentExecution, turns) -> None:
        self._execution = execution
        self._turns = turns
        bind_startup = getattr(execution, "bind_startup_presenter", None)
        if bind_startup is not None:
            bind_startup(self._show_startup)

    async def _show_startup(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None:
        is_new = self._execution.session is None
        title = self._startup_title()
        active_dock = self._execution.ui.get_dock()
        startup_event = StartupShown(
            model=self._execution.config.model.model,
            provider=self._execution.config.model.provider,
            workspace=self._execution.workspace,
            session_title=title,
            is_new=is_new,
            profile_configured=self._execution.model is not None,
        )
        startup_via_event = active_dock is not None and self._execution.ui.events.is_running and not prefer_direct
        if startup_via_event:
            await self._execution.ui.events.request(startup_event)
            if append_transcript:
                await self._execution.restore_transcript_snapshot(append=True)
            return

        if active_dock is not None and active_dock.active:
            active_dock.append_startup(
                model=self._execution.config.model.model,
                provider=self._execution.config.model.provider,
                workspace=self._execution.workspace,
                session_title=title,
                is_new=is_new,
                profile_configured=self._execution.model is not None,
            )
            if append_transcript:
                await self._execution.restore_transcript_snapshot(append=True)
            return

        self._execution.ui.show_startup(
            console=self._execution.ui.ui,
            model=self._execution.config.model.model,
            provider=self._execution.config.model.provider,
            workspace=self._execution.workspace,
            session_title=title,
            is_new=is_new,
        )
        if self._execution.model is None:
            self._execution.ui.ui.print()
            self._execution.ui.ui.print("[yellow]No profile configured — chat is disabled until you set one up.[/yellow]")
            self._execution.ui.ui.print(f"[dim]  Use [cyan]/model new[/cyan] to create a profile interactively[/dim]")
            self._execution.ui.ui.print()

    def _startup_title(self) -> str:
        title = self._execution.session.title if self._execution.session else "New session"
        if len(title) > 60:
            title = title[:57] + "..."
        return title

    async def _show_update_check_if_needed(self) -> None:
        settings = self._execution.settings
        if settings is None:
            return
        try:
            update_check_due = getattr(settings, "update_check_due", None)
            if not callable(update_check_due) or not update_check_due():
                return

            from voidx.selfupdate import check_for_update, upgrade_hint

            result = await check_for_update()
            mark_update_check = getattr(settings, "mark_update_check", None)
            if callable(mark_update_check):
                mark_update_check(result.latest_version)
            if result.update_available and result.latest_version:
                self._execution.ui.dock.append_message(
                    "[yellow]Update available:[/yellow] "
                    f"voidx {result.current_version} -> {result.latest_version}. "
                    f"[dim]{upgrade_hint()}[/dim]",
                    markup=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_tool_event("startup_update_check_failed", message=f"Startup update check failed: {exc}")

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

        title = self._startup_title()

        self._execution.ui.dock.begin_capture()
        active_dock = self._execution.ui.get_dock()
        gateway_session: GatewaySession | None = None
        gateway_server: GatewayServer | None = None
        lsp_startup_tasks: list[asyncio.Task] = []
        update_check_task: asyncio.Task[None] | None = None
        if active_dock is not None:
            consumer = DockEventConsumer(active_dock)
            if web:
                gateway_session = GatewaySession(
                    lambda: active_dock.tree,
                    thread_id=self._execution.session.id if self._execution.session else "",
                    session_id=self._execution.session.id if self._execution.session else "",
                    workspace=self._execution.workspace,
                    runtime_state_provider=lambda: {
                        "provider": self._execution.config.model.provider,
                        "model": self._execution.config.model.model,
                        "workspace": self._execution.workspace,
                        "profile_configured": self._execution.model is not None,
                        "permission_mode": getattr(self._execution.permission, "permission_mode", ""),
                        "ai_approval_count": getattr(self._execution.permission, "ai_approval_count", 0),
                    },
                    settings_update_handler=self._execution.apply_settings_update,
                )
                self._execution.ui.events.start(CompositeEventConsumer(
                    primary=consumer,
                    mirrors=[GatewayEventConsumer(gateway_session)],
                ))
            else:
                self._execution.ui.events.start(consumer)
        await self._execution.restore_runtime_state()
        await self._show_startup(append_transcript=True)

        exit_message: str | None = None

        async def cleanup_run_loop() -> None:
            await self._execution.delete_empty_current_session()
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
            reasoning_effort=self._execution.config.model.reasoning_effort or "xhigh",
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
        app.set_external_command_handler(partial(self._handle_web_command, app))
        update_check_task = asyncio.create_task(self._show_update_check_if_needed())

        self._execution.gateway_session = gateway_session
        if gateway_session is not None:
            gateway_session.set_command_handler(partial(self._handle_web_command, app))
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
            context: ThreadExecutionContext | None = None,
            thread_id: str = "",
        ) -> bool:
            nonlocal exit_message
            context = context or ThreadExecutionContext(thread_id=thread_id, session_id=thread_id)
            keep_running, next_exit_message = await self._handle_user_input(
                app,
                user_input,
                context=context,
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

    async def _handle_web_command(self, app: Any, command: Any) -> None:
        if isinstance(command, dict) and command.get("kind") == "guide":
            self.submit_guidance(str(command.get("text", "")), source="user")
            return
        kind = _ui_command_kind(command)
        if kind == "submit":
            text = command.text
            if text.strip().startswith("/guide "):
                self.submit_guidance(
                    text.strip().removeprefix("/guide").strip(),
                    source="user",
                )
            else:
                self._ensure_gateway_thread()
                thread_id = str(getattr(command, "thread_id", "") or "")
                context = ThreadExecutionContext(thread_id=thread_id, session_id=thread_id)
                app.submit_external_input(text, context=context)
        elif kind == "cancel":
            thread_id = str(getattr(command, "thread_id", "") or "")
            context = ThreadExecutionContext(thread_id=thread_id, session_id=thread_id)
            app.cancel_external_input(context=context)

    def _ensure_gateway_thread(self) -> None:
        """Register the active session as a gateway thread if not yet registered.

        self._execution.session is None when GatewaySession is constructed (session is
        created lazily on first turn). This ensures the thread/adapter exist
        before events start flowing.
        """
        gs = self._execution.gateway_session
        if gs is None or self._execution.session is None:
            return
        tid = self._execution.session.id
        if tid and tid not in gs._threads:
            asyncio.ensure_future(gs.register_thread(tid, title=self._execution.session.title or "", directory=getattr(self._execution.session, "directory", "") or ""))

    async def _handle_user_input(
        self,
        app,
        user_input: str,
        *,
        context: ThreadExecutionContext | None = None,
        thread_id: str = "",
    ) -> tuple[bool, str | None]:
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
                self._execution.ui.dock.start_turn(user_input)
            dispatched = await self._dispatch_slash(user_input)
            if not dispatched:
                self._execution.ui.ui.print(f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]")
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            return True, None

        try:
            await self._turns.run(
                self._execution.session_id,
                user_input,
                self._execution.runtime_snapshot(),
                context=context,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._execution.ui.ui.print(f"\n[dim]Interrupted.[/dim]")
        return True, None

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._execution.slash.dispatch(inp)
