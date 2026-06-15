"""Permission preset settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.enums import ApprovalPolicy, ApprovalReviewer, PermissionMode, SandboxMode
from voidx.config.permissions import permission_mode_defaults, permission_mode_reviewer_default
from voidx.config.settings_utils import string_list as _string_list


class SettingsPermissionMixin:
    def get_permission_mode(self) -> PermissionMode:
        raw = self._effective_data().get("permission_mode")
        if raw is not None:
            try:
                return PermissionMode(raw)
            except ValueError:
                return PermissionMode.CUSTOM
        if (
            "sandbox_mode" in self._effective_data()
            or "approval_policy" in self._effective_data()
            or self.get_sandbox_workspace_write()
        ):
            return PermissionMode.CUSTOM
        return PermissionMode.DEFAULT

    def set_permission_mode(self, mode: PermissionMode) -> Path:
        self._data["permission_mode"] = mode.value
        if mode != PermissionMode.CUSTOM:
            sandbox_mode, approval_policy = permission_mode_defaults(mode)
            self._data["sandbox_mode"] = sandbox_mode.value
            self._data["approval_policy"] = approval_policy.value
            self._data["approval_reviewer"] = permission_mode_reviewer_default(mode).value
            self._data.pop("sandbox_workspace_write", None)
        self._save()
        return self._path

    def get_sandbox_mode(self) -> SandboxMode:
        raw = self._effective_data().get("sandbox_mode", "workspace-write")
        try:
            return SandboxMode(raw)
        except ValueError:
            return SandboxMode.WORKSPACE_WRITE

    def set_sandbox_mode(self, mode: SandboxMode) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["sandbox_mode"] = mode.value
        self._save()
        return self._path

    def get_sandbox_workspace_write(self) -> list[str]:
        paths = self._effective_data().get("sandbox_workspace_write", [])
        return _string_list(paths)

    def set_sandbox_workspace_write(self, paths: list[str]) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["sandbox_workspace_write"] = list(paths)
        self._save()
        return self._path

    def get_approval_policy(self) -> ApprovalPolicy:
        raw = self._effective_data().get("approval_policy", "untrusted")
        try:
            return ApprovalPolicy(raw)
        except ValueError:
            return ApprovalPolicy.UNTRUSTED

    def set_approval_policy(self, policy: ApprovalPolicy) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["approval_policy"] = policy.value
        self._save()
        return self._path

    def get_approval_reviewer(self) -> ApprovalReviewer:
        raw = self._effective_data().get("approval_reviewer", "user")
        try:
            return ApprovalReviewer(raw)
        except ValueError:
            return ApprovalReviewer.USER

    def set_approval_reviewer(self, reviewer: ApprovalReviewer) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["approval_reviewer"] = reviewer.value
        self._save()
        return self._path
