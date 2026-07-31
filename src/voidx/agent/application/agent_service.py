"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from voidx.llm.service import get_context_limit
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
    ui,
    create_frontend,
    emit_web_gateway_bootstrap,
)
from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.logging.tool_log import log_tool_event
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.ports.execution_host import ExecutionHost
from voidx.agent.domain.thread import AgentThread


AgentExecution = ExecutionHost


class RunLoopStartupError(RuntimeError):
    """Raised when the run loop cannot start the selected frontend."""


def _ui_command_kind(command: Any) -> str:
    return str(getattr(command, "kind", "") or "")


# Commands that render their own turn bubble (via display_text) when they run a
# turn; the generic pre-dispatch echo would duplicate that bubble.
_SELF_DISPLAYING_COMMANDS = frozenset({"/loop", "/init"})


class AgentService:
    """Application-level startup and interactive run-loop service."""

    def __init__(
        self,
        execution: AgentExecution,
        runtime,
        *,
        chat_service=None,
        coding_service=None,
    ) -> None:
        self._execution = execution
        self._runtime = runtime
        self._chat_service = chat_service
        self._coding_service = coding_service
        bind_startup = getattr(execution, "bind_startup_presenter", None)
        if bind_startup is not None:
            bind_startup(self._show_startup)
        bind_coding_turn = getattr(execution, "bind_coding_turn_runner", None)
        if bind_coding_turn is not None:
            bind_coding_turn(self.run_coding_turn)

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
                    usage_stats_provider=lambda: self._execution.usage_stats,
                    mcp_catalog_provider=lambda: (
                        self._execution.mcp_manager.catalog_snapshot()
                        if self._execution.mcp_manager is not None
                        else []
                    ),
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
            goal_label=lambda: "",
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
        app.set_external_command_handler(partial(self._handle_web_command, app))
        if hasattr(app, "set_mcp_catalog_provider"):
            app.set_mcp_catalog_provider(
                lambda: (
                    self._execution.mcp_manager.catalog_snapshot()
                    if self._execution.mcp_manager is not None
                    else []
                )
            )
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
            keep_running, next_exit_message = await self._handle_user_input(
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

    async def _handle_web_command(self, app: Any, command: Any) -> None:
        if isinstance(command, dict) and command.get("kind") == "guide":
            self.submit_guidance(str(command.get("text", "")), source="user")
            return
        kind = _ui_command_kind(command)
        if kind == "submit":
            text = command.text
            if text.strip().startswith("/guide "):
                self.submit_guidance(text.strip().removeprefix("/guide").strip(), source="user")
            else:
                self._ensure_gateway_thread()
                thread_id = str(getattr(command, "thread_id", "") or "")
                session_id = self._execution.session_id or thread_id
                resolved_thread_id = thread_id or session_id or "coding"
                context = TurnExecutionContext(
                    thread_id=resolved_thread_id,
                    session_id=session_id,
                    runtime_profile=CODING_PROFILE,
                    workspace=self._execution.workspace,
                )
                app.submit_external_input(text, context=context)
        elif kind == "cancel":
            thread_id = str(getattr(command, "thread_id", "") or "")
            session_id = self._execution.session_id or thread_id
            resolved_thread_id = thread_id or session_id or "coding"
            context = TurnExecutionContext(
                thread_id=resolved_thread_id,
                session_id=session_id,
                runtime_profile=CODING_PROFILE,
                workspace=self._execution.workspace,
            )
            app.cancel_external_input(context=context)

    def can_submit_guidance(self) -> bool:
        return callable(getattr(self._execution, "submit_guidance", None))

    def submit_guidance(self, text: str, **kwargs: Any) -> bool:
        submit = getattr(self._execution, "submit_guidance", None)
        if not callable(submit):
            return False
        return bool(submit(text, **kwargs))

    async def run_coding_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ) -> None:
        if self._coding_service is not None:
            session_id = (
                (getattr(context, "session_id", "") or None)
                if context is not None
                else (self._execution.session_id or None)
            )
            await self._coding_service.run_turn(
                user_text=user_text,
                thread_id=thread_id,
                session_id=session_id,
                context=context,
                display_text=display_text,
                workspace=self._execution.workspace,
            )
            return

        from voidx.agent.runtime.contracts import TurnRequest

        if context is not None:
            resolved_thread_id = thread_id or context.thread_id or self._execution.session_id or "coding"
            session_id = context.session_id or None
            execution_context = context
        else:
            session_id = self._execution.session_id or None
            resolved_thread_id = thread_id or session_id or "coding"
            execution_context = TurnExecutionContext(
                thread_id=resolved_thread_id,
                session_id=session_id or "",
                runtime_profile=CODING_PROFILE,
                workspace=self._execution.workspace,
            )

        await self._runtime.run_turn(
            TurnRequest(
                thread=AgentThread(
                    thread_id=resolved_thread_id,
                    session_id=session_id,
                ),
                user_text=user_text,
                runtime=None,
                display_text=display_text,
                context=execution_context,
            )
        )

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
        context: TurnExecutionContext | None = None,
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
            self_displays = user_input.split(maxsplit=1)[0] in _SELF_DISPLAYING_COMMANDS
            if not is_quiet and not self_displays:
                self._execution.ui.dock.start_turn(user_input)
            dispatched = await self._dispatch_slash(user_input)
            if not dispatched:
                self._execution.ui.ui.print(f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]")
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            return True, None

        try:
            if await self._route_chat_turn(user_input, thread_id=thread_id):
                return True, None
            if await self._route_autonomous_first_message(user_input, thread_id=thread_id):
                return True, None
            await self.run_coding_turn(
                user_text=user_input,
                thread_id=thread_id,
                context=context,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._execution.ui.ui.print(f"\n[dim]Interrupted.[/dim]")
        return True, None

    async def _route_autonomous_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Start a goal/loop from the first message of a goal/loop-profile session.

        Only the first message (session has no messages yet) is consumed as the
        prompt. Later messages fall through to the default coding path; the
        autonomous runtime runs on its own dedicated thread/session.
        """
        session = getattr(self._execution, "session", None)
        profile = getattr(session, "runtime_profile", "coding")
        if profile not in {"goal", "loop"}:
            return False
        message_count = getattr(session, "message_count", 0) or 0
        if message_count > 0:
            return False
        if profile == "goal":
            return await self._handle_goal_first_message(user_input, thread_id=thread_id)
        return await self._handle_loop_first_message(user_input, thread_id=thread_id)

    async def _route_chat_turn(self, user_input: str, *, thread_id: str) -> bool:
        """Route a turn to ChatService when the target thread is a chat session.

        Returns True when the turn was handled by the chat profile. Coding
        sessions and unknown threads fall through to the default coding path
        (False). With no explicit thread_id, the host's current session decides:
        a resumed chat session is routed to ChatService, anything else to coding.
        """
        if self._chat_service is None:
            return False
        target_id = thread_id or self._execution.session_id or ""
        if not target_id:
            return False
        from voidx.memory.service import get_session

        target = await get_session(target_id)
        if target is None or target.runtime_profile != "chat":
            return False
        workspace = target.workspace or target.directory or None
        await self._chat_service.run_turn(
            user_text=user_input,
            thread=AgentThread(
                thread_id=f"chat:{target.id}",
                session_id=target.id,
            ),
            workspace=workspace,
        )
        return True

    async def _persist_first_message(self, user_input: str) -> None:
        """Save the consumed first message to the host session.

        The autonomous intake/start turns run with persist_user_input=False, so
        this records the prompt in the host session and bumps message_count to
        keep the first-message dispatch from firing again.
        """
        session = getattr(self._execution, "session", None)
        if session is None:
            return
        from voidx.memory.service import memory_now, save_message
        from voidx.memory.service import MessageRow

        await save_message(
            MessageRow(
                session_id=session.id,
                role="user",
                content=user_input,
                content_format="text",
                created_at=memory_now(),
            )
        )
        session.message_count = (getattr(session, "message_count", 0) or 0) + 1

    async def _handle_loop_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Start a dynamic loop from the first message of a loop-profile session."""
        service = getattr(self._execution, "loop_service", None)
        if service is None:
            return False
        parent = thread_id or self._execution.session_id or ""
        if not parent:
            return False
        from voidx.agent.domain.loop import LoopSpec

        status = await service.start(parent, LoopSpec(prompt=user_input, interval_seconds=None))
        ui.print(f"[dim]/loop started · {status.loop_thread_id}.[/dim]")
        await self._persist_first_message(user_input)
        return True

    async def _handle_goal_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Confirm a GoalSpec from the first message, then start the goal."""
        goal_service = getattr(self._execution, "goal_service", None)
        if goal_service is None:
            return False
        parent = thread_id or self._execution.session_id or ""
        if not parent:
            return False
        from voidx.agent.application.goal_intake import GoalIntakeError, GoalIntakeService

        intake = GoalIntakeService(self._runtime, goal_service)
        try:
            status = await intake.run(
                user_input,
                parent,
                workspace=getattr(self._execution, "workspace", "") or "",
            )
        except GoalIntakeError as exc:
            ui.print(f"[dim]{exc}[/dim]")
            return True
        ui.print(
            f"[dim]/goal started: [cyan]{status.objective_summary}[/cyan] "
            f"attempt {status.attempt_count}/{status.max_attempts}[/dim]"
        )
        await self._persist_first_message(user_input)
        return True

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._execution.slash.dispatch(inp)
