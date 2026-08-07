"""User interaction values emitted by tool plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field


class UserInteraction(BaseModel):
    prompt: str
    options: list[str | tuple[str, str, str]] = Field(default_factory=list)
    timeout: float | None = None


class UserResponse(BaseModel):
    value: str
    cancelled: bool = False
    free_text: bool = False


UserInteractionCallback = Callable[[UserInteraction], Awaitable[UserResponse]]


__all__ = ["UserInteraction", "UserResponse", "UserInteractionCallback"]
