"""Windows clipboard text capture tests.

These tests monkeypatch ``platform.system`` to simulate Windows so the
``_capture_clipboard_text`` Windows branch is exercised on any OS.
"""

from __future__ import annotations

import pytest

from voidx.ui.tools import clipboard_text


def _patch_windows(monkeypatch):
    monkeypatch.setattr(clipboard_text.platform, "system", lambda: "Windows")


def test_windows_capture_text_returns_ok(monkeypatch):
    _patch_windows(monkeypatch)
    monkeypatch.setattr(clipboard_text, "_win32_clipboard_text", lambda: "hello from windows")

    status, text = clipboard_text._capture_clipboard_text()
    assert status == "ok"
    assert text == "hello from windows"


def test_windows_capture_text_empty_returns_no_text(monkeypatch):
    _patch_windows(monkeypatch)
    monkeypatch.setattr(clipboard_text, "_win32_clipboard_text", lambda: "")

    result = clipboard_text.read_clipboard_text()
    assert result.status == "no_text"


def test_windows_capture_text_no_text_format_returns_no_text(monkeypatch):
    _patch_windows(monkeypatch)
    monkeypatch.setattr(clipboard_text, "_win32_clipboard_text", lambda: None)

    result = clipboard_text.read_clipboard_text()
    assert result.status == "no_text"


def test_windows_capture_text_error_returns_error(monkeypatch):
    _patch_windows(monkeypatch)

    def _boom():
        raise OSError("clipboard locked")

    monkeypatch.setattr(clipboard_text, "_win32_clipboard_text", _boom)

    result = clipboard_text.read_clipboard_text()
    assert result.status == "error"
    assert "clipboard locked" in result.message
