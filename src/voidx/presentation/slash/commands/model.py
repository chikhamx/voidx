"""Slash /model commands."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from voidx.presentation.slash.runtime import _select_from_list, get_providers


@dataclass(frozen=True, slots=True)
class _ProfileInput:
    """Minimal profile payload accepted by the settings host."""

    name: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None

    @property
    def provider(self) -> str:
        return self.name.split("/", 1)[0] if "/" in self.name else self.name

    @property
    def model(self) -> str:
        return self.name.split("/", 1)[1] if "/" in self.name else self.name


class ModelCommandsMixin:
    async def _dispatch_model(self, args: str) -> None:
        if args == "new":
            await self._model_new()
        elif args == "list":
            await self._model_list()
        elif args == "test" or args.startswith("test "):
            target = args.removeprefix("test").strip()
            await self._model_test(target)
        elif args == "del" or args.startswith("del "):
            target = args.removeprefix("del").strip()
            await self._model_del(target)
        elif args == "switch" or args.startswith("switch "):
            target = args.removeprefix("switch").strip()
            await self._model_switch(target)
        elif args == "reasoning" or args.startswith("reasoning "):
            target = args.removeprefix("reasoning").strip()
            await self._model_reasoning(target)
        elif args == "ctx" or args.startswith("ctx "):
            target = args.removeprefix("ctx").strip()
            await self._model_ctx(target)
        elif args:
            await self._model_switch(args)
        else:
            await self._model_switch("")

    async def _model_new(self) -> None:
        """Interactive model configuration — create or update a named profile."""
        self.host.ui.print("[bold]Configure LLM[/bold]")

        # Step 1: choose provider via arrow keys
        providers = await get_providers(
            self.host.settings,
            provider_specs=self.host.provider_specs,
        )
        provider_choices = providers + ["Add custom provider..."]
        idx = await _select_from_list(self.host.app, "Provider", provider_choices)
        if idx is None:
            self.host.ui.print("[dim]Cancelled.[/dim]")
            return
        if provider_choices[idx] == "Add custom provider...":
            new_provider = await self._prompt("Provider name")
            if not new_provider or not new_provider.strip():
                self.host.ui.error("Provider name is required.")
                return
            new_provider = new_provider.strip()
            protocol_choices = ["openai", "anthropic", "gemini", "deepseek"]
            proto_idx = await _select_from_list(self.host.app, "Protocol", protocol_choices)
            if proto_idx is None:
                self.host.ui.print("[dim]Cancelled.[/dim]")
                return
            protocol = protocol_choices[proto_idx]
            if protocol == "deepseek":
                self.host.ui.print("[dim]  deepseek: China-domestic OpenAI-compatible providers (DeepSeek, Qwen, Zhipu, etc.)[/dim]")
            self.host.ui.print(f"[dim]  Custom provider: {new_provider} (protocol={protocol})[/dim]")
        else:
            new_provider = provider_choices[idx]
            protocol = (await self.host.settings.resolve_protocol(new_provider)) if self.host.settings else None
        self.host.ui.print(f"[dim]  Provider: {new_provider}[/dim]")

        # Step 2: connection details, used immediately for model discovery
        current_base_url = ""
        current_key = ""
        if self.host.settings:
            current_base_url = await self.host.settings.resolve_base_url(new_provider) or ""
            current_key = await self.host.settings.resolve_api_key(new_provider) or ""

        base_url_input = await self._prompt("Base URL (optional)", default=current_base_url)
        if base_url_input is None:
            self.host.ui.print("[dim]Cancelled.[/dim]")
            return
        base_url = base_url_input.strip() or current_base_url or None

        masked = self._mask_key(current_key) if current_key else "(not set)"
        self.host.ui.print(f"[dim]Current: {masked}[/dim]")
        new_key = await self._prompt("API key", default="", secret=True)
        if new_key is None:
            self.host.ui.print("[dim]Cancelled.[/dim]")
            return
        if new_key.strip():
            api_key = new_key.strip()
        else:
            if not current_key:
                self.host.ui.error(
                    f"No API key found for '{new_provider}'. Provide one now."
                )
                return
            api_key = current_key

        # Step 3: choose model from fetched list or enter manually
        try:
            known = await asyncio.wait_for(
                self.host.model_catalog.list_models_for_config(
                    new_provider,
                    api_key=api_key,
                    base_url=base_url,
                    protocol=protocol,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            known = await self.host.model_catalog.list_fallback_models(
                new_provider, protocol=protocol
            )
            self.host.ui.warn("Model list fetch timed out; using saved/static model list.")
        model_choices = known + ["Other (enter manually)"]
        self.host.ui.print()
        model_idx = await _select_from_list(self.host.app, "Model", model_choices)
        if model_idx is None:
            self.host.ui.print("[dim]Cancelled.[/dim]")
            return
        if model_choices[model_idx] == "Other (enter manually)":
            new_model = await self._prompt(
                f"Model name",
                default=self.host.config.model.model,
            )
            if new_model is None:
                self.host.ui.print("[dim]Cancelled.[/dim]")
                return
            if not new_model.strip():
                self.host.ui.error("Model name is required.")
                return
            new_model = new_model.strip()
        else:
            new_model = model_choices[model_idx]
            self.host.ui.print(f"[dim]  Model: {new_model}[/dim]")

        # Step 4: build and validate
        test_cfg = self.host.config.model.model_copy()
        test_cfg.provider = new_provider
        test_cfg.model = new_model
        test_cfg.base_url = base_url
        test_cfg.protocol = protocol

        test_model = self.host._model_factory(api_key, test_cfg)

        self.host.ui.print()
        self.host.ui.print(f"[dim]  Testing connection to {new_provider}/{new_model}...[/dim]")

        ok, err_msg = await self._test_connection(test_model)
        if not ok:
            self.host.ui.error(f"Connection failed: {err_msg}")
            self.host.ui.print("[dim]Configuration not saved. Check your API key and try again.[/dim]")
            return

        # Step 5: save profile (key = provider/model) and activate
        profile_key = f"{new_provider}/{new_model}"
        profile = _ProfileInput(
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

        self.host.ui.print(f"  [cyan]{profile_key}[/cyan] [green]✓ configured[/green]")
        self.host.ui.print(f"[dim]Saved to {env_path}[/dim]")
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

        current = f"{self.host.config.model.provider}/{self.host.config.model.model}"
        self.host.ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]\n")

        for provider in await get_providers(
            self.host.settings,
            provider_specs=self.host.provider_specs,
        ):
            self.host.ui.print(f"  [bold]{provider}[/bold] ", end="")
            try:
                models = await asyncio.wait_for(
                    self.host.model_catalog.list_models(provider), timeout=15.0
                )
            except asyncio.TimeoutError:
                models = []
                self.host.ui.print("[dim](fetch timed out)[/dim]")
                continue
            if models:
                shown = models[:8]
                suffix = f" [dim](+{len(models) - 8} more)[/dim]" if len(models) > 8 else ""
                self.host.ui.print(f"{'  '.join(shown)}{suffix}")
            else:
                self.host.ui.print("[dim](none)[/dim]")
        self.host.ui.print()
        self.host.ui.print("[dim]Usage: /model list|new|reasoning|ctx|test|del|switch|<name>[/dim]")

    async def _model_list(self) -> None:
        cfg = self.host.config
        if self.host.settings is None:
            self.host.ui.error("No Settings reference.")
            return

        current = f"{cfg.model.provider}/{cfg.model.model}"
        self.host.ui.print(f"[bold]Current:[/bold] [cyan]{current}[/cyan]")

        profiles = await self.host.settings.list_profiles()
        if not profiles:
            self.host.ui.print("[dim]No profiles configured. Use /model new.[/dim]")
            return

        self.host.ui.print()
        for p in profiles:
            is_active = p.name == current
            marker = " *" if is_active else "  "
            masked = self._mask_key(p.api_key) if p.api_key else "(env)"
            self.host.ui.print(f" {marker} [cyan]{p.name}[/cyan] {masked}")

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
            self.host.ui.print("[yellow]No profiles configured. Use /model new first.[/yellow]")
            return

        self.host.ui.print(f"[bold]{action}[/bold] — select profile (↑↓ Enter, ESC cancel):")
        idx = await _select_from_list(self.host.app, action, names)
        if idx is None:
            self.host.ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])
        _sys.stdout.flush()

    async def _model_test(self, target: str) -> None:
        async def _do_test(profile_name: str) -> None:
            settings = self.host.settings
            if settings is None:
                self.host.ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if not profile:
                self.host.ui.error(f"Profile not found: {profile_name}")
                return
            cfg = self.host.config.model.model_copy()
            cfg.provider = profile.provider
            cfg.model = profile.model
            cfg.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            cfg.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            model = self.host._model_factory(profile.api_key, cfg)
            self.host.ui.print(f"[dim]Testing {profile.name} ({profile.provider}/{profile.model})...[/dim]")
            ok, err_msg = await self._test_connection(model)
            if ok:
                self.host.ui.print(f"[green]✓ {profile.name} — connection successful[/green]")
            else:
                self.host.ui.print(f"[red]✗ {profile.name} — {err_msg}[/red]")

        await self._pick_or_act("Test", target, _do_test)

    async def _model_del(self, target: str) -> None:
        async def _do_delete(profile_name: str) -> None:
            if self.host.settings is None:
                self.host.ui.error("No Settings reference.")
                return
            profile = await self.host.settings.resolve_profile(profile_name)
            if not profile:
                self.host.ui.error(f"Profile not found: {profile_name}")
                return
            env_path = await self.host.settings.delete_profile(profile_name)
            was_active = (self.host.config.model.provider == profile.provider
                          and self.host.config.model.model == profile.model)
            if was_active:
                self.host.model = None
                self.host.api_key = None
                self.host.ui.print(f"[yellow]'{profile_name}' removed. Model disconnected.[/yellow]")
            else:
                self.host.ui.print(f"[dim]'{profile_name}' removed.[/dim]")
            self.host.ui.print(f"[dim]Cleaned {env_path}[/dim]")

        await self._pick_or_act("Delete", target, _do_delete)

    async def _model_switch(self, target: str) -> None:
        from voidx.agent.adapters.persistence.session_repository import update_session_model

        target, scope = self._model_switch_scope(target)

        async def _do_switch(profile_name: str) -> None:
            settings = self.host.settings
            if settings is None:
                self.host.ui.error("No Settings reference.")
                return
            profile = await settings.resolve_profile(profile_name)
            if profile is None:
                if "/" in profile_name:
                    new_provider, new_model = profile_name.split("/", 1)
                    new_provider = new_provider.lower()
                elif " " in profile_name:
                    parts = profile_name.split(None, 1)
                    new_provider = parts[0].lower()
                    new_model = parts[1]
                else:
                    new_provider = self.host.config.model.provider
                    new_model = profile_name
                new_key = await settings.resolve_api_key(new_provider)
                if not new_key:
                    self.host.ui.error(f"No API key found for '{new_provider}'. Use /model new.")
                    return
                profile = _ProfileInput(
                    name=f"{new_provider}/{new_model}",
                    api_key=new_key,
                    base_url=await settings.resolve_base_url(new_provider),
                    protocol=await settings.resolve_protocol(new_provider),
                )
            self.host.config.model.provider = profile.provider
            self.host.config.model.model = profile.model
            self.host.config.model.base_url = profile.base_url or await settings.resolve_base_url(profile.provider)
            self.host.config.model.protocol = profile.protocol or await settings.resolve_protocol(profile.provider)
            self._sync_context_limit()
            self.host.api_key = profile.api_key
            self.host.model = self.host._model_factory(profile.api_key, self.host.config.model)
            await settings.save_profile(profile, scope=scope)
            if self.host.session:
                await update_session_model(self.host.session.id, profile.provider, profile.model)
            scope_label = "global + local" if scope == "global" else "local"
            self.host.ui.print(f"[cyan]{profile.name}[/cyan] ({profile.provider}/{profile.model}) [green]✓ switched ({scope_label})[/green]")

        await self._pick_or_act("Switch", target, _do_switch)

    async def _model_reasoning(self, effort: str) -> None:
        reasoning_effort_type = self.host.reasoning_effort_type
        valid = tuple(item.value for item in reasoning_effort_type)

        if effort and effort in valid:
            new_effort = reasoning_effort_type(effort)
        elif not effort:
            current = (
                self.host.config.model.reasoning_effort.value
                if self.host.config.model.reasoning_effort is not None
                else reasoning_effort_type.XHIGH.value
            )
            choices = list(valid)
            idx = await _select_from_list(self.host.app, "Select effort", choices)
            if idx is None:
                self.host.ui.print("[dim]Cancelled.[/dim]")
                return
            new_effort = reasoning_effort_type(choices[idx])
        else:
            self.host.ui.error(f"Invalid effort: '{effort}'. Use: {', '.join(valid)}")
            return

        self.host.config.model.reasoning_effort = new_effort
        self._sync_context_limit()

        if self.host.api_key:
            self.host.model = self.host._model_factory(self.host.api_key, self.host.config.model)

        self.host.ui.print(f"Reasoning effort: [cyan]{new_effort.value}[/cyan] [green]✓[/green]")

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
                self.host.ui.error(f"Invalid context window: '{target}'. Use: {', '.join(choices_map)}")
                return
            new_label, new_value = normalized[key]
        else:
            choices = list(choices_map)
            idx = await _select_from_list(self.host.app, "Context window", choices)
            if idx is None:
                self.host.ui.print("[dim]Cancelled.[/dim]")
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
        self.host.ui.print(f"Context window: [cyan]{display}[/cyan] [green]✓[/green]")

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
        limit = self.host.context_limit_resolver(self.host.config.model.provider, self.host.config.model.protocol or "", self.host.config.model.context_window)
        stats = self.host.usage_stats
        if stats is not None:
            stats.context_limit = limit
        compaction = getattr(self.host, "_compaction", None)
        if compaction is not None:
            compaction.context_limit = limit
        app = self.host.app
        if app is not None:
            app.status.context_limit = limit
            app.status.provider = self.host.config.model.provider
            app.status.model = self.host.config.model.model
            app.status.reasoning_effort = (
                self.host.config.model.reasoning_effort.value
                if self.host.config.model.reasoning_effort is not None
                else "xhigh"
            )

