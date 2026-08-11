"""Pure filesystem grant resolution rules."""

from __future__ import annotations

from pathlib import Path

from voidx.tooling.domain.grants import (
    AccessGrant,
    AccessGrants,
    AccessIntent,
    AccessMode,
    AccessResolution,
    GrantDelta,
    GrantPersistence,
    ObjectType,
)


def resolve_access(
    workspace: str,
    file_path: str,
    *,
    access: AccessMode,
    readable_files: tuple[str, ...] | list[str] = (),
    readable_dirs: tuple[str, ...] | list[str] = (),
    writable_files: tuple[str, ...] | list[str] = (),
    writable_dirs: tuple[str, ...] | list[str] = (),
    access_grants: AccessGrants | None = None,
    require_exists: bool = False,
    allow_missing_write_file: bool = False,
    object_type: ObjectType | None = None,
) -> AccessResolution:
    """Resolve a tool path against workspace and grants without side effects."""
    normalized = _normalize_path(workspace, file_path)
    if normalized is None:
        return AccessResolution("deny", reason=f"Path traversal blocked: {file_path}")

    workspace_path = Path(workspace).expanduser().resolve()
    is_workspace_path = _contains(workspace_path, normalized)
    resolved_object_type: ObjectType = object_type or ("dir" if normalized.is_dir() else "file")
    intent = AccessIntent(
        requested_path=file_path,
        normalized_path=normalized,
        access=access,
        object_type=resolved_object_type,
        is_workspace_path=is_workspace_path,
        grant_matched=False,
    )

    grants = access_grants or AccessGrants.from_parts(
        readable_files=readable_files,
        readable_dirs=readable_dirs,
        writable_files=writable_files,
        writable_dirs=writable_dirs,
    )
    if is_workspace_path:
        return AccessResolution("allow", intent=intent)
    if not grants.permission_state_ready:
        return AccessResolution("deny", intent=intent, reason="Permission state not ready.")
    if require_exists and not normalized.exists():
        return AccessResolution("defer", intent=intent, reason=f"File not found; external path deferred: {file_path}")
    if access == "write" and not normalized.exists() and not allow_missing_write_file:
        return AccessResolution("defer", intent=intent, reason=f"Path does not exist; external path deferred: {file_path}")
    if _matches_grant(
        normalized,
        access,
        grants.readable_files,
        grants.readable_dirs,
        grants.writable_files,
        grants.writable_dirs,
    ):
        return AccessResolution(
            "allow",
            intent=AccessIntent(
                requested_path=file_path,
                normalized_path=normalized,
                access=access,
                object_type=resolved_object_type,
                is_workspace_path=False,
                grant_matched=True,
            ),
        )
    return AccessResolution("defer", intent=intent, reason=f"Permission deferred to tool: {file_path}")


def grant_for_intent(
    intent: AccessIntent,
    persistence: GrantPersistence,
    *,
    object_type: ObjectType | None = None,
) -> AccessGrant:
    selected_type = object_type or intent.object_type
    path = intent.normalized_path if selected_type == "file" else _grant_dir_for_intent(intent)
    return AccessGrant(path=str(path), access=intent.access, object_type=selected_type, persistence=persistence)


def persistent_grants_from_paths(
    readable_files: list[str],
    readable_dirs: list[str],
    writable_files: list[str],
    writable_dirs: list[str],
) -> list[AccessGrant]:
    return [
        *(AccessGrant(path=path, access="read", object_type="file", persistence="persistent") for path in readable_files),
        *(AccessGrant(path=path, access="read", object_type="dir", persistence="persistent") for path in readable_dirs),
        *(AccessGrant(path=path, access="write", object_type="file", persistence="persistent") for path in writable_files),
        *(AccessGrant(path=path, access="write", object_type="dir", persistence="persistent") for path in writable_dirs),
    ]


def delta_for_grant(grant: AccessGrant) -> GrantDelta:
    if grant.access == "read" and grant.object_type == "file":
        return GrantDelta(readable_files=[grant.path])
    if grant.access == "read" and grant.object_type == "dir":
        return GrantDelta(readable_dirs=[grant.path])
    if grant.access == "write" and grant.object_type == "file":
        return GrantDelta(writable_files=[grant.path])
    return GrantDelta(writable_dirs=[grant.path])


def _grant_dir_for_intent(intent: AccessIntent) -> Path:
    if intent.object_type == "dir":
        return intent.normalized_path
    return intent.normalized_path.parent


def _normalize_path(workspace: str, file_path: str) -> Path | None:
    if not file_path:
        return None
    try:
        workspace_path = Path(workspace).expanduser().resolve()
        raw = Path(file_path)
        if file_path.startswith("~") or raw.is_absolute():
            return raw.expanduser().resolve(strict=False)
        return (workspace_path / raw).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _matches_grant(
    path: Path,
    access: AccessMode,
    readable_files: tuple[str, ...] | list[str],
    readable_dirs: tuple[str, ...] | list[str],
    writable_files: tuple[str, ...] | list[str],
    writable_dirs: tuple[str, ...] | list[str],
) -> bool:
    file_grants = [*writable_files]
    dir_grants = [*writable_dirs]
    if access == "read":
        file_grants.extend(readable_files)
        dir_grants.extend(readable_dirs)
    return _matches_file_grants(path, file_grants) or _matches_dir_grants(path, dir_grants)


def _matches_file_grants(path: Path, grants: tuple[str, ...] | list[str]) -> bool:
    return any(path == grant for grant in _normalized_grants(grants))


def _matches_dir_grants(path: Path, grants: tuple[str, ...] | list[str]) -> bool:
    return any(_contains(grant, path) for grant in _normalized_grants(grants))


def _normalized_grants(grants: tuple[str, ...] | list[str]) -> list[Path]:
    paths: list[Path] = []
    for grant in grants:
        try:
            paths.append(Path(grant).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return paths


def _contains(base: Path, path: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except (OSError, ValueError):
        return False
