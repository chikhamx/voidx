from __future__ import annotations

import pytest

from tests.tool_registry import build_registry

from voidx.agent.domain.agent_profile import ResourcePolicy
from voidx.agent.domain.run_config import resolve_run_config
from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy
from voidx.skills.application.api import SkillsApi
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService
from voidx.tooling.adapters.skills import ReadOnlySkillsTool, SkillsTool


def _policy(*, hitl_mode: str, skills: tuple[str, ...] | None) -> ProfileToolPolicy:
    return ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode=hitl_mode, skills=skills),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )


def _provider(workspace: str) -> SkillsApi:
    return SkillsApi(SkillService(SkillRegistry(workspace)))


def test_autonomous_registry_replaces_skill_with_read_only_contract(tmp_path) -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    scoped = scoped_tool_registry(
        registry,
        _policy(hitl_mode="autonomous", skills=("docs",)),
        skills_api_provider=_provider,
    )

    assert isinstance(registry.get("skill"), SkillsTool)
    assert not isinstance(registry.get("skill"), ReadOnlySkillsTool)
    assert isinstance(scoped.get("skill"), ReadOnlySkillsTool)
    assert "create" not in scoped.get_def("skill").description.lower()
    assert scoped.get_def("skill").parameters["properties"]["op"]["enum"] == [
        "load",
        "list",
    ]


def test_autonomous_registry_hides_skill_for_empty_effective_allowlist() -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    scoped = scoped_tool_registry(
        registry,
        _policy(hitl_mode="autonomous", skills=None),
        skills_api_provider=_provider,
    )

    assert "skill" in registry.ids()
    assert "skill" not in scoped.ids()


def test_interactive_registry_keeps_full_skill_contract() -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    scoped = scoped_tool_registry(
        registry,
        _policy(hitl_mode="interactive", skills=None),
        skills_api_provider=_provider,
    )

    assert isinstance(scoped.get("skill"), SkillsTool)
    assert not isinstance(scoped.get("skill"), ReadOnlySkillsTool)
    assert "create" in scoped.get_def("skill").parameters["properties"]["op"]["enum"]


def test_tool_registry_for_uses_bound_turn_registry() -> None:
    from types import SimpleNamespace

    from voidx.agent.adapters.langgraph.runtime.thread_context import tool_registry_for
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )

    shared = build_registry()
    scoped = shared.filtered_copy({"read"})
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(tool_registry=scoped)
    )
    try:
        assert tool_registry_for(SimpleNamespace(tools=shared)) is scoped
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)


def test_tool_registry_for_falls_back_to_shared_registry() -> None:
    from types import SimpleNamespace

    from voidx.agent.adapters.langgraph.runtime.thread_context import tool_registry_for

    shared = build_registry()

    assert tool_registry_for(SimpleNamespace(tools=shared)) is shared


@pytest.mark.parametrize("mcp_servers", [None, ()])
def test_autonomous_registry_hides_mcp_for_empty_effective_allowlist(mcp_servers) -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(
            hitl_mode="autonomous",
            skills=None,
            mcp_servers=mcp_servers,
        ),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )

    scoped = scoped_tool_registry(registry, policy)

    assert "mcp" in registry.ids()
    assert "mcp" not in scoped.ids()


def test_autonomous_registry_replaces_mcp_with_scoped_gateway() -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry
    from voidx.tooling.adapters.mcp import McpGatewayTool

    registry = build_registry()
    original = registry.get("mcp")
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(
            hitl_mode="autonomous",
            skills=None,
            mcp_servers=("tavily",),
        ),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )

    scoped = scoped_tool_registry(registry, policy)

    assert isinstance(original, McpGatewayTool)
    assert registry.get("mcp") is original
    assert scoped.get("mcp") is not original
    assert "allowlisted" in scoped.get_def("mcp").description.lower()


def test_interactive_registry_keeps_original_mcp_gateway() -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    original = registry.get("mcp")

    scoped = scoped_tool_registry(
        registry,
        _policy(hitl_mode="interactive", skills=None),
    )

    assert scoped.get("mcp") is original


def test_autonomous_registry_hides_allowlisted_skill_without_scoped_provider() -> None:
    from voidx.bootstrap.tooling import scoped_tool_registry

    registry = build_registry()
    scoped = scoped_tool_registry(
        registry,
        _policy(hitl_mode="autonomous", skills=("docs",)),
        skills_api_provider=None,
    )

    assert "skill" in registry.ids()
    assert "skill" not in scoped.ids()


@pytest.mark.asyncio
async def test_concurrent_autonomous_scoped_registries_do_not_cross_contaminate(
    tmp_path,
) -> None:
    import asyncio

    from voidx.bootstrap.tooling import scoped_tool_registry
    from voidx.agent.adapters.langgraph.runtime.tool_surface import (
        ToolSurfaceContext,
        resolve_tool_surface,
    )
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.tooling.domain.context import ToolExecutionContext

    shared = build_registry()

    def write_skill(workspace, name):
        directory = workspace / ".voidx" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\n{name} body",
            encoding="utf-8",
        )

    async def run_profile(label: str, skill_name: str, server: str, other: str):
        workspace = tmp_path / label
        write_skill(workspace, skill_name)
        policy = ProfileToolPolicy(
            baseline=CodingToolPolicy(),
            resource_policy=ResourcePolicy(
                hitl_mode="autonomous",
                skills=(skill_name,),
                mcp_servers=(server,),
            ),
            run_config=resolve_run_config("single"),
            snapshot_hash=f"snapshot-{label}",
            phase="turn",
        )
        scoped = scoped_tool_registry(
            shared,
            policy,
            skills_api_provider=_provider,
        )
        surface = resolve_tool_surface(
            scoped,
            ToolSurfaceContext(
                runtime_profile=RuntimeProfile(
                    profile_id=label,
                    revision=1,
                    name=label,
                ),
                tool_policy=policy,
            ),
        )
        skill_result = await scoped.execute_tool(
            "skill",
            {"op": "list", "name": None},
            ToolExecutionContext(workspace=str(workspace)),
        )
        mcp_result = await scoped.execute_tool(
            "mcp",
            {"op": "load", "server": other, "tool": None},
            ToolExecutionContext(workspace=str(workspace)),
        )
        definitions = {
            item["function"]["name"]: item["function"]
            for item in surface.definitions
        }
        return scoped, skill_result, mcp_result, definitions

    alpha, beta = await asyncio.gather(
        run_profile("alpha", "alpha-skill", "alpha-server", "beta-server"),
        run_profile("beta", "beta-skill", "beta-server", "alpha-server"),
    )

    for result, own_skill in ((alpha, "alpha-skill"), (beta, "beta-skill")):
        scoped, skill_result, mcp_result, definitions = result
        assert skill_result.metadata["skills"][0]["name"] == own_skill
        assert skill_result.metadata["count"] == 1
        assert mcp_result.metadata["error_kind"] == "server_not_allowed"
        assert definitions["skill"]["parameters"]["properties"]["op"]["enum"] == [
            "load",
            "list",
        ]
        assert "mcp" in definitions
        assert scoped is not shared

    assert alpha[0] is not beta[0]
    assert "create" in shared.get_def("skill").parameters["properties"]["op"]["enum"]
    assert shared.get("mcp") is not alpha[0].get("mcp")
    assert shared.get("mcp") is not beta[0].get("mcp")
