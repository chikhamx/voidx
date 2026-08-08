"""Terminal startup presentation lifecycle."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from voidx.agent.ports.presentation import RuntimeStatusReader
from voidx.agent.ports.ui import AgentUiPort
from voidx.observability.tool_log import log_tool_event
from voidx.presentation.output.events import StartupShown


class StartupPresenter:
    def __init__(
        self,
        status_reader: RuntimeStatusReader,
        ui: AgentUiPort,
        *,
        restore_snapshot: Callable[..., Awaitable[bool]],
        update_check_due: Callable[[], bool] | None = None,
        mark_update_check: Callable[[str | None], None] | None = None,
    ) -> None:
        self._status_reader = status_reader
        self._ui = ui
        self._restore_snapshot = restore_snapshot
        self._update_check_due = update_check_due
        self._mark_update_check = mark_update_check

    def title(self) -> str:
        title = self._status_reader.runtime_status().session.title
        if len(title) > 60:
            title = title[:57] + "..."
        return title

    async def show(self, *, append_transcript: bool = False, prefer_direct: bool = False) -> None:
        status = self._status_reader.runtime_status()
        title = self.title()
        active_dock = self._ui.get_dock()
        startup_event = StartupShown(
            model=status.model,
            provider=status.provider,
            workspace=status.workspace,
            session_title=title,
            is_new=status.session.is_new,
            profile_configured=status.profile_configured,
        )
        startup_via_event = active_dock is not None and self._ui.events.is_running and not prefer_direct
        if startup_via_event:
            await self._ui.events.request(startup_event)
            if append_transcript:
                await self._restore_snapshot(append=True)
            return

        if active_dock is not None and active_dock.active:
            active_dock.append_startup(
                model=status.model,
                provider=status.provider,
                workspace=status.workspace,
                session_title=title,
                is_new=status.session.is_new,
                profile_configured=status.profile_configured,
            )
            if append_transcript:
                await self._restore_snapshot(append=True)
            return

        self._ui.show_startup(
            console=self._ui.ui,
            model=status.model,
            provider=status.provider,
            workspace=status.workspace,
            session_title=title,
            is_new=status.session.is_new,
        )
        if not status.profile_configured:
            self._ui.ui.print()
            self._ui.ui.print("[yellow]No profile configured — chat is disabled until you set one up.[/yellow]")
            self._ui.ui.print("[dim]  Use [cyan]/model new[/cyan] to create a profile interactively[/dim]")
            self._ui.ui.print()

    async def show_update_check_if_needed(self) -> None:
        if self._update_check_due is None or not self._update_check_due():
            return
        try:
            from voidx.update.service import check_for_update, upgrade_hint

            result = await check_for_update()
            if self._mark_update_check is not None:
                self._mark_update_check(result.latest_version)
            if result.update_available and result.latest_version:
                self._ui.dock.append_message(
                    "[yellow]Update available:[/yellow] "
                    f"voidx {result.current_version} -> {result.latest_version}. "
                    f"[dim]{upgrade_hint()}[/dim]",
                    markup=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_tool_event("startup_update_check_failed", message=f"Startup update check failed: {exc}")
