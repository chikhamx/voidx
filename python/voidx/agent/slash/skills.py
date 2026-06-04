"""Slash command support for /skills operations."""

from __future__ import annotations

from voidx.agent.slash.runtime import ui
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService


class SlashSkillsMixin:
    async def _skills(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if action in ("", "list"):
            self._skills_list()
        elif action == "show":
            self._skills_show(target)
        elif action == "enable":
            self._skills_set_enabled(target, True)
        elif action == "disable":
            self._skills_set_enabled(target, False)
        elif action == "paths":
            self._skills_paths()
        else:
            ui.error("Usage: /skills [list|show|enable|disable|paths]")

    def _skill_service(self) -> SkillService:
        selection = (
            self._g._settings.get_skill_selection()
            if getattr(self._g, "_settings", None) is not None
            else None
        )
        return SkillService(
            SkillRegistry(getattr(self._g, "_workspace", ".")),
            selection=selection,
        )

    def _skills_list(self) -> None:
        service = self._skill_service()
        skills = service.list_skills()
        ui.print("[bold]Skills:[/bold]")
        if not skills:
            ui.print("[dim]No skills found. Add SKILL.md files under ~/.voidx/skills or .voidx/skills.[/dim]")
            return
        for skill in skills:
            state = "[green]enabled[/green]" if service.is_enabled(skill) else "[dim]disabled[/dim]"
            scope = skill.meta.scope
            desc = f" — {skill.meta.description}" if skill.meta.description else ""
            ui.print(f"  [cyan]{skill.name}[/cyan] · {state} · [dim]{scope}[/dim]{desc}")
        ui.print("[dim]Usage: /skills show|enable|disable|paths[/dim]")

    def _skills_show(self, name: str) -> None:
        if not name:
            ui.error("Usage: /skills show <name>")
            return
        service = self._skill_service()
        skill = service.get(name)
        if skill is None:
            ui.error(f"Skill not found: {name}")
            return
        state = "enabled" if service.is_enabled(skill) else "disabled"
        ui.print(f"[bold]{skill.name}[/bold] [{state}]")
        ui.print(f"[dim]{skill.path}[/dim]")
        if skill.meta.description:
            ui.print(skill.meta.description)
        if skill.meta.triggers:
            ui.print(f"[dim]Triggers: {', '.join(skill.meta.triggers)}[/dim]")
        ui.print()
        ui.print(skill.body or "[dim](empty skill body)[/dim]")

    def _skills_set_enabled(self, name: str, enabled: bool) -> None:
        if not name:
            command = "enable" if enabled else "disable"
            ui.error(f"Usage: /skills {command} <name>")
            return
        if getattr(self._g, "_settings", None) is None:
            ui.error("No settings file available.")
            return
        service = self._skill_service()
        if service.get(name) is None:
            ui.error(f"Skill not found: {name}")
            return
        path = self._g._settings.set_skill_enabled(name, enabled)
        state = "enabled" if enabled else "disabled"
        ui.print(f"[dim]{name} {state}. Saved to {path}[/dim]")

    def _skills_paths(self) -> None:
        registry = SkillRegistry(getattr(self._g, "_workspace", "."))
        ui.print("[bold]Skill paths:[/bold]")
        ui.print(f"  bundled [dim]{registry.bundled_dir}[/dim]")
        ui.print(f"  global  [dim]{registry.global_dir}[/dim]")
        ui.print(f"  project [dim]{registry.project_dir}[/dim]")
