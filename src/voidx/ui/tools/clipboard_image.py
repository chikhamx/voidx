"""Clipboard image capture and compression helpers."""

from __future__ import annotations

import os
import platform
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from voidx.paths import CLIPBOARD_ATTACHMENT_DIR
from voidx.runtime.attachments import MAX_IMAGE_ATTACHMENT_BYTES
from voidx.ui.tools.file_picker import format_size as _format_size
KEEP_ORIGINAL_BYTES = 3_000_000
TARGET_IMAGE_BYTES = 4_000_000
MAX_IMAGE_EDGE = 2048
JPEG_QUALITIES = (85, 75, 65, 55)

CaptureClipboardPng = Callable[[Path], str]
CompressImage = Callable[[Path, Path], bool]
NameFactory = Callable[[], str]

_CAPTURE_SCRIPT = """set outPath to do shell script "echo $VOIDX_CLIP_OUT"
use framework "AppKit"
use framework "Foundation"

set pb to current application's NSPasteboard's generalPasteboard()

-- Try PNG data directly
set pngData to pb's dataForType:"public.png"
if pngData is not missing value then
	set theResult to pngData's writeToFile:outPath atomically:true
	if theResult then return "ok"
end if

-- Fall back to NSImage → TIFF → PNG
set theImage to current application's NSImage's alloc()'s initWithPasteboard:pb
if theImage is missing value then return "no_image"

set theTIFF to theImage's TIFFRepresentation()
if theTIFF is missing value then return "no_image"

set theRep to current application's NSBitmapImageRep's imageRepWithData:theTIFF
if theRep is missing value then return "no_image"

set pngData to theRep's representationUsingType:(current application's NSPNGFileType) |properties|:(missing value)
if pngData is missing value then return "no_image"

set theResult to pngData's writeToFile:outPath atomically:true
if theResult then return "ok"
return "write_failed"
"""


@dataclass(frozen=True)
class ClipboardImageResult:
    status: str
    message: str
    rel_path: str = ""
    size: int = 0
    compressed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def paste_clipboard_image(
    workspace: str,
    *,
    capture_clipboard_png: CaptureClipboardPng | None = None,
    compress_image: CompressImage | None = None,
    name_factory: NameFactory | None = None,
) -> ClipboardImageResult:
    root = Path(workspace).resolve()
    target_dir = root / CLIPBOARD_ATTACHMENT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = name_factory() if name_factory else _attachment_stem()
    png_path = target_dir / f"{stem}.png"
    jpg_path = target_dir / f"{stem}.jpg"

    capture = capture_clipboard_png or _capture_clipboard_png
    status = capture(png_path)
    if status != "ok":
        _safe_unlink(png_path)
        return ClipboardImageResult(status=_result_status(status), message=_capture_message(status))

    if not png_path.exists() or png_path.stat().st_size == 0:
        _safe_unlink(png_path)
        return ClipboardImageResult(status="error", message="Clipboard image could not be saved.")

    original_size = png_path.stat().st_size
    if original_size <= KEEP_ORIGINAL_BYTES:
        return _ok_result(root, png_path, compressed=False)

    compressor = compress_image or _compress_image_to_jpeg
    compressed = compressor(png_path, jpg_path)
    choice = _best_usable_image(png_path, jpg_path if compressed else None)
    if choice is not None:
        chosen_path, chosen_compressed = choice
        for path in (png_path, jpg_path):
            if path != chosen_path:
                _safe_unlink(path)
        return _ok_result(root, chosen_path, compressed=chosen_compressed)

    smallest = _smallest_existing_size(png_path, jpg_path)
    _safe_unlink(png_path)
    _safe_unlink(jpg_path)
    size_text = _format_size(smallest) if smallest else "unknown size"
    return ClipboardImageResult(
        status="too_large",
        message=f"Clipboard image too large after compression: {size_text}",
    )


def _capture_clipboard_png(output_path: Path) -> str:
    system = platform.system()
    if system == "Darwin":
        return _capture_clipboard_png_macos(output_path)
    if system == "Windows":
        return _capture_clipboard_png_windows(output_path)
    return "unsupported"


def _capture_clipboard_png_macos(output_path: Path) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", _CAPTURE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env={**os.environ, "VOIDX_CLIP_OUT": str(output_path)},
        )
    except FileNotFoundError:
        return "unsupported"
    except subprocess.TimeoutExpired:
        return "error: clipboard read timed out"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"error: {detail or 'clipboard read failed'}"
    return (result.stdout or "").strip() or "error: clipboard read failed"


def _capture_clipboard_png_windows(output_path: Path) -> str:
    try:
        return _win32_capture_clipboard_png(output_path)
    except OSError as exc:
        return f"error: {exc}"
    except ImportError:
        return "error: Pillow is required for image paste on Windows"


def _win32_capture_clipboard_png(output_path: Path) -> str:
    """Capture clipboard image as PNG on Windows via Win32 API + Pillow.

    Returns ``"ok"`` on success, ``"no_image"`` when the clipboard has no
    bitmap, or raises ``OSError`` on Win32 API failures.
    """
    import ctypes
    from ctypes import wintypes

    CF_DIB = 8
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
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    if not user32.OpenClipboard(None):
        raise OSError("failed to open clipboard")
    try:
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return "no_image"
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("failed to lock clipboard data")
        try:
            size = kernel32.GlobalSize(handle)
            raw = ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()

    return _dib_bytes_to_png(raw, output_path)


def _dib_bytes_to_png(dib_bytes: bytes, output_path: Path) -> str:
    """Convert raw DIB (BITMAPINFO + pixel data) bytes to a PNG file via Pillow."""
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(dib_bytes))
    img.save(output_path, format="PNG")
    return "ok"


def _compress_image_to_jpeg(source: Path, destination: Path) -> bool:
    system = platform.system()
    if system == "Darwin":
        return _compress_image_to_jpeg_macos(source, destination)
    if system == "Windows":
        return _compress_image_to_jpeg_windows(source, destination)
    return False


def _compress_image_to_jpeg_macos(source: Path, destination: Path) -> bool:
    wrote_file = False
    for quality in JPEG_QUALITIES:
        try:
            result = subprocess.run(
                [
                    "sips",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    str(quality),
                    "--resampleHeightWidthMax",
                    str(MAX_IMAGE_EDGE),
                    str(source),
                    "--out",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return wrote_file
        if result.returncode != 0 or not destination.exists():
            continue
        wrote_file = True
        if destination.stat().st_size <= TARGET_IMAGE_BYTES:
            return True
    return wrote_file


def _compress_image_to_jpeg_windows(source: Path, destination: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        img = Image.open(source)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_EDGE:
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    except (OSError, ValueError):
        return False

    wrote_file = False
    for quality in JPEG_QUALITIES:
        try:
            img.save(destination, format="JPEG", quality=quality)
        except (OSError, ValueError):
            continue
        wrote_file = True
        if destination.stat().st_size <= TARGET_IMAGE_BYTES:
            return True
    return wrote_file


def _best_usable_image(png_path: Path, jpg_path: Path | None) -> tuple[Path, bool] | None:
    candidates: list[tuple[int, Path, bool]] = []
    if png_path.exists():
        candidates.append((png_path.stat().st_size, png_path, False))
    if jpg_path is not None and jpg_path.exists():
        candidates.append((jpg_path.stat().st_size, jpg_path, True))
    usable = [item for item in candidates if item[0] <= MAX_IMAGE_ATTACHMENT_BYTES]
    if not usable:
        return None
    _, path, compressed = min(usable, key=lambda item: item[0])
    return path, compressed


def _ok_result(root: Path, path: Path, *, compressed: bool) -> ClipboardImageResult:
    rel_path = path.resolve().relative_to(root).as_posix()
    size = path.stat().st_size
    suffix = " (compressed)" if compressed else ""
    return ClipboardImageResult(
        status="ok",
        message=f"Pasted image: {rel_path} ({_format_size(size)}){suffix}",
        rel_path=rel_path,
        size=size,
        compressed=compressed,
    )


def _capture_message(status: str) -> str:
    if status == "no_image":
        return "Clipboard does not contain an image."
    if status == "unsupported":
        return "Clipboard image paste is only supported on macOS and Windows."
    if status == "write_failed":
        return "Clipboard image could not be written."
    if status.startswith("error:"):
        return status.removeprefix("error:").strip() or "Clipboard image paste failed."
    return "Clipboard image paste failed."


def _result_status(status: str) -> str:
    if status in {"no_image", "unsupported"}:
        return status
    return "error"


def _smallest_existing_size(*paths: Path) -> int:
    sizes = [path.stat().st_size for path in paths if path.exists()]
    return min(sizes) if sizes else 0


def _attachment_stem() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"clipboard-{timestamp}-{secrets.token_hex(4)}"


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


