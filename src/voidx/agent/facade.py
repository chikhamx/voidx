"""Public facade for running the composed agent application."""

from __future__ import annotations

from typing import Protocol

from collections.abc import Mapping

from voidx.agent.application.agent_profile_service import (
    AgentProfileConflictError,
    AgentProfileReadOnlyError,
    ProfileLoadError,
    AgentProfileService,
)
from voidx.agent.application.agent_service import RunLoopStartupError

AgentProfileConflict = AgentProfileConflictError
AgentProfileReadOnly = AgentProfileReadOnlyError
AgentProfileValidationError = ProfileLoadError


class AgentProfileNotFound(KeyError):
    """Raised when an editable profile target does not exist."""


class AgentRunLoop(Protocol):
    async def run(self, **kwargs: object) -> None: ...


class AgentFacade:
    """Stable application boundary with presentation supplied separately."""

    def __init__(self, *, run_loop: AgentRunLoop) -> None:
        self._run_loop = run_loop

    async def run(self, **kwargs: object) -> None:
        await self._run_loop.run(**kwargs)


__all__ = [
    "AgentFacade",
    "AgentProfileConflict",
    "AgentProfileNotFound",
    "AgentProfileReadOnly",
    "AgentProfileValidationError",
    "RunLoopStartupError",
    "default_session_profile_tool_policy",
    "delete_agent_profile",
    "get_agent_profile",
    "list_agent_profiles",
    "resolve_agent_profile",
    "restore_session_runtime_profile",
    "save_agent_profile",
    "validate_agent_profile",
]


def _agent_profile_service(workspace: str) -> AgentProfileService:
    from voidx.agent.application.agent_registry import agent_registry_for

    return AgentProfileService(agent_registry_for(workspace or "."))


def list_agent_profiles(workspace: str) -> object:
    return _agent_profile_service(workspace).list_profiles()


def get_agent_profile(workspace: str, *, scope: str, name: str) -> object:
    try:
        return _agent_profile_service(workspace).get_profile(scope=scope, name=name)
    except KeyError as exc:
        raise AgentProfileNotFound(name) from exc

def resolve_agent_profile(workspace: str, name: str) -> object:
    from voidx.agent.application.agent_registry import agent_registry_for

    return agent_registry_for(workspace or ".").resolve(name)



def validate_agent_profile(
    workspace: str,
    *,
    scope: str,
    name: str,
    yaml_text: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> object:
    return _agent_profile_service(workspace).validate_profile(
        scope=scope,
        name=name,
        yaml_text=yaml_text,
        payload=payload,
    )


def save_agent_profile(
    workspace: str,
    *,
    scope: str,
    name: str,
    yaml_text: str | None = None,
    payload: Mapping[str, object] | None = None,
    expected_revision: int | None = None,
    expected_hash: str | None = None,
) -> object:
    return _agent_profile_service(workspace).save_profile(
        scope=scope,
        name=name,
        yaml_text=yaml_text,
        payload=payload,
        expected_revision=expected_revision,
        expected_hash=expected_hash,
    )


def delete_agent_profile(
    workspace: str,
    *,
    scope: str,
    name: str,
    expected_revision: int | None = None,
    expected_hash: str | None = None,
) -> None:
    try:
        _agent_profile_service(workspace).delete_profile(
            scope=scope,
            name=name,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
        )
    except KeyError as exc:
        raise AgentProfileNotFound(name) from exc


def restore_session_runtime_profile(
    workspace: str,
    profile_id: str,
    snapshot: object | None,
) -> object:
    """Restore a pinned session profile through the public agent boundary."""
    from voidx.agent.application.agent_profile_snapshot import restore_session_profile
    from voidx.agent.application.agent_registry import agent_registry_for

    return restore_session_profile(
        agent_registry_for(workspace or "."),
        profile_id=profile_id,
        snapshot=snapshot,
    )


def default_session_profile_tool_policy(
    profile: object, *, phase: str = "turn"
) -> object:
    """Build the default pinned policy through the public agent boundary."""
    from voidx.agent.application.profile_tool_policy import (
        default_profile_tool_policy_for,
    )

    return default_profile_tool_policy_for(profile, phase=phase)  # type: ignore[arg-type]
