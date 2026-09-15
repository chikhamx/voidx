"""Versioned semantic DTOs and bounded, independent JSON wire snapshots.

Ordering, redaction and result-reference authorization belong to publishers;
this module performs no queueing, storage or sequence allocation across producers.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Annotated, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator

from voidx.agent.domain.task.todo import TodoStatus
from voidx.agent.domain.turn_metadata import TurnMetadata
from voidx.llm.usage import TokenUsage
from voidx.tooling.domain.interaction import InteractionRequest, InteractionResolution

DEFAULT_MAX_EVENT_BYTES = 256 * 1024
DEFAULT_QUEUE_CAPACITY = 256
NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
AgentId: TypeAlias = int | NonEmpty | None


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, revalidate_instances="always", use_enum_values=True)


class SemanticEventConfig(ContractModel):
    max_event_bytes: PositiveInt = DEFAULT_MAX_EVENT_BYTES
    queue_capacity: PositiveInt = DEFAULT_QUEUE_CAPACITY


class EventSizeExceeded(ValueError):
    """The complete UTF-8 wire event exceeds the configured limit."""


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("Expected finite JSON values with string object keys")


class TurnStartedPayload(ContractModel):
    text: str
    metadata: TurnMetadata


class TurnCompletedPayload(ContractModel):
    usage: TokenUsage


class DiagnosticPayload(ContractModel):
    code: NonEmpty
    summary: str
    recoverable: bool


class ReasonPayload(ContractModel):
    reason: NonEmpty


class AssistantStreamPayload(ContractModel):
    stream_id: NonEmpty
    phase: Literal["thinking", "text"]


class AssistantChunkPayload(AssistantStreamPayload):
    delta: str


class AssistantCommittedPayload(AssistantStreamPayload):
    text: str | None = None
    result_ref: NonEmpty | None = None

    @model_validator(mode="after")
    def content_required(self):
        if self.text is None and self.result_ref is None:
            raise ValueError("Final text or result_ref is required")
        return self


class AssistantDiscardedPayload(AssistantStreamPayload):
    reason: NonEmpty


class ToolPayload(ContractModel):
    tool_call_id: NonEmpty
    name: NonEmpty


class ToolStartedPayload(ToolPayload):
    arguments: dict[str, JsonValue]

    @field_validator("arguments", mode="before")
    @classmethod
    def json_arguments(cls, value):
        _json_value(value)
        return value


class ToolFinishedPayload(ToolPayload):
    elapsed: Annotated[float, Field(ge=0)]
    ok: bool


class ToolResultPayload(ToolPayload):
    summary: str
    result_ref: NonEmpty | None = None


class FileChangedPayload(ContractModel):
    tool_call_id: NonEmpty
    path: NonEmpty
    diff: str | None = None
    result_ref: NonEmpty | None = None

    @model_validator(mode="after")
    def content_required(self):
        if self.diff is None and self.result_ref is None:
            raise ValueError("Diff or result_ref is required")
        return self


class StatusPayload(ContractModel):
    status_id: NonEmpty
    stage: NonEmpty
    description: str
    parent_tool_call_id: NonEmpty | None


class StatusFinishedPayload(StatusPayload):
    ok: bool
    result: str


class TodoItem(ContractModel):
    id: NonEmpty
    content: str
    status: TodoStatus


class TodoPayload(ContractModel):
    items: list[TodoItem]
    operation: Literal["write", "update", "read", "clear"]
    boundary_id: NonEmpty


class SubagentPayload(ContractModel):
    subagent_id: NonEmpty
    parent_agent_id: AgentId
    parent_tool_call_id: NonEmpty | None
    description: str


class SubagentStepPayload(SubagentPayload):
    step_id: NonEmpty


class SubagentFinishedPayload(SubagentPayload):
    reason: NonEmpty
    summary: str
    ok: bool


class ContextPressureUpdatedPayload(ContractModel):
    pressure_id: NonEmpty
    level: Literal["soft", "hard"]
    action: Literal["converge_hint"]
    outcome: Literal["hint_injected", "hint_present", "hint_upgraded"]
    reason: str
    can_compact: bool
    turn_count: Annotated[int, Field(ge=0)]
    pre_tokens: Annotated[int, Field(ge=0)]
    soft_threshold: Annotated[int, Field(ge=0)]
    hard_threshold: Annotated[int, Field(ge=0)]


class ContextPressureFinishedPayload(ContractModel):
    pressure_id: NonEmpty
    level: Literal["soft", "hard"]
    outcome: Literal["turn_converged", "compacted", "model_overflow_failed"]
    detail: str
    ok: bool


class ContextCompactedPayload(ContractModel):
    pre_tokens: Annotated[int, Field(ge=0)]
    post_tokens: Annotated[int, Field(ge=0)]
    summary: str


class GuidancePayload(ContractModel):
    text: str
    source: Literal["user", "guard", "system"]
    truncated: bool


class SemanticEventEnvelope(ContractModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    session_id: NonEmpty
    thread_id: NonEmpty
    turn_id: NonEmpty
    sequence: PositiveInt
    agent_id: AgentId
    parent_tool_call_id: NonEmpty | None
    timestamp: Annotated[float, Field(strict=True)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_one(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported schema_version")
        return value


class TurnStarted(SemanticEventEnvelope):
    kind: Literal["turn.started"] = "turn.started"
    payload: TurnStartedPayload


class TurnCompleted(SemanticEventEnvelope):
    kind: Literal["turn.completed"] = "turn.completed"
    payload: TurnCompletedPayload


class TurnFailed(SemanticEventEnvelope):
    kind: Literal["turn.failed"] = "turn.failed"
    payload: DiagnosticPayload


class TurnCancelled(SemanticEventEnvelope):
    kind: Literal["turn.cancelled"] = "turn.cancelled"
    payload: ReasonPayload


class AssistantStreamStarted(SemanticEventEnvelope):
    kind: Literal["assistant.stream_started"] = "assistant.stream_started"
    payload: AssistantStreamPayload


class AssistantChunk(SemanticEventEnvelope):
    kind: Literal["assistant.chunk"] = "assistant.chunk"
    payload: AssistantChunkPayload


class AssistantCommitted(SemanticEventEnvelope):
    kind: Literal["assistant.committed"] = "assistant.committed"
    payload: AssistantCommittedPayload


class AssistantDiscarded(SemanticEventEnvelope):
    kind: Literal["assistant.discarded"] = "assistant.discarded"
    payload: AssistantDiscardedPayload


class ToolStarted(SemanticEventEnvelope):
    kind: Literal["tool.started"] = "tool.started"
    payload: ToolStartedPayload


class ToolFinished(SemanticEventEnvelope):
    kind: Literal["tool.finished"] = "tool.finished"
    payload: ToolFinishedPayload


class ToolResult(SemanticEventEnvelope):
    kind: Literal["tool.result"] = "tool.result"
    payload: ToolResultPayload


class FileChanged(SemanticEventEnvelope):
    kind: Literal["file.changed"] = "file.changed"
    payload: FileChangedPayload


class StatusStarted(SemanticEventEnvelope):
    kind: Literal["status.started"] = "status.started"
    payload: StatusPayload


class StatusUpdated(SemanticEventEnvelope):
    kind: Literal["status.updated"] = "status.updated"
    payload: StatusPayload


class StatusFinished(SemanticEventEnvelope):
    kind: Literal["status.finished"] = "status.finished"
    payload: StatusFinishedPayload


class TodoUpdated(SemanticEventEnvelope):
    kind: Literal["todo.updated"] = "todo.updated"
    payload: TodoPayload


class TodoCommitted(SemanticEventEnvelope):
    kind: Literal["todo.committed"] = "todo.committed"
    payload: TodoPayload


class TodoCleared(SemanticEventEnvelope):
    kind: Literal["todo.cleared"] = "todo.cleared"
    payload: TodoPayload


class SubagentStarted(SemanticEventEnvelope):
    kind: Literal["subagent.started"] = "subagent.started"
    payload: SubagentPayload


class SubagentStepStarted(SemanticEventEnvelope):
    kind: Literal["subagent.step_started"] = "subagent.step_started"
    payload: SubagentStepPayload


class SubagentFinished(SemanticEventEnvelope):
    kind: Literal["subagent.finished"] = "subagent.finished"
    payload: SubagentFinishedPayload


class DiagnosticWarning(SemanticEventEnvelope):
    kind: Literal["diagnostic.warning"] = "diagnostic.warning"
    payload: DiagnosticPayload


class DiagnosticError(SemanticEventEnvelope):
    kind: Literal["diagnostic.error"] = "diagnostic.error"
    payload: DiagnosticPayload


class ContextPressureUpdated(SemanticEventEnvelope):
    kind: Literal["context.pressure_updated"] = "context.pressure_updated"
    payload: ContextPressureUpdatedPayload


class ContextPressureFinished(SemanticEventEnvelope):
    kind: Literal["context.pressure_finished"] = "context.pressure_finished"
    payload: ContextPressureFinishedPayload


class ContextCompacted(SemanticEventEnvelope):
    kind: Literal["context.compacted"] = "context.compacted"
    payload: ContextCompactedPayload


class GuidanceSubmitted(SemanticEventEnvelope):
    kind: Literal["guidance.submitted"] = "guidance.submitted"
    payload: GuidancePayload


class GuidanceCommitted(SemanticEventEnvelope):
    kind: Literal["guidance.committed"] = "guidance.committed"
    payload: GuidancePayload


class GuidanceApplied(SemanticEventEnvelope):
    kind: Literal["guidance.applied"] = "guidance.applied"
    payload: GuidancePayload


class InteractionRequiredPayload(ContractModel):
    request: InteractionRequest


class InteractionResolvedPayload(ContractModel):
    interaction_id: NonEmpty
    purpose: Literal["permission", "checkpoint", "clarify", "goal", "loop", "generic"]
    resolution: InteractionResolution


class InteractionRequired(SemanticEventEnvelope):
    kind: Literal["interaction.required"] = "interaction.required"
    payload: InteractionRequiredPayload

    @model_validator(mode="after")
    def matching_ownership(self):
        for field in ("session_id", "thread_id", "turn_id"):
            if getattr(self, field) != getattr(self.payload.request, field):
                raise ValueError(f"Interaction request {field} must match envelope")
        return self


class InteractionResolved(SemanticEventEnvelope):
    kind: Literal["interaction.resolved"] = "interaction.resolved"
    payload: InteractionResolvedPayload


SemanticEvent: TypeAlias = Annotated[
    TurnStarted | TurnCompleted | TurnFailed | TurnCancelled
    | AssistantStreamStarted | AssistantChunk | AssistantCommitted | AssistantDiscarded
    | ToolStarted | ToolFinished | ToolResult | FileChanged
    | StatusStarted | StatusUpdated | StatusFinished
    | TodoUpdated | TodoCommitted | TodoCleared
    | SubagentStarted | SubagentStepStarted | SubagentFinished
    | DiagnosticWarning | DiagnosticError | ContextPressureUpdated
    | ContextPressureFinished | ContextCompacted
    | GuidanceSubmitted | GuidanceCommitted | GuidanceApplied
    | InteractionRequired | InteractionResolved,
    Field(discriminator="kind"),
]
_EVENT_ADAPTER = TypeAdapter(SemanticEvent)


def _limit(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("max_event_bytes must be a positive integer")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def encode_event(event: SemanticEvent, *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> bytes:
    """Validate again and freeze the complete wire representation into bytes."""
    _limit(max_event_bytes)
    validated = _EVENT_ADAPTER.validate_python(event)
    data = validated.model_dump(mode="python")
    data["event_id"] = str(validated.event_id)
    _json_value(data)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_event_bytes:
        raise EventSizeExceeded(f"Event is {len(encoded)} bytes; limit is {max_event_bytes}")
    return encoded


def decode_event(snapshot: bytes, *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> SemanticEvent:
    """Decode a fresh consumer object, rejecting duplicate keys and non-JSON data."""
    _limit(max_event_bytes)
    if not isinstance(snapshot, bytes):
        raise ValueError("A UTF-8 bytes snapshot is required")
    if len(snapshot) > max_event_bytes:
        raise EventSizeExceeded(f"Event is {len(snapshot)} bytes; limit is {max_event_bytes}")
    data = json.loads(snapshot.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    _json_value(data)
    return _EVENT_ADAPTER.validate_python(data)


def chunk_text_event(event: AssistantChunk, *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> Iterator[AssistantChunk]:
    """Split delta at Unicode boundaries using actual JSON byte costs.

    Returned sequences are provisional consecutive values; a concurrent publisher
    must allocate from its shared turn sequence allocator before publication.
    """
    _limit(max_event_bytes)
    remaining = event.payload.delta
    sequence = event.sequence
    while True:
        event_id = uuid4()

        def candidate(text: str) -> AssistantChunk:
            return event.model_copy(update={"event_id": event_id, "sequence": sequence, "payload": event.payload.model_copy(update={"delta": text})})

        low, high = 0, len(remaining)
        encode_event(candidate(""), max_event_bytes=max_event_bytes)
        while low < high:
            mid = (low + high + 1) // 2
            try:
                encode_event(candidate(remaining[:mid]), max_event_bytes=max_event_bytes)
            except EventSizeExceeded:
                high = mid - 1
            else:
                low = mid
        if remaining and not low:
            raise EventSizeExceeded("Event envelope and one character exceed limit")
        yield candidate(remaining[:low])
        remaining = remaining[low:]
        if not remaining:
            break
        sequence += 1
