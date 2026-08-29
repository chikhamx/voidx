"""ToolSurface resolver 专项测试：固定 profile/phase/child/policy 的最终可见工具面。"""

from __future__ import annotations

from tests.tool_registry import build_registry
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.adapters.langgraph.runtime.tool_surface import (
    ToolSurfaceContext,
    resolve_tool_surface,
)
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.domain.capability import ToolCapability


class _AllowAll:
    def allows(self, tool_name: str) -> bool:
        return True


class _Deny:
    def __init__(self, denied: set[str]) -> None:
        self._denied = denied

    def allows(self, tool_name: str) -> bool:
        return tool_name not in self._denied


def _registry(*tool_ids: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tool_id in tool_ids:
        registry.register(
            tool_id,
            object(),
            f"{tool_id} description",
            {"type": "object", "properties": {}},
            capability=ToolCapability.ORCHESTRATION,
        )
    return registry


def _names(surface) -> list[str]:
    return [item["function"]["name"] for item in surface.definitions]


def _coding_profile() -> RuntimeProfile:
    return RuntimeProfile(profile_id="coding", revision=1, name="Coding")


def test_coding_main_exposes_turn_but_hides_goal_loop_and_execution_only() -> None:
    registry = _registry("read", "bash", "agent", "goal", "loop", "git", "lsp_format", "compact")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=_coding_profile(), tool_policy=_AllowAll()),
    )

    names = _names(surface)
    assert "turn" in names
    assert "read" in names and "bash" in names and "agent" in names
    for hidden in ("goal", "loop", "git", "lsp_format", "compact"):
        assert hidden not in names
    assert surface.dropped["git"] == "execution_only"
    assert surface.dropped["lsp_format"] == "execution_only"
    assert surface.dropped["compact"] == "execution_only"


def test_compact_never_reaches_llm_surface() -> None:
    registry = _registry("read", "compact")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=_coding_profile(), tool_policy=_AllowAll()),
    )

    assert "compact" not in _names(surface)
    assert surface.dropped["compact"] == "execution_only"


def test_loop_profile_exposes_loop_in_idle_and_work() -> None:
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")
    registry = _registry("read", "agent", "goal", "loop")

    # loop/goal 运行时的 agent 屏蔽由闭集 policy 承担，resolver 不做特判。
    policy = _Deny({"agent"})
    for phase in ("idle", "work"):
        surface = resolve_tool_surface(
            registry,
            ToolSurfaceContext(
                runtime_profile=profile,
                loop_phase=phase,
                tool_policy=policy,
            ),
        )
        names = _names(surface)
        assert "loop" in names
        for hidden in ("goal", "turn", "agent"):
            assert hidden not in names


def test_goal_profile_exposes_phase_specific_tool_by_phase() -> None:
    profile = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")
    registry = _registry(
        "read", "write", "agent", "goal", "goal_init", "goal_checkpoint", "goal_decision",
        "loop", "git", "lsp_format",
    )

    policy = _Deny({"agent"})
    expected = {"idle": "goal_init", "intake": "goal_init", "work": "goal_checkpoint", "evaluator": "goal_decision"}
    for phase, control_tool in expected.items():
        surface = resolve_tool_surface(
            registry,
            ToolSurfaceContext(runtime_profile=profile, goal_phase=phase, tool_policy=policy),
        )
        names = _names(surface)
        assert control_tool in names
        assert "goal" not in names
        for hidden in ("loop", "turn", "agent"):
            assert hidden not in names



def test_goal_work_matches_coding_tools_except_clarify_and_checkpoint() -> None:
    from voidx.agent.domain.automation.goal import GoalToolView

    profile = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")
    registry = _registry(
        "read", "find", "search", "lsp", "document", "websearch", "webfetch",
        "mcp", "skill", "bash", "powershell", "write", "replace", "manage",
        "agent", "workflow", "todo", "clarify", "checkpoint", "goal_checkpoint",
    )
    policy = GoalToolView.default(phase="work").bind(set(registry.ids()))

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(
            runtime_profile=profile,
            goal_phase="work",
            tool_policy=policy,
        ),
    )
    names = set(_names(surface))

    import os

    shell = "bash" if os.name != "nt" else "powershell"
    assert {"read", shell, "write", "replace", "manage"} <= names
    assert {"agent", "workflow", "todo", "goal_checkpoint"} <= names
    assert "clarify" not in names
    assert "checkpoint" not in names
    work = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=profile, goal_phase="work", tool_policy=_AllowAll()),
    )
    work_names = _names(work)
    assert "goal_checkpoint" in work_names
    assert "read" in work_names and "write" in work_names
    assert "git" not in work_names and "lsp_format" not in work_names


def test_unknown_or_missing_phase_uses_minimal_visibility() -> None:
    loop_profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")
    goal_profile = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")
    registry = _registry("read", "goal", "loop")

    for profile, kwargs in (
        (loop_profile, {"loop_phase": "bogus"}),
        (loop_profile, {"loop_phase": None}),
        (goal_profile, {"goal_phase": "bogus"}),
        (goal_profile, {"goal_phase": None}),
    ):
        surface = resolve_tool_surface(
            registry,
            ToolSurfaceContext(runtime_profile=profile, tool_policy=_AllowAll(), **kwargs),
        )
        names = _names(surface)
        for hidden in ("goal", "goal_init", "goal_checkpoint", "goal_decision", "loop", "turn"):
            assert hidden not in names



def test_child_surface_blocks_delegation_and_lifecycle_but_keeps_message() -> None:
    registry = _registry("read", "agent", "clarify", "checkpoint", "message", "goal", "loop")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(
            runtime_profile=_coding_profile(),
            tool_policy=_AllowAll(),
            child_agent=True,
        ),
    )

    names = _names(surface)
    assert "message" in names
    assert "read" in names
    for hidden in (
        "agent", "clarify", "checkpoint", "goal", "goal_init", "goal_checkpoint", "goal_decision", "loop", "turn",
    ):
        assert hidden not in names




def test_profile_policy_uses_registry_capability_for_visibility() -> None:
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy

    registry = ToolRegistry()
    registry.register(
        "clarify",
        object(),
        "clarify description",
        {},
        capability=ToolCapability.HITL_INTERACTION,
    )
    registry.register(
        "workflow",
        object(),
        "workflow description",
        {},
        capability=ToolCapability.ORCHESTRATION,
    )
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode="autonomous"),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=_coding_profile(), tool_policy=policy),
    )

    assert "clarify" not in _names(surface)
    assert "workflow" in _names(surface)
    assert surface.dropped["clarify"] == "policy"


def test_tool_policy_bridge_normalizes_alias_and_registry_capability() -> None:
    from voidx.agent.adapters.langgraph.runtime.tool_policy_bridge import (
        check_tool_policy,
    )
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy

    registry = ToolRegistry()
    registry.register(
        "replace",
        object(),
        "replace description",
        {},
        capability=ToolCapability.EXECUTION_GATED,
    )
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(tools_block=frozenset({"replace"})),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )

    decision = check_tool_policy(
        policy, registry, "Edit", {"file_path": "example.py"}
    )

    assert not decision.allowed
    assert decision.reason == "profile_blocked"
    assert decision.canonical_tool == "replace"
    assert decision.capability == "execution_gated"
def test_policy_applies_to_protocol_injected_tools() -> None:
    registry = _registry("read")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=_coding_profile(), tool_policy=_Deny({"turn"})),
    )

    assert "turn" not in _names(surface)


def test_protocol_definition_overrides_catalog_same_name() -> None:
    registry = _registry("read", "loop")
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=profile, loop_phase="work", tool_policy=_AllowAll()),
    )

    loop_defs = [item for item in surface.definitions if item["function"]["name"] == "loop"]
    assert len(loop_defs) == 1
    assert loop_defs[0]["function"]["description"] != "loop description"


def test_goal_evaluator_surface_is_read_only_plus_goal_decision() -> None:
    import os

    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.automation.goal import GoalToolView
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import ProfileToolPolicy

    registry = build_registry()
    policy = ProfileToolPolicy(
        baseline=GoalToolView.default(
            workflow_enabled=True, phase="evaluator"
        ).bind(registry.ids()),
        resource_policy=ResourcePolicy(hitl_mode="autonomous"),
        run_config=resolve_run_config("goal_eval"),
        snapshot_hash="snapshot-1",
        phase="evaluator",
    )
    profile = RuntimeProfile(
        profile_id="goal", revision=1, name="Goal", protocol="goal"
    )

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(
            runtime_profile=profile,
            goal_phase="evaluator",
            tool_policy=policy,
        ),
    )

    assert set(_names(surface)) == {
        "read",
        "find",
        "search",
        "document",
        "goal_decision",
    }
    shell = "powershell" if os.name == "nt" else "bash"
    for hidden in (
        "workflow",
        "todo",
        shell,
        "write",
        "replace",
        "manage",
        "mcp",
        "goal_checkpoint",
    ):
        assert hidden not in _names(surface)


def test_goal_work_keeps_goal_approval_for_normal_tools() -> None:
    from voidx.agent.adapters.langgraph.runtime.tool_policy_bridge import check_tool_policy
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.automation.goal import GoalToolView
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy

    registry = ToolRegistry()
    for tool_id in ("bash", "write"):
        registry.register(
            tool_id,
            object(),
            f"{tool_id} description",
            {},
            capability=ToolCapability.EXECUTION_GATED,
        )
    goal_policy = ProfileToolPolicy(
        baseline=GoalToolView.default(phase="work").bind(registry.ids()),
        resource_policy=ResourcePolicy(hitl_mode="interactive"),
        run_config=resolve_run_config("goal_eval"),
        snapshot_hash="goal-snapshot",
        phase="work",
    )
    coding_policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode="interactive"),
        run_config=resolve_run_config("single"),
        snapshot_hash="coding-snapshot",
        phase="turn",
    )

    goal_bash = check_tool_policy(goal_policy, registry, "bash", {})
    coding_bash = check_tool_policy(coding_policy, registry, "bash", {})
    assert goal_bash.allowed is coding_bash.allowed is True
    assert goal_bash.requests_approval is True
    assert coding_bash.requests_approval is False

    goal_write = check_tool_policy(goal_policy, registry, "write", {})
    coding_write = check_tool_policy(coding_policy, registry, "write", {})
    assert goal_write.allowed is coding_write.allowed is True
    assert goal_write.requests_approval is coding_write.requests_approval is False
