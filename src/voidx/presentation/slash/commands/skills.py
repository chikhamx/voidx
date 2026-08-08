"""Slash /skills commands."""
from __future__ import annotations

from voidx.skills.service import SkillService


class SkillsCommandsMixin:
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
        elif action == "auto":
            self._skills_set_auto(target, True)
        elif action == "manual":
            self._skills_set_auto(target, False)
        elif action == "paths":
            self._skills_paths()
        else:
            self.host.ui.error("Usage: /skills [list|show|enable|disable|auto|manual|paths]")

    def _skill_service(self) -> SkillService:
        return self.host.skills_api.service

    def _skills_list(self) -> None:
        service = self._skill_service()
        skills = service.list_skills()
        self.host.ui.print("[bold]Skills:[/bold]")
        if not skills:
            self.host.ui.print("[dim]No skills found. Add SKILL.md files under ~/.voidx/skills or .voidx/skills.[/dim]")
            return
        for skill in skills:
            state = "[green]enabled[/green]" if service.is_enabled(skill) else "[dim]disabled[/dim]"
            mode = "[green]auto[/green]" if service.is_auto(skill) else "[dim]manual[/dim]"
            scope = skill.meta.scope
            desc = f" — {skill.meta.description}" if skill.meta.description else ""
            self.host.ui.print(f"  [cyan]{skill.name}[/cyan] · {state} · {mode} · [dim]{scope}[/dim]{desc}")
        self.host.ui.print("[dim]Usage: /skills show|enable|disable|auto|manual|paths[/dim]")

    def _skills_show(self, name: str) -> None:
        if not name:
            self.host.ui.error("Usage: /skills show <name>")
            return
        service = self._skill_service()
        skill = service.get(name)
        if skill is None:
            self.host.ui.error(f"Skill not found: {name}")
            return
        state = "enabled" if service.is_enabled(skill) else "disabled"
        mode = "auto" if service.is_auto(skill) else "manual"
        self.host.ui.print(f"[bold]{skill.name}[/bold] [{state}, {mode}]")
        self.host.ui.print(f"[dim]{skill.path}[/dim]")
        if skill.meta.description:
            self.host.ui.print(skill.meta.description)
        if skill.meta.triggers:
            self.host.ui.print(f"[dim]Triggers: {', '.join(skill.meta.triggers)}[/dim]")
        self.host.ui.print()
        self.host.ui.print(skill.body or "[dim](empty skill body)[/dim]")

    def _skills_set_enabled(self, name: str, enabled: bool) -> None:
        if not name:
            command = "enable" if enabled else "disable"
            self.host.ui.error(f"Usage: /skills {command} <name>")
            return
        if self.host.settings is None:
            self.host.ui.error("No settings file available.")
            return
        service = self._skill_service()
        if service.get(name) is None:
            self.host.ui.error(f"Skill not found: {name}")
            return
        path = self.host.settings.set_skill_enabled(name, enabled)
        self.host.invalidate_skill_service_cache()
        state = "enabled" if enabled else "disabled"
        self.host.ui.print(f"[dim]{name} {state}. Saved to {path}[/dim]")

    def _skills_set_auto(self, name: str, auto: bool) -> None:
        if not name:
            command = "auto" if auto else "manual"
            self.host.ui.error(f"Usage: /skills {command} <name>")
            return
        if self.host.settings is None:
            self.host.ui.error("No settings file available.")
            return
        service = self._skill_service()
        if service.get(name) is None:
            self.host.ui.error(f"Skill not found: {name}")
            return
        path = self.host.settings.set_skill_auto(name, auto)
        self.host.invalidate_skill_service_cache()
        mode = "auto" if auto else "manual"
        self.host.ui.print(f"[dim]{name} set to {mode}. Saved to {path}[/dim]")

    def _skills_paths(self) -> None:
        registry = self._skill_service().registry
        self.host.ui.print("[bold]Skill paths:[/bold]")
        self.host.ui.print(f"  bundled [dim]{registry.bundled_dir}[/dim]")
        self.host.ui.print(f"  global  [dim]{registry.global_dir}[/dim]")
        self.host.ui.print(f"  project [dim]{registry.project_dir}[/dim]")

