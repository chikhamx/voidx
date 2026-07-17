from __future__ import annotations

from pathlib import Path


class SettingsLspMixin:
    def get_lsp_format_after_edit(self) -> bool:
        raw = self._effective_data().get("lsp", {})
        if not isinstance(raw, dict):
            return True
        value = raw.get("format_after_edit", True)
        return value if isinstance(value, bool) else True

    def set_lsp_format_after_edit(self, enabled: bool) -> Path:
        value, _, target = self._target_mapping("lsp")
        value["format_after_edit"] = bool(enabled)
        return self._save_target_mapping("lsp", value, target)
