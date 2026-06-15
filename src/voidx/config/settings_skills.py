"""Skill selection settings helpers."""

from __future__ import annotations

import json
from pathlib import Path

from voidx.config.settings_utils import string_list as _string_list


class SettingsSkillsMixin:
    def get_skill_selection(self):
        from voidx.skills.schema import SkillSelectionConfig

        data = self._skills_data()
        return SkillSelectionConfig(
            enabled=set(_string_list(data.get("enabled", []))),
            disabled=set(_string_list(data.get("disabled", []))),
            auto=set(_string_list(data.get("auto", []))),
        )

    def set_skill_enabled(self, name: str, enabled: bool) -> Path:
        skills = self._skills_data()
        enabled_list = _string_list(skills.get("enabled", []))
        disabled_list = _string_list(skills.get("disabled", []))
        auto_list = _string_list(skills.get("auto", []))
        if enabled:
            if name not in enabled_list:
                enabled_list.append(name)
            disabled_list = [item for item in disabled_list if item != name]
        else:
            if name not in disabled_list:
                disabled_list.append(name)
            enabled_list = [item for item in enabled_list if item != name]
            auto_list = [item for item in auto_list if item != name]
        skills["version"] = 2
        skills["enabled"] = sorted(enabled_list)
        skills["disabled"] = sorted(disabled_list)
        skills["auto"] = sorted(auto_list)
        self._save_skills_data(skills)
        if self._data.pop("skills", None) is not None:
            self._save()
        return self.skills_path

    def set_skill_auto(self, name: str, auto: bool) -> Path:
        skills = self._skills_data()
        enabled_list = _string_list(skills.get("enabled", []))
        disabled_list = _string_list(skills.get("disabled", []))
        auto_list = _string_list(skills.get("auto", []))
        if auto:
            if name not in enabled_list:
                enabled_list.append(name)
            disabled_list = [item for item in disabled_list if item != name]
            if name not in auto_list:
                auto_list.append(name)
        else:
            auto_list = [item for item in auto_list if item != name]
        skills["version"] = 2
        skills["enabled"] = sorted(enabled_list)
        skills["disabled"] = sorted(disabled_list)
        skills["auto"] = sorted(auto_list)
        self._save_skills_data(skills)
        if self._data.pop("skills", None) is not None:
            self._save()
        return self.skills_path

    def _skills_data(self) -> dict:
        if self.skills_path.exists():
            try:
                skills = json.loads(self.skills_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
            return dict(skills) if isinstance(skills, dict) else {}
        skills = self._data.get("skills", {})
        return dict(skills) if isinstance(skills, dict) else {}

    def _save_skills_data(self, data: dict) -> None:
        self.skills_path.parent.mkdir(parents=True, exist_ok=True)
        self.skills_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
