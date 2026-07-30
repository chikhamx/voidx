"""Permission decision context models."""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.config import PermissionMode
from voidx.permission.grants import AccessGrants, AccessIntent
from voidx.permission.rules import PermissionCapability
from voidx.permission.risk import ApprovalScope, RiskAssessment
from voidx.permission.schema import Action


@dataclass(frozen=True)
class PermissionContext:
    workspace: str
    interaction_mode: str = "auto"
    permission_mode: str = PermissionMode.SAFE.value
    sandbox_readable_files: tuple[str, ...] = ()
    sandbox_readable_dirs: tuple[str, ...] = ()
    sandbox_writable_files: tuple[str, ...] = ()
    sandbox_writable_dirs: tuple[str, ...] = ()
    access_grants: AccessGrants = field(default_factory=AccessGrants)
    permission_state_ready: bool = True
    session_allow: frozenset[str] = field(default_factory=frozenset)
    session_deny: frozenset[str] = field(default_factory=frozenset)
    process_sandbox: object | None = None

    @property
    def sandbox_mode(self) -> str:
        try:
            preset = PermissionMode(self.permission_mode)
            return preset.sandbox_mode
        except ValueError:
            return "workspace-write"

    @property
    def approval_policy(self) -> str:
        try:
            preset = PermissionMode(self.permission_mode)
            return preset.approval_policy
        except ValueError:
            return "untrusted"


    def __post_init__(self) -> None:
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
            permission_mode=getattr(service, "permission_mode", PermissionMode.SAFE.value),
            sandbox_readable_files=tuple(getattr(service, "sandbox_readable_files", []) or []),
            sandbox_readable_dirs=tuple(getattr(service, "sandbox_readable_dirs", []) or []),
            sandbox_writable_files=tuple(getattr(service, "sandbox_writable_files", []) or []),
            sandbox_writable_dirs=tuple(getattr(service, "sandbox_writable_dirs", []) or []),
            access_grants=getattr(service, "get_access_grants", lambda: AccessGrants())(),
            permission_state_ready=getattr(service, "permission_state_ready", True),
            session_allow=frozenset(getattr(service, "_session_allow", set())),
            session_deny=frozenset(getattr(service, "_session_deny", set())),
            process_sandbox=getattr(service, "process_sandbox", None),
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
    risk: RiskAssessment | None = None
    allowed_scopes: tuple[ApprovalScope, ...] = ()
    default_scope: ApprovalScope | None = None
    ai_approval_failure: str = ""
    access_intents: tuple["AccessIntent", ...] = ()

    @property
    def primary_access_intent(self) -> "AccessIntent | None":
        return self.access_intents[0] if len(self.access_intents) == 1 else None
