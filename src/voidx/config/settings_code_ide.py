"""Code IDE preference settings helpers."""

from __future__ import annotations

from pathlib import Path

from voidx.platform.code_ide import CodeIde


class SettingsCodeIdeMixin:
    def get_code_ide(self) -> CodeIde:
        raw = self._effective_data().get("codeIde", CodeIde.TRAE.value)
        try:
            return CodeIde(raw)
        except ValueError:
            return CodeIde.TRAE

    def set_code_ide(self, ide: CodeIde) -> Path:
        return self._set_setting("codeIde", ide.value)
