from __future__ import annotations

from voidx.agent.domain.agent_profile import ResourcePolicy
from voidx.agent.domain.run_config import resolve_run_config
from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy


def _policy(
    *,
    allow: frozenset[str] | None = None,
    block: frozenset[str] = frozenset(),
    hitl_mode: str = "interactive",
    run_mode: str = "single",
    phase: str = "turn",
    child_agent: bool = False,
) -> ProfileToolPolicy:
    return ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(
            tools_allow=allow,
            tools_block=block,
            hitl_mode=hitl_mode,
        ),
        run_config=resolve_run_config(run_mode),
        snapshot_hash="snapshot-1",
        phase=phase,
        child_agent=child_agent,
    )


def test_profile_block_wins_and_allow_only_intersects_baseline() -> None:
    policy = _policy(
        allow=frozenset({"read", "write", "goal"}),
        block=frozenset({"write"}),
    )

    assert policy.allows("read", capability="read_only")
    assert not policy.allows("write", capability="execution_gated")
    assert not policy.allows("bash", capability="execution_gated")
    assert not policy.allows("goal", capability="orchestration")


def test_lifecycle_tool_cannot_be_expanded_by_profile_allow() -> None:
    single = _policy(allow=frozenset({"turn", "goal", "loop"}))
    goal_work = _policy(
        allow=frozenset({"goal"}), run_mode="goal_eval", phase="work"
    )
    goal_idle = _policy(
        allow=frozenset({"goal"}), run_mode="goal_eval", phase="idle"
    )

    assert single.allows("turn")
    assert not single.allows("goal")
    assert not single.allows("loop")
    assert not goal_work.allows("goal")
    assert goal_idle.allows("goal")


def test_autonomous_and_child_constraints_are_enforced() -> None:
    autonomous = _policy(hitl_mode="autonomous")
    child = _policy(child_agent=True)

    assert not autonomous.allows(
        "clarify", capability="hitl_interaction"
    )
    assert not autonomous.check_tool_call(
        "checkpoint", {}, capability="hitl_interaction"
    ).allowed
    for tool_id in ("agent", "clarify", "checkpoint"):
        assert not child.allows(tool_id, capability="orchestration")


def test_canonical_tool_checks_record_pinned_evidence() -> None:
    policy = _policy(block=frozenset({"replace"}), phase="turn")

    decision = policy.check_tool_call(
        "replace", {"file_path": "a.py"}, capability="execution_gated"
    )

    assert not decision.allowed
    assert decision.reason == "profile_blocked"
    assert decision.canonical_tool == "replace"
    assert decision.snapshot_hash == "snapshot-1"
    assert decision.phase == "turn"
    assert decision.capability == "execution_gated"


def test_visible_ids_and_execution_check_use_the_same_decision() -> None:
    policy = _policy(block=frozenset({"write"}))
    capabilities = {
        "read": "read_only",
        "write": "execution_gated",
    }

    assert policy.visible_tool_ids(capabilities) == frozenset({"read"})
    assert policy.check_tool_call(
        "read", {}, capability="read_only"
    ).allowed
    assert not policy.check_tool_call(
        "write", {}, capability="execution_gated"
    ).allowed


def test_bound_tool_ids_compatibility_filters_legacy_baseline() -> None:
    from voidx.agent.domain.tool_view import BoundToolView

    policy = ProfileToolPolicy(
        baseline=BoundToolView(
            bound_tool_ids=frozenset({"read", "write", "clarify"})
        ),
        resource_policy=ResourcePolicy(
            tools_block=frozenset({"write"}),
            hitl_mode="autonomous",
        ),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )

    assert policy.bound_tool_ids == frozenset({"read", "clarify"})


def test_bound_tool_ids_compatibility_is_safe_for_unbounded_baseline() -> None:
    policy = _policy(block=frozenset({"write"}))

    assert policy.bound_tool_ids == frozenset()
