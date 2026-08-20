from __future__ import annotations

import os
from pathlib import Path

import pytest

from voidx.agent.application.agent_profile_loader import ProfileLoaderContext
from voidx.agent.application.agent_profile_service import (
    AgentProfileConflictError,
    AgentProfileReadOnlyError,
    AgentProfileService,
)
from voidx.agent.application.agent_registry import AgentRegistry


MINIMAL = """\
name: my-reviewer
revision: 1
display_name: My Reviewer
"""

UPDATED = """\
name: my-reviewer
revision: 2
display_name: Updated Reviewer
"""


def _service(tmp_path: Path) -> tuple[AgentProfileService, AgentRegistry]:
    bundled = tmp_path / "bundled"
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    bundled.mkdir()
    registry = AgentRegistry(
        str(tmp_path),
        bundled_dir=bundled,
        global_dir=global_dir,
        project_dir=project,
        loader_context=ProfileLoaderContext(),
    )
    return AgentProfileService(registry), registry


def test_validate_accepts_yaml_or_payload_without_writing(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)

    from_yaml = service.validate_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL
    )
    from_payload = service.validate_profile(
        scope="project",
        name="my-reviewer",
        payload={"name": "my-reviewer", "revision": 1, "display_name": "My Reviewer"},
    )

    assert from_yaml.valid is True
    assert from_yaml.snapshot is not None
    assert from_payload.valid is True
    assert from_payload.snapshot is not None
    assert from_payload.snapshot.content_hash == from_yaml.snapshot.content_hash
    assert registry.project_dir.exists() is False


def test_validate_returns_loader_diagnostics_as_data(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    result = service.validate_profile(
        scope="project", name="my-reviewer", yaml_text="name: wrong\nrevision: 1\n"
    )

    assert result.valid is False
    assert result.snapshot is None
    assert result.diagnostics
    assert any(d.code == "name_mismatch" for d in result.diagnostics)


@pytest.mark.parametrize("scope", ["bundled", "elsewhere", "../global"])
def test_mutating_scope_must_be_global_or_project(tmp_path: Path, scope: str) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="scope"):
        service.validate_profile(scope=scope, name="my-reviewer", yaml_text=MINIMAL)


@pytest.mark.parametrize("name", ["../escape", "UPPER", "bad_name", ""])
def test_name_must_be_a_canonical_profile_name(tmp_path: Path, name: str) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="name"):
        service.validate_profile(scope="project", name=name, yaml_text=MINIMAL)


def test_save_create_and_update_with_revision_or_hash_guards(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)

    created = service.save_profile(
        scope="project",
        name="my-reviewer",
        yaml_text=MINIMAL,
        expected_revision=0,
    )
    assert created.snapshot.revision == 1
    assert registry.resolve("my-reviewer").runtime_profile.name == "My Reviewer"

    updated = service.save_profile(
        scope="project",
        name="my-reviewer",
        yaml_text=UPDATED,
        expected_hash=created.snapshot.content_hash,
    )
    assert updated.snapshot.revision == 2
    assert registry.resolve("my-reviewer").runtime_profile.name == "Updated Reviewer"

    with pytest.raises(AgentProfileConflictError) as exc_info:
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text="name: my-reviewer\nrevision: 3\n",
            expected_revision=1,
        )
    assert exc_info.value.current is not None
    assert exc_info.value.current.revision == 2


def test_save_returns_target_scope_snapshot_when_higher_scope_shadows_it(
    tmp_path: Path,
) -> None:
    service, registry = _service(tmp_path)
    registry.project_dir.mkdir(parents=True)
    (registry.project_dir / "my-reviewer.yaml").write_text(
        "name: my-reviewer\nrevision: 7\ndisplay_name: Project Override\n",
        encoding="utf-8",
    )

    saved = service.save_profile(
        scope="global",
        name="my-reviewer",
        yaml_text=MINIMAL,
        expected_revision=0,
    )

    assert saved.snapshot.source == "global"
    assert saved.snapshot.revision == 1
    assert registry.resolve("my-reviewer").snapshot.source == "project"


def test_save_persists_normalized_canonical_payload(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)

    saved = service.save_profile(
        scope="project",
        name="my-reviewer",
        yaml_text="display_name: My Reviewer\nrevision: 1\nname: my-reviewer\n",
        expected_revision=0,
    )

    path = registry.project_dir / "my-reviewer.yaml"
    persisted = path.read_text(encoding="utf-8")
    assert persisted.startswith("name: my-reviewer\nrevision: 1\n")
    assert "prompt_policy: coding\n" in persisted
    assert registry.resolve("my-reviewer").snapshot.content_hash == saved.snapshot.content_hash


def test_save_requires_exactly_one_optimistic_guard(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="expected"):
        service.save_profile(
            scope="project", name="my-reviewer", yaml_text=MINIMAL
        )
    with pytest.raises(ValueError, match="expected"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=MINIMAL,
            expected_revision=0,
            expected_hash="",
        )


def test_save_rejects_revision_jump(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )

    result = service.validate_profile(
        scope="project",
        name="my-reviewer",
        yaml_text="name: my-reviewer\nrevision: 4\n",
    )
    assert result.valid is True

    with pytest.raises(AgentProfileConflictError, match="revision"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text="name: my-reviewer\nrevision: 4\n",
            expected_revision=1,
        )


def test_failed_replace_leaves_old_file_and_registry_value(tmp_path: Path, monkeypatch) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    path = registry.project_dir / "my-reviewer.yaml"
    before = path.read_bytes()

    def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_hash=created.snapshot.content_hash,
        )

    assert path.read_bytes() == before
    assert registry.resolve("my-reviewer").snapshot.revision == 1
    assert list(path.parent.glob(".my-reviewer.*.tmp")) == []


def test_corrupt_existing_file_cannot_be_silently_overwritten(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    registry.project_dir.mkdir(parents=True)
    path = registry.project_dir / "my-reviewer.yaml"
    path.write_text("name: [\n", encoding="utf-8")

    with pytest.raises(AgentProfileConflictError) as exc_info:
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=MINIMAL,
            expected_revision=0,
        )

    assert exc_info.value.diagnostics
    assert path.read_text(encoding="utf-8") == "name: [\n"


def test_failed_directory_fsync_after_replace_restores_old_file(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    path = registry.project_dir / "my-reviewer.yaml"
    before = path.read_bytes()

    def fail_fsync(_directory: Path) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(service, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_hash=created.snapshot.content_hash,
        )

    assert path.read_bytes() == before
    assert registry.resolve("my-reviewer").snapshot.revision == 1
    assert list(path.parent.glob(".my-reviewer.*.backup")) == []


def test_delete_requires_guard_and_does_not_invalidate_held_snapshot(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="global", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    held = registry.resolve("my-reviewer")

    service.delete_profile(
        scope="global",
        name="my-reviewer",
        expected_hash=created.snapshot.content_hash,
    )

    assert held.snapshot.content_hash == created.snapshot.content_hash
    with pytest.raises(KeyError):
        registry.resolve("my-reviewer")


def test_save_bundled_profile_is_read_only(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(AgentProfileReadOnlyError):
        service.save_profile(
            scope="bundled",
            name="coding",
            yaml_text="name: coding\nrevision: 2\n",
            expected_revision=1,
        )


def test_failed_delete_commit_fsync_restores_profile(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    path = registry.project_dir / "my-reviewer.yaml"
    before = path.read_bytes()

    def fail_fsync(_directory: Path) -> None:
        raise OSError("delete fsync failed")

    monkeypatch.setattr(service, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="delete fsync failed"):
        service.delete_profile(
            scope="project",
            name="my-reviewer",
            expected_hash=created.snapshot.content_hash,
        )

    assert path.read_bytes() == before
    assert registry.resolve("my-reviewer").snapshot.revision == 1
    assert list(path.parent.glob(".my-reviewer.*.delete")) == []


def test_bundled_profiles_are_read_only(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    (registry.bundled_dir / "coding.yaml").write_text(
        "name: coding\nrevision: 1\n", encoding="utf-8"
    )

    with pytest.raises(AgentProfileReadOnlyError):
        service.delete_profile(scope="bundled", name="coding", expected_revision=1)


def test_save_rollback_failure_preserves_old_backup(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    path = registry.project_dir / "my-reviewer.yaml"
    before = path.read_bytes()
    real_replace = os.replace
    replace_calls = 0

    def fail_rollback(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("rollback failed")
        real_replace(src, dst)

    def fail_fsync(_directory: Path) -> None:
        raise OSError("commit fsync failed")

    monkeypatch.setattr(os, "replace", fail_rollback)
    monkeypatch.setattr(service, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="rollback failed"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_hash=created.snapshot.content_hash,
        )

    backups = list(path.parent.glob(".my-reviewer.*.backup"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_delete_rollback_failure_preserves_old_tombstone(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project", name="my-reviewer", yaml_text=MINIMAL, expected_revision=0
    )
    path = registry.project_dir / "my-reviewer.yaml"
    before = path.read_bytes()
    real_replace = os.replace
    replace_calls = 0

    def fail_rollback(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("rollback failed")
        real_replace(src, dst)

    def fail_fsync(_directory: Path) -> None:
        raise OSError("commit fsync failed")

    monkeypatch.setattr(os, "replace", fail_rollback)
    monkeypatch.setattr(service, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="rollback failed"):
        service.delete_profile(
            scope="project",
            name="my-reviewer",
            expected_hash=created.snapshot.content_hash,
        )

    tombstones = list(path.parent.glob(".my-reviewer.*.delete"))
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == before


def test_save_canonical_workflow_round_trips_expanded_nodes(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    workflow = """\
name: workflow-reviewer
revision: 1
workflow:
  nodes:
    - ref: review
      rules: [extra-rule]
"""

    saved = service.save_profile(
        scope="project",
        name="workflow-reviewer",
        yaml_text=workflow,
        expected_revision=0,
    )

    persisted = (registry.project_dir / "workflow-reviewer.yaml").read_text(
        encoding="utf-8"
    )
    assert "ref: review" not in persisted
    resolved = registry.resolve("workflow-reviewer")
    assert resolved.snapshot.content_hash == saved.snapshot.content_hash
    assert resolved.workflow_context is not None
    assert "extra-rule" in resolved.workflow_context.dag.nodes["review"].rules


def test_save_rejects_symlink_target(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    registry.project_dir.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text(MINIMAL, encoding="utf-8")
    target = registry.project_dir / "my-reviewer.yaml"
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_revision=1,
        )

    assert outside.read_text(encoding="utf-8") == MINIMAL


def test_read_error_diagnostic_does_not_expose_target_path(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    registry.project_dir.mkdir(parents=True)
    target = registry.project_dir / "my-reviewer.yaml"
    target.write_text(MINIMAL, encoding="utf-8")
    original_read_text = Path.read_text

    def fail_target_read(path: Path, *args, **kwargs) -> str:
        if path == target:
            raise PermissionError(f"permission denied: {target}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_target_read)
    with pytest.raises(AgentProfileConflictError) as exc_info:
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_revision=1,
        )

    diagnostic = exc_info.value.diagnostics[0]
    assert diagnostic.code == "read_error"
    assert diagnostic.message == "agent profile could not be read"
    assert str(tmp_path) not in diagnostic.model_dump_json()


def test_get_profile_reads_exact_scope_as_canonical_yaml(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    registry.global_dir.mkdir(parents=True)
    registry.project_dir.mkdir(parents=True)
    (registry.global_dir / "my-reviewer.yaml").write_text(MINIMAL, encoding="utf-8")
    (registry.project_dir / "my-reviewer.yaml").write_text(
        "name: my-reviewer\nrevision: 7\ndisplay_name: Project Override\n",
        encoding="utf-8",
    )

    detail = service.get_profile(scope="global", name="my-reviewer")

    assert detail.info.source == "global"
    assert detail.info.revision == 1
    assert detail.read_only is False
    assert detail.yaml_text.startswith("name: my-reviewer\nrevision: 1\n")
    assert "prompt_policy: coding\n" in detail.yaml_text
    assert str(registry.global_dir) not in detail.model_dump_json()


def test_get_bundled_profile_is_read_only(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)
    (registry.bundled_dir / "coding.yaml").write_text(
        "name: coding\nrevision: 1\n", encoding="utf-8"
    )

    detail = service.get_profile(scope="bundled", name="coding")

    assert detail.info.source == "bundled"
    assert detail.read_only is True
    assert detail.yaml_text.startswith("name: coding\nrevision: 1\n")


def test_get_profile_rejects_unknown_and_invalid_existing_profile(tmp_path: Path) -> None:
    service, registry = _service(tmp_path)

    with pytest.raises(KeyError):
        service.get_profile(scope="project", name="missing")

    registry.project_dir.mkdir(parents=True)
    (registry.project_dir / "broken.yaml").write_text("name: [\n", encoding="utf-8")
    with pytest.raises(AgentProfileConflictError) as exc_info:
        service.get_profile(scope="project", name="broken")
    assert exc_info.value.diagnostics
    assert str(registry.project_dir) not in str(exc_info.value.diagnostics)


@pytest.mark.parametrize("linked_component", ["agents", ".voidx"])
def test_save_rejects_symlinked_project_directory_components(
    tmp_path: Path, linked_component: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundled = tmp_path / "bundled-safe"
    bundled.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    project_dir = workspace / ".voidx" / "agents"
    if linked_component == "agents":
        project_dir.parent.mkdir(parents=True)
        project_dir.symlink_to(outside, target_is_directory=True)
    else:
        (workspace / ".voidx").symlink_to(outside, target_is_directory=True)
    registry = AgentRegistry(
        str(workspace),
        bundled_dir=bundled,
        global_dir=tmp_path / "global-safe",
        project_dir=project_dir,
        loader_context=ProfileLoaderContext(),
    )
    service = AgentProfileService(registry)

    with pytest.raises(ValueError, match="symbolic link|outside"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=MINIMAL,
            expected_revision=0,
        )

    assert not (outside / "my-reviewer.yaml").exists()


def test_save_rechecks_guard_immediately_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project",
        name="my-reviewer",
        yaml_text=MINIMAL,
        expected_revision=0,
    )
    target = registry.project_dir / "my-reviewer.yaml"
    external = "name: my-reviewer\nrevision: 2\ndisplay_name: External Writer\n"
    original_atomic_write = service._atomic_write

    def race_before_commit(*args, **kwargs) -> None:
        target.write_text(external, encoding="utf-8")
        original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(service, "_atomic_write", race_before_commit)

    with pytest.raises(AgentProfileConflictError, match="changed"):
        service.save_profile(
            scope="project",
            name="my-reviewer",
            yaml_text=UPDATED,
            expected_hash=created.snapshot.content_hash,
        )

    assert target.read_text(encoding="utf-8") == external


def test_delete_rechecks_guard_immediately_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    service, registry = _service(tmp_path)
    created = service.save_profile(
        scope="project",
        name="my-reviewer",
        yaml_text=MINIMAL,
        expected_revision=0,
    )
    target = registry.project_dir / "my-reviewer.yaml"
    external = "name: my-reviewer\nrevision: 2\ndisplay_name: External Writer\n"
    original_atomic_delete = service._atomic_delete

    def race_before_commit(*args, **kwargs) -> None:
        target.write_text(external, encoding="utf-8")
        original_atomic_delete(*args, **kwargs)

    monkeypatch.setattr(service, "_atomic_delete", race_before_commit)

    with pytest.raises(AgentProfileConflictError, match="changed"):
        service.delete_profile(
            scope="project",
            name="my-reviewer",
            expected_hash=created.snapshot.content_hash,
        )

    assert target.read_text(encoding="utf-8") == external
