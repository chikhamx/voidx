"""Runtime-only compatibility projection; no publishing or inferred UI commands.

Pressure requires an observed injection, unlike legacy defensive orphan finishes.
A finished ID cannot restart within a turn. hint_present may report a soft
 decision while the existing hint remains hard (legacy upsert behavior).
Guidance has no correlation ID: batches, identical texts, standalone commits and
system preview clears are legal; sequence validation belongs to the owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from voidx.agent.domain import semantic_events as semantic
from voidx.agent.domain import ui_events as ui
from voidx.presentation.adapters.semantic_interaction_projector import ProjectionResult

SUPPORTED = (
    semantic.DiagnosticWarning, semantic.DiagnosticError, semantic.ContextCompacted,
    semantic.ContextPressureUpdated, semantic.ContextPressureFinished,
    semantic.GuidanceSubmitted, semantic.GuidanceCommitted, semantic.GuidanceApplied,
)


@dataclass
class RuntimeProjectionState:
    pressure: dict[str, Literal["soft", "hard"]] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)


class SemanticRuntimeProjector:
    def project(self, event, state: RuntimeProjectionState, common: dict) -> ProjectionResult:
        payload = event.payload
        if isinstance(event, (semantic.DiagnosticWarning, semantic.DiagnosticError)):
            cls = ui.WarningAppended if isinstance(event, semantic.DiagnosticWarning) else ui.ErrorAppended
            output = (cls(**common, message=payload.summary),)
        elif isinstance(event, semantic.ContextPressureUpdated):
            previous = state.pressure.get(payload.pressure_id)
            if payload.pressure_id in state.finished:
                raise ValueError("Pressure already finished within turn")
            if payload.outcome == "hint_injected":
                if previous is not None:
                    raise ValueError("Duplicate pressure injection")
            elif payload.outcome == "hint_upgraded":
                if previous != "soft" or payload.level != "hard":
                    raise ValueError("Pressure upgrade requires soft to hard")
            elif previous is None or (previous == "soft" and payload.level == "hard"):
                raise ValueError("Pressure present requires an existing compatible hint")
            output = (ui.ContextPressureUpdated(**common, **payload.model_dump()),)
            state.pressure[payload.pressure_id] = previous if payload.outcome == "hint_present" else payload.level
        elif isinstance(event, semantic.ContextPressureFinished):
            if state.pressure.get(payload.pressure_id) != payload.level:
                raise ValueError("Unknown pressure or mismatched finish level")
            output = (ui.ContextPressureFinished(**common, **payload.model_dump()),)
            del state.pressure[payload.pressure_id]
            state.finished.add(payload.pressure_id)
        elif isinstance(event, semantic.GuidanceSubmitted):
            output = (ui.GuidanceSubmitted(**common, text=payload.text, truncated=payload.truncated),)
        elif isinstance(event, semantic.GuidanceCommitted):
            output = (ui.GuidanceCommitted(**common, **payload.model_dump()),)
        elif isinstance(event, (semantic.GuidanceApplied, semantic.ContextCompacted)):
            output = ()
        else:
            raise NotImplementedError(f"Unsupported semantic kind: {event.kind}")
        return ProjectionResult(events=output, runtime=event.model_copy(deep=True))
