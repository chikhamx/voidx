"""Clipboard text capture helpers."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Callable


CaptureClipboardText = Callable[[], tuple[str, str]]


@dataclass(frozen=True)
class ClipboardTextResult:
    status: str
    message: str
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def read_clipboard_text(
    *,
    capture_clipboard_text: CaptureClipboardText | None = None,
) -> ClipboardTextResult:
    capture = capture_clipboard_text or _capture_clipboard_text
    status, text = capture()
    if status != "ok":
        return ClipboardTextResult(status=_result_status(status), message=_capture_message(status))
    if not text:
        return ClipboardTextResult(status="no_text", message="Clipboard does not contain text.")
    return ClipboardTextResult(status="ok", message="Pasted text from clipboard.", text=text)


def _capture_clipboard_text() -> tuple[str, str]:
    if platform.system() != "Darwin":
        return "unsupported", ""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except FileNotFoundError:
        return "unsupported", ""
    except subprocess.TimeoutExpired:
        return "error: clipboard read timed out", ""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"error: {detail or 'clipboard read failed'}", ""
    return "ok", result.stdout


def _capture_message(status: str) -> str:
    if status == "no_text":
        return "Clipboard does not contain text."
    if status == "unsupported":
        return "Clipboard text paste fallback is only supported on macOS right now."
    if status.startswith("error:"):
        return status.removeprefix("error:").strip() or "Clipboard text paste failed."
    return "Clipboard text paste failed."


def _result_status(status: str) -> str:
    if status in {"no_text", "unsupported"}:
        return status
    return "error"
