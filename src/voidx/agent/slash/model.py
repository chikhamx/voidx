"""Slash command support for /model operations."""

from __future__ import annotations

import asyncio

from voidx.agent.slash.runtime import PROVIDERS, get_providers, _select_from_list
from voidx.runtime.ui import ui


class SlashModelMixin:
    async def _model_new(self) -> None:
        """Interactive model configuration — create or update a named profile."""
        from voidx.config import Profile
        from voidx.llm.service import create_chat_model

        ui.print("[bold]Configure LLM[/bold]")

        # Step 1: choose provider via arrow keys
        providers = await get_providers(self.host.settings)
        provider_choices = providers + ["Add custom provider..."]
        idx = await _select_from_list(self.host.app, "Provider", provider_choices)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if provider_choices[idx] == "Add custom provider...":
            new_provider = await self._prompt("Provider name")
            if not new_provider or not new_provider.strip():
                ui.error("Provider name is required.")
                return
            new_provider = new_provider.strip()
            protocol_choices = ["openai", "anthropic", "gemini", "deepseek"]
            proto_idx = await _select_from_list(self.host.app, "Protocol", protocol_choices)
            if proto_idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            protocol = protocol_choices[proto_idx]
            if protocol == "deepseek":
                ui.print("[dim]  deepseek: China-domestic OpenAI-compatible providers (DeepSeek, Qwen, Zhipu, etc.)[/dim]")
            custom_base_url = await self._prompt("Base URL (optional)", default="")
            if custom_base_url is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            custom_base_url = custom_base_url.strip()
            ui.print(f"[dim]  Custom provider: {new_provider} (protocol={protocol})[/dim]")
        else:
            new_provider = provider_choices[idx]
            protocol = (await self.host.settings.resolve_protocol(new_provider)) if self.host.settings else None
            custom_base_url = ""
        ui.print(f"[dim]  Provider: {new_provider}[/dim]")

        # Step 2: choose model from known list or enter manually
        from voidx.llm.catalog import list_models as list_provider_models
        try:
            known = await asyncio.wait_for(list_provider_models(new_provider), timeout=15.0)
        except asyncio.TimeoutError:
            known = []
            ui.warn("Model list fetch timed out; enter model name manually.")
        model_choices = known + ["Other (enter manually)"]
        ui.print()
        model_idx = await _select_from_list(self.host.app, "Model", model_choices)
        if model_idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if model_choices[model_idx] == "Other (enter manually)":
            new_model = await self._prompt(
                f"Model name",
                default=self.host.config.model.model,
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
        if self.host.settings:
            current_key = await self.host.settings.resolve_api_key(new_provider) or ""
        masked = self._mask_key(current_key) if current_key else "(not set)"
        ui.print(f"[dim]Current: {masked}[/dim]")
        new_key = await self._prompt("API key", default="", secret=True)
        if new_key is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        if new_key.strip():
            api_key = new_key.strip()
        else:
            if self.host.settings:
                key = await self.host.settings.resolve_api_key(new_provider)
                if not key:
                    ui.error(
                        f"No API key found for '{new_provider}'. Provide one now."
                    )
                    return
                api_key = key
            else:
                return

        # Step 4: build and validate
        base_url = custom_base_url or (await self.host.settings.resolve_base_url(new_provider) if self.host.settings else None)
        test_cfg = self.host.config.model.model_copy()
        test_cfg.provider = new_provider
        test_cfg.model = new_model
        test_cfg.base_url = base_url
        test_cfg.protocol = protocol

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
            protocol=protocol,
        )
        env_path = await self.host.settings.save_profile(profile)

        self.host.config.model.provider = new_provider
        self.host.config.model.model = new_model
        self.host.config.model.base_url = base_url
        self.host.config.model.protocol = protocol
        self._sync_context_limit()
        self.host.api_key = api_key
        self.host.model = test_model

        ui.print(f"  [cyan]{profile_key}[/cyan] [green]✓ configured[/green]")
        ui.print(f"[dim]Saved to {env_path}[/dim]")
        await self._show_startup(prefer_direct=True)

    @staticmethod
    async def _test_connection(model, timeout: float = 30.0) -> tuple[bool, str]:
        """Test an LLM connection with a minimal prompt. Returns (ok, error_msg)."""
        from langchain_core.messages import HumanMessage
        try:
            resp = await asyncio.wait_for(
                model.ainvoke([HumanMessage(content="hi")]),
                timeout=timeout,
            )
            if resp and getattr(resp, "content", None):
                return True, ""
            return False, "empty response"
        except asyncio.TimeoutError:
            return False, f"timed out after {timeout:.0f}s"
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

    async def _list_models(self) -> None:
        from voidx.llm.catalog import list_models

        current = f"{self.host.config.model.provider}/{self.host.config.model.model}"
        ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]\n")

        for provider in await get_providers(self.host.settings):
            ui.print(f"  [bold]{provider}[/bold] ", end="")
            try:
                models = await asyncio.wait_for(list_models(provider), timeout=15.0)
            except asyncio.TimeoutError:
                models = []
                ui.print("[dim](fetch timed out)[/dim]")
                continue
            if models:
                shown = models[:8]
                suffix = f" [dim](+{len(models) - 8} more)[/dim]" if len(models) > 8 else ""
                ui.print(f"{'  '.join(shown)}{suffix}")
            else:
                ui.print("[dim](none)[/dim]")
        ui.print()
        ui.print("[dim]Usage: /model list|new|reasoning|ctx|test|del|switch|<name>[/dim]")

    async def _model_list(self) -> None:
        cfg = self.host.config
        if self.host.settings is None:
            ui.error("No Settings reference.")
            return

        current = f"{cfg.model.provider}/{cfg.model.model}"
        ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]")

        profiles = await self.host.settings.list_profiles()
        if not profiles:
            ui.print("[dim]No profiles configured. Use /model new.[/dim]")
            return

        ui.print()
        for p in profiles:
            is_active = p.name == current
            marker = " *" if is_active else "  "
            masked = self._mask_key(p.api_key) if p.api_key else "(env)"
            ui.print(f" {marker} [cyan]{p.name}[/cyan] {masked}")

    # ── /model action helpers ─────────────────────────────────────────────

    async def _profile_names(self) -> list[str]:
        """Return names of configured profiles."""
        if self.host.settings is None:
            return []
        return [p.name for p in await self.host.settings.list_profiles()]

    async def _pick_or_act(self, action: str, target: str, callback) -> None:
        """If *target* is a profile name, call callback(target).
        Otherwise show profiles for arrow-key selection, then call callback."""
        import sys as _sys

        if target:
            await callback(target)
            _sys.stdout.flush()
            return

        names = await self._profile_names()
        if not names:
            ui.print("[yellow]No profiles configured. Use /model new first.[/yellow]")
            return

        ui.print(f"[bold]{action}[/bold] — select profile (↑↓ Enter, ESC cancel):")
        idx = await _select_from_list(self.host.app, action, names)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])
        _sys.stdout.flush()

    async def _model_test(self, target: str) -> None:
        async def _do_test(profile_name: str) -> None:
            from voidx.llm.service import create_chat_model
            settings = self.host.settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            cfg = self.host.config.model.model_copy()
            cfg.provider = profile.provider
            cfg.model = profile.model
            cfg.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            cfg.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            model = create_chat_model(profile.api_key, cfg)
            ui.print(f"[dim]Testing {profile.name} ({profile.provider}/{profile.model})...[/dim]")
            ok, err_msg = await self._test_connection(model)
            if ok:
                ui.print(f"[green]✓ {profile.name} — connection successful[/green]")
            else:
                ui.print(f"[red]✗ {profile.name} — {err_msg}[/red]")

        await self._pick_or_act("Test", target, _do_test)

    async def _model_del(self, target: str) -> None:
        async def _do_delete(profile_name: str) -> None:
            if self.host.settings is None:
                ui.error("No Settings reference.")
                return
            profile = await self.host.settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            env_path = await self.host.settings.delete_profile(profile_name)
            was_active = (self.host.config.model.provider == profile.provider
                          and self.host.config.model.model == profile.model)
            if was_active:
                self.host.model = None
                self.host.api_key = None
                ui.print(f"[yellow]'{profile_name}' removed. Model disconnected.[/yellow]")
            else:
                ui.print(f"[dim]'{profile_name}' removed.[/dim]")
            ui.print(f"[dim]Cleaned {env_path}[/dim]")

        await self._pick_or_act("Delete", target, _do_delete)

    async def _model_switch(self, target: str) -> None:
        target, scope = self._model_switch_scope(target)

        async def _do_switch(profile_name: str) -> None:
            from voidx.llm.service import create_chat_model
            settings = self.host.settings
            if settings is None:
                ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if not profile:
                ui.error(f"Profile not found: {profile_name}")
                return
            self.host.config.model.provider = profile.provider
            self.host.config.model.model = profile.model
            self.host.config.model.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            self.host.config.model.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            self._sync_context_limit()
            self.host.api_key = profile.api_key
            self.host.model = create_chat_model(profile.api_key, self.host.config.model)
            await settings.save_profile(profile, scope=scope)
            scope_label = "global + local" if scope == "global" else "local"
            ui.print(f"[cyan]{profile.name}[/cyan] ({profile.provider}/{profile.model}) [green]✓ switched ({scope_label})[/green]")
            await self._show_startup(prefer_direct=True)

        await self._pick_or_act("Switch", target, _do_switch)

    async def _model_reasoning(self, effort: str) -> None:
        valid = ("off", "low", "medium", "high", "xhigh")

        if effort and effort in valid:
            new_effort = effort
        elif not effort:
            current = self.host.config.model.reasoning_effort or "xhigh"
            choices = list(valid)
            idx = await _select_from_list(self.host.app, "Select effort", choices)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            new_effort = choices[idx]
        else:
            ui.error(f"Invalid effort: '{effort}'. Use: {', '.join(valid)}")
            return

        self.host.config.model.reasoning_effort = new_effort
        self._sync_context_limit()

        if self.host.api_key:
            from voidx.llm.service import create_chat_model
            self.host.model = create_chat_model(self.host.api_key, self.host.config.model)

        ui.print(f"Reasoning effort: [cyan]{new_effort}[/cyan] [green]✓[/green]")

    async def _model_ctx(self, target: str) -> None:
        choices_map: dict[str, int | None] = {
            "128k": 128_000,
            "256k": 256_000,
            "384k": 384_000,
            "512k": 512_000,
            "1M": 1_000_000,
            "Auto": None,
        }

        if target:
            key = target.lower()
            normalized = {c.lower(): (c, v) for c, v in choices_map.items()}
            if key not in normalized:
                ui.error(f"Invalid context window: '{target}'. Use: {', '.join(choices_map)}")
                return
            new_label, new_value = normalized[key]
        else:
            choices = list(choices_map)
            idx = await _select_from_list(self.host.app, "Context window", choices)
            if idx is None:
                ui.print("[dim]Cancelled.[/dim]")
                return
            new_value = choices_map[choices[idx]]
            new_label = choices[idx]

        self.host.config.model.context_window = new_value
        self._sync_context_limit()

        settings = self.host.settings
        if settings is not None:
            if new_value is None:
                settings._pop_setting("context_window")
            else:
                settings._set_setting("context_window", new_value)

        display = "Auto (provider default)" if new_value is None else f"{new_label}"
        ui.print(f"Context window: [cyan]{display}[/cyan] [green]✓[/green]")

    async def _switch_model(self, model_spec: str) -> None:
        from voidx.llm.service import create_chat_model
        from voidx.memory.service import update_session_model

        if not model_spec:
            await self._list_models()
            return

        spec, scope = self._model_switch_scope(model_spec)
        if not spec:
            await self._list_models()
            return

        if " " in spec:
            parts = spec.split(None, 1)
            new_provider = parts[0].lower()
            new_model = parts[1]
        elif "/" in spec:
            new_provider, new_model = spec.split("/", 1)
            new_provider = new_provider.lower()
        else:
            new_provider = self.host.config.model.provider
            new_model = spec

        # Resolve API key for the target provider
        if self.host.settings is None:
            ui.error("No Settings reference available.")
            return
        new_key = await self.host.settings.resolve_api_key(new_provider)
        if not new_key:
            ui.error(
                f"No API key found for '{new_provider}'. Use /model new."
            )
            return

        self.host.api_key = new_key

        old = f"{self.host.config.model.provider}/{self.host.config.model.model}"
        self.host.config.model.provider = new_provider
        self.host.config.model.model = new_model
        self.host.config.model.base_url = (
            (await self.host.settings.resolve_base_url(new_provider)) if self.host.settings else None
        )
        self.host.config.model.protocol = (
            (await self.host.settings.resolve_protocol(new_provider)) if self.host.settings else None
        )

        from voidx.config import Profile
        existing = await self.host.settings.resolve_profile(f"{new_provider}/{new_model}")
        if existing:
            self.host.config.model.base_url = existing.base_url or self.host.config.model.base_url
            self.host.config.model.protocol = existing.protocol or self.host.config.model.protocol

        self._sync_context_limit()

        self.host.model = create_chat_model(self.host.api_key, self.host.config.model)

        new_profile = Profile(
            name=f"{new_provider}/{new_model}",
            api_key=new_key,
            base_url=self.host.config.model.base_url,
            protocol=self.host.config.model.protocol,
        )
        await self.host.settings.save_profile(new_profile, scope=scope)

        if self.host.session:
            await update_session_model(self.host.session.id, new_provider, new_model)

        ui.print(f"[dim]  {old}[/dim]")
        scope_label = "global + local" if scope == "global" else "local"
        ui.print(f"  [cyan]→ {new_provider}/{new_model}[/cyan] [green]✓ ({scope_label})[/green]")

    @staticmethod
    def _model_switch_scope(raw: str) -> tuple[str, str]:
        scope = "local"
        filtered: list[str] = []
        for token in raw.strip().split():
            if token == "--local":
                scope = "local"
            elif token == "--global":
                scope = "global"
            else:
                filtered.append(token)
        return " ".join(filtered), scope

    def _sync_context_limit(self) -> None:
        from voidx.llm.service import get_context_limit

        limit = get_context_limit(self.host.config.model.provider, self.host.config.model.protocol or "", self.host.config.model.context_window)
        stats = self.host.usage_stats
        if stats is not None:
            stats.context_limit = limit
        app = self.host.app
        if app is not None and hasattr(app, "status"):
            app.status.context_limit = limit
            app.status.provider = self.host.config.model.provider
            app.status.model = self.host.config.model.model
            app.status.reasoning_effort = self.host.config.model.reasoning_effort or "xhigh"
