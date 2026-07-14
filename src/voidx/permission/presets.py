"""Preset resolver for ask-first permission decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from voidx.config import PermissionPreset
from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag

PresetAction = Literal["allow", "ask", "blocked_ack"]


@dataclass(frozen=True)
class PresetDecision:
    action: PresetAction
    risk: RiskAssessment
    allowed_scopes: tuple[ApprovalScope, ...] = ()
    default_scope: ApprovalScope | None = None


def resolve_preset_decision(preset: PermissionPreset, risk: RiskAssessment) -> PresetDecision:
    if risk.level == RiskLevel.BLOCKED:
        return PresetDecision(action="blocked_ack", risk=risk)
    if risk.level == RiskLevel.NORMAL:
        return PresetDecision(action="allow", risk=risk)
    if risk.level == RiskLevel.EXTREME:
        return _ask_once(risk)
    if preset == PermissionPreset.READ_ONLY:
        return _ask_once(risk)
    if preset == PermissionPreset.SAFE:
        return _ask_scoped(risk, (ApprovalScope.ONCE, ApprovalScope.SESSION))
    if preset == PermissionPreset.PROJECT_TRUSTED:
        if _project_trusted_allows(risk):
            return PresetDecision(action="allow", risk=risk)
        return _ask_scoped(risk, (ApprovalScope.ONCE, ApprovalScope.SESSION, ApprovalScope.PROJECT))
    if preset == PermissionPreset.FULL_ACCESS:
        if _full_access_asks(risk):
            return _ask_once(risk)
        return PresetDecision(action="allow", risk=risk)
    return _ask_once(risk)


def _ask_once(risk: RiskAssessment) -> PresetDecision:
    return PresetDecision(action="ask", risk=risk, allowed_scopes=(ApprovalScope.ONCE,), default_scope=ApprovalScope.ONCE)


def _ask_scoped(risk: RiskAssessment, scopes: tuple[ApprovalScope, ...]) -> PresetDecision:
    return PresetDecision(action="ask", risk=risk, allowed_scopes=scopes, default_scope=ApprovalScope.ONCE)


def _project_trusted_allows(risk: RiskAssessment) -> bool:
    return bool(risk.tags) and set(risk.tags).issubset({RiskTag.WORKSPACE_EDIT, RiskTag.SAFE_READ})


def _full_access_asks(risk: RiskAssessment) -> bool:
    return any(
        tag in risk.tags
        for tag in {
            RiskTag.EXTERNAL_PATH,
            RiskTag.NETWORK,
            RiskTag.GIT_PUSH,
            RiskTag.SYSTEM_DESTRUCTIVE,
            RiskTag.PRIVILEGE_ESCALATION,
        }
    )
