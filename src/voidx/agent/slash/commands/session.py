"""Slash /session commands."""
from __future__ import annotations

from voidx.agent.slash.runtime import _select_from_list
from voidx.runtime.ui import get_dock, session_tracker, ui
from voidx.agent.slash.helpers import _format_bytes


def _order_sessions_by_workspace(sessions, workspace):
    by_recency = sorted(sessions, key=lambda s: getattr(s, "updated_at", "") or "", reverse=True)
    return sorted(by_recency, key=lambda s: s.workspace != workspace)


class SessionCommandsMixin:
    async def _session(self, args: str) -> None:
        subcommand, _, rest = args.strip().partition(" ")
        if subcommand in {"list", "ls"}:
            await self._list_sessions()
            return
        if subcommand == "new":
            profile = None
            rest_lower = rest.strip().lower()
            if rest_lower in {"chat", "--chat", "-c"}:
                profile = "chat"
            elif rest_lower in {"coding", "--coding"}:
                profile = "coding"
            elif rest_lower in {"goal", "--goal"}:
                profile = "goal"
            elif rest_lower in {"loop", "--loop"}:
                profile = "loop"

            if profile is None:
                await self._clear()
                return

            from voidx.memory.service import create_session
            config = getattr(self.host, "config", None)
            model_info = getattr(config, "model", None) if config else None
            provider = getattr(model_info, "provider", "anthropic") if model_info else "anthropic"
            model = getattr(model_info, "model", "claude-3-5-sonnet") if model_info else "claude-3-5-sonnet"
            workspace = getattr(self.host, "workspace", "")

            session = await create_session(
                workspace=workspace,
                provider=provider,
                model=model,
                profile=profile,
                title="Chat session" if profile == "chat" else "New session",
            )

            if hasattr(self.host, "resume_session"):
                await self.host.resume_session(session)
            session_tracker.clear()
            active_dock = get_dock()
            if active_dock is not None:
                active_dock.reset()
            await self._show_startup(prefer_direct=True)
            return

        if subcommand == "resume":
            await self._resume(f"/resume {rest}".rstrip())
            return
        if subcommand in {"del", "delete"}:
            await self._session_del(rest)
            return
        ui.print("[dim]Usage: /session list|new|resume|del[/dim]")

    async def _switch_profile(self, profile: str) -> None:
        """Switch the session's runtime profile.

        A fresh session (no messages yet) is reused in place: its profile is
        updated and the host's session object is refreshed. A session that has
        messages is locked, so a new session is created in the target profile.
        """
        session = getattr(self.host, "session", None)
        message_count = getattr(session, "message_count", 0) or 0
        if session is not None and message_count == 0:
            from voidx.memory.service import update_session_profile

            await update_session_profile(session.id, profile)
            session.runtime_profile = profile
            ui.print(f"[dim]Mode set to [cyan]{profile}[/cyan] — next message starts the {profile} session.[/dim]")
            return
        await self._session(f"new {profile}".strip())

    async def _chat_shortcut(self, args: str) -> None:
        if not args.strip():
            await self._switch_profile("chat")
            return
        await self._session(f"new chat {args}".strip())

    async def _coding_shortcut(self, args: str) -> None:
        if not args.strip():
            await self._switch_profile("coding")
            return
        await self._session(f"new coding {args}".strip())

    async def _session_del(self, args: str) -> None:
        parts = args.split()
        dry_run = "--dry-run" in parts
        scope_parts = [part for part in parts if part != "--dry-run"]
        if not scope_parts:
            selected_scope = await self._select_session_delete_scope()
            if selected_scope is None:
                ui.print("[dim]Deletion cancelled.[/dim]")
                return
            scope = selected_scope
        else:
            scope = scope_parts[0]

        from voidx.memory.cleanup import apply_session_delete_plan, plan_session_delete

        try:
            plan = await plan_session_delete(scope)
        except ValueError as exc:
            ui.error(str(exc))
            return

        self._print_session_delete_plan(plan, dry_run=dry_run)
        if dry_run or not plan.candidates:
            return

        confirmed = await self._confirm_session_delete()
        if not confirmed:
            ui.print("[dim]Deletion cancelled.[/dim]")
            return

        deleted = await apply_session_delete_plan(plan)
        ui.print(f"[green]Deleted {deleted} session(s).[/green]")

    def _print_session_delete_plan(self, plan, *, dry_run: bool) -> None:
        label = "Dry run" if dry_run else "Delete preview"
        ui.print(
            f"[bold]{label}:[/bold] "
            f"{plan.total_sessions} session(s), "
            f"{plan.empty_sessions} empty, "
            f"{plan.sessions_with_messages} with messages, "
            f"{_format_bytes(plan.bytes_to_reclaim)} reclaimable"
        )
        if not plan.candidates:
            ui.print("[dim]No sessions match deletion scope.[/dim]")
            return
        for candidate in plan.candidates:
            title = candidate.title[:50] + ("..." if len(candidate.title) > 50 else "")
            workspace = candidate.workspace[:40] + ("..." if len(candidate.workspace) > 40 else "")
            updated = candidate.updated_at[:10]
            ui.print(
                f"  {candidate.session_id[:8]} | {updated} | {candidate.message_count} msgs | "
                f"{_format_bytes(candidate.bytes_to_reclaim)} | {workspace} | {title}"
            )

    async def _confirm_session_delete(self) -> bool:
        app = self.host.app
        if app is None:
            answer = await self._prompt("Delete these sessions? Type y to confirm", default="")
            return (answer or "").strip().lower() in {"y", "yes"}
        choice = await app.ask_choice(
            "Delete these sessions?",
            [
                ("Cancel", "no", "Keep saved sessions"),
                ("Delete", "yes", "Permanently remove listed sessions"),
            ],
        )
        return choice == "yes"

    async def _select_session_delete_scope(self) -> str | None:
        app = self.host.app
        if app is None:
            return "30d"
        choice = await app.ask_choice(
            "Delete sessions older than:",
            [
                ("7 days", "7d", "Delete sessions older than 7 days"),
                ("15 days", "15d", "Delete sessions older than 15 days"),
                ("30 days", "30d", "Delete sessions older than 30 days"),
                ("All sessions", "all", "Delete every saved session"),
                ("Cancel", "cancel", "Keep saved sessions"),
            ],
        )
        if choice in {None, "cancel"}:
            return None
        return str(choice)

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
        if app is None:
            answer = await self._prompt("Rollback these changes? Type y to confirm", default="")
            return (answer or "").strip().lower() in {"y", "yes"}
        choice = await app.ask_choice(
            "Rollback these changes?",
            [
                ("Cancel", "no", "Keep current files"),
                ("Rollback", "yes", "Restore captured snapshots"),
            ],
        )
        return choice == "yes"

    async def _list_sessions(self) -> None:
        from voidx.memory.service import list_sessions

        sessions = _order_sessions_by_workspace(
            await list_sessions(), getattr(self.host, "workspace", "")
        )
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
        else:
            for item in items:
                ui.print(f"  {item}")

        if idx is not None:
            await self._resume(f"/resume {sessions[idx].id}")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.service import get_session, list_sessions

        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            sessions = _order_sessions_by_workspace(
                await list_sessions(), getattr(self.host, "workspace", "")
            )
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

