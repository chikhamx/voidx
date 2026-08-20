"""Resolve the active workflow DAG for tool execution from the session snapshot.

The DAG is never a module-level default: it comes from the profile snapshot
pinned by the tool call's session. Legacy sessions without a snapshot resolve
their profile id through the registry (bundled presets keep prior behavior);
a profile without a workflow yields None — no implicit default DAG.
"""

from __future__ import annotations

from voidx.agent.application.agent_profile_snapshot import restore_session_profile
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.domain.agent_profile import WorkflowRuntimeContext


async def current_workflow_context(ctx) -> WorkflowRuntimeContext | None:
    """Return the workflow context pinned by the tool context's session."""
    from voidx.agent.adapters.persistence.session_repository import get_session

    session_id = str(getattr(ctx, "session_id", "") or "")
    if not session_id or session_id == "default":
        return None
    info = await get_session(session_id)
    if info is None:
        return None
    resolved = restore_session_profile(
        AgentRegistry(str(getattr(ctx, "workspace", "") or ".")),
        profile_id=info.runtime_profile,
        snapshot=info.profile_snapshot,
    )
    return resolved.workflow_context
