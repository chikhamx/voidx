"""AgentRegistry: three-layer discovery, signature cache, resolve, diagnostics."""

import os
from pathlib import Path

import pytest

from voidx.agent.application.agent_profile_loader import ProfileLoadError
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.domain.profile import CHAT_PROFILE, CODING_PROFILE, RuntimeProfile
from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.prompt_policy import (
    ChatPromptPolicy,
    CodingPromptPolicy,
    GoalPromptPolicy,
    LoopPromptPolicy,
)


def _write(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _profile_yaml(name: str, revision: int = 1, display_name: str = "") -> str:
    return f"name: {name}\nrevision: {revision}\ndisplay_name: {display_name or name}\n"


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "bundled": tmp_path / "bundled_agents",
        "global": tmp_path / "global_agents",
        "project": tmp_path / "project_agents",
    }


@pytest.fixture()
def registry(tmp_path: Path, dirs: dict[str, Path]) -> AgentRegistry:
    return AgentRegistry(
        str(tmp_path),
        bundled_dir=dirs["bundled"],
        global_dir=dirs["global"],
        project_dir=dirs["project"],
    )


def test_discovers_three_layers_with_project_override(registry, dirs) -> None:
    _write(dirs["bundled"], "reviewer", _profile_yaml("reviewer", 1, "Bundled"))
    _write(dirs["global"], "reviewer", _profile_yaml("reviewer", 2, "Global"))
    _write(dirs["project"], "reviewer", _profile_yaml("reviewer", 3, "Project"))
    _write(dirs["global"], "global-only", _profile_yaml("global-only"))

    infos = {info.name: info for info in registry.discover()}

    assert set(infos) == {"reviewer", "global-only"}
    assert infos["reviewer"].source == "project"
    assert infos["reviewer"].revision == 3
    assert infos["reviewer"].display_name == "Project"
    assert infos["reviewer"].available is True
    assert infos["reviewer"].content_hash
    assert infos["reviewer"].run_mode == "single"
    assert infos["reviewer"].hitl_mode == "interactive"
    assert infos["global-only"].source == "global"


def test_discover_result_is_cached_until_signature_changes(registry, dirs) -> None:
    path = _write(dirs["project"], "reviewer", _profile_yaml("reviewer", 1))

    first = registry.discover()
    second = registry.discover()
    assert first is second  # signature cache hit: identical object, no watcher needed

    path.write_text(_profile_yaml("reviewer", 2), encoding="utf-8")
    third = registry.discover()
    assert third is not first
    assert third[0].revision == 2

    # A brand-new file is picked up on the next discover without invalidate().
    _write(dirs["project"], "another", _profile_yaml("another"))
    assert {info.name for info in registry.discover()} == {"reviewer", "another"}


def test_invalidate_forces_rediscovery(registry, dirs) -> None:
    _write(dirs["project"], "reviewer", _profile_yaml("reviewer", 1))
    first = registry.discover()
    registry.invalidate()
    assert registry.discover() is not first


def test_resolve_returns_resolved_profile(registry, dirs) -> None:
    _write(dirs["project"], "reviewer", _profile_yaml("reviewer", 4, "Reviewer"))

    resolved = registry.resolve("reviewer")

    assert resolved.snapshot.profile_id == "reviewer"
    assert resolved.snapshot.revision == 4
    assert resolved.snapshot.source == "project"
    assert resolved.runtime_profile.name == "Reviewer"
    assert resolved.run_config.protocol == "turn"


def test_resolve_unknown_name_raises(registry) -> None:
    with pytest.raises(KeyError):
        registry.resolve("ghost")


def test_broken_file_is_unavailable_with_diagnostics(registry, dirs) -> None:
    _write(dirs["project"], "broken", "name: broken\nrevision: nope\n")

    infos = {info.name: info for info in registry.discover()}
    assert infos["broken"].available is False
    assert infos["broken"].diagnostics
    assert infos["broken"].diagnostics[0].code

    with pytest.raises(ProfileLoadError):
        registry.resolve("broken")


def test_broken_file_rejects_new_resolution_after_last_valid_snapshot(registry, dirs) -> None:
    path = _write(dirs["project"], "reviewer", _profile_yaml("reviewer", 5))
    pinned = registry.resolve("reviewer").snapshot

    path.write_text("name: reviewer\nrevision: nope\n", encoding="utf-8")
    infos = {info.name: info for info in registry.discover()}

    assert pinned.revision == 5
    assert infos["reviewer"].available is False
    assert infos["reviewer"].diagnostics
    with pytest.raises(ProfileLoadError):
        registry.resolve("reviewer")


def test_broken_project_file_shadows_global_without_fallback(registry, dirs) -> None:
    _write(dirs["global"], "reviewer", _profile_yaml("reviewer", 2, "Global"))
    _write(dirs["project"], "reviewer", "name: reviewer\nrevision: nope\n")

    infos = {info.name: info for info in registry.discover()}
    assert infos["reviewer"].available is False


def test_bundled_four_modes_discoverable() -> None:
    registry = AgentRegistry(".")
    infos = {info.name: info for info in registry.discover()}

    assert {"coding", "chat", "goal", "loop"} <= set(infos)
    for name in ("coding", "chat", "goal", "loop"):
        assert infos[name].source == "bundled"
        assert infos[name].available is True


def _assert_equivalent(resolved, legacy: RuntimeProfile, policy_type: type) -> None:
    profile = resolved.runtime_profile
    assert profile.profile_id == legacy.profile_id
    assert profile.name == legacy.name
    assert profile.protocol == legacy.protocol
    assert profile.system_prompt == legacy.system_prompt
    assert profile.constraints == legacy.constraints
    assert profile.persona == legacy.persona
    assert type(profile.prompt_policy) is policy_type


def test_bundled_coding_matches_legacy_profile_and_default_dag() -> None:
    resolved = AgentRegistry(".").resolve("coding")

    _assert_equivalent(resolved, CODING_PROFILE, CodingPromptPolicy)
    context = resolved.workflow_context
    assert context is not None
    assert context.dag.model_dump() == DEFAULT_WORKFLOW_DAG.model_dump()
    assert resolved.run_config.run_mode == "single"
    assert resolved.resource_policy.hitl_mode == "interactive"


def test_bundled_chat_matches_legacy_profile_without_workflow() -> None:
    resolved = AgentRegistry(".").resolve("chat")

    _assert_equivalent(resolved, CHAT_PROFILE, ChatPromptPolicy)
    assert resolved.workflow_context is None


def test_bundled_goal_matches_legacy_profile() -> None:
    resolved = AgentRegistry(".").resolve("goal")

    _assert_equivalent(resolved, GOAL_PROFILE, GoalPromptPolicy)
    assert resolved.run_config.run_mode == "goal_eval"
    assert resolved.run_config.protocol == "goal"


def test_bundled_loop_matches_legacy_profile() -> None:
    resolved = AgentRegistry(".").resolve("loop")

    _assert_equivalent(resolved, LOOP_PROFILE, LoopPromptPolicy)
    assert resolved.run_config.protocol == "loop"
    assert resolved.run_config.lifecycle_tool == "loop"
