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
    system = platform.system()
    if system == "Darwin":
        return _capture_clipboard_text_macos()
    if system == "Windows":
        return _capture_clipboard_text_windows()
    return "unsupported", ""


def _capture_clipboard_text_macos() -> tuple[str, str]:
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


def _capture_clipboard_text_windows() -> tuple[str, str]:
    try:
        text = _win32_clipboard_text()
    except OSError as exc:
        return f"error: {exc}", ""
    if text is None:
        return "no_text", ""
    return "ok", text


def _win32_clipboard_text() -> str | None:
    """Read text from the Windows clipboard via Win32 API.

    Returns the clipboard text, or ``None`` when the clipboard does not
    contain a text format.
    """
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        raise OSError("failed to open clipboard")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("failed to lock clipboard data")
        try:
            raw = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
        return raw.lstrip("\ufeff")
    finally:
        user32.CloseClipboard()


def _capture_message(status: str) -> str:
    if status == "no_text":
        return "Clipboard does not contain text."
    if status == "unsupported":
        return "Clipboard text paste is only supported on macOS and Windows."
    if status.startswith("error:"):
        return status.removeprefix("error:").strip() or "Clipboard text paste failed."
    return "Clipboard text paste failed."


def _result_status(status: str) -> str:
    if status in {"no_text", "unsupported"}:
        return status
    return "error"
