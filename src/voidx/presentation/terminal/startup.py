"""Terminal startup presentation lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any

from voidx.logging.tool_log import log_tool_event
from voidx.runtime.ui import StartupShown


class StartupPresenter:
    """Render and publish startup presentation events for an agent execution."""

    def __init__(self, execution: Any) -> None:
        self._execution = execution

    def title(self) -> str:
        title = self._execution.session.title if self._execution.session else "New session"
        if len(title) > 60:
            title = title[:57] + "..."
        return title

    async def show(
        self,
        *,
        append_transcript: bool = False,
        prefer_direct: bool = False,
    ) -> None:
        is_new = self._execution.session is None
        title = self.title()
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

    async def show_update_check_if_needed(self) -> None:
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
