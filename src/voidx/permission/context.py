"""Permission decision context models."""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.config import ApprovalPolicy, ApprovalReviewer, PermissionMode
from voidx.permission.rules import PermissionCapability
from voidx.permission.schema import Action


@dataclass(frozen=True)
class PermissionContext:
    workspace: str
    interaction_mode: str = "auto"
    permission_mode: str = PermissionMode.DEFAULT.value
    sandbox_mode: str = "workspace-write"
    sandbox_workspace_write: tuple[str, ...] = ()
    approval_policy: str = ApprovalPolicy.UNTRUSTED.value
    approval_reviewer: str = ApprovalReviewer.USER.value
    session_allow: frozenset[str] = field(default_factory=frozenset)
    session_deny: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_service(
        cls,
        service,
        *,
        workspace: str,
        interaction_mode: str | None = None,
        plan_mode: bool = False,
    ) -> "PermissionContext":
        mode = interaction_mode or "auto"
        if plan_mode:
            mode = "plan"
        return cls(
            workspace=workspace,
            interaction_mode=mode,
            permission_mode=getattr(service, "permission_mode", PermissionMode.DEFAULT.value),
            sandbox_mode=getattr(service, "sandbox_mode", "workspace-write"),
            sandbox_workspace_write=tuple(getattr(service, "sandbox_workspace_write", []) or []),
            approval_policy=getattr(service, "approval_policy", ApprovalPolicy.UNTRUSTED.value),
            approval_reviewer=getattr(service, "approval_reviewer", ApprovalReviewer.USER.value),
            session_allow=frozenset(getattr(service, "_session_allow", set())),
            session_deny=frozenset(getattr(service, "_session_deny", set())),
        )


@dataclass(frozen=True)
class PermissionDecision:
    action: Action
    tool_call: dict
    name: str
    args: dict
    pattern: str
    capability: PermissionCapability
    source: str
    reason: str = ""
    failure_check: bool = False
