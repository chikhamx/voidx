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

from voidx.agent.attachments import MAX_IMAGE_ATTACHMENT_BYTES

CLIPBOARD_ATTACHMENT_DIR = ".voidx/attachments"
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
    if platform.system() != "Darwin":
        return "unsupported"
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


def _compress_image_to_jpeg(source: Path, destination: Path) -> bool:
    if platform.system() != "Darwin":
        return False

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
        return "Clipboard image paste is only supported on macOS right now."
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


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
