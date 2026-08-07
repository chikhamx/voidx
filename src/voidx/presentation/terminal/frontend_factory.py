"""Terminal frontend factory owned by presentation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

FrontendFactory = Callable[[Any, list[tuple[str, str]]], Any]


def _default_tui_frontend_factory(status: Any, commands: list[tuple[str, str]]) -> Any:
    try:
        from voidx_cli import PureTui
    except ModuleNotFoundError:
        raise RuntimeError(
            "voidx_cli is required for terminal UI mode. "
            "Install it with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load voidx_cli: {exc}. "
            "Reinstall with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from exc
    try:
        return PureTui(status, commands)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialize terminal UI: {exc}. "
            "Reinstall with: pip install voidx-cli, or reinstall via npm (npm install -g @chikhamx/voidx)"
        ) from exc


_default_frontend_factory: FrontendFactory | None = _default_tui_frontend_factory


def register_default_frontend(factory: FrontendFactory) -> None:
    global _default_frontend_factory
    _default_frontend_factory = factory


def reset_default_frontend() -> None:
    global _default_frontend_factory
    _default_frontend_factory = None


def create_frontend(status: Any, commands: list[tuple[str, str]]) -> Any:
    if _default_frontend_factory is None:
        raise RuntimeError("No frontend registered. Install or register an interaction frontend.")
    return _default_frontend_factory(status, commands)
