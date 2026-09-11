"""Runtime-only task-state snapshots for cache-prefix experiments."""

from copy import deepcopy
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

_SNAPSHOT = "_voidx_task_state_snapshot"
_RETAINED = "_voidx_task_state_retained"


def task_state_snapshot(content: str) -> HumanMessage:
    return HumanMessage(content=content, additional_kwargs={_SNAPSHOT: True})


def _signature(message: BaseMessage) -> dict[str, Any]:
    return {
        "type": message.type,
        "content": message.content,
        "name": message.name,
        "tool_calls": getattr(message, "tool_calls", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }


class TaskStateHistory:
    def __init__(self) -> None:
        self._semantic: list[dict[str, Any]] = []
        self._snapshots: list[tuple[int, BaseMessage]] = []

    def restore(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        semantic = [
            _signature(message) for message in messages
            if not isinstance(message, SystemMessage)
            and not message.additional_kwargs.get(_SNAPSHOT)
        ]
        common = 0
        for previous, current in zip(self._semantic, semantic):
            if previous != current:
                break
            common += 1
        snapshots: dict[int, list[BaseMessage]] = {}
        for anchor, message in self._snapshots:
            if anchor <= common:
                snapshots.setdefault(anchor, []).append(message.model_copy(deep=True))
        restored: list[BaseMessage] = []
        consumed = 0
        for message in messages:
            if isinstance(message, SystemMessage):
                restored.append(message)
                continue
            restored.extend(snapshots.pop(consumed, []))
            if message.additional_kwargs.get(_RETAINED):
                continue
            restored.append(message)
            if not message.additional_kwargs.get(_SNAPSHOT):
                consumed += 1
        restored.extend(snapshots.pop(consumed, []))
        return restored

    def remember(self, messages: list[BaseMessage]) -> None:
        semantic: list[dict[str, Any]] = []
        snapshots: list[tuple[int, BaseMessage]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                continue
            if message.additional_kwargs.get(_SNAPSHOT):
                retained = message.model_copy(deep=True)
                retained.additional_kwargs[_RETAINED] = True
                snapshots.append((len(semantic), retained))
            else:
                semantic.append(deepcopy(_signature(message)))
        self._semantic = semantic
        self._snapshots = snapshots
