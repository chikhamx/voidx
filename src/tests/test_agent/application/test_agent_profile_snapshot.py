"""Profile snapshot restore: hash-verified rebuild and session restore precedence."""

from pathlib import Path

import pytest

from voidx.agent.application.agent_profile_loader import (
    ProfileLoaderContext,
    ProfileLoadError,
)
from voidx.agent.application.agent_profile_snapshot import (
    restore_from_snapshot,
    restore_session_profile,
)
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.domain.agent_profile import content_hash_of
from voidx.agent.domain.prompt_policy import ChatPromptPolicy, CodingPromptPolicy


def _write(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


WORKFLOW_YAML = """
name: reviewer
revision: 2
display_name: Reviewer
persona: review
identity: "严格评审"
extra_rules: ["只输出意见"]
workflow:
  nodes:
    - ref: review
run_mode: single
hitl_mode: interactive
tools:
  allow: [read]
skills: []
mcp_servers: []
"""


@pytest.fixture()
def registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        str(tmp_path),
        global_dir=tmp_path / "global_agents",
        project_dir=tmp_path / "project_agents",
        loader_context=ProfileLoaderContext(known_tools=frozenset({"read"})),
    )


def test_restore_roundtrip_rebuilds_all_layers(registry: AgentRegistry, tmp_path: Path) -> None:
    _write(tmp_path / "project_agents", "reviewer", WORKFLOW_YAML)
    resolved = registry.resolve("reviewer")

    restored = restore_from_snapshot(resolved.snapshot)

    assert restored.snapshot == resolved.snapshot
    assert restored.run_config == resolved.run_config
    assert restored.resource_policy == resolved.resource_policy
    assert restored.workflow_context is not None
    assert restored.workflow_context.model_dump() == resolved.workflow_context.model_dump()

    profile = restored.runtime_profile
    assert profile.profile_id == "reviewer"
    assert profile.revision == 2
    assert profile.name == "Reviewer"
    assert profile.persona == "review"
    assert profile.system_prompt == "严格评审"
    assert profile.constraints == ("只输出意见",)
    assert isinstance(profile.prompt_policy, CodingPromptPolicy)



def test_restore_bundled_legacy_snapshot_preserves_pinned_payload(
    registry: AgentRegistry,
) -> None:
    pinned = registry.resolve("coding").snapshot
    payload = {
        **pinned.canonical_payload,
        "display_name": "Pinned Coding",
        "identity": "Pinned system identity",
        "extra_rules": ["Keep the pinned rule"],
        "persona": "review",
        "prompt_policy": "chat",
    }
    content_hash = content_hash_of(payload)
    snapshot = pinned.model_copy(update={
        "canonical_payload": payload,
        "content_hash": content_hash,
        "snapshot_hash": content_hash_of({
            "source": pinned.source,
            "profile_id": pinned.profile_id,
            "revision": pinned.revision,
            "content_hash": content_hash,
        }),
    })

    restored = restore_from_snapshot(snapshot)

    assert restored.runtime_profile.name == "Pinned Coding"
    assert restored.runtime_profile.system_prompt == "Pinned system identity"
    assert restored.runtime_profile.constraints == ("Keep the pinned rule",)
    assert restored.runtime_profile.persona == "review"
    assert isinstance(restored.runtime_profile.prompt_policy, ChatPromptPolicy)

def test_restore_detects_tampered_payload(registry: AgentRegistry, tmp_path: Path) -> None:
    _write(tmp_path / "project_agents", "reviewer", "name: reviewer\nrevision: 1\n")
    resolved = registry.resolve("reviewer")

    tampered = resolved.snapshot.model_copy(update={
        "canonical_payload": {**resolved.snapshot.canonical_payload, "revision": 99}
    })
    with pytest.raises(ProfileLoadError) as excinfo:
        restore_from_snapshot(tampered)
    assert [d.code for d in excinfo.value.diagnostics] == ["snapshot_mismatch"]


def test_session_restore_prefers_snapshot_over_changed_file(
    registry: AgentRegistry, tmp_path: Path
) -> None:
    path = _write(tmp_path / "project_agents", "reviewer", "name: reviewer\nrevision: 1\n")
    pinned = registry.resolve("reviewer").snapshot

    # File changes after pinning must not affect the pinned snapshot.
    path.write_text("name: reviewer\nrevision: 2\n", encoding="utf-8")
    restored = restore_session_profile(registry, profile_id="reviewer", snapshot=pinned)
    assert restored.snapshot.revision == 1
    assert restored.snapshot.content_hash == pinned.content_hash

    # File deletion after pinning must not affect the pinned snapshot either.
    path.unlink()
    restored = restore_session_profile(registry, profile_id="reviewer", snapshot=pinned)
    assert restored.snapshot.snapshot_hash == pinned.snapshot_hash


def test_session_restore_maps_legacy_ids_to_bundled(registry: AgentRegistry) -> None:
    for name in ("coding", "chat", "goal", "loop"):
        resolved = restore_session_profile(registry, profile_id=name, snapshot=None)
        assert resolved.snapshot.profile_id == name
        assert resolved.snapshot.source == "bundled"


def test_session_restore_resolves_existing_custom_file(
    registry: AgentRegistry, tmp_path: Path
) -> None:
    _write(tmp_path / "project_agents", "reviewer", "name: reviewer\nrevision: 7\n")
    resolved = restore_session_profile(registry, profile_id="reviewer", snapshot=None)
    assert resolved.snapshot.revision == 7


def test_session_restore_marks_missing_profile_unavailable(registry: AgentRegistry) -> None:
    with pytest.raises(ProfileLoadError) as excinfo:
        restore_session_profile(registry, profile_id="ghost", snapshot=None)
    assert [d.code for d in excinfo.value.diagnostics] == ["profile_unavailable"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_hash", "0" * 64),
        ("profile_id", "other-profile"),
        ("revision", 99),
        ("source", "global"),
    ],
)
def test_restore_detects_tampered_snapshot_envelope(
    registry: AgentRegistry, tmp_path: Path, field: str, value: object
) -> None:
    _write(tmp_path / "project_agents", "reviewer", "name: reviewer\nrevision: 1\n")
    resolved = registry.resolve("reviewer")
    tampered = resolved.snapshot.model_copy(update={field: value})

    with pytest.raises(ProfileLoadError) as excinfo:
        restore_from_snapshot(tampered)

    assert [diagnostic.code for diagnostic in excinfo.value.diagnostics] == [
        "snapshot_mismatch"
    ]
