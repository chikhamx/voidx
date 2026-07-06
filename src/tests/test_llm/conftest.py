"""Tests for CompactionService — token counting, select, prune, build_prompt."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


from voidx.llm.compaction import (
    COMPACTION_MAX_RETRIES,
    COMPACTION_THRESHOLD,
    CompactionSelection,
    CompactionService,
    DEFAULT_TAIL_TURNS,
    STEP_HINT_MARKER,
)
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.llm.usage import estimate_context_tokens



class _NoopUiSink:
    width = 80

    def print(self, *_args, **_kwargs) -> None:
        return None


class _NoopEvents:
    async def emit(self, _event) -> bool:
        return True


class _FakeUiPort:
    def __init__(self, *, via_events: bool = False, events=None) -> None:
        self._via_events = via_events
        self.events = events or _NoopEvents()
        self.ui = _NoopUiSink()
        self.console = _NoopUiSink()

    def via_events(self) -> bool:
        return self._via_events


def _make_messages_with_tool_calls(n_turns: int = 5) -> list:
    """Build messages where AI messages have tool_calls — the key difference
    between the old select() counting and estimate_context_tokens."""
    messages = []
    for i in range(n_turns):
        messages.append(HumanMessage(content=f"User message {i}", id=str(i * 3 + 1)))
        ai = AIMessage(
            content=f"Assistant reply {i}",
            tool_calls=[
                {"name": "read", "args": {"file_path": f"/tmp/file_{i}.py"}, "id": f"tc_{i}"},
            ],
        )
        messages.append(ai)
        messages.append(ToolMessage(content=f"Tool result {i}" * 50, tool_call_id=f"tc_{i}"))
    return messages
