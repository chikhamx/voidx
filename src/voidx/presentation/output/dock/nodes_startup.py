"""Startup node mutations for BottomInputDock."""

from __future__ import annotations

from voidx.presentation.output.tree import OutputNode


class DockStartupNodeMixin:
    def append_startup(
        self,
        *,
        model: str,
        provider: str,
        workspace: str,
        session_title: str,
        is_new: bool,
        profile_configured: bool = True,
        session_id: str = "",
    ) -> OutputNode | None:
        from voidx.presentation.session import render_startup_lines

        lines = render_startup_lines(
            self._width(),
            model=model,
            provider=provider,
            workspace=workspace,
            session_title=session_title,
            is_new=is_new,
            session_id=session_id,
        )
        if not profile_configured:
            lines.extend([
                "[yellow]No profile configured — chat is disabled until you set one up.[/yellow]",
                "[dim]  Use [cyan]/model new[/cyan] to create a profile interactively[/dim]",
            ])
        if not lines:
            return None
        startup_nodes = [
            child for child in self._tree.root.children if child.node_type == "startup"
        ]
        if startup_nodes:
            existing = startup_nodes[0]
            for duplicate in startup_nodes[1:]:
                self._remove_node(duplicate)
            existing.header = lines[0]
            existing.body_lines = lines[1:]
            existing.collapsed = False
            existing.payload.update(
                model=model,
                provider=provider,
                workspace=workspace,
                is_new=is_new,
                session_title=session_title,
                session_id=session_id,
            )
            self._tree.mark_dirty()
            self._mark_completed(existing)
            self.refresh()
            return existing
        node = self._tree.new_node(
            parent=self._tree.root,
            node_type="startup",
            header=lines[0],
            body_lines=lines[1:],
            collapsed=False,
            payload={
                "model": model,
                "provider": provider,
                "workspace": workspace,
                "is_new": is_new,
                "session_title": session_title,
                "session_id": session_id,
            },
        )
        self._mark_completed(node)
        self.refresh()
        return node

