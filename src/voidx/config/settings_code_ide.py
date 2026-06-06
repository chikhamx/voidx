"""Code IDE preference settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.config.enums import CodeIde


class SettingsCodeIdeMixin:
    def get_code_ide(self) -> CodeIde:
        raw = self._data.get("codeIde", CodeIde.TRAE.value)
        try:
            return CodeIde(raw)
        except ValueError:
            return CodeIde.TRAE

    def set_code_ide(self, ide: CodeIde) -> Path:
        self._data["codeIde"] = ide.value
        self._save()
        return self._path
