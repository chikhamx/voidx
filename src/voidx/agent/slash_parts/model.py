"""Slash command support for /model operations."""

from __future__ import annotations

import asyncio

from voidx.agent.slash_parts.runtime import PROVIDERS, _select_from_list, ui


class SlashModelMixin:
    async def _model_config(self) -> None:
        """Interactive model configuration — create or update a named profile."""
        from voidx.config import Profile
        from voidx.llm.provider import create_chat_model

        ui.print("[bold]Configure LLM[/bold]")

        # Step 1: choose provider via arrow keys
        idx = await _select_from_list(self._g._app, "Provider", PROVIDERS)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        new_provider = PROVIDERS[idx]
        ui.print(f"[dim]  Provider: {new_provider}[/dim]")

        # Step 2: choose model from known list or enter manually
        from voidx.llm.catalog import list_models as list_provider_models
        known = await list_provider_models(new_provider)
        model_choices = known + ["Other (enter manually)"]
        ui.print()
        model_idx = await _select_from_list(self._g._app, "Model", model_choices)
        if model_idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if model_choices[model_idx] == "Other (enter manually)":
            new_model = await self._prompt(
                f"Model name",
                default=self._g.config.model.model,
            )
            if new_model is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            if not new_model.strip():
                ui.error("Model name is required.")
                return
            new_model = new_model.strip()
        else:
            new_model = model_choices[model_idx]
            ui.print(f"[dim]  Model: {new_model}[/dim]")

        # Step 3: API key
        current_key = ""
        if self._g._settings:
            current_key = self._g._settings.resolve_api_key(new_provider) or ""
        masked = self._mask_key(current_key) if current_key else "(not set)"
        ui.print(f"[dim]Current: {masked}[/dim]")
        new_key = await self._prompt("API key", default="", secret=True)
        if new_key is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if new_key.strip():
            api_key = new_key.strip()
        else:
            if self._g._settings:
                key = self._g._settings.resolve_api_key(new_provider)
                if not key:
                    ui.error(
                        f"No API key found for '{new_provider}'. Provide one now."
                    )
                    return
                api_key = key
            else:
                return

        # Step 4: build and validate
        base_url = self._g._settings.resolve_base_url(new_provider) if self._g._settings else None
        test_cfg = self._g.config.model.model_copy()
        test_cfg.provider = new_provider
        test_cfg.model = new_model
        test_cfg.base_url = base_url

        test_model = create_chat_model(api_key, test_cfg)

        ui.print()
        ui.print(f"[dim]  Testing connection to {new_provider}/{new_model}...[/dim]")

        ok, err_msg = await self._test_connection(test_model)
        if not ok:
            ui.error(f"Connection failed: {err_msg}")
            ui.print("[dim]Configuration not saved. Check your API key and try again.[/dim]")
            return

        # Step 5: save profile (key = provider/model) and activate
        profile_key = f"{new_provider}/{new_model}"
        profile = Profile(
            name=profile_key,
            api_key=api_key,
            base_url=base_url,
        )
        env_path = self._g._settings.save_profile(profile)

        self._g.config.model.provider = new_provider
        self._g.config.model.model = new_model
        self._g.config.model.base_url = base_url
        self._g.api_key = api_key
        self._g.model = test_model

        ui.print(f"  [cyan]{profile_key}[/cyan] [green]✓ configured[/green]")
        ui.print(f"[dim]Saved to {env_path}[/dim]")

    @staticmethod
    async def _test_connection(model) -> tuple[bool, str]:
        """Test an LLM connection with a minimal prompt. Returns (ok, error_msg)."""
        from langchain_core.messages import HumanMessage
        try:
            resp = await model.ainvoke([HumanMessage(content="hi")])
            if resp and getattr(resp, "content", None):
                return True, ""
            return False, "empty response"
        except Exception as e:
            msg = str(e)
            # Extract the most useful part of the error
            if len(msg) > 300:
                msg = msg[:300] + "..."
            return False, msg

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "****" + key[-4:]

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        app = getattr(self._g, "_app", None)
        if app is not None and hasattr(app, "ask_text"):
            return await app.ask_text(text, default=default, secret=secret)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: input(f"  {text}: ").strip(),
        )
        return result if result else default

    async def _list_models(self) -> None:
        from voidx.llm.catalog import list_models

        current = f"{self._g.config.model.provider}/{self._g.config.model.model}"
        ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]\n")

        for provider in PROVIDERS:
            ui.print(f"  [bold]{provider}[/bold] ", end="")
            models = await list_models(provider)
            if models:
                shown = models[:8]
                suffix = f" [dim](+{len(models) - 8} more)[/dim]" if len(models) > 8 else ""
                ui.print(f"{'  '.join(shown)}{suffix}")
            else:
                ui.print("[dim](none)[/dim]")
        ui.print()
        ui.print("[dim]Usage: /model list|config|test|delete|switch|<name>[/dim]")

    async def _model_list(self) -> None:
        cfg = self._g.config
        if self._g._settings is None:
            ui.error("No Settings reference.")
            return
        profiles = self._g._settings.list_profiles()
        if not profiles:
            ui.print("No profiles configured.")
            return

        ui.print("[bold]Configured Profiles:[/bold]")
        items = []
        current = f"{cfg.model.provider}/{cfg.model.model}"
        for p in profiles:
            is_active = p.name == current
            marker = " *" if is_active else "  "
            masked = self._mask_key(p.api_key) if p.api_key else "(env)"
            has_model = self._g.model is not None
            status = "[green]✓[/green]" if (is_active and has_model) else "[dim]✗[/dim]"
            ui.print(f" {marker} [cyan]{p.name}[/cyan] {masked} {status}")

            active_str = " (active)" if is_active else ""
            items.append(f"{p.name}{active_str}")

        idx = None
        if getattr(self._g, "_app", None):
            idx = await _select_from_list(self._g._app, "Select profile", items)

        if idx is not None:
            await self._model_switch(profiles[idx].name)

    # ── /model action helpers ─────────────────────────────────────────────

    def _profile_names(self) -> list[str]:
        """Return names of configured profiles."""
        if self._g._settings is None:
            return []
        return [p.name for p in self._g._settings.list_profiles()]

    async def _pick_or_act(self, action: str, target: str, callback) -> None:
        """If *target* is a profile name, call callback(target).
        Otherwise show profiles for arrow-key selection, then call callback."""
        import sys as _sys

        if target:
            await callback(target)
            _sys.stdout.flush()
            return

        names = self._profile_names()
        if not names:
            ui.print("[yellow]No profiles configured. Use /model config first.[/yellow]")
            return

        ui.print(f"[bold]{action}[/bold] — select profile (↑↓ Enter, ESC cancel):")
        idx = await _select_from_list(self._g._app, action, names)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])
        _sys.stdout.flush()

    async def _model_test(self, target: str) -> None:
        async def _do_test(profile_name: str) -> None:
            from voidx.llm.provider import create_chat_model
            settings = self._g._settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            cfg = self._g.config.model.model_copy()
            cfg.provider = profile.provider
            cfg.model = profile.model
            cfg.base_url = profile.base_url or settings.resolve_base_url(profile.provider)
            model = create_chat_model(profile.api_key, cfg)
            ui.print(f"[dim]Testing {profile.name} ({profile.provider}/{profile.model})...[/dim]")
            ok, err_msg = await self._test_connection(model)
            if ok:
                ui.print(f"[green]✓ {profile.name} — connection successful[/green]")
            else:
                ui.print(f"[red]✗ {profile.name} — {err_msg}[/red]")

        await self._pick_or_act("Test", target, _do_test)

    async def _model_delete(self, target: str) -> None:
        async def _do_delete(profile_name: str) -> None:
            if self._g._settings is None:
                ui.error("No Settings reference.")
                return
            profile = self._g._settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            env_path = self._g._settings.delete_profile(profile_name)
            was_active = (self._g.config.model.provider == profile.provider
                          and self._g.config.model.model == profile.model)
            if was_active:
                self._g.model = None
                self._g.api_key = None
                ui.print(f"[yellow]'{profile_name}' removed. Model disconnected.[/yellow]")
            else:
                ui.print(f"[dim]'{profile_name}' removed.[/dim]")
            ui.print(f"[dim]Cleaned {env_path}[/dim]")

        await self._pick_or_act("Delete", target, _do_delete)

    async def _model_switch(self, target: str) -> None:
        async def _do_switch(profile_name: str) -> None:
            from voidx.llm.provider import create_chat_model
            settings = self._g._settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            self._g.config.model.provider = profile.provider
            self._g.config.model.model = profile.model
            self._g.config.model.base_url = profile.base_url or settings.resolve_base_url(profile.provider)
            self._g.api_key = profile.api_key
            self._g.model = create_chat_model(profile.api_key, self._g.config.model)
            ui.print(f"[cyan]{profile.name}[/cyan] ({profile.provider}/{profile.model}) [green]✓ switched[/green]")

        await self._pick_or_act("Switch", target, _do_switch)

    async def _switch_model(self, model_spec: str) -> None:
        from voidx.llm.provider import create_chat_model
        from voidx.memory.session import update_session_model

        if not model_spec:
            await self._list_models()
            return

        spec = model_spec.strip()

        if " " in spec:
            parts = spec.split(None, 1)
            new_provider = parts[0].lower()
            new_model = parts[1]
        elif "/" in spec:
            new_provider, new_model = spec.split("/", 1)
            new_provider = new_provider.lower()
        else:
            new_provider = self._g.config.model.provider
            new_model = spec

        # Resolve API key for the target provider
        if self._g._settings is None:
            ui.error("No Settings reference available.")
            return
        new_key = self._g._settings.resolve_api_key(new_provider)
        if not new_key:
            ui.error(
                f"No API key found for '{new_provider}'. Use /model config."
            )
            return

        self._g.api_key = new_key

        old = f"{self._g.config.model.provider}/{self._g.config.model.model}"
        self._g.config.model.provider = new_provider
        self._g.config.model.model = new_model
        self._g.config.model.base_url = (
            self._g._settings.resolve_base_url(new_provider) if self._g._settings else None
        )

        self._g.model = create_chat_model(self._g.api_key, self._g.config.model)

        if self._g._session:
            await update_session_model(self._g._session.id, new_provider, new_model)

        ui.print(f"[dim]  {old}[/dim]")
        ui.print(f"  [cyan]→ {new_provider}/{new_model}[/cyan] [green]✓[/green]")
