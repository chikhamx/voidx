"""Opt-in semantic turn/assistant projection; callers route each turn separately.

Sequence numbers are contiguous from 1 per (session, thread, turn). Terminal
history is not retained: replay detection across retired turns belongs upstream.
Legacy turn events have no usage/reason/envelope slots; turn failures are encoded
in message. Runtime metadata requires project_result. Raw input, absent from the semantic DTO, can be supplied at start.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from voidx.agent.domain import semantic_events as semantic
from voidx.agent.domain import ui_events as ui
from voidx.agent.domain.display_policy import ToolDisplayPolicy
from voidx.presentation.adapters.semantic_tool_projector import (
    SUPPORTED as TOOL_SUPPORTED, SemanticToolProjector, StatusPolicy,
    ToolLabel, ToolProjectionState,
)


from voidx.presentation.adapters.semantic_interaction_projector import (
    SUPPORTED as INTERACTION_SUPPORTED, InteractionProjectionState,
    ProjectionResult, SemanticInteractionProjector,
)


from voidx.presentation.adapters.semantic_runtime_projector import (
    SUPPORTED as RUNTIME_SUPPORTED, RuntimeProjectionState, SemanticRuntimeProjector,
)


from voidx.presentation.adapters.semantic_subagent_projector import (
    SUPPORTED as SUBAGENT_SUPPORTED, SemanticSubagentProjector,
    SubagentProjectionState, SubagentResolver,
)


@dataclass
class _Phase:
    text: str = ""
    status: str = "active"


@dataclass
class _Stream:
    phases: dict[str, _Phase] = field(default_factory=dict)
    closed: bool = False


@dataclass
class _Turn:
    agent_id: int | None
    sequence: int = 1
    streams: dict[str, _Stream] = field(default_factory=dict)
    tools: ToolProjectionState = field(default_factory=ToolProjectionState)
    interactions: InteractionProjectionState = field(default_factory=InteractionProjectionState)
    subagents: SubagentProjectionState = field(default_factory=SubagentProjectionState)
    runtime: RuntimeProjectionState = field(default_factory=RuntimeProjectionState)


_SUPPORTED = (
    semantic.TurnStarted, semantic.TurnCompleted, semantic.TurnFailed,
    semantic.TurnCancelled, semantic.AssistantStreamStarted,
    semantic.AssistantChunk, semantic.AssistantCommitted, semantic.AssistantDiscarded,
)


class SemanticEventProjector:
    """Return ordered legacy events without publishing or changing default routes."""

    def __init__(self, *, display_policy: ToolDisplayPolicy | None = None,
                 tool_label: ToolLabel | None = None,
                 status_policy: StatusPolicy | None = None,
                 subagent_resolver: SubagentResolver | None = None) -> None:
        self._subagents = SemanticSubagentProjector(subagent_resolver)
        self._interactions = SemanticInteractionProjector()
        self._runtime = SemanticRuntimeProjector()
        self._turns: dict[tuple[str, str, str], _Turn] = {}
        self._tools = SemanticToolProjector(display_policy=display_policy,
            tool_label=tool_label, status_policy=status_policy)

    @property
    def active_turn_count(self) -> int:
        return len(self._turns)

    @property
    def pending_interaction_count(self) -> int:
        return sum(len(t.interactions.pending) for t in self._turns.values())

    def project(
        self, event: semantic.SemanticEvent, *, raw_text: str | None = None,
    ) -> tuple[ui.UiEvent, ...]:
        if isinstance(event, INTERACTION_SUPPORTED + RUNTIME_SUPPORTED + SUBAGENT_SUPPORTED):
            raise ValueError("Interaction/runtime/subagent events carry metadata; use project_result")
        return self.project_result(event, raw_text=raw_text).events

    def project_result(
        self, event: semantic.SemanticEvent, *, raw_text: str | None = None,
    ) -> ProjectionResult:
        if type(event) not in _SUPPORTED + TOOL_SUPPORTED + INTERACTION_SUPPORTED + RUNTIME_SUPPORTED + SUBAGENT_SUPPORTED:
            raise NotImplementedError(f"Unsupported semantic kind: {event.kind}")
        if raw_text is not None and not isinstance(event, semantic.TurnStarted):
            raise ValueError("raw_text is only valid for turn.started")
        if event.agent_id is not None and type(event.agent_id) is not int:
            raise ValueError("Legacy agent_id requires an integer or None")
        key = (event.session_id, event.thread_id, event.turn_id)
        common = dict(thread_id=event.thread_id, agent_id=event.agent_id if event.agent_id is not None else -1)
        if isinstance(event, semantic.TurnStarted):
            if key in self._turns or event.sequence != 1:
                raise ValueError("turn must start once at sequence 1")
            output = ui.TurnStarted(**common, text=event.payload.text,
                                    raw_text=raw_text if raw_text is not None else "",
                                    metadata=event.payload.metadata)
            self._turns[key] = _Turn(agent_id=event.agent_id)
            return ProjectionResult(events=(output,))
        current = self._turns.get(key)
        if current is None:
            raise ValueError("No active turn")
        if event.sequence != current.sequence + 1:
            raise ValueError("Expected the next contiguous sequence")
        if event.agent_id != current.agent_id:
            raise ValueError("agent_id changed within turn")
        if isinstance(event, semantic.InteractionRequired):
            request = event.payload.request
            if any(getattr(request, name) != getattr(event, name)
                   for name in ("session_id", "thread_id", "turn_id")):
                raise ValueError("Interaction ownership does not match event")
            for active in self._turns.values():
                if request.interaction_id in active.interactions.seen:
                    raise ValueError("Legacy interaction_id collides across active turns")
                if request.purpose == "permission" and any(
                    r.purpose == "permission" for r in active.interactions.pending.values()
                ):
                    raise ValueError("Legacy permission status has only one slot")
        if isinstance(event, semantic.SubagentStarted):
            if any(event.payload.subagent_id in t.subagents.children for t in self._turns.values()):
                raise ValueError("Legacy subagent_id collides across active turns")
        # Failed validation or DTO construction must leave the retry position intact.
        turn = deepcopy(current)
        result = ProjectionResult()
        output: list[ui.UiEvent] = []
        terminal = isinstance(event, (semantic.TurnCompleted, semantic.TurnFailed, semantic.TurnCancelled))
        if terminal:
            turn.subagents.validate_terminal()
            if turn.interactions.pending:
                raise ValueError("Cannot end turn with pending interactions")
            if isinstance(event, semantic.TurnCompleted):
                turn.tools.validate_complete()
            for stream_id, stream in turn.streams.items():
                if stream.closed:
                    continue
                if isinstance(event, semantic.TurnCompleted):
                    if any(p.status == "active" for p in stream.phases.values()):
                        raise ValueError("Cannot complete turn with active phases")
                    output.extend(self._finish(stream_id, stream, common))
                else:
                    output.append(ui.AssistantStreamDiscarded(**common, stream_id=stream_id))
            if isinstance(event, semantic.TurnFailed):
                output.append(ui.TurnFailed(**common, message=json.dumps(event.payload.model_dump(), ensure_ascii=False)))
            elif isinstance(event, semantic.TurnCancelled):
                output.append(ui.TurnCancelled(**common))
            else:
                output.append(ui.TurnCompleted(**common))
        elif type(event) in SUBAGENT_SUPPORTED:
            reserved_ids = {t.agent_id for t in self._turns.values() if t.agent_id is not None}
            reserved_ids.update(c.agent_id for t in self._turns.values() for c in t.subagents.children.values())
            result = self._subagents.project(event, turn.subagents, turn.tools, common, reserved_ids)
            output.extend(result.events)
        elif type(event) in RUNTIME_SUPPORTED:
            result = self._runtime.project(event, turn.runtime, common)
            output.extend(result.events)
        elif type(event) in INTERACTION_SUPPORTED:
            result = self._interactions.project(event, turn.interactions, common)
            output.extend(result.events)
        elif type(event) in TOOL_SUPPORTED:
            output.extend(self._tools.project(event, turn.tools, common))
        else:
            output.extend(self._assistant(event, turn, common))
        if terminal:
            del self._turns[key]
        else:
            turn.sequence = event.sequence
            self._turns[key] = turn
        return ProjectionResult(tuple(output), result.request, result.interaction, result.resolved, result.runtime, result.subagent)

    def _assistant(self, event, turn: _Turn, common: dict) -> list[ui.UiEvent]:
        payload = event.payload
        stream_id, phase = payload.stream_id, payload.phase
        stream = turn.streams.get(stream_id)
        output = []
        if isinstance(event, semantic.AssistantStreamStarted):
            if stream is None:
                for old_id, old in turn.streams.items():
                    if old.closed:
                        continue
                    if any(p.status == "active" for p in old.phases.values()):
                        raise ValueError("Legacy consumer cannot overlap stream ids in one turn")
                    output.extend(self._finish(old_id, old, common))
                stream = turn.streams[stream_id] = _Stream()
                output.append(ui.AssistantStreamStarted(**common, stream_id=stream_id))
            if stream.closed or phase in stream.phases:
                raise ValueError("Stream/phase already started or closed")
            stream.phases[phase] = _Phase()
            return output
        if stream is None or stream.closed or phase not in stream.phases:
            raise ValueError("Stream phase must be started and open")
        part = stream.phases[phase]
        if part.status != "active":
            raise ValueError("Stream phase already ended")
        if isinstance(event, semantic.AssistantChunk):
            part.text += payload.delta
            return [self._update(stream_id, phase, part.text, common)]
        if isinstance(event, semantic.AssistantCommitted):
            if payload.text is None:
                raise ValueError("Unresolved result_ref: final text is required")
            part.text = payload.text
            part.status = "committed"
            output.append(self._update(stream_id, phase, part.text, common))
        else:
            part.text = ""
            part.status = "discarded"
            # Replace the visible discarded phase, including when another is active.
            survivor = next((name for name in ("text", "thinking")
                             if name in stream.phases and stream.phases[name].status != "discarded"), phase)
            output.append(self._update(stream_id, survivor, stream.phases[survivor].text, common))
        ended = all(p.status != "active" for p in stream.phases.values())
        if ended and ("text" in stream.phases or all(p.status == "discarded" for p in stream.phases.values())):
            output.extend(self._finish(stream_id, stream, common))
        return output

    @staticmethod
    def _update(stream_id: str, phase: str, text: str, common: dict) -> ui.AssistantStreamUpdated:
        return ui.AssistantStreamUpdated(**common, stream_id=stream_id, phase=phase,
                                         text=text, snapshot_contract="cumulative")

    def _finish(self, stream_id: str, stream: _Stream, common: dict) -> list[ui.UiEvent]:
        phase = next((name for name in ("text", "thinking")
                      if name in stream.phases and stream.phases[name].status == "committed"), None)
        output = []
        if phase is None:
            output.append(ui.AssistantStreamDiscarded(**common, stream_id=stream_id))
        else:
            output.append(self._update(stream_id, phase, stream.phases[phase].text, common))
            output.append(ui.AssistantStreamCommitted(**common, stream_id=stream_id))
        stream.closed = True
        stream.phases.clear()
        return output
