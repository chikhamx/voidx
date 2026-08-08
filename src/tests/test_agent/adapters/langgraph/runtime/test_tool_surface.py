"""ToolSurface resolver 专项测试：固定 profile/phase/child/policy 的最终可见工具面。"""

from __future__ import annotations

from tests.tool_registry import build_registry
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.adapters.langgraph.runtime.tool_surface import (
    ToolSurfaceContext,
    resolve_tool_surface,
)
from voidx.tooling.application.registry import ToolRegistry


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
        registry.register(tool_id, object(), f"{tool_id} description", {"type": "object", "properties": {}})
    return registry


def _names(surface) -> list[str]:
    return [item["function"]["name"] for item in surface.definitions]


def _coding_profile() -> RuntimeProfile:
    return RuntimeProfile(profile_id="coding", revision=1, name="Coding")


def test_coding_main_exposes_turn_but_hides_goal_loop_and_execution_only() -> None:
    registry = _registry("read", "bash", "agent", "goal", "loop", "git", "lsp_format")

    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=_coding_profile(), tool_policy=_AllowAll()),
    )

    names = _names(surface)
    assert "turn" in names
    assert "read" in names and "bash" in names and "agent" in names
    for hidden in ("goal", "loop", "git", "lsp_format"):
        assert hidden not in names
    assert surface.dropped["git"] == "execution_only"
    assert surface.dropped["lsp_format"] == "execution_only"


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


def test_goal_profile_exposes_goal_in_idle_intake_evaluator_but_not_work() -> None:
    profile = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")
    registry = _registry("read", "write", "agent", "goal", "loop", "git", "lsp_format")

    policy = _Deny({"agent"})
    for phase in ("idle", "intake", "evaluator"):
        surface = resolve_tool_surface(
            registry,
            ToolSurfaceContext(
                runtime_profile=profile,
                goal_phase=phase,
                tool_policy=policy,
            ),
        )
        names = _names(surface)
        assert "goal" in names
        for hidden in ("loop", "turn", "agent"):
            assert hidden not in names

    work = resolve_tool_surface(
        registry,
        ToolSurfaceContext(runtime_profile=profile, goal_phase="work", tool_policy=_AllowAll()),
    )
    work_names = _names(work)
    assert "goal" not in work_names
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
        for hidden in ("goal", "loop", "turn"):
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
    for hidden in ("agent", "clarify", "checkpoint", "goal", "loop", "turn"):
        assert hidden not in names


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
