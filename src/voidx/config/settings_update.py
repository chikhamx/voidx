"""Update check settings helpers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60


class SettingsUpdateMixin:
    def get_update_check_enabled(self) -> bool:
        data = self._update_check_data()
        enabled = data.get("enabled", True)
        return enabled if isinstance(enabled, bool) else True

    def set_update_check_enabled(self, enabled: bool) -> Path:
        data = self._update_check_data()
        data["enabled"] = bool(enabled)
        self._data["update_check"] = data
        self._save()
        return self._path

    def get_update_check_last_checked_at(self) -> int | None:
        data = self._update_check_data()
        value = data.get("last_checked_at")
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        return None

    def get_update_check_latest_version(self) -> str | None:
        data = self._update_check_data()
        value = data.get("last_latest_version")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def update_check_due(
        self,
        *,
        now: int | None = None,
        interval_seconds: int = UPDATE_CHECK_INTERVAL_SECONDS,
    ) -> bool:
        if not self.get_update_check_enabled():
            return False
        last_checked_at = self.get_update_check_last_checked_at()
        if last_checked_at is None:
            return True
        current = int(time.time()) if now is None else int(now)
        return current - last_checked_at >= interval_seconds

    def mark_update_check(
        self,
        latest_version: str | None = None,
        *,
        now: int | None = None,
    ) -> Path:
        data = self._update_check_data()
        data.setdefault("enabled", True)
        data["last_checked_at"] = int(time.time()) if now is None else int(now)
        if latest_version:
            data["last_latest_version"] = latest_version
        else:
            data.pop("last_latest_version", None)
        self._data["update_check"] = data
        self._save()
        return self._path

    def _update_check_data(self) -> dict[str, Any]:
        data = self._data.get("update_check", {})
        return dict(data) if isinstance(data, dict) else {}
