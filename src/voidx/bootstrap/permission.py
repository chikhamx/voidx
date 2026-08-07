"""Permission service adapter composition."""

from __future__ import annotations

from collections.abc import Callable

from voidx.config import Config, Settings
from voidx.config.adapters.permission_grant_repository import PermissionGrantRepository
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service
from voidx.tooling.policy.filesystem.grants import persistent_grants_from_paths


def build_permission_service(
    config: Config,
    *,
    settings: Settings | None = None,
    notifier: Callable[[str], object],
):
    from voidx.persistence.sqlite import DATA_DIR

    writable_dirs = list(config.sandbox_writable_dirs)
    data_dir = str(DATA_DIR.resolve())
    if data_dir not in writable_dirs:
        writable_dirs.append(data_dir)
    return create_permission_service(
        permission_mode=config.permission_mode.value,
        sandbox_readable_files=list(config.sandbox_readable_files),
        sandbox_readable_dirs=list(config.sandbox_readable_dirs),
        sandbox_writable_files=list(config.sandbox_writable_files),
        sandbox_writable_dirs=writable_dirs,
        persistent_grants=persistent_grants_from_paths(
            settings.get_persistent_readable_files(),
            settings.get_persistent_readable_dirs(),
            settings.get_persistent_writable_files(),
            settings.get_persistent_writable_dirs(),
        ) if settings is not None else [],
        notifier=notifier,
        persistent_grant_writer=PermissionGrantRepository(settings) if settings is not None else None,
    )
