"""Domain results produced by context compaction."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


@dataclass(frozen=True)
class CompactionResult:
    """Completed compaction result applicable to a live message state."""

    summary: str
    removed_messages: list[BaseMessage]
    live_messages: list[BaseMessage]
    tail_id: str | None
    fallback: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


class PreflightCompactionResult(BaseModel):
    compacted: bool
    summary: str = ""
    removed_message_count: int = 0
    retained_turn_count: int = 0
    pre_tokens: int = 0
    post_tokens: int = 0
    post_target_tokens: int = 0
    tail_anchor_id: str = ""
    fallback: bool = False
    reason: str = ""

    @classmethod
    def from_compaction_result(cls, result: CompactionResult | None) -> PreflightCompactionResult:
        if result is None:
            return cls(compacted=False)
        metadata = result.metadata or {}
        return cls(
            compacted=True,
            summary=result.summary,
            removed_message_count=int(metadata.get("removed_message_count") or len(result.removed_messages)),
            retained_turn_count=int(metadata.get("retained_turn_count") or 0),
            pre_tokens=int(metadata.get("pre_tokens") or 0),
            post_tokens=int(metadata.get("post_tokens") or 0),
            post_target_tokens=int(metadata.get("post_compaction_target") or 0),
            tail_anchor_id=str(metadata.get("tail_anchor_id") or result.tail_id or ""),
            fallback=bool(metadata.get("fallback") or result.fallback),
            reason=str(metadata.get("compaction_reason") or ""),
        )


class ContextBudgetExhausted(RuntimeError):
    """Raised when context budget remains exhausted after compaction."""
    pass


class CompactionMessageMetadata(BaseModel):
    compaction_id: str
    source_range_hash: str
    compaction_depth: int = 1
    closed_segment_index: int = 0
    opened_segment_index: int = 1
    replacement_operation_id: str


@dataclass(frozen=True)
class ClosedToolBatch:
    ai_message: BaseMessage
    tool_messages: list[BaseMessage] = field(default_factory=list)

    @property
    def messages(self) -> list[BaseMessage]:
        return [self.ai_message, *self.tool_messages]


@dataclass(frozen=True)
class ReplacementResult:
    applied: bool
    operation_id: str
    replacement_message_id: int
    effective_message_count: int
    source_message_ids: list[int]
    winner_operation_id: str | None = None
