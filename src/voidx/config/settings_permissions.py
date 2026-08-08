"""Permission preset settings helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock

from voidx.config.enums import PermissionMode
from voidx.config.models import AiApprovalConfig
from voidx.config.settings_utils import string_list as _string_list
from voidx.config.grants import GrantDelta


logger = logging.getLogger(__name__)
_PERMISSION_TRANSACTION_LOCK = RLock()



class SettingsPermissionMixin:
    def get_ai_approval_config(self) -> AiApprovalConfig:
        raw = self._effective_data().get("ai_approval", {})
        try:
            return AiApprovalConfig.model_validate(raw if isinstance(raw, dict) else {})
        except Exception as exc:
            logger.warning("Invalid AI approval settings; using defaults: %s", exc)
            return AiApprovalConfig()

    def set_ai_approval_config(self, config: AiApprovalConfig) -> Path:
        return self._set_setting("ai_approval", config.model_dump(mode="json"))

    def set_ai_approval_profile(self, profile_name: str) -> Path:
        current = self.get_ai_approval_config()
        return self.set_ai_approval_config(
            current.model_copy(update={"profile_name": profile_name})
        )

    def get_permission_mode(self) -> PermissionMode:
        raw = self._effective_data().get("permission_mode", PermissionMode.SAFE.value)
        try:
            return PermissionMode(raw)
        except ValueError:
            return PermissionMode.SAFE

    def set_permission_mode(self, preset: PermissionMode) -> Path:
        self._data["permission_mode"] = preset.value
        self._save()
        return self._path


    def get_sandbox_readable_files(self) -> list[str]:
        return _string_list(self._effective_data().get("sandbox_readable_files", []))

    def set_sandbox_readable_files(self, paths: list[str]) -> Path:
        return self._set_sandbox_grant("sandbox_readable_files", paths)

    def get_sandbox_readable_dirs(self) -> list[str]:
        return _string_list(self._effective_data().get("sandbox_readable_dirs", []))

    def set_sandbox_readable_dirs(self, paths: list[str]) -> Path:
        return self._set_sandbox_grant("sandbox_readable_dirs", paths)

    def get_sandbox_writable_files(self) -> list[str]:
        return _string_list(self._effective_data().get("sandbox_writable_files", []))

    def set_sandbox_writable_files(self, paths: list[str]) -> Path:
        return self._set_sandbox_grant("sandbox_writable_files", paths)

    def get_sandbox_writable_dirs(self) -> list[str]:
        return _string_list(self._effective_data().get("sandbox_writable_dirs", []))

    def set_sandbox_writable_dirs(self, paths: list[str]) -> Path:
        return self._set_sandbox_grant("sandbox_writable_dirs", paths)

    def _set_sandbox_grant(self, key: str, paths: list[str]) -> Path:
        self._data[key] = list(paths)
        self._save()
        return self._path

    def get_persistent_readable_files(self) -> list[str]:
        return _string_list(self._effective_data().get("persistent_readable_files", []))

    def get_persistent_readable_dirs(self) -> list[str]:
        return _string_list(self._effective_data().get("persistent_readable_dirs", []))

    def get_persistent_writable_files(self) -> list[str]:
        return _string_list(self._effective_data().get("persistent_writable_files", []))

    def get_persistent_writable_dirs(self) -> list[str]:
        return _string_list(self._effective_data().get("persistent_writable_dirs", []))


    def add_persistent_grant_delta(self, delta: GrantDelta) -> Path:
        with _PERMISSION_TRANSACTION_LOCK:
            latest = self._load_path(self._path)
            for key, values in (
                ("persistent_readable_files", delta.readable_files),
                ("persistent_readable_dirs", delta.readable_dirs),
                ("persistent_writable_files", delta.writable_files),
                ("persistent_writable_dirs", delta.writable_dirs),
            ):
                if not values:
                    continue
                merged = [*_string_list(latest.get(key, [])), *values]
                latest[key] = list(dict.fromkeys(merged))
            self._data = latest
            self._save()
            return self._path

    def _clear_sandbox_grants(self) -> None:
        for key in (
            "sandbox_readable_files",
            "sandbox_readable_dirs",
            "sandbox_writable_files",
            "sandbox_writable_dirs",
            "persistent_readable_files",
            "persistent_readable_dirs",
            "persistent_writable_files",
            "persistent_writable_dirs",
            "sandbox_workspace_write",
        ):
            self._data.pop(key, None)


