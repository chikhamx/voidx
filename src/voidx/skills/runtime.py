"""Compatibility aliases for workflow runtime state.

Workflow runtime now lives in :mod:`voidx.workflow.runtime`.
"""

from __future__ import annotations

from voidx.workflow.runtime import (
    SkillActivationSource,
    SkillEvidence,
    SkillRunState,
    SkillRunStatus,
    SkillStateEvent,
    SkillStateEventKind,
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_skill_states,
    advance_workflow_states,
    source_from_reason,
)

__all__ = [
    "SkillActivationSource",
    "SkillEvidence",
    "SkillRunState",
    "SkillRunStatus",
    "SkillStateEvent",
    "SkillStateEventKind",
    "WorkflowActivationSource",
    "WorkflowEvidence",
    "WorkflowRunState",
    "WorkflowRunStatus",
    "WorkflowStateEvent",
    "WorkflowStateEventKind",
    "advance_skill_states",
    "advance_workflow_states",
    "source_from_reason",
]
