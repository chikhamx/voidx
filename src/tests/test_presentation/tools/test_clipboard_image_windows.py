"""Windows clipboard image capture tests.

These tests monkeypatch ``platform.system`` to simulate Windows so the
Windows branches in ``clipboard_image`` are exercised on any OS.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voidx.presentation.tools import clipboard_image


def _patch_windows(monkeypatch):
    monkeypatch.setattr(clipboard_image.platform, "system", lambda: "Windows")


def test_windows_capture_png_writes_file(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)

    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

    def fake_win32_capture(path: Path) -> str:
        path.write_bytes(fake_png)
        return "ok"

    monkeypatch.setattr(clipboard_image, "_win32_capture_clipboard_png", fake_win32_capture)

    out = tmp_path / "out.png"
    status = clipboard_image._capture_clipboard_png(out)
    assert status == "ok"
    assert out.read_bytes() == fake_png


def test_windows_capture_png_no_image(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)
    monkeypatch.setattr(clipboard_image, "_win32_capture_clipboard_png", lambda _p: "no_image")

    out = tmp_path / "out.png"
    status = clipboard_image._capture_clipboard_png(out)
    assert status == "no_image"


def test_windows_capture_png_error(monkeypatch, tmp_path):
    _patch_windows(monkeypatch)

    def _boom(_p):
        raise OSError("clipboard locked")

    monkeypatch.setattr(clipboard_image, "_win32_capture_clipboard_png", _boom)

    out = tmp_path / "out.png"
    status = clipboard_image._capture_clipboard_png(out)
    assert status.startswith("error:")


def test_windows_compress_image_uses_pillow(monkeypatch, tmp_path):
    """Windows compression should use Pillow when available."""
    _patch_windows(monkeypatch)

    source = tmp_path / "src.png"
    dest = tmp_path / "dst.jpg"
    source.write_bytes(b"\x89PNG fake")

    called = {}

    class FakeImage:
        size = (100, 100)
        mode = "RGB"

        def __init__(self, path):
            called["opened"] = str(path)

        @classmethod
        def open(cls, path):
            return cls(path)

        def convert(self, mode):
            called["mode"] = mode
            return self

        def thumbnail(self, size):
            called["thumb_size"] = size

        def save(self, path, format=None, quality=None):
            called["save_path"] = str(path)
            called["save_format"] = format
            called["save_quality"] = quality
            Path(path).write_bytes(b"compressed jpeg data")

    fake_pil = type("PIL", (), {"Image": FakeImage})
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    result = clipboard_image._compress_image_to_jpeg(source, dest)
    assert result is True
    assert dest.exists()
    assert called["save_format"] == "JPEG"
    assert called["save_quality"] in clipboard_image.JPEG_QUALITIES



def test_windows_compress_retries_after_save_failure(monkeypatch, tmp_path):
    """Windows compression should try the next quality when save fails, like macOS."""
    _patch_windows(monkeypatch)

    source = tmp_path / "src.png"
    dest = tmp_path / "dst.jpg"
    source.write_bytes(b"\x89PNG fake")

    call_count = {"n": 0}

    class RetryImage:
        size = (100, 100)
        mode = "RGB"

        @classmethod
        def open(cls, path):
            return cls()

        def convert(self, mode):
            return self

        def thumbnail(self, size):
            pass

        def save(self, path, format=None, quality=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("transient failure")
            Path(path).write_bytes(b"compressed jpeg data")

    fake_pil = type("PIL", (), {"Image": RetryImage})
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    result = clipboard_image._compress_image_to_jpeg(source, dest)
    assert result is True
    assert dest.exists()
    assert call_count["n"] >= 2


def test_windows_compress_returns_false_when_all_saves_fail(monkeypatch, tmp_path):
    """Windows compression should return False when every quality save fails."""
    _patch_windows(monkeypatch)

    source = tmp_path / "src.png"
    dest = tmp_path / "dst.jpg"
    source.write_bytes(b"\x89PNG fake")

    class FailingImage:
        size = (100, 100)
        mode = "RGB"

        @classmethod
        def open(cls, path):
            return cls()

        def convert(self, mode):
            return self

        def thumbnail(self, size):
            pass

        def save(self, path, format=None, quality=None):
            raise OSError("disk full")

    fake_pil = type("PIL", (), {"Image": FailingImage})
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    result = clipboard_image._compress_image_to_jpeg(source, dest)
    assert result is False
    assert not dest.exists()


def test_dib_bytes_to_png_real_pillow(tmp_path):
    """_dib_bytes_to_png should convert a real DIB byte stream to a valid PNG."""
    pytest.importorskip("PIL")

    import struct

    from PIL import Image

    # Build a minimal 2x2 24-bit DIB (BITMAPINFOHEADER + pixel data).
    width, height = 2, 2
    bpp = 24
    row_size = ((width * bpp + 31) // 32) * 4  # DWORD-aligned
    pixel_data_size = row_size * height
    header_size = 40
    dib = struct.pack(
        "<IIIHHIIIIII",
        header_size,      # biSize
        width,            # biWidth
        height,           # biHeight (positive = bottom-up)
        1,                # biPlanes
        bpp,              # biBitCount
        0,                # biCompression (BI_RGB)
        pixel_data_size,  # biSizeImage
        0,                # biXPelsPerMeter
        0,                # biYPelsPerMeter
        0,                # biClrUsed
        0,                # biClrImportant
    )
    # Pixels: bottom-up rows, each row padded to row_size.
    row = b"\xff\x00\x00" + b"\x00\xff\x00"  # blue, green (BGR)
    row += b"\x00" * (row_size - len(row))    # pad
    dib += row * height

    out = tmp_path / "out.png"
    status = clipboard_image._dib_bytes_to_png(dib, out)
    assert status == "ok"
    assert out.exists()
    img = Image.open(out)
    assert img.format == "PNG"
    assert img.size == (width, height)