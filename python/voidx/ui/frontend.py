"""Transport-independent UI frontend interfaces."""

from __future__ import annotations

from typing import Protocol

from voidx.ui.output.events.schema import UiEvent
from voidx.ui.protocol import UiRequest, UiResponse


class UiController(Protocol):
    async def submit_text(self, text: str) -> None:
        """Submit user-authored text to the agent core."""

    async def cancel(self) -> None:
        """Request cancellation of the current agent action."""

    async def run_slash(self, command: str) -> None:
        """Run a slash command through the agent core."""


class UiFrontend(Protocol):
    def emit(self, event: UiEvent) -> None:
        """Deliver an asynchronous UI event to the frontend."""

    async def request(self, request: UiRequest) -> UiResponse:
        """Ask the frontend for a user decision or text input."""

    async def run(self, controller: UiController) -> None:
        """Start the frontend event loop with a controller callback surface."""
