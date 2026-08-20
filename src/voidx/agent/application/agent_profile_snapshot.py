"""Rebuild a ResolvedAgentProfile from its persisted snapshot.

Session restore precedence: a persisted snapshot always wins over the current
profile file — turns, goal attempts, and loop iterations must keep using the
snapshot pinned at session/attempt start, even when the source file changed or
was deleted. Without a snapshot, resolve from the registry; an unresolvable
profile is marked unavailable with diagnostics, never silently downgraded.
"""

from __future__ import annotations

from voidx.agent.application.agent_profile_loader import ProfileLoadError
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.domain.agent_profile import (
    AgentProfileSnapshot,
    ProfileDiagnostic,
    ResolvedAgentProfile,
    ResourcePolicy,
    WorkflowRuntimeContext,
    content_hash_of,
)
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import resolve_prompt_policy
from voidx.agent.domain.run_config import resolve_run_config


def restore_from_snapshot(snapshot: AgentProfileSnapshot) -> ResolvedAgentProfile:
    """Rebuild the resolved profile pinned by a session/attempt snapshot.

    The canonical payload is verified against its content hash first; a
    mismatch means the snapshot cannot be trusted and is a hard error.
    """
    payload = snapshot.canonical_payload
    expected_content_hash = content_hash_of(payload)
    expected_snapshot_hash = content_hash_of({
        "source": snapshot.source,
        "profile_id": snapshot.profile_id,
        "revision": snapshot.revision,
        "content_hash": snapshot.content_hash,
    })
    metadata_matches = (
        payload.get("name") == snapshot.profile_id
        and payload.get("revision") == snapshot.revision
    )
    if (
        expected_content_hash != snapshot.content_hash
        or expected_snapshot_hash != snapshot.snapshot_hash
        or not metadata_matches
    ):
        raise ProfileLoadError([
            ProfileDiagnostic(
                path="",
                code="snapshot_mismatch",
                message=(
                    f"persisted snapshot for profile '{snapshot.profile_id}' failed "
                    "its integrity check"
                ),
            )
        ])

    run_config = resolve_run_config(str(payload.get("run_mode") or "single"))

    workflow_context: WorkflowRuntimeContext | None = None
    workflow_payload = payload.get("workflow")
    if isinstance(workflow_payload, dict):
        dag = WorkflowDAG.model_validate(workflow_payload)
        workflow_context = WorkflowRuntimeContext(
            dag=dag,
            dag_revision=snapshot.revision,
            dag_hash=content_hash_of(dag.model_dump(mode="json")),
            source=snapshot.source,
        )

    tools = payload.get("tools") or {}
    tools_allow = tools.get("allow")
    tools_block = tools.get("block")
    skills = payload.get("skills")
    mcp_servers = payload.get("mcp_servers")
    persona = payload.get("persona")

    runtime_profile = RuntimeProfile(
        profile_id=snapshot.profile_id,
        revision=snapshot.revision,
        name=str(payload.get("display_name") or "").strip() or snapshot.profile_id,
        protocol=run_config.protocol,
        system_prompt=str(payload.get("identity") or "").strip(),
        constraints=tuple(
            str(rule) for rule in (payload.get("extra_rules") or []) if str(rule).strip()
        ),
        persona=str(persona).strip().lower() if persona else None,
        prompt_policy=resolve_prompt_policy(str(payload.get("prompt_policy") or "coding")),
    )

    return ResolvedAgentProfile(
        snapshot=snapshot,
        runtime_profile=runtime_profile,
        workflow_context=workflow_context,
        run_config=run_config,
        resource_policy=ResourcePolicy(
            hitl_mode=str(payload.get("hitl_mode") or "interactive"),  # type: ignore[arg-type]
            tools_allow=frozenset(tools_allow) if tools_allow is not None else None,
            tools_block=frozenset(tools_block or []),
            skills=tuple(skills) if skills is not None else None,
            mcp_servers=tuple(mcp_servers) if mcp_servers is not None else None,
        ),
    )


def restore_session_profile(
    registry: AgentRegistry,
    *,
    profile_id: str,
    snapshot: AgentProfileSnapshot | None,
) -> ResolvedAgentProfile:
    """Restore the profile for a loaded session following snapshot precedence."""
    if snapshot is not None:
        return restore_from_snapshot(snapshot)
    try:
        return registry.resolve(profile_id)
    except KeyError as exc:
        raise ProfileLoadError([
            ProfileDiagnostic(
                path="",
                code="profile_unavailable",
                message=(
                    f"profile '{profile_id}' has no persisted snapshot and no "
                    "resolvable source; choose an available profile"
                ),
            )
        ]) from exc
