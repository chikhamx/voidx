"""Domain contracts for autonomous Goal execution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import (
    GOAL_EVALUATOR_DIRECTIVE,
    GOAL_IDLE_DIRECTIVE,
    GOAL_INTAKE_DIRECTIVE,
    GoalPromptPolicy,
)
from voidx.agent.domain.thread import LifecycleState
from voidx.agent.domain.tool_view import BoundToolView


GoalPhase = Literal["init", "checkpoint", "decision"]
GoalProtocolStatus = Literal["submitted", "projected"]
GoalDecisionStatus = Literal["finished", "continue", "blocked"]
GoalProgress = Literal["none", "partial", "meaningful"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def goal_sequence_number(phase: GoalPhase | str, attempt_number: int) -> int:
    """Return the only valid journal position for one Goal phase."""
    if phase == "init":
        if attempt_number != 0:
            raise ValueError("init must use attempt_number=0")
        return 0
    if phase == "checkpoint":
        if attempt_number < 1:
            raise ValueError("checkpoint attempt_number must be positive")
        return attempt_number * 2 - 1
    if phase == "decision":
        if attempt_number < 1:
            raise ValueError("decision attempt_number must be positive")
        return attempt_number * 2
    raise ValueError(f"unknown Goal protocol phase: {phase}")


def goal_phase_session_id(generation: str, phase: Literal["work", "evaluator"]) -> str:
    """Return the stable opaque session id for one Goal generation phase."""
    if not generation.strip():
        raise ValueError("generation must not be empty")
    digest = hashlib.sha256(generation.encode("utf-8")).hexdigest()[:32]
    return f"goal-{phase}-{digest}"


def is_goal_terminal(lifecycle: LifecycleState | str) -> bool:
    """Goal terminal semantics are separate from generic thread semantics."""
    value = lifecycle.value if isinstance(lifecycle, LifecycleState) else str(lifecycle)
    return value in {"completed", "blocked", "failed", "cancelled"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class GoalSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = Field(default=20, ge=1, le=200)
    workflow_enabled: bool = False
    generation: str = "active"

    @field_validator("objective", "acceptance_condition", "generation")
    @classmethod
    def require_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("achievement_method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.strip()

    def goal_thread_id(self, parent_thread_id: str | None) -> str:
        parent = (parent_thread_id or "default").strip() or "default"
        return f"goal:{parent}:{self.generation}"

    def goal_session_id(self, parent_thread_id: str | None) -> str:
        return self.goal_thread_id(parent_thread_id)

    def objective_summary(self) -> str:
        return self.objective.replace("\n", " ")[:80]


class GoalSpecSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = Field(default=20, ge=1, le=200)
    workflow_enabled: bool = False
    generation: str
    parent_session_id: str
    parent_thread_id: str = ""
    workspace: str = ""
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_snapshot: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_spec(
        cls,
        spec: GoalSpec,
        *,
        parent_session_id: str,
        parent_thread_id: str = "",
        workspace: str = "",
        profile_snapshot: dict[str, Any] | None = None,
        model_snapshot: dict[str, Any] | None = None,
    ) -> "GoalSpecSnapshot":
        return cls(
            objective=spec.objective,
            acceptance_condition=spec.acceptance_condition,
            achievement_method=spec.achievement_method,
            max_attempts=spec.max_attempts,
            workflow_enabled=spec.workflow_enabled,
            generation=spec.generation,
            parent_session_id=parent_session_id,
            parent_thread_id=parent_thread_id,
            workspace=workspace,
            profile_snapshot=profile_snapshot or {},
            model_snapshot=model_snapshot or {},
        )

    def to_spec(self) -> GoalSpec:
        return GoalSpec(
            objective=self.objective,
            acceptance_condition=self.acceptance_condition,
            achievement_method=self.achievement_method,
            max_attempts=self.max_attempts,
            workflow_enabled=self.workflow_enabled,
            generation=self.generation,
        )


class WorkCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    generation: str
    attempt_number: int = Field(ge=1)
    source: Literal["model", "runtime_fallback"] = "model"
    completeness: Literal["complete", "incomplete"] = "complete"
    summary: str
    evidence: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    next_hint: str = ""
    progress: GoalProgress = "none"
    work_turn_id: str
    observed_assistant_summary: str = ""
    observed_tool_result_summaries: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=_now)

    @field_validator("generation", "summary", "work_turn_id")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator(
        "evidence",
        "changed_files",
        "verification",
        "observed_tool_result_summaries",
        mode="before",
    )
    @classmethod
    def normalize_text_list(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())


class GoalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    generation: str
    attempt_number: int = Field(ge=1)
    status: GoalDecisionStatus
    summary: str
    evidence: tuple[str, ...] = ()
    reason: str = ""
    next_hint: str = ""
    missing_evidence: tuple[str, ...] = ()
    progress: GoalProgress = "none"
    created_at: datetime = Field(default_factory=_now)

    @field_validator("generation", "summary")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("reason", "next_hint")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("evidence", "missing_evidence", mode="before")
    @classmethod
    def normalize_text_list(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())


class GoalProtocolRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol_id: str
    parent_session_id: str
    generation: str
    phase: GoalPhase
    attempt_number: int = Field(ge=0)
    sequence_number: int = Field(ge=0)
    turn_id: str
    session_id: str
    payload_type: Literal["GoalSpecSnapshot", "WorkCheckpoint", "GoalDecision"]
    payload: dict[str, Any]
    status: GoalProtocolStatus = "submitted"
    payload_hash: str
    submitted_at: datetime = Field(default_factory=_now)
    projected_at: datetime | None = None

    @classmethod
    def submitted(
        cls,
        *,
        protocol_id: str,
        parent_session_id: str,
        generation: str,
        phase: GoalPhase,
        attempt_number: int,
        turn_id: str,
        session_id: str,
        payload: BaseModel,
    ) -> "GoalProtocolRecord":
        sequence_number = goal_sequence_number(phase, attempt_number)
        payload_json = _canonical_json(payload.model_dump(mode="json"))
        payload_type = type(payload).__name__
        expected_type = {
            "init": "GoalSpecSnapshot",
            "checkpoint": "WorkCheckpoint",
            "decision": "GoalDecision",
        }[phase]
        if payload_type != expected_type:
            raise ValueError(f"{phase} requires {expected_type}, got {payload_type}")
        return cls(
            protocol_id=protocol_id,
            parent_session_id=parent_session_id,
            generation=generation,
            phase=phase,
            attempt_number=attempt_number,
            sequence_number=sequence_number,
            turn_id=turn_id,
            session_id=session_id,
            payload_type=payload_type,
            payload=payload.model_dump(mode="json"),
            payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        )

    def payload_model(self) -> GoalSpecSnapshot | WorkCheckpoint | GoalDecision:
        model = {
            "GoalSpecSnapshot": GoalSpecSnapshot,
            "WorkCheckpoint": WorkCheckpoint,
            "GoalDecision": GoalDecision,
        }[self.payload_type]
        return model.model_validate(self.payload)

    def projected(self, *, projected_at: datetime | None = None) -> "GoalProtocolRecord":
        if self.status == "projected":
            return self
        return self.model_copy(
            update={
                "status": "projected",
                "projected_at": projected_at or _now(),
            }
        )


class PublicSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    generation: str
    phase: Literal["work", "evaluator", "runtime"]
    outcome: Literal["completed", "blocked", "failed"]
    objective_summary: str
    attempt_count: int = Field(ge=0)
    summary: str
    created_at: datetime = Field(default_factory=_now)


class GoalRuntimeFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    generation: str
    observed_sequence: int = Field(ge=-1)
    reason: str
    evidence: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=_now)


class UserGuidance(BaseModel):
    model_config = ConfigDict(frozen=True)

    guidance_id: str
    generation: str
    target_phase: Literal["work", "evaluator", "any"] = "any"
    text: str
    source: Literal["user", "system"] = "user"
    created_at: datetime = Field(default_factory=_now)
    delivered_attempt_id: str | None = None
    delivered_phase: Literal["work", "evaluator"] | None = None
    consumed_at: datetime | None = None

    @field_validator("guidance_id", "generation", "text")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class GoalGenerationBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    generation: str
    main_session_id: str
    evaluator_session_id: str
    work_session_id: str
    goal_thread_id: str | None = None
    visibility: Literal["internal"] = "internal"
    created_at: datetime = Field(default_factory=_now)
    terminal_at: datetime | None = None
    archived_at: datetime | None = None


GOAL_ITERATION_USER_TEXT = "Start the autonomous goal attempt."
GOAL_PROFILE = RuntimeProfile(
    profile_id="goal", revision=1, name="Goal", protocol="goal",
    prompt_policy=GoalPromptPolicy(),
)



class GoalState(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = Field(ge=1, le=200)
    attempt_count: int = Field(default=0, ge=0)
    evaluator_failure_count: int = Field(default=0, ge=0)
    last_progress_key: str = ""
    repeated_progress_count: int = Field(default=0, ge=0)
    last_evaluator_summary: str = ""
    last_evaluator_next_hint: str = ""
    last_evaluator_missing: tuple[str, ...] = ()
    blocked_reason: str = ""
    generation: str = ""
    main_session_id: str = ""
    work_session_id: str = ""
    evaluator_session_id: str = ""
    projected_sequence_number: int = -1
    current_phase: Literal["work", "evaluator"] = "work"
    phase_status: Literal["running", "needs_resume"] = "running"
    last_work_checkpoint: WorkCheckpoint | None = None
    last_protocol_id: str = ""
    interrupt_reason: str = ""
    protocol_repair_count: int = Field(default=0, ge=0)

    @classmethod
    def from_spec(
        cls,
        spec: GoalSpec,
        *,
        run_id: str,
        main_session_id: str = "",
        work_session_id: str = "",
        evaluator_session_id: str = "",
    ) -> "GoalState":
        return cls(
            run_id=run_id,
            objective=spec.objective,
            acceptance_condition=spec.acceptance_condition,
            achievement_method=spec.achievement_method,
            max_attempts=spec.max_attempts,
            generation=spec.generation,
            main_session_id=main_session_id,
            work_session_id=work_session_id,
            evaluator_session_id=evaluator_session_id,
        )


class GoalToolView(BoundToolView):
    workflow_enabled: bool = False
    phase: str = "work"

    @classmethod
    def default(cls, *, workflow_enabled: bool = False, phase: str = "work") -> "GoalToolView":
        return cls(workflow_enabled=workflow_enabled, phase=phase)

    def bind(self, available_tool_ids: set[str] | list[str] | tuple[str, ...]) -> "GoalToolView":
        readonly = {
            "read", "find", "search", "lsp", "document", "websearch", "webfetch",
            "mcp", "skill",
        }
        if self.phase == "work":
            allowed = readonly | {
                "bash", "powershell", "write", "replace", "manage", "git",
                "agent", "agent_control", "workflow", "todo", "goal_checkpoint",
            }
        elif self.phase == "intake":
            allowed = (readonly - {"websearch", "webfetch", "mcp", "skill"}) | {
                "clarify", "goal_init",
            }
        elif self.phase == "evaluator":
            allowed = (readonly - {"websearch", "webfetch", "mcp", "skill"}) | {
                "goal_decision",
            }
        elif self.phase == "idle":
            allowed = (readonly - {"websearch", "webfetch", "mcp", "skill"}) | {
                "clarify", "goal_init",
            }
        else:
            allowed = set()
        return self.model_copy(update={"bound_tool_ids": frozenset(set(available_tool_ids) & allowed)})


__all__ = [
    "GoalDecision",
    "GoalGenerationBinding",
    "GoalProtocolRecord",
    "GoalRuntimeFailure",
    "GoalSpec",
    "GoalSpecSnapshot",
    "GoalState",
    "GoalToolView",
    "UserGuidance",
    "WorkCheckpoint",
    "GOAL_EVALUATOR_DIRECTIVE",
    "GOAL_IDLE_DIRECTIVE",
    "GOAL_INTAKE_DIRECTIVE",
    "GOAL_ITERATION_USER_TEXT",
    "GOAL_PROFILE",
    "goal_phase_session_id",
    "goal_sequence_number",
    "is_goal_terminal",
]
