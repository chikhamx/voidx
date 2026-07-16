"""Mode resolver for ask-first permission decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from voidx.config import PermissionMode
from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag
from voidx.permission.constants import PROJECT_TRUSTED_DENY_TAGS

ModeAction = Literal["allow", "ask", "blocked_ack"]


@dataclass(frozen=True)
class ModeDecision:
    action: ModeAction
    risk: RiskAssessment
    allowed_scopes: tuple[ApprovalScope, ...] = ()
    default_scope: ApprovalScope | None = None


def resolve_mode_decision(preset: PermissionMode, risk: RiskAssessment) -> ModeDecision:
    if risk.level == RiskLevel.BLOCKED:
        return ModeDecision(action="blocked_ack", risk=risk)
    if risk.level == RiskLevel.NORMAL:
        return ModeDecision(action="allow", risk=risk)
    if risk.level == RiskLevel.EXTREME:
        if preset == PermissionMode.PROJECT_TRUSTED and _project_trusted_allows(risk):
            return ModeDecision(action="allow", risk=risk)
        if preset == PermissionMode.FULL_ACCESS and not _full_access_asks(risk):
            return ModeDecision(action="allow", risk=risk)
        return _ask_once(risk)
    if preset == PermissionMode.READ_ONLY:
        return _ask_once(risk)
    if preset == PermissionMode.SAFE:
        return _ask_scoped(risk, (ApprovalScope.ONCE, ApprovalScope.SESSION))
    if preset == PermissionMode.PROJECT_TRUSTED:
        if _project_trusted_allows(risk):
            return ModeDecision(action="allow", risk=risk)
        return _ask_scoped(risk, (ApprovalScope.ONCE, ApprovalScope.SESSION, ApprovalScope.PROJECT))
    if preset == PermissionMode.FULL_ACCESS:
        if _full_access_asks(risk):
            return _ask_once(risk)
        return ModeDecision(action="allow", risk=risk)
    return _ask_once(risk)


def _ask_once(risk: RiskAssessment) -> ModeDecision:
    return ModeDecision(action="ask", risk=risk, allowed_scopes=(ApprovalScope.ONCE,), default_scope=ApprovalScope.ONCE)


def _ask_scoped(risk: RiskAssessment, scopes: tuple[ApprovalScope, ...]) -> ModeDecision:
    return ModeDecision(action="ask", risk=risk, allowed_scopes=scopes, default_scope=ApprovalScope.ONCE)




def _project_trusted_allows(risk: RiskAssessment) -> bool:
    return not (set(risk.tags) & PROJECT_TRUSTED_DENY_TAGS)


def _full_access_asks(risk: RiskAssessment) -> bool:
    return any(
        tag in risk.tags
        for tag in {
            RiskTag.EXTERNAL_PATH,
            RiskTag.GIT_PUSH,
            RiskTag.SYSTEM_DESTRUCTIVE,
            RiskTag.PRIVILEGE_ESCALATION,
        }
    )
