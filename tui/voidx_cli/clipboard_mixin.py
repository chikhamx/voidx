"""Clipboard image paste support for PureTui."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from voidx.ui.tools.clipboard_image import (
    ClipboardImageResult,
    paste_clipboard_image as _paste_clipboard_image_from_system,
)
from voidx.ui.tools.clipboard_text import (
    ClipboardTextResult,
    read_clipboard_text as _read_clipboard_text_from_system,
)


class _ClipboardMixin:
    def paste_clipboard_image(self, *, quiet_no_image: bool = False) -> ClipboardImageResult:
        result = _paste_clipboard_image(self.status.workspace)
        if result.ok:
            stem = Path(result.rel_path).stem
            self._insert_text_token(self._register_image_paste(stem, result.size) + " ")
        if result.ok or not quiet_no_image:
            self._notice = result.message
        self.invalidate()
        return result

    def _paste_clipboard_image_quiet(self) -> None:
        self.paste_clipboard_image(quiet_no_image=True)

    def paste_clipboard_text(self, *, quiet_no_text: bool = False) -> ClipboardTextResult:
        result = _read_clipboard_text()
        if result.ok:
            self._insert_pasted_text(result.text)
        if result.ok or not quiet_no_text:
            self._notice = result.message
        self.invalidate()
        return result

    def _paste_clipboard_text_quiet(self) -> ClipboardTextResult:
        return self.paste_clipboard_text(quiet_no_text=True)

    def _paste_clipboard_quiet(self) -> None:
        image_result = self.paste_clipboard_image(quiet_no_image=True)
        if image_result.ok:
            return
        if image_result.status not in {"no_image", "unsupported"}:
            return
        self._paste_clipboard_text_quiet()

    _CHANGE_COUNT_SCRIPT = (
        'use framework "AppKit"\n'
        "set pb to current application's NSPasteboard's generalPasteboard()\n"
        "return (pb's changeCount) as text"
    )

    def _read_clipboard_change_count(self) -> int:
        try:
            result = subprocess.run(
                ["osascript", "-e", self._CHANGE_COUNT_SCRIPT],
                capture_output=True, text=True, timeout=2, check=False,
            )
            return int(result.stdout.strip()) if result.returncode == 0 else -1
        except Exception:
            return -1


def _paste_clipboard_image(workspace: str) -> ClipboardImageResult:
    app_module = sys.modules.get("voidx_cli.app")
    paste = getattr(app_module, "paste_clipboard_image_from_system", None)
    if callable(paste):
        return paste(workspace)
    return _paste_clipboard_image_from_system(workspace)


def _read_clipboard_text() -> ClipboardTextResult:
    app_module = sys.modules.get("voidx_cli.app")
    paste = getattr(app_module, "paste_clipboard_text_from_system", None)
    if callable(paste):
        return paste()
    return _read_clipboard_text_from_system()
