"""Parse and materialize user file attachments."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voidx.runtime.attachments import MAX_IMAGE_ATTACHMENT_BYTES

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_TEXT_ATTACHMENT_BYTES = 200_000
MAX_DIR_LISTING_ITEMS = 500
_DIR_TREE_SKIP = {"__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build", ".pytest_cache", ".mypy_cache"}
_ATTACHMENT_RE = re.compile(r'(?<!\S)(?:@(?:"([^"]+)"|(\S+))|\[image-([^\]]+)\])')
_CLIPBOARD_ATTACHMENT_DIR = ".voidx/attachments"


@dataclass(frozen=True)
class Attachment:
    path: Path
    rel_path: str
    kind: str
    mime_type: str
    size: int


@dataclass
class UserMessagePayload:
    raw_text: str
    clean_text: str
    display_text: str
    title_text: str
    content: str | list[dict[str, Any]]
    content_format: str
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_structured_content(content: str, content_format: str) -> str | list[dict[str, Any]]:
    if content_format != "structured":
        return content
    try:
        parsed = json.loads(content)
    except Exception:
        return content
    return parsed if isinstance(parsed, list) else content


def serialize_message_content(content: str | list[dict[str, Any]]) -> tuple[str, str]:
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False), "structured"
    return content, "text"


def build_user_message_payload(
    user_text: str,
    workspace: str,
    *,
    text_prefix: str = "",
    extra_removed_spans: list[tuple[int, int]] | None = None,
) -> UserMessagePayload:
    workspace_path = Path(workspace).resolve()
    tokens = _attachment_tokens(user_text)
    removed_spans: list[tuple[int, int]] = []
    attachments: list[Attachment] = []
    warnings: list[str] = []
    text_sections: list[str] = []
    image_parts: list[dict[str, Any]] = []

    for start, end, raw_path in tokens:
        if raw_path.startswith(":image:"):
            stem = raw_path[len(":image:"):]
            resolved = _resolve_image_stem(workspace_path, stem)
            display_label = f"[image-{stem}]"
        else:
            resolved = _resolve_workspace_path(workspace_path, raw_path)
            display_label = raw_path
        if resolved is None:
            warnings.append(f"Attachment skipped outside workspace: {display_label}")
            continue
        if not resolved.exists():
            warnings.append(f"Attachment not found: {display_label}")
            continue
        if not resolved.is_file() and not resolved.is_dir():
            warnings.append(f"Attachment is not a file or directory: {display_label}")
            continue
        is_dir = resolved.is_dir()
        removed_spans.append((start, end))
        rel_path = _relative_path(workspace_path, resolved)
        mime_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        size = resolved.stat().st_size
        kind = "dir" if is_dir else ("image" if is_image_path(resolved) else "file")
        attachment = Attachment(resolved, rel_path, kind, mime_type, size)
        attachments.append(attachment)

        if kind == "image":
            if size > MAX_IMAGE_ATTACHMENT_BYTES:
                warnings.append(f"Image skipped because it is too large: {rel_path}")
                continue
            image_parts.append(_image_part(resolved, mime_type))
            continue

        if kind == "dir":
            section, warning = _directory_section(attachment)
            if warning:
                warnings.append(warning)
            if section:
                text_sections.append(section)
            continue

        section, warning = _text_file_section(attachment)
        if warning:
            warnings.append(warning)
        if section:
            text_sections.append(section)

    display_clean_text = _normalize_text(_remove_spans(user_text, removed_spans))
    if extra_removed_spans:
        removed_spans.extend(extra_removed_spans)
    clean_text = _normalize_text(_remove_spans(user_text, removed_spans))
    text_content = _build_text_content(clean_text, attachments, text_sections)
    if text_prefix.strip():
        text_content = _prefix_text_content(text_prefix.strip(), text_content)
    content: str | list[dict[str, Any]]
    content_format = "text"
    if image_parts:
        content = [{"type": "text", "text": text_content}, *image_parts]
        content_format = "structured"
    else:
        content = text_content

    display_text = _display_text(display_clean_text, attachments)
    title_text = clean_text or (f"Attached {attachments[0].rel_path}" if attachments else user_text)
    return UserMessagePayload(
        raw_text=user_text,
        clean_text=clean_text,
        display_text=display_text,
        title_text=title_text,
        content=content,
        content_format=content_format,
        attachments=attachments,
        warnings=warnings,
    )


def is_image_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def _attachment_tokens(text: str) -> list[tuple[int, int, str]]:
    tokens: list[tuple[int, int, str]] = []
    for match in _ATTACHMENT_RE.finditer(text):
        image_stem = match.group(3)
        if image_stem:
            raw_path = f":image:{image_stem}"
        else:
            raw_path = match.group(1) or match.group(2)
        if raw_path:
            tokens.append((match.start(), match.end(), raw_path))
    return tokens


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    result: list[str] = []
    last = 0
    for start, end in sorted(spans):
        if start < last:
            continue
        result.append(text[last:start])
        result.append(" ")
        last = end
    result.append(text[last:])
    return "".join(result)


def _prefix_text_content(prefix: str, text: str) -> str:
    text = text.strip()
    if not text:
        return prefix
    return f"{prefix}\n\n{text}"


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved


def _resolve_image_stem(workspace: Path, stem: str) -> Path | None:
    target_dir = workspace / _CLIPBOARD_ATTACHMENT_DIR
    if not target_dir.is_dir():
        return None
    matches = sorted(target_dir.glob(f"{stem}.*"))
    return matches[0] if matches else None


def _relative_path(workspace: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def _image_part(path: Path, mime_type: str) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
    }


def _text_file_section(attachment: Attachment) -> tuple[str, str]:
    raw = attachment.path.read_bytes()
    truncated = len(raw) > MAX_TEXT_ATTACHMENT_BYTES
    if truncated:
        raw = raw[:MAX_TEXT_ATTACHMENT_BYTES]
    if b"\x00" in raw:
        return (
            f"Attached binary file: {attachment.rel_path} ({attachment.mime_type}, {attachment.size} bytes).",
            "",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    suffix = ""
    warning = ""
    if truncated:
        omitted = attachment.size - MAX_TEXT_ATTACHMENT_BYTES
        suffix = f"\n\n[Attachment truncated: omitted {omitted} bytes]"
        warning = f"Attachment truncated: {attachment.rel_path}"
    lang = _language_from_path(attachment.rel_path)
    return f"Attached file: {attachment.rel_path}\n```{lang}\n{text}{suffix}\n```", warning


def _directory_section(attachment: Attachment) -> tuple[str, str]:
    listing: list[str] = []
    try:
        _walk_dir_tree(attachment.path, "", listing, depth=0, max_depth=3)
    except (OSError, PermissionError) as exc:
        return "", f"Cannot read directory {attachment.rel_path}: {exc}"
    tree_text = "\n".join(listing) if listing else "(empty)"
    label = f"Attached directory: {attachment.rel_path}"
    if listing and listing[-1].startswith("..."):
        item_count = sum(1 for line in listing if not line.endswith("..."))
        label += f" (showing first {item_count} items)"
    return f"{label}\n{tree_text}", ""


def _walk_dir_tree(root: Path, prefix: str, listing: list[str], depth: int, max_depth: int) -> None:
    if depth >= max_depth or len(listing) >= MAX_DIR_LISTING_ITEMS:
        if not listing or not listing[-1].startswith("..."):
            listing.append(f"{prefix}...")
        return
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (OSError, PermissionError):
        return
    for i, entry in enumerate(entries):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and entry.name in _DIR_TREE_SKIP:
            continue
        if len(listing) >= MAX_DIR_LISTING_ITEMS:
            if not listing[-1].startswith("..."):
                listing.append(f"{prefix}...")
            return
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        listing.append(f"{prefix}{connector}{entry.name}")
        if entry.is_dir():
            ext_prefix = "    " if is_last else "│   "
            _walk_dir_tree(entry, prefix + ext_prefix, listing, depth + 1, max_depth)


def _language_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
        "jsx": "jsx",
        "json": "json",
        "md": "markdown",
        "sh": "bash",
        "yml": "yaml",
        "yaml": "yaml",
        "html": "html",
        "css": "css",
    }
    return mapping.get(suffix, suffix)


def _build_text_content(clean_text: str, attachments: list[Attachment], text_sections: list[str]) -> str:
    parts: list[str] = []
    if clean_text:
        parts.append(clean_text)
    if attachments:
        image_lines = [f"- {item.rel_path} ({item.mime_type}, {item.size} bytes)" for item in attachments if item.kind == "image"]
        if image_lines:
            parts.append("Attached images:\n" + "\n".join(image_lines))
    parts.extend(text_sections)
    if parts:
        return "\n\n".join(parts)
    return "Please review the attached file."


def _display_text(clean_text: str, attachments: list[Attachment]) -> str:
    if not attachments:
        return clean_text
    names = ", ".join(item.rel_path for item in attachments[:3])
    if len(attachments) > 3:
        names += f", +{len(attachments) - 3} more"
    base = clean_text or "Attached files"
    return f"{base}\n[attachments: {names}]"


def _normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()
