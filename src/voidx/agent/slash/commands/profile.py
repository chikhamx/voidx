"""Slash /profile commands."""
from __future__ import annotations

from voidx.agent.slash.runtime import _select_from_list
from voidx.config import UserProfile
from voidx.runtime.ui import ui
from voidx.agent.slash.helpers import _normalize_language, _normalize_tone


class ProfileCommandsMixin:
    async def _lang(self, args: str) -> None:
        value = args.strip()
        if not value:
            await self._lang_interactive()
            return
        self._apply_language(value)

    async def _tone(self, args: str) -> None:
        value = args.strip()
        if not value:
            await self._tone_interactive()
            return
        self._apply_tone(value)

    async def _lang_interactive(self) -> None:
        from voidx.agent.application.prompts import _LANGUAGE_LABELS

        items: list[str] = []
        values: list[str] = []
        for _key, (name, tag) in _LANGUAGE_LABELS.items():
            items.append(f"{name} [{tag}]")
            values.append(tag)
        if self.host.app is None:
            await self._lang_headless(values)
            return
        selected = await self._pick_or_reset(
            "Language",
            items,
            values,
            "Language code (e.g. fr, de, pt-BR; auto to reset)",
            "Other (enter manually)",
            "Reset (auto-detect)",
        )
        if selected is not None:
            self._apply_language(selected)

    async def _tone_interactive(self) -> None:
        from voidx.agent.application.runtime_context import _TONE_LABELS

        items: list[str] = []
        values: list[str] = []
        for value, (name, description, _instruction) in _TONE_LABELS.items():
            items.append(f"{name} - {description}")
            values.append(value)
        if self.host.app is None:
            await self._tone_headless(values)
            return
        selected = await self._pick_or_reset(
            "Tone",
            items,
            values,
            "Tone (e.g. patient, enthusiastic; default to reset)",
            "Other (enter manually)",
            "Reset (default)",
        )
        if selected is not None:
            self._apply_tone(selected)

    async def _pick_or_reset(
        self,
        title: str,
        option_items: list[str],
        values: list[str],
        prompt_label: str,
        other_label: str,
        reset_label: str,
    ) -> str | None:
        items = [*option_items, other_label, reset_label]
        idx = await _select_from_list(self.host.app, title, items)
        if idx is None or idx < 0 or idx >= len(items):
            ui.print("[dim]Cancelled.[/dim]")
            return None
        if idx == len(values):
            result = await self._prompt(prompt_label)
            if result is None or not result.strip():
                ui.print("[dim]Cancelled.[/dim]")
                return None
            return result.strip()
        if idx == len(values) + 1:
            return ""
        return values[idx]

    async def _lang_headless(self, values: list[str]) -> None:
        ui.print(f"Language: [cyan]{self._current_language_label()}[/cyan]")
        ui.print(f"[dim]Available: {', '.join(values)}[/dim]")
        value = await self._prompt("Language code (or 'auto' to reset)", default="")
        if value is None or not value.strip():
            ui.print("[dim]Cancelled.[/dim]")
            return
        self._apply_language(value)

    async def _tone_headless(self, values: list[str]) -> None:
        ui.print(f"Tone: [cyan]{self._current_tone_label()}[/cyan]")
        ui.print(f"[dim]Available: {', '.join(values)}[/dim]")
        value = await self._prompt("Tone (or 'default' to reset)", default="")
        if value is None or not value.strip():
            ui.print("[dim]Cancelled.[/dim]")
            return
        self._apply_tone(value)

    def _apply_language(self, value: str) -> None:
        settings = self.host.settings
        if settings is not None:
            settings.set_user_language(value)
            profile = settings.get_user_profile()
        else:
            profile = self._current_user_profile()
            profile.language = _normalize_language(value)
        self._set_current_user_profile(profile)
        ui.print(f"Language: [cyan]{profile.language or 'auto-detect'}[/cyan] [green]✓[/green]")

    def _apply_tone(self, value: str) -> None:
        settings = self.host.settings
        if settings is not None:
            settings.set_user_tone(value)
            profile = settings.get_user_profile()
        else:
            profile = self._current_user_profile()
            profile.tone = _normalize_tone(value)
        self._set_current_user_profile(profile)
        ui.print(f"Tone: [cyan]{profile.tone or 'default'}[/cyan] [green]✓[/green]")

    def _current_user_profile(self) -> UserProfile:
        profile = getattr(self.host.config, "user_profile", None)
        if isinstance(profile, UserProfile):
            return profile.model_copy()
        return UserProfile()

    def _set_current_user_profile(self, profile: UserProfile) -> None:
        self.host.config.user_profile = profile

    def _current_language_label(self) -> str:
        profile = self._current_user_profile()
        return profile.language or "auto"

    def _current_tone_label(self) -> str:
        profile = self._current_user_profile()
        return profile.tone or "default"

