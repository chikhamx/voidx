"""Context compaction use cases."""

from collections.abc import Callable

from voidx.agent.domain.events import AgentEvent, AgentEventKind
from voidx.agent.ports.compaction import CompactionEngine


class CompactionService:
    def __init__(
        self,
        engine: CompactionEngine,
        publish: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._engine = engine
        self._publish = publish

    async def compact_live_messages(
        self,
        messages: list,
        session_messages: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
    ) -> tuple[list | None, str | None]:
        result = await self._engine.compact(
            messages,
            session_messages,
            force=force,
            ask=ask,
            preflight=preflight,
        )
        if result is None:
            return None, None
        messages[:] = result.live_messages
        if self._publish is not None:
            self._publish(
                AgentEvent(
                    kind=AgentEventKind.COMPACTION_COMPLETED,
                    metadata={"tail_id": result.tail_id or "", "fallback": result.fallback},
                )
            )
        return result.removed_messages, result.tail_id
