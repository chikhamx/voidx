"""Clipboard image paste support for PureTui."""

from __future__ import annotations

from pathlib import Path

from voidx.presentation.tools.clipboard_image import (
    ClipboardImageResult,
    paste_clipboard_image as _paste_clipboard_image_from_system,
)
from voidx.presentation.tools.clipboard_text import (
    ClipboardTextResult,
    read_clipboard_text as _read_clipboard_text_from_system,
)


class _ClipboardMixin:
    def paste_clipboard_image(self, *, quiet_no_image: bool = False) -> ClipboardImageResult:
        result = _paste_clipboard_image_from_system(self.status.workspace)
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
        result = _read_clipboard_text_from_system()
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


