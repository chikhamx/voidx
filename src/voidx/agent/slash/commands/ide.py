"""Slash /ide commands."""
from __future__ import annotations

from voidx.platform.code_ide import CodeIde
from voidx.platform.code_ide import code_ide_status, detect_code_ides, normalize_ide
from voidx.agent.slash.helpers import _ide_label


class IdeCommandsMixin:
    async def _code_ide(self, args: str) -> None:
        settings = self.host.settings
        if settings is None:
            self.host.ui.error("No settings file available.")
            return

        value = args.strip().lower()
        if value == "status":
            self.host.ui.print(code_ide_status(settings))
            return

        valid = {item.value for item in CodeIde}
        if not value:
            app = self.host.app
            if app is not None:
                detected = detect_code_ides()
                detected_ids = {item.id for item in detected}
                choices = []
                for ide in CodeIde:
                    label = _ide_label(ide.value)
                    desc = "configured default" if ide.value == settings.get_code_ide().value else ""
                    if ide.value in detected_ids:
                        desc = (desc + " · " if desc else "") + "detected"
                    elif ide.value not in {CodeIde.AUTO.value, CodeIde.SYSTEM.value}:
                        desc = (desc + " · " if desc else "") + "not detected"
                    choices.append((label, ide.value, desc))
                selected = await app.ask_choice("Code IDE", choices)
                if selected:
                    value = selected
            if not value:
                self.host.ui.print(code_ide_status(settings))
                self.host.ui.print("Usage: /code-ide [auto|trae|cursor|code|windsurf|zed|sublime|jetbrains|ghostty|system|status]")
                return

        value = normalize_ide(value)
        if value not in valid:
            self.host.ui.error(f"Invalid code IDE: {value}. Use: {', '.join(sorted(valid))}")
            return

        path = settings.set_code_ide(CodeIde(value))
        self.host.ui.print(f"[dim]Code IDE set to [cyan]{value}[/cyan]. Saved to {path}[/dim]")
        self.host.ui.print(code_ide_status(settings))

