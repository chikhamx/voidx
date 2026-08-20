"""Validated lifecycle operations for editable agent profile files."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Mapping

import yaml
from pydantic import BaseModel, ConfigDict

from voidx.agent.application.agent_profile_loader import ProfileLoadError, load_profile
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.domain.agent_profile import (
    PROFILE_NAME_RE,
    AgentProfileInfo,
    AgentProfileSnapshot,
    ProfileDiagnostic,
    ProfileSource,
)


class AgentProfileValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    diagnostics: tuple[ProfileDiagnostic, ...] = ()
    snapshot: AgentProfileSnapshot | None = None


class AgentProfileSaveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot: AgentProfileSnapshot
    diagnostics: tuple[ProfileDiagnostic, ...] = ()


class AgentProfileDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    info: AgentProfileInfo
    yaml_text: str
    read_only: bool


class AgentProfileConflictError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        current: AgentProfileInfo | None = None,
        diagnostics: tuple[ProfileDiagnostic, ...] = (),
    ) -> None:
        self.current = current
        self.diagnostics = diagnostics
        super().__init__(message)


class AgentProfileReadOnlyError(RuntimeError):
    pass


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class AgentProfileService:
    """Owns validation and atomic persistence for editable profiles."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def list_profiles(self) -> list[AgentProfileInfo]:
        return self._registry.discover()

    def get_profile(self, *, scope: str, name: str) -> AgentProfileDetail:
        source, target = self._profile_target(scope, name)
        self._reject_symlink(target)
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise KeyError(f"agent profile not found: {name}") from exc
        except OSError as exc:
            diagnostic = ProfileDiagnostic(
                path="",
                code="read_error",
                message="agent profile could not be read",
            )
            raise AgentProfileConflictError(
                "agent profile could not be read",
                diagnostics=(diagnostic,),
            ) from exc
        try:
            resolved, diagnostics = load_profile(
                text,
                source=source,
                context=self._registry._resolve_context(),
                expected_name=name,
            )
        except ProfileLoadError as exc:
            raise AgentProfileConflictError(
                "agent profile is invalid",
                diagnostics=tuple(exc.diagnostics),
            ) from exc
        info = AgentProfileInfo(
            name=resolved.snapshot.profile_id,
            display_name=resolved.runtime_profile.name,
            revision=resolved.snapshot.revision,
            content_hash=resolved.snapshot.content_hash,
            source=resolved.snapshot.source,
            run_mode=resolved.run_config.run_mode,
            hitl_mode=resolved.resource_policy.hitl_mode,
            diagnostics=diagnostics,
        )
        return AgentProfileDetail(
            info=info,
            yaml_text=self._canonical_yaml(resolved.snapshot.canonical_payload),
            read_only=scope == "bundled",
        )

    def validate_profile(
        self,
        *,
        scope: str,
        name: str,
        yaml_text: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> AgentProfileValidationResult:
        source, _ = self._editable_target(scope, name)
        text = self._input_text(yaml_text=yaml_text, payload=payload)
        try:
            resolved, diagnostics = load_profile(
                text,
                source=source,
                context=self._registry._resolve_context(),
                expected_name=name,
            )
        except ProfileLoadError as exc:
            return AgentProfileValidationResult(
                valid=False,
                diagnostics=tuple(exc.diagnostics),
            )
        return AgentProfileValidationResult(
            valid=True,
            diagnostics=diagnostics,
            snapshot=resolved.snapshot,
        )

    def save_profile(
        self,
        *,
        scope: str,
        name: str,
        yaml_text: str | None = None,
        payload: Mapping[str, object] | None = None,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> AgentProfileSaveResult:
        if scope == "bundled":
            raise AgentProfileReadOnlyError("bundled profiles are read-only")
        source, target = self._editable_target(scope, name)
        self._require_guard(expected_revision, expected_hash)
        text = self._input_text(yaml_text=yaml_text, payload=payload)
        try:
            candidate, diagnostics = load_profile(
                text,
                source=source,
                context=self._registry._resolve_context(),
                expected_name=name,
            )
        except ProfileLoadError:
            raise

        with _lock_for(target):
            self._reject_symlink(target)
            current = self._read_current(target, source)
            self._check_guard(
                current=current,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
            )
            expected_next_revision = 1 if current is None else current.revision + 1
            if candidate.snapshot.revision != expected_next_revision:
                raise AgentProfileConflictError(
                    f"profile revision must be {expected_next_revision}",
                    current=current,
                )
            persisted_text = self._canonical_yaml(candidate.snapshot.canonical_payload)
            persisted, _ = load_profile(
                persisted_text,
                source=source,
                context=self._registry._resolve_context(),
                expected_name=name,
            )
            if persisted.snapshot.content_hash != candidate.snapshot.content_hash:
                raise RuntimeError("canonical profile serialization changed content hash")
            self._atomic_write(
                target,
                persisted_text.encode("utf-8"),
                source=source,
                expected_current=current,
            )
            self._registry.invalidate()

        return AgentProfileSaveResult(
            snapshot=candidate.snapshot,
            diagnostics=diagnostics,
        )

    def delete_profile(
        self,
        *,
        scope: str,
        name: str,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> None:
        if scope == "bundled":
            raise AgentProfileReadOnlyError("bundled profiles are read-only")
        source, target = self._editable_target(scope, name)
        self._require_guard(expected_revision, expected_hash)
        with _lock_for(target):
            self._reject_symlink(target)
            current = self._read_current(target, source)
            if current is None:
                raise KeyError(f"agent profile not found: {name}")
            self._check_guard(
                current=current,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
            )
            self._atomic_delete(
                target,
                source=source,
                expected_current=current,
            )
            self._registry.invalidate()

    def _profile_target(self, scope: str, name: str) -> tuple[ProfileSource, Path]:
        if scope not in {"bundled", "global", "project"}:
            raise ValueError("scope must be bundled, global, or project")
        if name != name.strip().lower() or PROFILE_NAME_RE.fullmatch(name) is None:
            raise ValueError("name must be a canonical agent profile name")
        roots = {
            "bundled": self._registry.bundled_dir,
            "global": self._registry.global_dir,
            "project": self._registry.project_dir,
        }
        root = roots[scope]
        target = root / f"{name}.yaml"
        self._validate_target_path(scope, root, target)
        return scope, target  # type: ignore[return-value]

    def _editable_target(self, scope: str, name: str) -> tuple[ProfileSource, Path]:
        if scope == "bundled":
            raise ValueError("scope must be global or project")
        return self._profile_target(scope, name)

    @staticmethod
    def _input_text(
        *,
        yaml_text: str | None,
        payload: Mapping[str, object] | None,
    ) -> str:
        if (yaml_text is None) == (payload is None):
            raise ValueError("exactly one of yaml_text or payload is required")
        if yaml_text is not None:
            if not isinstance(yaml_text, str):
                raise ValueError("yaml_text must be a string")
            return yaml_text
        assert payload is not None
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        return yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True)

    @staticmethod
    def _validate_target_path(scope: str, root: Path, target: Path) -> None:
        root_absolute = root.expanduser().absolute()
        target_absolute = target.expanduser().absolute()
        try:
            target_absolute.relative_to(root_absolute)
        except ValueError as exc:
            raise ValueError(f"agent profile target is outside the {scope} scope") from exc

        current = target_absolute
        while True:
            if current.is_symlink():
                raise ValueError("agent profile path must not contain a symbolic link")
            if current == current.parent:
                break
            current = current.parent

        resolved_root = root_absolute.resolve(strict=False)
        resolved_target = target_absolute.resolve(strict=False)
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"agent profile target is outside the {scope} scope") from exc

    @staticmethod
    def _reject_symlink(target: Path) -> None:
        if target.is_symlink():
            raise ValueError("agent profile target must not be a symbolic link")

    @staticmethod
    def _canonical_yaml(canonical_payload: Mapping[str, object]) -> str:
        payload = dict(canonical_payload)
        workflow = payload.get("workflow")
        if isinstance(workflow, dict):
            persisted_workflow = {
                key: value
                for key, value in workflow.items()
                if key in {"name", "nodes", "edges", "terminal_exit"}
            }
            nodes = persisted_workflow.get("nodes")
            if isinstance(nodes, dict):
                persisted_workflow["nodes"] = list(nodes.values())
            payload["workflow"] = persisted_workflow
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    @staticmethod
    def _require_guard(expected_revision: int | None, expected_hash: str | None) -> None:
        if (expected_revision is None) == (expected_hash is None):
            raise ValueError("exactly one expected_revision or expected_hash is required")
        if expected_revision is not None and expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")

    def _read_current(self, target: Path, source: ProfileSource) -> AgentProfileInfo | None:
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            diagnostic = ProfileDiagnostic(
                path="",
                code="read_error",
                message="agent profile could not be read",
            )
            raise AgentProfileConflictError(
                "current profile cannot be read",
                diagnostics=(diagnostic,),
            ) from exc
        try:
            resolved, diagnostics = load_profile(
                text,
                source=source,
                context=self._registry._resolve_context(),
                expected_name=target.stem,
            )
        except ProfileLoadError as exc:
            raise AgentProfileConflictError(
                "current profile is invalid",
                diagnostics=tuple(exc.diagnostics),
            ) from exc
        return AgentProfileInfo(
            name=resolved.snapshot.profile_id,
            display_name=resolved.runtime_profile.name,
            revision=resolved.snapshot.revision,
            content_hash=resolved.snapshot.content_hash,
            source=resolved.snapshot.source,
            run_mode=resolved.run_config.run_mode,
            hitl_mode=resolved.resource_policy.hitl_mode,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _check_guard(
        *,
        current: AgentProfileInfo | None,
        expected_revision: int | None,
        expected_hash: str | None,
    ) -> None:
        if current is None:
            matches = expected_revision == 0 if expected_revision is not None else expected_hash == ""
        elif expected_revision is not None:
            matches = current.revision == expected_revision
        else:
            matches = current.content_hash == expected_hash
        if not matches:
            raise AgentProfileConflictError("agent profile changed", current=current)

    def _ensure_current_unchanged(
        self,
        target: Path,
        *,
        source: ProfileSource,
        expected_current: AgentProfileInfo | None,
    ) -> None:
        current = self._read_current(target, source)
        if expected_current is None:
            unchanged = current is None
        elif current is None:
            unchanged = False
        else:
            unchanged = (
                current.revision == expected_current.revision
                and current.content_hash == expected_current.content_hash
            )
        if not unchanged:
            raise AgentProfileConflictError(
                "agent profile changed before commit",
                current=current,
            )

    def _atomic_write(
        self,
        target: Path,
        content: bytes,
        *,
        source: ProfileSource,
        expected_current: AgentProfileInfo | None,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._validate_target_path(source, target.parent, target)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        backup: Path | None = None
        replaced = False
        preserve_backup = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._ensure_current_unchanged(
                target,
                source=source,
                expected_current=expected_current,
            )
            if target.exists():
                backup_fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target.stem}.", suffix=".backup", dir=target.parent
                )
                os.close(backup_fd)
                backup = Path(backup_name)
                backup.unlink()
                os.link(target, backup)
            os.replace(temporary, target)
            replaced = True
            try:
                self._fsync_directory(target.parent)
            except Exception:
                if backup is not None:
                    try:
                        os.replace(backup, target)
                    except Exception:
                        preserve_backup = True
                        raise
                    backup = None
                else:
                    target.unlink(missing_ok=True)
                try:
                    self._fsync_directory(target.parent)
                except OSError:
                    pass
                raise
            if backup is not None:
                backup.unlink()
                backup = None
        finally:
            temporary.unlink(missing_ok=True)
            if backup is not None and not preserve_backup:
                if replaced and not target.exists():
                    os.replace(backup, target)
                else:
                    backup.unlink(missing_ok=True)

    def _atomic_delete(
        self,
        target: Path,
        *,
        source: ProfileSource,
        expected_current: AgentProfileInfo,
    ) -> None:
        self._ensure_current_unchanged(
            target,
            source=source,
            expected_current=expected_current,
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".delete", dir=target.parent
        )
        os.close(fd)
        tombstone = Path(temporary_name)
        tombstone.unlink()
        os.replace(target, tombstone)
        try:
            self._fsync_directory(target.parent)
        except Exception:
            os.replace(tombstone, target)
            try:
                self._fsync_directory(target.parent)
            except OSError:
                pass
            raise
        try:
            tombstone.unlink()
        except Exception:
            os.replace(tombstone, target)
            try:
                self._fsync_directory(target.parent)
            except OSError:
                pass
            raise
        try:
            self._fsync_directory(target.parent)
        except OSError:
            pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
