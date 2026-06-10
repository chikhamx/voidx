"""Slash command support for session lifecycle operations."""

from __future__ import annotations

from voidx.agent.slash.runtime import _select_from_list
from voidx.runtime.ui import ui
from voidx.ui.output.dock import get_dock
from voidx.ui.session import session_tracker


class SlashSessionMixin:
    async def _rollback(self) -> None:
        if not session_tracker.has_rollbackable_changes:
            ui.print("[dim]No file changes to roll back.[/dim]")
            return

        lines = session_tracker.rollback_summary_lines()
        if lines:
            ui.print("[bold]Files changed this turn:[/bold]")
            for line in lines:
                ui.print(line)
            ui.print("")
        ui.print("[yellow]Rollback will overwrite current file contents with the pre-edit snapshot.[/yellow]")

        confirmed = await self._confirm_rollback()
        if not confirmed:
            ui.print("[dim]Rollback cancelled.[/dim]")
            return

        result = session_tracker.rollback_current()
        if result.restored:
            ui.print(f"[green]Restored:[/green] {', '.join(result.restored)}")
        if result.removed:
            ui.print(f"[green]Removed:[/green] {', '.join(result.removed)}")
        if result.ok:
            if not result.restored and not result.removed:
                ui.print("[dim]No files needed rollback.[/dim]")
            return
        for err in result.errors:
            ui.error(err)

    async def _confirm_rollback(self) -> bool:
        app = self.host.app
        if app is not None and hasattr(app, "ask_choice"):
            choice = await app.ask_choice(
                "Rollback these changes?",
                [
                    ("Cancel", "no", "Keep current files"),
                    ("Rollback", "yes", "Restore captured snapshots"),
                ],
            )
            return choice == "yes"
        answer = await self._prompt("Rollback these changes? Type y to confirm", default="")
        return (answer or "").strip().lower() in {"y", "yes"}

    async def _clear(self) -> None:
        await self.host.clear_current_session()
        session_tracker.clear()
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._show_startup(prefer_direct=True)

    async def _list_sessions(self) -> None:
        from voidx.memory.session import list_sessions

        sessions = await list_sessions()
        if not sessions:
            ui.print("No saved sessions.")
            return

        ui.print("[bold]Sessions:[/bold]")
        items = []
        for session in sessions:
            title = session.title[:50] + ("..." if len(session.title) > 50 else "")
            items.append(f"{session.id[:8]} | {title} | {session.workspace} | {getattr(session, 'updated_at', '')[:16]}")

        idx = None
        app = self.host.app
        if app is not None:
            idx = await _select_from_list(app, "Resume session?", items)

        if idx is not None:
            await self._resume(f"/resume {sessions[idx].id}")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.session import get_session, list_sessions

        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            sessions = await list_sessions()
            if not sessions:
                ui.print("[dim]No saved sessions.[/dim]")
                return
            items = []
            for session in sessions:
                title = session.title[:50] + ("..." if len(session.title) > 50 else "")
                items.append(f"{session.id[:8]} | {title} | {session.workspace} | {getattr(session, 'updated_at', '')[:16]}")
            idx = None
            app = self.host.app
            if app is not None:
                idx = await _select_from_list(app, "Resume session?", items)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            sid = sessions[idx].id

        session = await get_session(sid)
        if not session:
            ui.error(f"Session not found: {sid}")
            return

        await self.host.resume_session(session)
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._restore_transcript_snapshot(append=True)
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        session = self.host.session
        if not session:
            return
        title = cmd.removeprefix("/title").strip()
        if title.lower() == "auto":
            if await self.host.regenerate_session_title():
                ui.print("[dim]Regenerating title...[/dim]")
            else:
                ui.print("[dim]No user message available for title generation.[/dim]")
            return
        if title:
            if await self.host.set_session_title(title):
                ui.print(f"[dim]Title set: {title}[/dim]")

    async def _restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        return await self.host.restore_transcript_snapshot(append=append)

    async def _show_startup(self, **kwargs) -> None:
        await self.host.show_startup(**kwargs)
