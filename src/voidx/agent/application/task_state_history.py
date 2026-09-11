"""Runtime-only task-state snapshots for cache-prefix experiments."""

from difflib import SequenceMatcher

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

_SNAPSHOT = "_voidx_task_state_snapshot"
_RETAINED = "_voidx_task_state_retained"


def task_state_snapshot(content: str) -> HumanMessage:
    return HumanMessage(content=content, additional_kwargs={_SNAPSHOT: True})


def _signature(message: BaseMessage) -> tuple[str, tuple[str, ...], str | None]:
    return (
        message.type,
        tuple(call["id"] for call in getattr(message, "tool_calls", ())),
        getattr(message, "tool_call_id", None),
    )


class TaskStateHistory:
    def __init__(self) -> None:
        self._semantic: list[tuple[str, tuple[str, ...], str | None]] = []
        self._snapshots: list[tuple[int, BaseMessage]] = []

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)

    def clear(self) -> None:
        self._semantic = []
        self._snapshots = []

    def anchors_valid(self, messages: list[BaseMessage]) -> bool:
        semantic = [
            _signature(message) for message in messages
            if not isinstance(message, SystemMessage)
            and not message.additional_kwargs.get(_SNAPSHOT)
        ]
        return semantic[:len(self._semantic)] == self._semantic

    def history_reset(self, messages: list[BaseMessage]) -> bool:
        return not self.anchors_valid(messages)

    def restore(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        snapshots: dict[int, list[BaseMessage]] = {}
        semantic = [
            _signature(message) for message in messages
            if not isinstance(message, SystemMessage)
            and not message.additional_kwargs.get(_SNAPSHOT)
        ]
        matches = [
            (block.a + offset, block.b + offset)
            for block in SequenceMatcher(None, self._semantic, semantic, autojunk=False).get_matching_blocks()
            for offset in range(block.size)
        ]
        position = 0
        for anchor, message in self._snapshots:
            before = next(((old, new) for old, new in reversed(matches) if old < anchor), None)
            after = next(((old, new) for old, new in matches if old >= anchor), None)
            if before is not None:
                mapped = before[1] + anchor - before[0]
                if after is not None:
                    mapped = min(mapped, after[1])
            elif after is not None:
                mapped = max(0, after[1] - (after[0] - anchor))
            else:
                mapped = anchor
            # Missing anchors never imply compaction; clamp while keeping snapshot order.
            position = max(position, min(mapped, len(semantic)))
            snapshots.setdefault(position, []).append(message.model_copy(deep=True))
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
        semantic: list[tuple[str, tuple[str, ...], str | None]] = []
        snapshots: list[tuple[int, BaseMessage]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                continue
            if message.additional_kwargs.get(_SNAPSHOT):
                retained = message.model_copy(deep=True)
                retained.additional_kwargs[_RETAINED] = True
                snapshots.append((len(semantic), retained))
            else:
                semantic.append(_signature(message))
        self._semantic = semantic
        self._snapshots = snapshots
