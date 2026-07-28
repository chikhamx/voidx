import sys
from pathlib import Path


from voidx.agent.application.attachments import MAX_IMAGE_ATTACHMENT_BYTES
from voidx.ui.tools.clipboard_image import (
    KEEP_ORIGINAL_BYTES,
    paste_clipboard_image,
)


def test_small_clipboard_image_keeps_png(tmp_path):
    def capture(path: Path) -> str:
        path.write_bytes(b"x" * 1024)
        return "ok"

    result = paste_clipboard_image(
        str(tmp_path),
        capture_clipboard_png=capture,
        name_factory=lambda: "clip",
    )

    assert result.ok
    assert result.rel_path == ".voidx/attachments/clip.png"
    assert result.compressed is False
    assert (tmp_path / result.rel_path).read_bytes() == b"x" * 1024


def test_large_clipboard_image_uses_compressed_jpeg(tmp_path):
    def capture(path: Path) -> str:
        path.write_bytes(b"x" * (KEEP_ORIGINAL_BYTES + 1))
        return "ok"

    def compress(source: Path, destination: Path) -> bool:
        destination.write_bytes(b"j" * 2048)
        return True

    result = paste_clipboard_image(
        str(tmp_path),
        capture_clipboard_png=capture,
        compress_image=compress,
        name_factory=lambda: "clip",
    )

    assert result.ok
    assert result.rel_path == ".voidx/attachments/clip.jpg"
    assert result.compressed is True
    assert (tmp_path / result.rel_path).read_bytes() == b"j" * 2048
    assert not (tmp_path / ".voidx/attachments/clip.png").exists()


def test_large_clipboard_image_keeps_original_if_compression_is_unavailable(tmp_path):
    def capture(path: Path) -> str:
        path.write_bytes(b"x" * (KEEP_ORIGINAL_BYTES + 1))
        return "ok"

    result = paste_clipboard_image(
        str(tmp_path),
        capture_clipboard_png=capture,
        compress_image=lambda _source, _destination: False,
        name_factory=lambda: "clip",
    )

    assert result.ok
    assert result.rel_path == ".voidx/attachments/clip.png"
    assert result.size == KEEP_ORIGINAL_BYTES + 1


def test_clipboard_image_too_large_after_compression_is_rejected(tmp_path):
    def capture(path: Path) -> str:
        path.write_bytes(b"x" * (MAX_IMAGE_ATTACHMENT_BYTES + 1))
        return "ok"

    def compress(source: Path, destination: Path) -> bool:
        destination.write_bytes(b"j" * (MAX_IMAGE_ATTACHMENT_BYTES + 1))
        return True

    result = paste_clipboard_image(
        str(tmp_path),
        capture_clipboard_png=capture,
        compress_image=compress,
        name_factory=lambda: "clip",
    )

    assert result.status == "too_large"
    assert "too large" in result.message
    assert not (tmp_path / ".voidx/attachments/clip.png").exists()
    assert not (tmp_path / ".voidx/attachments/clip.jpg").exists()


def test_clipboard_without_image_returns_no_image(tmp_path):
    result = paste_clipboard_image(
        str(tmp_path),
        capture_clipboard_png=lambda _path: "no_image",
        name_factory=lambda: "clip",
    )

    assert result.status == "no_image"
    assert "does not contain an image" in result.message
