"""Slash command support for session lifecycle operations."""

from __future__ import annotations

from voidx.agent.slash.runtime import _select_from_list, ui
from voidx.runtime.ui import get_dock, session_tracker


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
        app = self._host_app()
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
        clearer = getattr(self._g, "clear_current_session", None)
        if callable(clearer):
            await clearer()
        else:
            await self._clear_current_session_compat()
        session_tracker.clear()
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._show_startup()

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
            items.append(f"{session.id[:8]} | {title} | {getattr(session, 'updated_at', '')[:16]}")

        idx = None
        app = self._host_app()
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
                items.append(f"{session.id[:8]} | {title} | {getattr(session, 'updated_at', '')[:16]}")
            idx = None
            app = self._host_app()
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

        resumer = getattr(self._g, "resume_session", None)
        if callable(resumer):
            await resumer(session)
        else:
            await self._resume_session_compat(session)
        active_dock = get_dock()
        if active_dock is not None:
            active_dock.reset()
        await self._restore_transcript_snapshot(append=True)
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        session = self._host_session()
        if not session:
            return
        title = cmd.removeprefix("/title").strip()
        if title:
            setter = getattr(self._g, "set_session_title", None)
            if callable(setter):
                await setter(title)
            else:
                from voidx.memory.session import update_title

                await update_title(session.id, title)
                self._set_legacy_attr("_session", session.model_copy(update={"title": title}))
            ui.print(f"[dim]Title set: {title}[/dim]")

    async def _clear_current_session_compat(self) -> None:
        session = self._host_session()
        if not session:
            return

        from voidx.memory.session import clear_messages, update_title

        await clear_messages(session.id)
        await update_title(session.id, "New session")
        clear_runtime_state = self._legacy_attr("_clear_runtime_state")
        if callable(clear_runtime_state):
            await clear_runtime_state()
        self._set_legacy_attr("_session", session.model_copy(update={
            "title": "New session",
            "message_count": 0,
        }))
        self._set_legacy_attr("_session_msg_cache", [])
        tracker = self._legacy_attr("_tracker")
        if tracker is not None:
            tracker.clear_todos()
        permission = self._host_permission()
        if permission is not None:
            permission.clear_session_permissions()
        stats = self._host_usage_stats()
        if stats is not None:
            stats.reset()

    async def _resume_session_compat(self, session) -> None:
        self._set_legacy_attr("_session", session)
        self._set_legacy_attr("_workspace", session.workspace)
        self._g.config.workspace = session.workspace
        self._set_legacy_attr("_session_msg_cache", None)
        restore_runtime_state = self._legacy_attr("_restore_runtime_state")
        if callable(restore_runtime_state):
            await restore_runtime_state()

    async def _restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        restorer = getattr(self._g, "restore_transcript_snapshot", None)
        if callable(restorer):
            return bool(await restorer(append=append))
        restorer = self._legacy_attr("_restore_transcript_snapshot")
        return bool(await restorer(append=append))

    async def _show_startup(self) -> None:
        shower = getattr(self._g, "show_startup", None)
        if callable(shower):
            await shower()
            return
        shower = self._legacy_attr("_show_startup")
        await shower()
