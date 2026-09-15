"""Awaitable semantic output for the existing graph's runtime components."""
from __future__ import annotations

import time
from uuid import uuid4

from voidx.agent.domain import semantic_events as e
from voidx.agent.ports.events import SemanticEventPublisher


_SENSITIVE_ARGUMENT_KEYS = frozenset({
    "apikey", "token", "accesstoken", "refreshtoken", "idtoken",
    "password", "secret", "clientsecret", "authorization",
    "proxyauthorization", "cookie", "setcookie",
})


def _redact_arguments(value):
    """Copy JSON containers and redact known keys, not secrets in free text."""
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if "".join(char for char in str(key).casefold() if char.isalnum())
            in _SENSITIVE_ARGUMENT_KEYS
            else _redact_arguments(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value]
    return value


class SemanticOutput:
    def __init__(
        self, publisher: SemanticEventPublisher, *, session_id: str,
        thread_id: str, turn_id: str, agent_id: str | int | None = None,
        parent_tool_call_id: str | None = None,
    ) -> None:
        self.publisher = publisher
        self.identity = dict(
            session_id=session_id, thread_id=thread_id, turn_id=turn_id,
            agent_id=agent_id, parent_tool_call_id=parent_tool_call_id,
        )

    async def emit(self, event_type, payload) -> None:
        await self.publisher.publish(event_type(
            **self.identity, event_id=uuid4(), sequence=1,
            timestamp=time.time(), payload=payload,
        ))

    async def tool_started(self, call: dict) -> None:
        await self.emit(e.ToolStarted, e.ToolStartedPayload(
            tool_call_id=call["id"], name=call["name"],
            arguments=_redact_arguments(call.get("args", {})),
        ))

    async def tool_result(self, call: dict, result, *, ok: bool, elapsed: float) -> None:
        identity = dict(tool_call_id=call["id"], name=call["name"])
        await self.emit(e.ToolFinished, e.ToolFinishedPayload(
            **identity, elapsed=elapsed, ok=ok,
        ))
        await self.emit(e.ToolResult, e.ToolResultPayload(
            **identity, summary=result.summary or result.output,
        ))

    async def file_changed(self, result, tool_call_id: str) -> None:
        await self.emit(e.FileChanged, e.FileChangedPayload(
            tool_call_id=tool_call_id, path=result.metadata["file"], diff=result.diff,
        ))

    async def stream_started(self, stream_id: str, phase: str) -> None:
        await self.emit(e.AssistantStreamStarted, e.AssistantStreamPayload(
            stream_id=stream_id, phase=phase,
        ))

    async def stream_chunk(self, stream_id: str, phase: str, delta: str) -> None:
        await self.emit(e.AssistantChunk, e.AssistantChunkPayload(
            stream_id=stream_id, phase=phase, delta=delta,
        ))

    async def stream_committed(self, stream_id: str, phase: str, text: str) -> None:
        await self.emit(e.AssistantCommitted, e.AssistantCommittedPayload(
            stream_id=stream_id, phase=phase, text=text,
        ))

    async def stream_discarded(self, stream_id: str, phase: str, reason: str) -> None:
        await self.emit(e.AssistantDiscarded, e.AssistantDiscardedPayload(
            stream_id=stream_id, phase=phase, reason=reason,
        ))

    async def stream_error(self) -> None:
        await self.emit(e.DiagnosticError, e.DiagnosticPayload(
            code="model_stream_failed", summary="Model stream failed.", recoverable=False,
        ))
