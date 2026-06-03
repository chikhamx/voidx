"""Agent graph adapter — wraps voidx_core RustAgent in a PureTui-compatible API."""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from voidx.ui.startup import show_startup


class VoidXGraph:
    """Adapter that bridges the PureTui to the Rust agent engine."""

    def __init__(self, cfg, api_key=None, session=None, settings=None):
        self._config = cfg
        self._api_key = api_key
        self._session = session
        self._settings = settings
        self._workspace = cfg.workspace
        self._rust_agent = None  # lazy init when first used

    # ── Public API ────────────────────────────────────────────────────

    async def run(
        self,
        web: bool = False,
        web_headless: bool = False,
        web_host: str = "127.0.0.1",
        web_port: int = 0,
        web_token: str = "",
    ) -> None:
        if web:
            print("Web mode not yet bridged.")
            return
        await self._run_tui()

    async def _run_tui(self) -> None:
        from rich.text import Text
        from voidx.ui.tui import PureTui
        from voidx.ui.commands import COMMANDS

        # Build commands list for the TUI footer
        cmd_list = list(COMMANDS) if isinstance(COMMANDS, list) else []

        # Status bar
        status = Text("voidx — Rust Core", style="bold green")

        app = PureTui(status=status, commands=cmd_list)
        self._app = app  # save ref for callbacks

        # Show startup banner
        show_startup(
            app._console,
            model=self._config.model.model,
            provider=self._config.model.provider,
            workspace=str(self._workspace),
            session_title=getattr(self._session, "title", "") or "New Session",
            is_new=getattr(self._session, "id", None) is None,
        )

        # ── Run loop ──────────────────────────────────────────────
        await app.run(on_submit=self._handle_submit)

    async def _handle_submit(self, text: str) -> bool:
        """Called by PureTui when user submits input. Returns True if handled."""
        from rich.text import Text

        if not text.strip():
            return True

        # Handle slash commands
        if text.startswith("/"):
            return await self._handle_slash(text)

        # Lazy-init Rust agent on first message
        if self._rust_agent is None:
            self._init_agent()

        status = Text("Thinking...", style="italic yellow")
        app = getattr(self, "_app", None)
        if app:
            app.status = status

        # Call Rust agent (sync, in thread to not block event loop)
        session_id = self._session.id if self._session else "default"
        try:
            result = await asyncio.to_thread(
                self._rust_agent.run_text, text
            )
        except Exception as e:
            result = f"[bold red]Error:[/bold red] {e}"

        # Print result to the app's console
        if app:
            from rich.console import Console
            console = app._console
            console.print()
            console.print(result)
            console.print()
            app.status = Text("Ready", style="dim")

        return True

    def _init_agent(self) -> None:
        """Create and initialize the Rust agent."""
        import voidx_core

        key = self._api_key
        if not key:
            key = self._resolve_key()

        if not key:
            raise RuntimeError(
                "No API key configured. Use /model to set one, "
                "or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY env var."
            )

        model_cfg = voidx_core.ModelConfig(
            provider=self._config.model.provider,
            model=self._config.model.model,
            temperature=self._config.model.temperature,
            max_tokens=self._config.model.max_tokens,
        )
        rust_cfg = voidx_core.Config(
            workspace=str(self._workspace),
            model=model_cfg,
        )
        self._rust_agent = voidx_core.RustAgent(rust_cfg, key)
        self._rust_agent.initialize()

    def _resolve_key(self) -> str | None:
        """Try to find API key from env or settings."""
        provider = self._config.model.provider.upper()
        return os.environ.get(f"{provider}_API_KEY")

    # ── Slash commands ───────────────────────────────────────────────

    async def _handle_slash(self, text: str) -> bool:
        """Handle /model, /mode etc."""
        parts = text.strip().split()
        cmd = parts[0].lower() if parts else ""

        if cmd in ("/q", "/quit", "/exit"):
            return False  # signal PureTui to exit

        if cmd == "/model":
            await self._slash_model(parts)
            return True

        if cmd == "/mode":
            if len(parts) >= 2:
                mode = parts[1]
                print(f"Mode set to: {mode}")
            return True

        print(f"Unknown command: {cmd}")
        return True

    async def _slash_model(self, parts: list[str]) -> None:
        """Handle /model command with interactive provider/model selection."""
        import voidx_core
        from voidx.ui.tui import PureTui

        # If no args, show providers
        if len(parts) < 3:
            providers = voidx_core.RustAgent.providers()
            app = getattr(self, "_app", None)
            if app and len(providers) > 0:
                choices = [(p, p, f"Models: {len(voidx_core.RustAgent.list_models(p))} available") for p in providers]
                choice = await app.ask_choice(
                    "Select provider (↑↓ to navigate, Enter to select):",
                    choices=choices,
                )
                if not choice:
                    return
                provider = choice
            else:
                print("Usage: /model <provider> <model>")
                return
        else:
            provider = parts[1]

        # If provider specified, show models
        if len(parts) < 3:
            models = voidx_core.RustAgent.list_models(provider)
            app = getattr(self, "_app", None)
            if app and models:
                choices = [(m, m, "") for m in models[:20]]
                choice = await app.ask_choice(
                    f"Select model for {provider}:",
                    choices=choices,
                )
                if not choice:
                    return
                model = choice
            else:
                print(f"Models for {provider}: {', '.join(models[:10])}")
                return
        else:
            model = parts[2]

        # Update config
        self._config.model.provider = provider
        self._config.model.model = model

        # Ask for API key
        app = getattr(self, "_app", None)
        if app:
            key = await app.ask_text(
                f"API key for {provider} (or leave blank to use env var):",
                secret=True,
            )
            if key and key.strip():
                self._api_key = key.strip()

        # Rebuild Rust agent with new config
        self._rust_agent = None  # force re-init on next message
        print(f"[green]Switched to {provider}/{model}[/green]")
