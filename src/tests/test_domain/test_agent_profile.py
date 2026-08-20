"""Domain contracts for composable agent profiles."""

import pytest
from pydantic import ValidationError

from voidx.agent.domain.agent_profile import (
    AgentProfileSnapshot,
    ProfileDiagnostic,
    ResolvedAgentProfile,
    ResourcePolicy,
    WorkflowRuntimeContext,
)
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG, WorkflowNode
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.run_config import RUN_CONFIG_PRESETS, resolve_run_config


def _node(name: str) -> WorkflowNode:
    return WorkflowNode(
        name=name,
        goal=f"{name} goal",
        description=f"{name} description",
        io={"input": {}, "output": {}},
        persona="implement",
    )


def _snapshot() -> AgentProfileSnapshot:
    return AgentProfileSnapshot(
        profile_id="coding",
        revision=1,
        source="bundled",
        content_hash="a" * 64,
        snapshot_hash="b" * 64,
        canonical_payload={"name": "coding", "revision": 1},
    )


def test_resource_policy_defaults_to_interactive_inherit() -> None:
    policy = ResourcePolicy()

    assert policy.hitl_mode == "interactive"
    assert policy.tools_allow is None
    assert policy.tools_block == frozenset()
    assert policy.skills is None
    assert policy.mcp_servers is None


def test_resource_policy_rejects_unknown_hitl_mode() -> None:
    with pytest.raises(ValidationError):
        ResourcePolicy(hitl_mode="yolo")


def test_snapshot_requires_revision_and_hashes() -> None:
    with pytest.raises(ValidationError):
        AgentProfileSnapshot(
            profile_id="coding",
            revision=0,
            source="bundled",
            content_hash="a" * 64,
            snapshot_hash="b" * 64,
            canonical_payload={},
        )
    with pytest.raises(ValidationError):
        AgentProfileSnapshot(
            profile_id="coding",
            revision=1,
            source="bundled",
            content_hash="",
            snapshot_hash="b" * 64,
            canonical_payload={},
        )


def test_snapshot_is_frozen() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValidationError):
        snapshot.revision = 2


def test_workflow_runtime_context_carries_dag_and_hash() -> None:
    dag = WorkflowDAG(name="custom", nodes={"work": _node("work")})
    context = WorkflowRuntimeContext(
        dag=dag, dag_revision=3, dag_hash="c" * 64, source="project"
    )

    assert context.dag is dag
    assert context.dag_revision == 3
    assert context.source == "project"


def test_run_config_presets_cover_four_fixed_modes() -> None:
    assert set(RUN_CONFIG_PRESETS) == {"single", "goal_eval", "loop_fixed", "loop_dynamic"}

    single = resolve_run_config("single")
    assert single.protocol == "turn"
    assert single.lifecycle_tool == "turn"
    assert single.phases == ("turn",)

    goal = resolve_run_config("goal_eval")
    assert goal.protocol == "goal"
    assert goal.lifecycle_tool == "goal"
    assert goal.phases == ("idle", "intake", "work", "evaluator")

    for mode in ("loop_fixed", "loop_dynamic"):
        loop = resolve_run_config(mode)
        assert loop.protocol == "loop"
        assert loop.lifecycle_tool == "loop"
        assert loop.phases == ("idle", "work")


def test_resolve_run_config_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown run_mode"):
        resolve_run_config("interval_eval")


def test_resolved_profile_requires_all_layers() -> None:
    resolved = ResolvedAgentProfile(
        snapshot=_snapshot(),
        runtime_profile=RuntimeProfile(profile_id="coding", revision=1, name="Coding"),
        workflow_context=None,
        run_config=resolve_run_config("single"),
        resource_policy=ResourcePolicy(),
    )

    assert resolved.snapshot.profile_id == "coding"
    assert resolved.workflow_context is None
    assert resolved.run_config.run_mode == "single"
    assert resolved.resource_policy.hitl_mode == "interactive"


def test_profile_diagnostic_carries_stable_path_code_message() -> None:
    diagnostic = ProfileDiagnostic(path="workflow.nodes[0].ref", code="unknown_ref", message="no such node")

    assert diagnostic.path == "workflow.nodes[0].ref"
    assert diagnostic.code == "unknown_ref"
