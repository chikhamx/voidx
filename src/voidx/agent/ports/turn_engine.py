"""Single-turn execution port."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.domain.state import SessionRuntimeState


class TurnEngine(Protocol):
    """Executes one turn and exposes the final lazy-created session identity.

    ``session_id`` is the authoritative final session id after ``run``. It may be
    empty when no session has been created yet. The runtime facade reads it to
    resolve lazy first-turn identity, so every engine must provide it.
    """

    @property
    def session_id(self) -> str: ...

    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context: Any | None = None,
        persist_user_input: bool = True,
    ) -> SessionRuntimeState: ...
