"""Permission system — aligned with opencode/Claude Code architecture."""

from voidx.permission.schema import Action, Rule, Ruleset
from voidx.permission.wildcard import match as wildcard_match
from voidx.permission.evaluate import evaluate, from_config, merge
from voidx.permission.service import PermissionService
from voidx.permission.presets import PermissionPreset, PresetDecision, resolve_preset_decision
from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag
from voidx.permission.sandbox import check_sandbox_filepath, check_sandbox_bash
from voidx.permission.engine import (
    PermissionCapability,
    PermissionContext,
    PermissionDecision,
    authorize_tool_call,
    classify_tool_call,
)

__all__ = [
    "Action", "Rule", "Ruleset",
    "wildcard_match",
    "evaluate", "from_config", "merge",
    "PermissionService",
    "PermissionPreset",
    "PresetDecision",
    "resolve_preset_decision",
    "ApprovalScope",
    "RiskAssessment",
    "RiskLevel",
    "RiskTag",
    "check_sandbox_filepath",
    "check_sandbox_bash",
    "PermissionCapability",
    "PermissionContext",
    "PermissionDecision",
    "authorize_tool_call",
    "classify_tool_call",
]
