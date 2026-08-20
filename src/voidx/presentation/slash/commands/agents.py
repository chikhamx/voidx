"""Slash commands for discovering and selecting agent profiles."""

from __future__ import annotations

from voidx.agent.facade import (
    AgentProfileValidationError,
    list_agent_profiles,
    resolve_agent_profile,
)


class AgentsCommandsMixin:
    async def _agents(self, args: str) -> None:
        command, _, raw_name = args.strip().partition(" ")
        if not command or command == "list":
            self._list_agents()
            return
        if command != "use" or not raw_name.strip():
            self.mode_port.ui.print("[dim]Usage: /agents list|use <name>[/dim]")
            return
        name = raw_name.strip()
        try:
            resolved = resolve_agent_profile(self.mode_port.workspace or ".", name)
        except AgentProfileValidationError as exc:
            for diagnostic in exc.diagnostics:
                self.mode_port.ui.error(diagnostic.message)
            return
        except KeyError:
            self.mode_port.ui.error(f"Unknown agent profile: {name}")
            return
        await self._switch_profile(
            resolved.snapshot.profile_id,
            resolved_profile=resolved,
        )

    def _list_agents(self) -> None:
        profiles = list_agent_profiles(self.mode_port.workspace or ".")
        if not profiles:
            self.mode_port.ui.print("[dim]No agent profiles found.[/dim]")
            return
        self.mode_port.ui.print("[bold]Agent profiles:[/bold]")
        for profile in profiles:
            state = "available" if profile.available else "unavailable"
            self.mode_port.ui.print(
                f"  [cyan]{profile.name}[/cyan] — {profile.display_name} "
                f"[{profile.source}, {state}]"
            )
            for diagnostic in profile.diagnostics:
                self.mode_port.ui.print(f"    [dim]{diagnostic.message}[/dim]")
