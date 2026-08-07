"""Interaction capability exposed to tools."""

from __future__ import annotations

from typing import Protocol

from voidx.tooling.domain.interaction import UserInteraction, UserResponse


class InteractionPort(Protocol):
    async def request(self, interaction: UserInteraction) -> UserResponse: ...


__all__ = ["InteractionPort"]
