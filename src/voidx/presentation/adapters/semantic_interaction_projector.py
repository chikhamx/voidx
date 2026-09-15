"""Pure legacy HITL projection; no transport, response registry or waiting.

UiRequest cannot encode timeout/free-text policy/global scopes or run ownership.
The detached interaction in ProjectionResult is mandatory bridge policy data:
callers must enforce it upstream, not infer permission from the legacy request.
Detached resolved events retain ownership and decisions as non-rendered metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from voidx.agent.domain import semantic_events as semantic
from voidx.agent.domain import ui_events as ui
from voidx.presentation.protocol.requests import (
    UiChoiceRequest, UiPermissionRequest, UiRequest, UiTextRequest,
)
from voidx.tooling.domain.interaction import InteractionRequest, InteractionResolution

SUPPORTED = (semantic.InteractionRequired, semantic.InteractionResolved)


@dataclass(frozen=True)
class ProjectionResult:
    events: tuple[ui.UiEvent, ...] = ()
    request: UiRequest | None = None
    interaction: InteractionRequest | None = None
    resolved: semantic.InteractionResolved | None = None
    runtime: (
        semantic.DiagnosticWarning | semantic.DiagnosticError
        | semantic.ContextPressureUpdated | semantic.ContextPressureFinished | semantic.ContextCompacted
        | semantic.GuidanceSubmitted | semantic.GuidanceCommitted | semantic.GuidanceApplied
        | None
    ) = None

    subagent: semantic.SubagentStarted | semantic.SubagentStepStarted | semantic.SubagentFinished | None = None


@dataclass
class InteractionProjectionState:
    pending: dict[str, InteractionRequest] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    checkpoint_scopes: set[str] = field(default_factory=set)


class SemanticInteractionProjector:
    def project(self, event, state: InteractionProjectionState, common: dict) -> ProjectionResult:
        if isinstance(event, semantic.InteractionRequired):
            request = InteractionRequest.model_validate(event.payload.request.model_dump())
            iid = request.interaction_id
            if iid in state.seen:
                raise ValueError("Duplicate interaction_id within turn")
            if len(state.pending) >= 64:
                raise ValueError("At most 64 pending interactions per turn")
            if request.checkpoint_stage == "scope" and request.checkpoint_id not in state.checkpoint_scopes:
                raise ValueError("Checkpoint scope requires a resolved modified choice")
            if (request.checkpoint_stage == "decision"
                    and len(state.pending) + len(state.checkpoint_scopes) >= 64):
                raise ValueError("At most 64 checkpoint associations per turn")
            result = self._required(request, common)
            if request.checkpoint_stage == "scope":
                state.checkpoint_scopes.remove(request.checkpoint_id)
            state.pending[iid] = request
            state.seen.add(iid)
            return result
        payload = event.payload
        request = state.pending.get(payload.interaction_id)
        if request is None or request.purpose != payload.purpose:
            raise ValueError("Unknown interaction or mismatched purpose")
        result = self._resolved(request, payload.resolution, common)
        x = payload.resolution
        if (request.checkpoint_stage == "decision" and x.decision == "modified"
                and x.value == "modified" and not x.free_text
                and x.resolution_reason == "answered"):
            state.checkpoint_scopes.add(request.checkpoint_id)
        del state.pending[payload.interaction_id]
        return ProjectionResult(events=tuple(result), resolved=event.model_copy(deep=True))

    def _required(self, r: InteractionRequest, common: dict) -> ProjectionResult:
        if r.secret and r.purpose != "generic":
            raise ValueError("Secret input requires generic purpose")
        if (r.input_kind == "permission") != (r.purpose == "permission"):
            raise ValueError("permission input and purpose must match")
        for purpose in ("checkpoint", "goal", "loop"):
            if (getattr(r, purpose) is not None) != (r.purpose == purpose):
                raise ValueError(f"{purpose} payload must match purpose")
        if r.input_kind != "permission" and (r.tools or r.allowed_scopes):
            raise ValueError("Permission details require permission input")
        if r.input_kind == "text" and r.choices:
            raise ValueError("Text request cannot carry choices")
        if r.input_kind != "text" and (r.default_value or r.secret):
            raise ValueError("Only text requests support default and secret")
        values = [c.value for c in r.choices]
        if len(values) != len(set(values)):
            raise ValueError("Choice values must be unique")
        choices = [(c.label, c.value, c.description) for c in r.choices]
        base = dict(request_id=r.interaction_id, thread_id=r.thread_id, prompt=r.prompt)
        events = []
        if r.input_kind == "permission":
            tools = [ui.PermissionToolDetail(**t.model_dump()) for t in r.tools]
            request = UiPermissionRequest(**base, choices=choices, tools=tools)
            events.append(ui.PermissionPromptShown(**common, request_id=r.interaction_id,
                prompt=r.prompt, choices=choices, tools=tools))
        elif r.input_kind == "text":
            request = UiTextRequest(**base, default=r.default_value, secret=r.secret)
        else:
            request = UiChoiceRequest(**base, choices=choices)
        card_choices = [c.model_dump() for c in r.choices]
        if r.purpose == "checkpoint" and r.checkpoint_stage != "scope":
            events.append(ui.CheckpointPromptShown(**common, checkpoint_id=r.checkpoint_id or r.interaction_id,
                plan=r.checkpoint.model_dump(), choices=card_choices))
        elif r.purpose in {"goal", "loop"}:
            cls = ui.GoalSpecPromptShown if r.purpose == "goal" else ui.LoopSpecPromptShown
            events.append(cls(**common, prompt_id=r.interaction_id,
                spec=getattr(r, r.purpose).model_dump(), choices=card_choices))
        elif r.purpose == "clarify":
            events.append(ui.ClarifyPromptShown(**common, clarify_id=r.interaction_id,
                question=r.prompt, options=[c.label for c in r.choices]))
        return ProjectionResult(tuple(events), request, r.model_copy(deep=True))

    def _resolved(self, r: InteractionRequest, resolution: InteractionResolution,
                  common: dict) -> list[ui.UiEvent]:
        x = resolution
        label = next((c.label for c in r.choices if c.value == x.value), x.value)
        events = []
        if r.purpose == "permission":
            events.append(ui.PermissionPromptCleared(**common, request_id=r.interaction_id))
        elif r.purpose == "checkpoint":
            events.append(ui.CheckpointDecisionSubmitted(**common, checkpoint_id=r.checkpoint_id or r.interaction_id,
                decision=x.decision, label=label, response=x.value, was_custom_input=x.free_text))
        elif r.purpose in {"goal", "loop"}:
            cls = ui.GoalSpecDecisionSubmitted if r.purpose == "goal" else ui.LoopSpecDecisionSubmitted
            events.append(cls(**common, prompt_id=r.interaction_id, decision=x.decision, response=x.value))
        elif r.purpose == "clarify":
            events.append(ui.ClarifyAnswerSubmitted(**common, clarify_id=r.interaction_id,
                answer=x.value, cancelled=x.resolution_reason != "answered", was_custom_input=x.free_text))
        return events
