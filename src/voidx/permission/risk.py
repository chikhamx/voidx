"""Risk classification models for ask-first permissions."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class RiskLevel(str, Enum):
    NORMAL = "normal"
    DANGEROUS = "dangerous"
    EXTREME = "extreme"
    BLOCKED = "blocked"


class RiskTag(str, Enum):
    SAFE_READ = "safe_read"
    WORKSPACE_EDIT = "workspace_edit"
    DYNAMIC_SHELL = "dynamic_shell"
    NESTED_INTERPRETER = "nested_interpreter"
    EXTERNAL_PATH = "external_path"
    NETWORK = "network"
    DEPENDENCY_INSTALL = "dependency_install"
    GIT_WRITE = "git_write"
    GIT_PUSH = "git_push"
    MASS_DELETE = "mass_delete"
    SYSTEM_DESTRUCTIVE = "system_destructive"
    PRIVILEGE_ESCALATION = "privilege_escalation"


class ApprovalScope(str, Enum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class RiskAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: RiskLevel
    tags: tuple[RiskTag, ...] = ()
    reason: str = ""
    tool_name: str
    pattern: str = ""

    @property
    def approvable(self) -> bool:
        return self.level != RiskLevel.BLOCKED

    @classmethod
    def normal(
        cls,
        *,
        tool_name: str,
        pattern: str = "",
        tags: tuple[RiskTag, ...] = (),
        reason: str = "",
    ) -> "RiskAssessment":
        return cls(level=RiskLevel.NORMAL, tags=tags, reason=reason, tool_name=tool_name, pattern=pattern)

    @classmethod
    def dangerous(
        cls,
        *,
        tool_name: str,
        pattern: str = "",
        tags: tuple[RiskTag, ...] = (),
        reason: str = "",
    ) -> "RiskAssessment":
        return cls(level=RiskLevel.DANGEROUS, tags=tags, reason=reason, tool_name=tool_name, pattern=pattern)

    @classmethod
    def extreme(
        cls,
        *,
        tool_name: str,
        pattern: str = "",
        tags: tuple[RiskTag, ...] = (),
        reason: str = "",
    ) -> "RiskAssessment":
        return cls(level=RiskLevel.EXTREME, tags=tags, reason=reason, tool_name=tool_name, pattern=pattern)

    @classmethod
    def blocked(
        cls,
        *,
        tool_name: str,
        pattern: str = "",
        tags: tuple[RiskTag, ...] = (),
        reason: str = "",
    ) -> "RiskAssessment":
        return cls(level=RiskLevel.BLOCKED, tags=tags, reason=reason, tool_name=tool_name, pattern=pattern)
