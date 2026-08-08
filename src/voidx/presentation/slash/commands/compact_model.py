"""Slash command for compaction summary model settings."""

from __future__ import annotations

import math



class CompactModelCommandsMixin:
    async def _compact_model(self, args: str) -> None:
        settings = self.preferences_port.preference_settings
        ui = self.preferences_port.ui
        if settings is None:
            ui.error("No settings file available.")
            return

        value = args.strip()
        config = settings.get_compaction_config()
        if not value:
            self._show_compact_model(config)
            return

        parts = value.split()
        if parts[0] == "clear" and len(parts) == 1:
            settings.set_compaction_config(config.model_copy(update={"profile_name": ""}))
            return
        if parts[0] == "timeout" and len(parts) == 2:
            try:
                timeout = float(parts[1])
                if not math.isfinite(timeout) or not 1 <= timeout <= 300:
                    raise ValueError
            except ValueError:
                ui.error("Timeout must be a finite number between 1 and 300 seconds.")
                return
            settings.set_compaction_config(config.model_copy(update={"timeout_seconds": timeout}))
            return
        if parts[0] == "reasoning":
            if len(parts) == 1:
                self._show_compact_reasoning(config)
                return
            if len(parts) != 2:
                ui.error("Usage: /compact-model reasoning [none|low|medium|high|xhigh|max|inherit]")
                return
            effort = parts[1].lower()
            if effort == "inherit":
                reasoning = None
            else:
                try:
                    reasoning = self.preferences_port.reasoning_effort_type(effort)
                except ValueError:
                    ui.error("Invalid compaction reasoning effort.")
                    return
            settings.set_compaction_config(config.model_copy(update={"reasoning_effort": reasoning}))
            return
        if len(parts) != 1:
            ui.error("Usage: /compact-model [profile|clear|timeout <seconds>|reasoning ...]")
            return
        profile = await settings.resolve_profile(value)
        if profile is None:
            ui.error(f"Unknown model profile: {value}")
            return
        settings.set_compaction_config(config.model_copy(update={"profile_name": value}))

    def _show_compact_model(self, config) -> None:
        main = self.preferences_port.model_config
        stored_profile = config.profile_name or "inherit"
        effective_profile = config.profile_name or f"{main.provider}/{main.model}"
        reasoning = config.reasoning_effort or main.reasoning_effort
        source = "compaction" if config.reasoning_effort is not None else "main"
        self.preferences_port.ui.print(
            "\n".join([
                f"stored profile: {stored_profile}",
                f"effective profile: {effective_profile}",
                f"stored reasoning: {config.reasoning_effort.value if config.reasoning_effort is not None else 'inherit'}",
                f"effective reasoning: {reasoning.value}",
                f"reasoning source: {source}",
                f"timeout: {config.timeout_seconds:g}",
            ])
        )

    def _show_compact_reasoning(self, config) -> None:
        main = self.preferences_port.model_config
        reasoning = config.reasoning_effort or main.reasoning_effort
        source = "compaction" if config.reasoning_effort is not None else "main"
        self.preferences_port.ui.print(
            "\n".join([
                f"stored reasoning: {config.reasoning_effort.value if config.reasoning_effort is not None else 'inherit'}",
                f"effective reasoning: {reasoning.value}",
                f"reasoning source: {source}",
            ])
        )
