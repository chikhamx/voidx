"""Resolved authorization values and permission decision DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.tooling.domain.grants import AccessGrants, AccessIntent
from voidx.tooling.domain.permission import Action
from voidx.tooling.domain.risk import ApprovalScope, RiskAssessment


@dataclass(frozen=True)
class AuthorizationContext:
    permission_mode: str = "safe"
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "untrusted"


@dataclass(frozen=True)
class PermissionContext:
    workspace: str
    interaction_mode: str = "auto"
    permission_mode: str = "safe"
    authorization: AuthorizationContext | None = None
    sandbox_readable_files: tuple[str, ...] = ()
    sandbox_readable_dirs: tuple[str, ...] = ()
    sandbox_writable_files: tuple[str, ...] = ()
    sandbox_writable_dirs: tuple[str, ...] = ()
    access_grants: AccessGrants = field(default_factory=AccessGrants)
    permission_state_ready: bool = True
    session_allow: frozenset[str] = field(default_factory=frozenset)
    session_deny: frozenset[str] = field(default_factory=frozenset)
    process_sandbox: object | None = None

    def __post_init__(self) -> None:
        authorization = self.authorization
        if authorization is None:
            authorization = AuthorizationContext(
                permission_mode=self.permission_mode,
                sandbox_mode=_sandbox_mode(self.permission_mode),
                approval_policy=_approval_policy(self.permission_mode),
            )
            object.__setattr__(self, "authorization", authorization)
        else:
            object.__setattr__(self, "permission_mode", authorization.permission_mode)
        if self.access_grants == AccessGrants() and any((
            self.sandbox_readable_files,
            self.sandbox_readable_dirs,
            self.sandbox_writable_files,
            self.sandbox_writable_dirs,
        )):
            object.__setattr__(
                self,
                "access_grants",
                AccessGrants.from_parts(
                    readable_files=self.sandbox_readable_files,
                    readable_dirs=self.sandbox_readable_dirs,
                    writable_files=self.sandbox_writable_files,
                    writable_dirs=self.sandbox_writable_dirs,
                ),
            )

    @property
    def sandbox_mode(self) -> str:
        assert self.authorization is not None
        return self.authorization.sandbox_mode

    @property
    def approval_policy(self) -> str:
        assert self.authorization is not None
        return self.authorization.approval_policy


def _sandbox_mode(permission_mode: str) -> str:
    return {
        "safe": "workspace-write",
        "read_only": "read-only",
        "full_access": "danger-full-access",
        "ai_approval": "workspace-write",
        "project_trusted": "workspace-write",
    }.get(permission_mode, "workspace-write")


def _approval_policy(permission_mode: str) -> str:
    return "trusted" if permission_mode == "full_access" else "untrusted"


@dataclass(frozen=True)
class PermissionDecision:
    action: Action
    tool_call: dict
    name: str
    args: dict
    pattern: str
    capability: object
    source: str = "preset"
    reason: str = ""
    failure_check: bool = False
    risk: RiskAssessment | None = None
    allowed_scopes: tuple[ApprovalScope, ...] = ()
    default_scope: ApprovalScope | None = None
    ai_approval_failure: str = ""
    access_intents: tuple[AccessIntent, ...] = ()

    @property
    def primary_access_intent(self) -> AccessIntent | None:
        return self.access_intents[0] if len(self.access_intents) == 1 else None


__all__ = ["AuthorizationContext", "PermissionContext", "PermissionDecision"]
